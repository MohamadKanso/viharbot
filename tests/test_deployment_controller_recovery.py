"""Create timeout recovery: provider-backed evidence, durable evidence before bind, later passes."""

from __future__ import annotations

import pytest
from deployment_controller_helpers import (
    KEY, Crash, alert_reasons, app_of, controller, execute, make_world, payload, reason_of,
    submit_approved,
)

from viharbot.deployment_controller import markers
from viharbot.deployment_controller.provider import ProviderTimeout
from viharbot.deployment_controller.store import RecordStore

TWO_HOURS = 2 * 3600


def after_call(provider, name, action):
    """Run ``action`` once, right after the provider call ``name`` returns."""
    original = getattr(provider, name)

    def wrapper(*args, **kwargs):
        result = original(*args, **kwargs)
        setattr(provider, name, original)
        action()
        return result
    setattr(provider, name, wrapper)


def assert_expired_not_bound(world, dep_id, approval_id):
    record = world.ctrl.get(dep_id)
    assert record["status"] == "failed_uncertain" and record["reason"] == "claim_expired"
    assert "bind_refused:claim_expired" in alert_reasons(world.ctrl)
    assert world.ctrl.get_approval(approval_id)["state"] == "reserved"
    for app in world.provider.apps.values():
        assert markers.BOUND_KEY not in app.env
    assert "update" not in world.provider.calls


@pytest.fixture
def world(tmp_path):
    return make_world(tmp_path)


def test_create_timeout_after_provider_created_reconciles_without_a_second_create(world):
    dep = submit_approved(world.ctrl, payload())
    world.provider.fail_next("create", "after")
    done = execute(world, dep)
    assert done["status"] == "running"
    assert markers.verified_bound(KEY, app_of(world, done), "team-1")
    assert world.provider.calls.count("create") == 1
    assert world.ctrl.get_approval(dep["approval_id"])["state"] == "executed"

def test_create_timeout_after_without_provider_evidence_fails_closed(tmp_path):
    world = make_world(tmp_path, operation_lookup=False)
    dep = submit_approved(world.ctrl, payload())
    world.provider.fail_next("create", "after")
    done = execute(world, dep)
    assert done["status"] == "failed_uncertain" and done["reason"] == "no_operation_evidence"
    assert markers.BOUND_KEY not in app_of(world, {"app_id": "app-0001"}).env
    assert world.ctrl.get_approval(dep["approval_id"])["state"] == "reserved"
    assert reason_of(execute, world, dep) == "approval_not_pending"
    assert world.provider.calls.count("create") == 1
    assert "create_outcome_unknown:no_operation_evidence" in alert_reasons(world.ctrl)


def test_create_timeout_before_provider_created_stays_reserved_and_alerts(world):
    dep = submit_approved(world.ctrl, payload())
    world.provider.fail_next("create", "before")
    done = execute(world, dep)
    assert done["status"] == "failed_uncertain" and done["reason"] == "no_candidate"
    assert world.ctrl.get_approval(dep["approval_id"])["state"] == "reserved"
    assert reason_of(execute, world, dep) == "approval_not_pending"
    assert world.provider.calls.count("create") == 1 and not world.provider.apps
    assert "create_outcome_unknown:no_candidate" in alert_reasons(world.ctrl)


def test_reconcile_unavailable_after_a_create_timeout_fails_closed(world):
    dep = submit_approved(world.ctrl, payload())
    world.provider.fail_next("create", "after")
    original = world.provider.list_apps
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) > 1:  # the quota check lists once; the reconciliation listing then fails
            raise ProviderTimeout("list")
        return original()

    world.provider.list_apps = flaky
    assert execute(world, dep)["reason"] == "reconcile_unavailable"


def test_evidence_is_recorded_durably_before_a_recovery_bind(world):
    dep = submit_approved(world.ctrl, payload())
    world.provider.fail_next("create", "after")

    def die(*_args, **_kwargs):
        raise Crash

    world.provider.update_app = die  # the process dies at the bind that follows the recovery
    with pytest.raises(Crash):
        execute(world, dep)
    saved = RecordStore(world.path)  # what a restart sees
    appr = saved.get("approvals", dep["approval_id"])
    assert appr["state"] == "reserved" and appr["app_id"] == "app-0001"
    assert saved.get("deployments", dep["deployment_id"])["status"] == "created_unbound"
    assert markers.BOUND_KEY not in world.provider.apps["app-0001"].env
    del world.provider.update_app
    restarted = controller(world)
    assert [r["action"] for r in restarted.reconcile()] == ["bound"]
    assert restarted.get_approval(dep["approval_id"])["state"] == "executed"


def test_a_copied_claim_never_rebinds_once_evidence_names_another_app(world):
    dep = submit_approved(world.ctrl, payload())
    world.provider.fail_next("create", "after")
    original = world.provider.update_app
    calls = []

    def lose_bind(*args):
        calls.append(1)
        raise ProviderTimeout("update")

    world.provider.update_app = lose_bind
    done = execute(world, dep)
    world.provider.update_app = original
    assert done["status"] == "created_unbound" and calls
    twin = world.provider.add_foreign("orbit-twin")
    world.provider.copy_markers(done["app_id"], twin.app_id, (markers.CLAIM_KEY,))
    world.provider.destroy_app(done["app_id"])  # only the copy remains
    restarted = controller(world)
    assert [r["action"] for r in restarted.reconcile()] == ["alert"]
    assert markers.BOUND_KEY not in twin.env


def unavailable_listing(world):
    dep = submit_approved(world.ctrl, payload())
    world.provider.fail_next("create", "after")
    original, calls = world.provider.list_apps, []

    def flaky():
        calls.append(1)
        if len(calls) > 1:
            raise ProviderTimeout("list")
        return original()

    world.provider.list_apps = flaky
    done = execute(world, dep)
    world.provider.list_apps = original
    assert done["reason"] == "reconcile_unavailable" and done["app_id"] is None
    return dep, done


def test_a_later_pass_recovers_an_unknown_create_from_operation_evidence(world):
    dep, done = unavailable_listing(world)
    lookup = world.provider.find_operation

    def broken(_claim_id):
        raise ProviderTimeout("lookup")

    world.provider.find_operation = broken  # a lookup failure is unresolved, not a bind
    first = controller(world)
    assert [r["action"] for r in first.reconcile()] == ["alert"]
    assert markers.BOUND_KEY not in world.provider.apps["app-0001"].env
    world.provider.find_operation = lookup
    second = controller(world)
    assert [r["action"] for r in second.reconcile()] == ["bound"]
    record = second.get(done["deployment_id"])
    assert record["status"] == "running" and record["app_id"] == "app-0001"
    assert second.get_approval(dep["approval_id"])["state"] == "executed"
    assert world.provider.calls.count("create") == 1


def test_a_later_pass_without_operation_evidence_stays_unbound(tmp_path):
    world = make_world(tmp_path, operation_lookup=False)
    dep, _ = unavailable_listing(world)
    assert [r["action"] for r in controller(world).reconcile()] == ["alert"]
    assert world.ctrl.get_approval(dep["approval_id"])["state"] == "reserved"


def test_creating_with_a_timeout_before_the_provider_acted_is_a_no_candidate(world):
    dep = submit_approved(world.ctrl, payload())
    world.provider.fail_next("create", "before")
    done = execute(world, dep)
    assert done["status"] == "failed_uncertain" and done["reason"] == "no_candidate"
    assert world.ctrl.get_approval(dep["approval_id"])["state"] == "reserved"
    assert "create_outcome_unknown:no_candidate" in alert_reasons(world.ctrl)


def test_expiry_during_timeout_recovery_is_never_bound(world):
    dep = submit_approved(world.ctrl, payload())
    world.provider.fail_next("create", "after")
    original = world.provider.find_operation

    def slow_lookup(claim_id):
        world.clock.now += TWO_HOURS
        return original(claim_id)
    world.provider.find_operation = slow_lookup
    done = execute(world, dep)
    assert_expired_not_bound(world, done["deployment_id"], dep["approval_id"])


def test_expiry_during_a_later_reconcile_lookup_is_never_bound(world):
    dep = submit_approved(world.ctrl, payload())
    world.provider.fail_next("create", "after")
    original_list, calls = world.provider.list_apps, []

    def flaky():
        calls.append(1)
        if len(calls) > 1:
            raise ProviderTimeout("list")
        return original_list()
    world.provider.list_apps = flaky
    done = execute(world, dep)
    world.provider.list_apps = original_list
    assert done["reason"] == "reconcile_unavailable"
    original = world.provider.find_operation

    def slow_lookup(claim_id):
        world.clock.now += TWO_HOURS  # the claim expires after the pass's own expiry check
        return original(claim_id)
    world.provider.find_operation = slow_lookup
    second = controller(world)
    assert [r["action"] for r in second.reconcile()] == ["alert"]
    world.ctrl = second
    assert_expired_not_bound(world, done["deployment_id"], dep["approval_id"])


# --- an unavailable operation lookup during immediate recovery ---------------------------------


def test_lookup_failure_during_immediate_recovery_is_a_normal_uncertain_record(world):
    dep = submit_approved(world.ctrl, payload())
    world.provider.fail_next("create", "after")
    world.provider.fail_next("find", "before")
    done = execute(world, dep)
    assert done["status"] == "failed_uncertain" and done["reason"] == "reconcile_unavailable"
    assert "create_outcome_unknown:reconcile_unavailable" in alert_reasons(world.ctrl)
    assert world.provider.calls.count("create") == 1
    assert world.ctrl.get_approval(dep["approval_id"])["state"] == "reserved"
    # a later pass recovers it from provider evidence without a second create
    assert [r["action"] for r in controller(world).reconcile()] == ["bound"]
    assert world.provider.calls.count("create") == 1
