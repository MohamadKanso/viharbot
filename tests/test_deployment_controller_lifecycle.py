"""Stop, delete and logs: only an app whose ownership proof verifies is touched (part 1)."""

from __future__ import annotations

import pytest
from deployment_controller_helpers import (
    KEY, alert_reasons, app_of, controller, execute, make_world, payload, reason_of, run_all,
    submit, submit_approved,
)

from viharbot.deployment_controller import markers


@pytest.fixture
def world(tmp_path):
    return make_world(tmp_path)

def test_stop_and_delete_refuse_an_app_without_verified_ownership(world):
    dep = run_all(world.ctrl, payload())
    del app_of(world, dep).spec["env"][markers.BOUND_KEY]
    for keep in (True, False):
        assert reason_of(world.ctrl.destroy, dep["deployment_id"], caller="orbit",
                         request_id="r", keep_record=keep) == "ownership_not_verified"
    assert dep["app_id"] in world.provider.apps and "ownership_not_verified" in alert_reasons(
        world.ctrl)


def test_stop_keeps_the_record_and_delete_removes_it(world):
    first = run_all(world.ctrl, payload(1))
    stopped = world.ctrl.destroy(first["deployment_id"], caller="orbit", request_id="r",
                                 keep_record=True)
    assert stopped["status"] == "stopped" and first["app_id"] not in world.provider.apps
    assert world.ctrl.get(first["deployment_id"])["status"] == "stopped"
    second = run_all(world.ctrl, payload(2))
    world.ctrl.destroy(second["deployment_id"], caller="orbit", request_id="r", keep_record=False)
    assert reason_of(world.ctrl.get, second["deployment_id"]) == "unknown_deployment"
    assert reason_of(world.ctrl.get_approval, "apr_none") == "unknown_approval"


def test_destroy_edge_cases(world):
    pending = submit(world, payload(1))
    assert reason_of(world.ctrl.destroy, pending["deployment_id"], caller="o", request_id="r",
                     keep_record=True) == "record_not_authoritative"
    dep = run_all(world.ctrl, payload(2))
    world.provider.fail_next("get", "before")
    assert reason_of(world.ctrl.destroy, dep["deployment_id"], caller="o", request_id="r",
                     keep_record=True) == "provider_unavailable"
    world.provider.fail_next("destroy", "before")
    assert reason_of(world.ctrl.destroy, dep["deployment_id"], caller="o", request_id="r",
                     keep_record=True) == "provider_unavailable"
    del world.provider.apps[dep["app_id"]]  # already gone: the record is still closed out
    assert world.ctrl.destroy(dep["deployment_id"], caller="o", request_id="r",
                              keep_record=True)["status"] == "stopped"


def test_logs_are_capped_and_need_ownership(tmp_path):
    world = make_world(tmp_path)
    world.ctrl = controller(world, max_log_bytes=50)
    dep = run_all(world.ctrl, payload())
    world.provider.logs[dep["app_id"]] = "\n".join(f"line {i}" for i in range(2000))
    text = world.ctrl.logs(dep["deployment_id"], 10**6, caller="o", request_id="r")
    assert text == ("\n".join(f"line {i}" for i in range(2000))[-50:])
    assert len(world.ctrl.logs(dep["deployment_id"], 0, caller="o", request_id="r").splitlines()) <= 2
    world.provider.fail_next("logs", "before")
    assert reason_of(world.ctrl.logs, dep["deployment_id"], 5, caller="o",
                     request_id="r") == "provider_unavailable"
    del app_of(world, dep).spec["env"][markers.BOUND_KEY]
    assert reason_of(world.ctrl.logs, dep["deployment_id"], 5, caller="o",
                     request_id="r") == "ownership_not_verified"
    del world.provider.apps[dep["app_id"]]
    assert reason_of(world.ctrl.logs, dep["deployment_id"], 5, caller="o",
                     request_id="r") == "app_gone"


def test_a_marker_copied_to_another_app_is_never_ownership_proof(world):
    original = run_all(world.ctrl, payload(1))
    copy_app = world.provider.add_foreign("orbit-copy")
    world.provider.copy_markers(original["app_id"], copy_app.app_id,
                                (markers.CLAIM_KEY, markers.BOUND_KEY))
    assert markers.verified_bound(KEY, copy_app, "team-1") is None
    assert markers.verified_bound(KEY, app_of(world, original), "team-1") is not None
    other_team = markers.verified_bound(KEY, app_of(world, original), "team-2")
    assert other_team is None


def test_stop_cannot_overwrite_a_conflict_record(tmp_path):
    world = make_world(tmp_path, conditional_update=False)
    dep = submit_approved(world.ctrl, payload())
    world.provider.before_update = lambda p, app_id: p.bump_version(app_id)
    done = execute(world, dep)
    assert done["status"] == "failed_uncertain"
    assert reason_of(world.ctrl.destroy, done["deployment_id"], caller="orbit", request_id="r",
                     keep_record=True) == "record_not_authoritative"
    assert world.ctrl.get(done["deployment_id"])["status"] == "failed_uncertain"
