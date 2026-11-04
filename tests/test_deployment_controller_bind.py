"""Bind safety: version condition, claim expiry on every bind path, lookup failures, log bytes."""

from __future__ import annotations

import pytest
from deployment_controller_helpers import (
    controller, make_world, payload, replacement, run_all, submit_approved,
)

from viharbot.deployment_controller import markers
from viharbot.deployment_controller.provider import ProviderTimeout

TWO_HOURS = 2 * 3600


@pytest.fixture
def world(tmp_path):
    return make_world(tmp_path)


def go(world, dep):
    return world.ctrl.execute(dep["approval_id"], caller="approver", request_id="r")


def alerts(ctrl):
    return [a["reason"] for a in ctrl.alerts]


def slip_in(world, edit):
    """A foreign writer that lands after the controller's read and before its update."""
    def hook(provider, app_id):
        edit(provider.apps[app_id])
        provider.before_update = None
    world.provider.before_update = hook


def foreign_write(app):
    app.spec["env"]["FOREIGN"] = "x"
    app.version += 1


def after_call(provider, name, action):
    """Run ``action`` once, right after the provider call ``name`` returns."""
    original = getattr(provider, name)

    def wrapper(*args, **kwargs):
        result = original(*args, **kwargs)
        setattr(provider, name, original)
        action()
        return result
    setattr(provider, name, wrapper)


def test_bind_with_a_clean_version_succeeds_and_is_conditional(world):
    seen = []
    original = world.provider.update_app
    world.provider.update_app = lambda *a, **k: (seen.append(a), original(*a, **k))[1]
    done = run_all(world.ctrl, payload())
    assert done["status"] == "running"
    assert seen[0][2] == 1  # the bind passed the version it had just read


def test_a_writer_between_read_and_bind_is_refused_by_a_conditional_provider(world):
    dep = submit_approved(world.ctrl, payload())
    slip_in(world, foreign_write)
    done = go(world, dep)
    assert done["status"] == "failed_uncertain" and done["reason"] == "bind_version_conflict"
    app = world.provider.apps[done["app_id"]]
    assert app.env["FOREIGN"] == "x" and markers.BOUND_KEY not in app.env  # not overwritten
    assert world.ctrl.get_approval(dep["approval_id"])["state"] == "reserved"
    assert "bind_failed_uncertain:version_conflict" in alerts(world.ctrl)
    assert world.provider.calls.count("update") == 1  # no blind retry


def test_a_writer_between_read_and_bind_is_detected_without_conditional_updates(tmp_path):
    world = make_world(tmp_path, conditional_update=False)
    dep = submit_approved(world.ctrl, payload())
    slip_in(world, foreign_write)
    done = go(world, dep)
    assert done["status"] == "failed_uncertain" and done["reason"] == "unexpected_writer"
    assert "bind_failed_uncertain:unverified" in alerts(world.ctrl)
    assert world.ctrl.get_approval(dep["approval_id"])["state"] == "reserved"
    controller(world).reconcile()  # reconcile never promotes the conflict record to executed
    assert world.ctrl.get(done["deployment_id"])["status"] == "failed_uncertain"


def test_a_clean_bind_without_conditional_updates_verifies_by_read_back(tmp_path):
    world = make_world(tmp_path, conditional_update=False)
    assert run_all(world.ctrl, payload())["status"] == "running"


def test_an_app_changed_before_the_bind_read_is_never_bound(world):
    dep = submit_approved(world.ctrl, payload())
    after_call(world.provider, "create_app",
               lambda: world.provider.apps["app-0001"].env.pop(markers.CLAIM_KEY))
    done = go(world, dep)
    assert done["status"] == "failed_uncertain" and done["reason"] == "unexpected_writer"
    assert "bind_failed_uncertain" in alerts(world.ctrl)


def test_a_read_failure_before_the_bind_leaves_it_pending(world):
    dep = submit_approved(world.ctrl, payload())
    after_call(world.provider, "create_app", lambda: world.provider.fail_next("get", "before"))
    done = go(world, dep)
    assert done["status"] == "created_unbound" and done["reason"] == "bind_pending"
    assert controller(world).reconcile()[0]["action"] == "bound"


def test_a_read_back_failure_without_conditional_updates_is_uncertain(tmp_path):
    world = make_world(tmp_path, conditional_update=False)
    dep = submit_approved(world.ctrl, payload())
    original = world.provider.update_app

    def update(*args, **kwargs):
        result = original(*args, **kwargs)
        world.provider.fail_next("get", "before")
        return result
    world.provider.update_app = update
    done = go(world, dep)
    assert done["status"] == "failed_uncertain" and done["reason"] == "unexpected_writer"


# --- claim expiry is rechecked immediately before every bind -----------------------------------


def assert_expired_not_bound(world, dep_id, approval_id):
    record = world.ctrl.get(dep_id)
    assert record["status"] == "failed_uncertain" and record["reason"] == "claim_expired"
    assert "bind_refused:claim_expired" in alerts(world.ctrl)
    assert world.ctrl.get_approval(approval_id)["state"] == "reserved"
    for app in world.provider.apps.values():
        assert markers.BOUND_KEY not in app.env
    assert "update" not in world.provider.calls


def test_expiry_during_an_immediate_create_is_never_bound(world):
    dep = submit_approved(world.ctrl, payload())
    after_call(world.provider, "create_app",
               lambda: setattr(world.clock, "now", world.clock.now + TWO_HOURS))
    done = go(world, dep)
    assert_expired_not_bound(world, done["deployment_id"], dep["approval_id"])


def test_expiry_during_timeout_recovery_is_never_bound(world):
    dep = submit_approved(world.ctrl, payload())
    world.provider.fail_next("create", "after")
    original = world.provider.find_operation

    def slow_lookup(claim_id):
        world.clock.now += TWO_HOURS
        return original(claim_id)
    world.provider.find_operation = slow_lookup
    done = go(world, dep)
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
    done = go(world, dep)
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


def test_expiry_before_a_replacement_update_is_never_written(world):
    running, dep = replacement(world)
    original = world.ctrl.store.commit

    def commit(**changes):
        original(**changes)
        if "actions" in changes:  # the reservation just became durable
            world.clock.now += TWO_HOURS
    world.ctrl.store.commit = commit
    calls_before = world.provider.calls.count("update")
    done = go(world, dep)
    assert done["status"] == "failed_uncertain" and done["reason"] == "claim_expired"
    assert world.provider.calls.count("update") == calls_before
    assert "bind_refused:claim_expired" in alerts(world.ctrl)
    assert world.ctrl.get_approval(dep["approval_id"])["state"] == "reserved"


# --- an unavailable operation lookup during immediate recovery ---------------------------------


def test_lookup_failure_during_immediate_recovery_is_a_normal_uncertain_record(world):
    dep = submit_approved(world.ctrl, payload())
    world.provider.fail_next("create", "after")
    world.provider.fail_next("find", "before")
    done = go(world, dep)
    assert done["status"] == "failed_uncertain" and done["reason"] == "reconcile_unavailable"
    assert "create_outcome_unknown:reconcile_unavailable" in alerts(world.ctrl)
    assert world.provider.calls.count("create") == 1
    assert world.ctrl.get_approval(dep["approval_id"])["state"] == "reserved"
    # a later pass recovers it from provider evidence without a second create
    assert [r["action"] for r in controller(world).reconcile()] == ["bound"]
    assert world.provider.calls.count("create") == 1


# --- log cap is bytes ---------------------------------------------------------------------------


def test_log_cap_is_utf8_bytes_and_never_splits_a_character(tmp_path):
    world = make_world(tmp_path)
    world.ctrl = controller(world, max_log_bytes=5)
    dep = run_all(world.ctrl, payload())
    world.provider.logs[dep["app_id"]] = "x" + "é" * 4  # 9 bytes; the cut falls inside a character
    text = world.ctrl.logs(dep["deployment_id"], 10, caller="o", request_id="r")
    assert text == "éé" and len(text.encode("utf-8")) <= 5
    world.provider.logs[dep["app_id"]] = "日本語"  # 9 bytes, 3-byte characters
    text = world.ctrl.logs(dep["deployment_id"], 10, caller="o", request_id="r")
    assert text == "語" and len(text.encode("utf-8")) <= 5
