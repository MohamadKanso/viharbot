"""App-scoped safeguards and truthful outcomes: stop/delete with an unresolved record, a lost
destroy response during expiry, and the audit decision of each recovery outcome."""

from __future__ import annotations

import pytest
from deployment_controller_helpers import (
    Crash, alert_reasons, controller, execute, make_world, payload, reason_of, replacement,
    run_all,
)

from viharbot.deployment_controller.policy import PolicyDenied
from viharbot.deployment_controller.provider import ProviderError


@pytest.fixture
def world(tmp_path):
    return make_world(tmp_path, conditional_update=False)


def uncertain_replacement(world):
    """The earlier generation keeps running; the replacement is failed_uncertain (a writer raced)."""
    running, dep = replacement(world)
    world.provider.before_update = lambda p, app_id: p.bump_version(app_id)
    done = execute(world, dep)
    world.provider.before_update = None
    assert done["status"] == "failed_uncertain"
    return running, done


def destroy(world, dep, keep_record):
    return world.ctrl.destroy(dep["deployment_id"], caller="orbit", request_id="r",
                              keep_record=keep_record)


@pytest.mark.parametrize("keep_record", [True, False])
def test_stop_and_delete_are_refused_through_either_generation(world, keep_record):
    running, done = uncertain_replacement(world)
    with pytest.raises(PolicyDenied) as earlier:  # the earlier generation: the app's safeguard
        destroy(world, running, keep_record)
    assert earlier.value.reason == "app_has_unresolved_outcome"
    with pytest.raises(PolicyDenied) as replacement_record:
        destroy(world, done, keep_record)
    assert replacement_record.value.reason == "record_not_authoritative"
    assert world.provider.apps and "destroy" not in world.provider.calls
    assert world.ctrl.get(running["deployment_id"])["status"] == "running"
    assert "destroy_refused:unresolved_outcome" in alert_reasons(world.ctrl)
    assert any(e["action"] == ("stop" if keep_record else "delete") and e["decision"] == "deny"
               and e["reason"] == "app_has_unresolved_outcome" for e in world.ctrl.audit.events)


def test_after_a_person_resolves_the_record_the_app_can_be_stopped(world):
    running, done = uncertain_replacement(world)
    # The documented manual step: inspect the app, then mark the record resolved in the store.
    done = world.ctrl.get(done["deployment_id"])
    done["status"] = "failed"
    world.ctrl.store.commit(deployments={done["deployment_id"]: done})
    assert destroy(world, running, True)["status"] == "stopped"
    assert not world.provider.apps


def test_a_different_app_is_unaffected(world):
    uncertain_replacement(world)
    other = run_all(world.ctrl, payload(9))
    assert destroy(world, other, True)["status"] == "stopped"
    assert len(world.provider.apps) == 1  # the unresolved app is still there


def test_a_reserved_replacement_after_a_crash_also_blocks_the_target_app(world):
    running, dep = replacement(world)

    def die(*_args, **_kwargs):
        raise Crash

    world.ctrl._finish = die
    with pytest.raises(Crash):
        execute(world, dep)
    restarted = controller(world)
    with pytest.raises(PolicyDenied) as refused:
        restarted.destroy(running["deployment_id"], caller="orbit", request_id="r",
                          keep_record=True)
    assert refused.value.reason == "app_has_unresolved_outcome"


def test_a_failed_record_cannot_destroy_the_app_it_targeted(world):
    running, dep = replacement(world)

    def refuse(*_):
        raise ProviderError("no")

    world.provider.update_app = refuse
    failed = execute(world, dep)
    assert failed["status"] == "failed"
    assert reason_of(destroy, world, failed, True) == "record_not_authoritative"
    assert world.provider.apps


def test_stopping_an_app_that_is_already_gone_is_not_reported_as_destroyed(world):
    dep = run_all(world.ctrl, payload(1))
    world.provider.apps.clear()
    destroy(world, dep, True)
    assert world.ctrl.audit.events[-1]["provider_status"] == "already_gone"


# --- expiry: a destroy whose outcome is unknown --------------------------------------------------


def expired_app(world):
    dep = run_all(world.ctrl, payload(1, ttl_minutes=1))
    world.clock.now += 120
    return dep


def expire_events(world):
    return [e for e in world.ctrl.audit.events if e["action"] == "expire_destroy"]


def test_lost_destroy_response_is_finalized_once_after_the_app_is_read_again(world):
    dep = expired_app(world)
    world.provider.fail_next("destroy", "after")  # deleted, then the response was lost
    assert [r["action"] for r in world.ctrl.reconcile()] == ["destroyed"]
    assert world.ctrl.get(dep["deployment_id"])["status"] == "expired"
    assert len(expire_events(world)) == 1 and not world.provider.apps
    assert world.ctrl.reconcile() == [] and len(expire_events(world)) == 1  # exactly once


def test_destroy_that_did_not_land_leaves_the_records_and_retries_next_pass(world):
    dep = expired_app(world)
    world.provider.fail_next("destroy", "before")
    assert [r["action"] for r in world.ctrl.reconcile()] == ["alert"]
    assert world.ctrl.get(dep["deployment_id"])["status"] == "running"
    assert "expired_destroy_failed" in alert_reasons(world.ctrl) and not expire_events(world)
    assert [r["action"] for r in world.ctrl.reconcile()] == ["destroyed"]
    assert world.ctrl.get(dep["deployment_id"])["status"] == "expired"


def test_destroy_and_lookup_both_failing_is_uncertain_never_expired_on_a_guess(world):
    dep = expired_app(world)
    real_get = world.provider.get_app
    state = {"calls": 0}

    def get(app_id):
        state["calls"] += 1
        if state["calls"] >= 2:  # the sweep's first read works, the re-read after the timeout fails
            raise ProviderError("down")
        return real_get(app_id)

    world.provider.get_app = get
    world.provider.fail_next("destroy", "after")
    assert [r["action"] for r in world.ctrl.reconcile()] == ["uncertain"]
    record = world.ctrl.get(dep["deployment_id"])
    assert record["status"] == "running" and record["reason"] == "expiry_destroy_unknown"
    assert "expired_destroy_outcome_unknown" in alert_reasons(world.ctrl)
    assert not expire_events(world)
    # The app is gone but the lookup keeps failing: still not marked expired, still alerting.
    world.ctrl.reconcile()
    assert world.ctrl.get(dep["deployment_id"])["status"] == "running"
    assert not expire_events(world)
    world.provider.get_app = real_get  # the provider recovers: absence is confirmed, finalized
    world.ctrl.reconcile()
    assert world.ctrl.get(dep["deployment_id"])["status"] == "expired"
    assert len(expire_events(world)) == 1


# --- reconcile reports the real recovery outcome ------------------------------------------------


def adopt_decisions(ctrl):
    return [(e["decision"], e["reason"]) for e in ctrl.audit.events if e["action"] == "adopt"]


def test_a_clean_app_is_adopted_and_audited_as_allowed(world):
    run_all(world.ctrl, payload(1))
    assert [r["action"] for r in world.ctrl.reconcile()] == ["adopted"]
    assert adopt_decisions(world.ctrl) == [("allow", "adopted")]


def test_left_uncertain_is_audited_denied_and_alerted(world):
    uncertain_replacement(world)
    assert [r["action"] for r in world.ctrl.reconcile()] == ["left_uncertain"]
    assert adopt_decisions(world.ctrl) == [("deny", "left_uncertain")]
    assert "adopt_left_uncertain" in alert_reasons(world.ctrl)


def test_recovery_rejected_is_audited_denied_and_alerted(world):
    running, dep = replacement(world)

    def die(*_args, **_kwargs):
        raise Crash

    world.ctrl._finish = die
    with pytest.raises(Crash):
        execute(world, dep)
    world.provider.bump_version(running["app_id"])
    restarted = controller(world)
    assert [r["action"] for r in restarted.reconcile()] == ["recovery_rejected"]
    assert adopt_decisions(restarted) == [("deny", "recovery_rejected")]
    assert "replace_failed_uncertain:unexpected_writer" in alert_reasons(restarted)


def test_execute_audit_decision_reports_the_real_outcome(world):
    uncertain_replacement(world)
    executes = [e for e in world.ctrl.audit.events if e["action"] == "execute"]
    assert executes[0]["decision"] == "allow"
    assert executes[-1]["decision"] == "alert" and executes[-1]["reason"] == "unexpected_writer"
