"""Replacement updates: preflight, the conditional update, read-back, races and record history."""

from __future__ import annotations

import pytest
from deployment_controller_helpers import (
    KEY, Crash, alert_reasons, app_of, controller, execute, make_world, payload, reason_of,
    replacement, run_all, submit_approved,
)

from viharbot.deployment_controller import markers
from viharbot.deployment_controller.provider import ProviderError
from viharbot.deployment_controller.store import RecordStore

TWO_HOURS = 2 * 3600


@pytest.fixture
def world(tmp_path):
    return make_world(tmp_path)

def updates(world):
    return world.provider.calls.count("update")


def test_replacement_binds_team_app_and_version_and_succeeds(world):
    running, dep = replacement(world)
    request = world.ctrl.get_approval(dep["approval_id"])["request"]
    assert request["replaces"] == {"app_id": running["app_id"], "expected_version": 2}
    done = execute(world, dep)
    app = app_of(world, running)
    assert done["status"] == "running" and app.version == 3 and app.env["LOG_LEVEL"] == "debug"
    assert markers.verified_bound(KEY, app, "team-1")
    assert done["kind"] == "replace" and world.provider.calls.count("create") == 1


def test_replacement_timeout_after_update_landed_is_recognised(world):
    _, dep = replacement(world)
    before = updates(world)
    world.provider.fail_next("update", "after")
    assert execute(world, dep)["status"] == "running"
    assert updates(world) == before + 1
    assert world.ctrl.get_approval(dep["approval_id"])["state"] == "executed"


def test_replacement_timeout_before_update_stays_reserved_and_alerts(world):
    running, dep = replacement(world)
    before = updates(world)
    world.provider.fail_next("update", "before")
    done = execute(world, dep)
    assert done["status"] == "failed_uncertain" and done["reason"] == "update_outcome_unknown"
    assert world.ctrl.get_approval(dep["approval_id"])["state"] == "reserved"
    assert reason_of(execute, world, dep) == "approval_not_pending"
    assert updates(world) == before + 1 and app_of(world, running).env["LOG_LEVEL"] == "info"
    assert "replace_failed_uncertain:update_outcome_unknown" in alert_reasons(world.ctrl)


def test_replacement_crash_between_reservation_and_update(world):
    running, dep = replacement(world)

    def die(*_):
        raise Crash

    world.provider.update_app = die
    with pytest.raises(Crash):
        execute(world, dep)
    restarted = controller(world)
    restarted.reconcile()
    assert "reservation_unresolved" in alert_reasons(restarted)
    assert reason_of(execute, world, dep, restarted) == "approval_not_pending"
    assert app_of(world, running).env["LOG_LEVEL"] == "info"


def test_replacement_lost_state_refuses_the_old_approval(world, tmp_path):
    _, dep = replacement(world)
    world.provider.fail_next("update", "before")
    execute(world, dep)
    wiped = controller(world, store=RecordStore(tmp_path / "wiped.json"))
    assert reason_of(execute, world, dep, wiped) == "unknown_approval"


def test_replacement_replay_of_consumed_approval_is_refused(world):
    running, dep = replacement(world)
    execute(world, dep)
    before = updates(world)
    assert reason_of(execute, world, dep) == "approval_not_pending"
    assert updates(world) == before and app_of(world, running).version == 3


def test_replacement_target_substitution_is_refused(world):
    running, dep = replacement(world)
    del world.provider.apps[running["app_id"]]
    world.provider.add_foreign("orbit-demo-1")  # a different app now sits behind the name
    before = updates(world)
    assert reason_of(execute, world, dep) == "replacement_preflight_failed"
    assert world.ctrl.get_approval(dep["approval_id"])["reason"] == "preflight_target_missing"
    assert updates(world) == before


def test_replacement_of_an_unowned_or_renamed_target_is_refused(world):
    running, dep = replacement(world)
    del app_of(world, running).spec["env"][markers.BOUND_KEY]
    assert reason_of(execute, world, dep) == "replacement_preflight_failed"
    assert world.ctrl.get_approval(dep["approval_id"])["reason"] == "preflight_target_not_owned"
    running, dep = replacement(world, 2)
    app_of(world, running).name = "orbit-renamed"
    assert reason_of(execute, world, dep) == "replacement_preflight_failed"
    assert world.ctrl.get_approval(dep["approval_id"])["reason"] == "preflight_target_name_mismatch"


def test_intervening_update_before_preflight_needs_a_fresh_approval(world):
    running, dep = replacement(world)
    world.provider.bump_version(running["app_id"])
    assert reason_of(execute, world, dep) == "replacement_preflight_failed"
    assert world.ctrl.get_approval(dep["approval_id"])["reason"] == "preflight_version_changed"
    assert reason_of(execute, world, dep) == "approval_not_pending"


def test_version_change_after_preflight_with_a_conditional_update_applies_nothing(world):
    running, dep = replacement(world)
    world.provider.before_update = lambda p, app_id: p.bump_version(app_id)
    done = execute(world, dep)
    assert done["status"] == "failed" and done["reason"] == "version_conflict"
    assert app_of(world, running).env["LOG_LEVEL"] == "info"


def test_version_change_after_preflight_without_a_conditional_update_is_detected(tmp_path):
    world = make_world(tmp_path, conditional_update=False)
    _, dep = replacement(world)
    world.provider.before_update = lambda p, app_id: p.bump_version(app_id)
    done = execute(world, dep)
    assert done["status"] == "failed_uncertain" and done["reason"] == "unexpected_writer"
    assert "replace_failed_uncertain:unexpected_writer" in alert_reasons(world.ctrl)


def test_definite_provider_error_on_replacement(world):
    running, dep = replacement(world)

    def refuse(*_):
        raise ProviderError("nope")

    world.provider.update_app = refuse
    assert execute(world, dep)["reason"] == "provider_error_on_update"


def test_daily_quota_counts_replacements(tmp_path):
    world = make_world(tmp_path)
    world.ctrl = controller(world, max_concurrent=99, max_creates_per_hour=99,
                            max_actions_per_day=2)
    running, dep = replacement(world)  # one create plus the approved replacement
    assert execute(world, dep)["status"] == "running"
    third = submit_approved(world.ctrl, payload(3))
    assert reason_of(execute, world, third) == "daily_quota_exhausted"
    world.clock.now += 86401
    assert run_all(world.ctrl, payload(4))["status"] == "running"


# --- review round: conflicts survive reconcile, evidence before bind, generations, locking -------


def race_world(tmp_path):
    world = make_world(tmp_path, conditional_update=False)
    running, dep = replacement(world)
    world.provider.before_update = lambda p, app_id: p.bump_version(app_id)
    done = execute(world, dep)
    assert done["reason"] == "unexpected_writer"
    world.provider.before_update = None
    return world, running, dep, done


def test_reconcile_after_the_writer_race_keeps_failed_uncertain_and_the_alert(tmp_path):
    world, running, dep, done = race_world(tmp_path)
    for ctrl in (world.ctrl, controller(world)):  # the same controller, then a restart
        results = ctrl.reconcile()
        assert [r["action"] for r in results] == ["left_uncertain"]  # never "adopted" over it
        record = ctrl.get(done["deployment_id"])
        assert record["status"] == "failed_uncertain" and record["reason"] == "unexpected_writer"
        assert ctrl.get_approval(dep["approval_id"])["state"] == "reserved"
        assert "failed_uncertain_unresolved:unexpected_writer" in alert_reasons(ctrl)
    assert controller(world).get(running["deployment_id"])["status"] == "running"
    assert len(world.ctrl.store.all("deployments")) == 2  # nothing "adopted" on top of the conflict


def test_reconcile_after_an_unknown_replacement_outcome_stays_failed_uncertain(world):
    running, dep = replacement(world)
    world.provider.fail_next("update", "before")
    done = execute(world, dep)
    assert done["reason"] == "update_outcome_unknown"
    restarted = controller(world)
    restarted.reconcile()
    assert restarted.get(done["deployment_id"])["status"] == "failed_uncertain"
    assert "failed_uncertain_unresolved:update_outcome_unknown" in alert_reasons(restarted)


def test_crash_after_a_clean_replacement_is_adopted_and_supersedes_the_old_record(world):
    running, dep = replacement(world)

    def die(*_args, **_kwargs):
        raise Crash

    world.ctrl._finish = die
    with pytest.raises(Crash):
        execute(world, dep)
    restarted = controller(world)
    assert [r["action"] for r in restarted.reconcile()] == ["adopted"]
    assert restarted.get_approval(dep["approval_id"])["state"] == "executed"
    assert restarted.get(dep["deployment_id"])["status"] == "running"
    assert restarted.get(running["deployment_id"])["status"] == "superseded"


def test_crash_after_a_replacement_with_a_foreign_version_is_not_adopted_as_success(tmp_path):
    world = make_world(tmp_path, conditional_update=False)
    running, dep = replacement(world)

    def die(*_args, **_kwargs):
        raise Crash

    world.ctrl._finish = die
    with pytest.raises(Crash):
        execute(world, dep)
    world.provider.bump_version(running["app_id"])
    restarted = controller(world)
    assert [r["action"] for r in restarted.reconcile()] == ["recovery_rejected"]
    assert restarted.get(dep["deployment_id"])["reason"] == "unexpected_writer"
    assert restarted.get_approval(dep["approval_id"])["state"] == "reserved"
    assert "replace_failed_uncertain:unexpected_writer" in alert_reasons(restarted)
    assert not [d for d in restarted.store.all("deployments") if d["kind"] == "adopted"]


def test_crash_after_a_replacement_with_an_unexplained_spec_is_not_adopted_as_success(world):
    running, dep = replacement(world)

    def die(*_args, **_kwargs):
        raise Crash

    world.ctrl._finish = die
    with pytest.raises(Crash):
        execute(world, dep)
    world.provider.apps[running["app_id"]].spec["size"] = "something-else"  # same version
    restarted = controller(world)
    assert [r["action"] for r in restarted.reconcile()] == ["recovery_rejected"]
    assert restarted.get(dep["deployment_id"])["reason"] == "unexpected_writer"
    assert restarted.get_approval(dep["approval_id"])["state"] == "reserved"
    assert "replace_failed_uncertain:unexpected_writer" in alert_reasons(restarted)
    assert not [d for d in restarted.store.all("deployments") if d["kind"] == "adopted"]


def test_a_replacement_supersedes_the_old_record_in_the_same_commit_as_its_own_success(world):
    running, dep = replacement(world)
    commits = []
    real = world.ctrl.store.commit
    world.ctrl.store.commit = lambda **ch: (commits.append(ch), real(**ch))[1]
    execute(world, dep)
    final = commits[-1]["deployments"]
    assert final[running["deployment_id"]]["status"] == "superseded"
    assert final[dep["deployment_id"]]["status"] == "running"


def replaced_and_running(world):
    running, dep = replacement(world)
    done = execute(world, dep)
    assert done["status"] == "running"
    return running, done


def test_one_app_id_has_one_authoritative_record_and_expiry_covers_it(world):
    running, done = replaced_and_running(world)
    assert world.ctrl.get(running["deployment_id"])["status"] == "superseded"
    world.clock.now += 3 * 3600
    assert [r["action"] for r in world.ctrl.reconcile()] == ["destroyed"]
    assert world.ctrl.get(done["deployment_id"])["status"] == "expired"
    assert world.ctrl.get(running["deployment_id"])["status"] == "superseded"  # history stays
    assert not world.provider.apps


def test_stop_marks_the_authoritative_record_and_leaves_history(world):
    running, done = replaced_and_running(world)
    stopped = world.ctrl.destroy(done["deployment_id"], caller="orbit", request_id="r",
                                 keep_record=True)
    assert stopped["status"] == "stopped"
    assert world.ctrl.get(running["deployment_id"])["status"] == "superseded"


def test_expiry_leaves_an_app_with_an_unresolved_record_for_a_person(tmp_path):
    world, running, _, done = race_world(tmp_path)
    world.clock.now += 3 * 3600
    assert [r["action"] for r in world.ctrl.reconcile()] == ["refused:app_has_unresolved_outcome"]
    assert world.provider.apps  # not destroyed while any record of the app is unresolved
    assert world.ctrl.get(running["deployment_id"])["status"] == "running"
    assert world.ctrl.get(done["deployment_id"])["status"] == "failed_uncertain"
    assert "expired_destroy_skipped:unresolved_outcome" in alert_reasons(world.ctrl)


def test_stopping_by_an_earlier_generation_stops_the_authoritative_record_too(world):
    old, new = replaced_and_running(world)
    world.ctrl.destroy(old["deployment_id"], caller="orbit", request_id="r", keep_record=True)
    assert world.ctrl.get(new["deployment_id"])["status"] == "stopped"
    assert not world.provider.apps


def test_expiry_before_a_replacement_update_is_never_written(world):
    world.ctrl = controller(world, approval_ttl_seconds=3 * TWO_HOURS)  # approval outlives the claim
    running, dep = replacement(world)
    original = world.ctrl.store.commit

    def commit(**changes):
        original(**changes)
        if "actions" in changes:  # the reservation just became durable
            world.clock.now += TWO_HOURS
    world.ctrl.store.commit = commit
    calls_before = world.provider.calls.count("update")
    done = execute(world, dep)
    assert done["status"] == "failed_uncertain" and done["reason"] == "claim_expired"
    assert world.provider.calls.count("update") == calls_before
    assert "bind_refused:claim_expired" in alert_reasons(world.ctrl)
    assert world.ctrl.get_approval(dep["approval_id"])["state"] == "reserved"


# --- a non-conditional provider: the update response is never trusted on its own ---------------


def after_update(provider, action):
    """Run ``action`` once, right after update_app returns (a writer after the returned snapshot)."""
    original = provider.update_app

    def wrapper(*args, **kwargs):
        result = original(*args, **kwargs)
        provider.update_app = original
        action()
        return result
    provider.update_app = wrapper


def gets(world):
    return world.provider.calls.count("get")


def test_a_conditional_replacement_is_verified_by_the_update_response_alone(world):
    running, dep = replacement(world)
    before = gets(world)  # the preflight read is the only one before the update
    done = execute(world, dep)
    assert done["status"] == "running" and gets(world) == before + 1


def test_a_non_conditional_replacement_with_a_clean_read_back_succeeds(tmp_path):
    world = make_world(tmp_path, conditional_update=False)
    running, dep = replacement(world)
    before = gets(world)
    done = execute(world, dep)
    assert done["status"] == "running" and gets(world) == before + 2  # preflight, then read-back
    assert app_of(world, running).version == 3
    assert world.ctrl.get_approval(dep["approval_id"])["state"] == "executed"
    assert not alert_reasons(world.ctrl)


def test_a_foreign_version_after_the_update_response_is_failed_uncertain(tmp_path):
    world = make_world(tmp_path, conditional_update=False)
    running, dep = replacement(world)
    after_update(world.provider, lambda: world.provider.bump_version(running["app_id"]))
    done = execute(world, dep)  # the response alone showed a clean version 3
    assert done["status"] == "failed_uncertain" and done["reason"] == "unexpected_writer"
    assert "replace_failed_uncertain:unexpected_writer" in alert_reasons(world.ctrl)
    assert world.ctrl.get_approval(dep["approval_id"])["state"] == "reserved"


def test_an_unexplained_spec_after_the_update_response_is_failed_uncertain(tmp_path):
    world = make_world(tmp_path, conditional_update=False)
    running, dep = replacement(world)  # a writer changes the spec without moving the version
    after_update(world.provider,
                 lambda: app_of(world, running).spec["env"].update(FOREIGN="x"))
    done = execute(world, dep)
    assert done["status"] == "failed_uncertain" and done["reason"] == "unexpected_writer"
    assert "replace_failed_uncertain:unexpected_writer" in alert_reasons(world.ctrl)


def test_an_unavailable_read_back_is_failed_uncertain_not_success(tmp_path):
    world = make_world(tmp_path, conditional_update=False)
    _, dep = replacement(world)
    after_update(world.provider, lambda: world.provider.fail_next("get", "before"))
    done = execute(world, dep)
    assert done["status"] == "failed_uncertain" and done["reason"] == "update_outcome_unknown"
    assert "replace_failed_uncertain:update_outcome_unknown" in alert_reasons(world.ctrl)
    assert world.ctrl.get_approval(dep["approval_id"])["state"] == "reserved"
