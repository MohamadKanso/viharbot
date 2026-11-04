"""Reconciliation: the TTL sweep, classification, adoption and completing an interrupted bind."""

from __future__ import annotations

import json

import pytest
from deployment_controller_helpers import (
    KEY, T0, alert_reasons, app_of, controller, execute, make_world, payload, run_all,
    submit_approved,
)

from viharbot.deployment_controller import markers
from viharbot.deployment_controller.store import RecordStore


@pytest.fixture
def world(tmp_path):
    return make_world(tmp_path)

def test_ttl_sweep_destroys_only_expired_apps_with_verified_ownership(world):
    short = run_all(world.ctrl, payload(1, ttl_minutes=1))
    long = run_all(world.ctrl, payload(2, ttl_minutes=60))
    world.clock.now += 120
    results = world.ctrl.reconcile()
    assert {r["action"] for r in results} == {"destroyed", "adopted"}
    assert short["app_id"] not in world.provider.apps and long["app_id"] in world.provider.apps
    assert world.ctrl.get(short["deployment_id"])["status"] == "expired"


def test_sweep_destroy_failure_alerts_and_leaves_the_app(world):
    dep = run_all(world.ctrl, payload(1, ttl_minutes=1))
    world.clock.now += 120
    world.provider.fail_next("destroy", "before")
    assert world.ctrl.reconcile()[0]["action"] == "alert"
    assert dep["app_id"] in world.provider.apps and "expired_destroy_failed" in alert_reasons(
        world.ctrl)


# --- ownership markers and reconciliation --------------------------------------------------------


def foreign_marker(world, app, *, team="team-other", expires=T0 + 1):
    claim = markers.Claim("c-x", "i", team, int(T0), int(expires), "h")
    app.spec["env"][markers.CLAIM_KEY] = markers.claim_entry(KEY, claim)
    app.spec["env"][markers.BOUND_KEY] = markers.bound_entry(KEY, claim, app.app_id)


def test_restart_classifies_valid_expired_missing_foreign_and_tampered(world):
    live = run_all(world.ctrl, payload(1, ttl_minutes=60))
    expiring = run_all(world.ctrl, payload(2, ttl_minutes=1))
    unmarked = world.provider.add_foreign("orbit-unmarked")
    other_team = world.provider.add_foreign("orbit-other-team")
    foreign_marker(world, other_team)
    tampered = world.provider.add_foreign("orbit-tampered")
    world.provider.copy_markers(live["app_id"], tampered.app_id, (markers.CLAIM_KEY,))
    data = json.loads(tampered.spec["env"][markers.CLAIM_KEY])
    data["expires_at"] = 1
    tampered.spec["env"][markers.CLAIM_KEY] = json.dumps(data)
    garbage = world.provider.add_foreign("orbit-garbage", {markers.CLAIM_KEY: "{not json"})
    unrelated = world.provider.add_foreign("someone-elses-app")
    world.clock.now += 3 * 60

    restarted = controller(world, store=RecordStore(world.path.with_name("lost.json")))
    results = {r["app_id"]: r for r in restarted.reconcile()}

    assert results[expiring["app_id"]]["action"] == "destroyed"
    assert results[live["app_id"]]["action"] == "adopted"
    for left in (unmarked, other_team, tampered, garbage):
        assert left.app_id in world.provider.apps
        assert results[left.app_id]["action"] == "alert"
    assert results[tampered.app_id]["classification"] == markers.UNOWNED
    assert unrelated.app_id in world.provider.apps and unrelated.app_id not in results
    adopted = restarted.store.all("deployments")
    assert [d["expires_at"] for d in adopted if d["app_id"] == live["app_id"]] == [int(T0) + 3600]


def test_non_ascii_marker_values_are_unowned_and_do_not_abort_reconciliation(world):
    dep = run_all(world.ctrl, payload())
    odd = world.provider.add_foreign("orbit-odd")
    world.provider.copy_markers(dep["app_id"], odd.app_id, (markers.CLAIM_KEY,))
    odd.spec["env"][markers.BOUND_KEY] = "caf\u00e9"
    garbled = json.loads(odd.spec["env"][markers.CLAIM_KEY])
    garbled["mac"] = "\u00e9"
    other = world.provider.add_foreign("orbit-odd2", {markers.CLAIM_KEY: json.dumps(garbled)})
    results = {r["app_id"]: r["action"] for r in world.ctrl.reconcile()}
    assert results[odd.app_id] == "alert" and results[other.app_id] == "alert"
    assert results[dep["app_id"]] == "adopted"


def test_foreign_orbit_resources_are_never_destroyed_however_late(world):
    apps = [world.provider.add_foreign(f"orbit-f{i}") for i in range(2)]
    foreign_marker(world, apps[1], team="team-1", expires=T0 - 10)
    del apps[1].spec["env"][markers.BOUND_KEY]  # an expired claim with no bound marker
    world.clock.now += 10 * 86400
    world.ctrl.reconcile()
    assert all(a.app_id in world.provider.apps for a in apps)
    assert "destroy" not in world.provider.calls


def test_copied_bound_marker_never_authorizes_adoption_or_deletion(world):
    original = run_all(world.ctrl, payload(1, ttl_minutes=1))
    copy_app = world.provider.add_foreign("orbit-copy")
    world.provider.copy_markers(original["app_id"], copy_app.app_id,
                                (markers.CLAIM_KEY, markers.BOUND_KEY))
    assert markers.verified_bound(KEY, copy_app, "team-1") is None
    assert markers.classify(KEY, copy_app, "team-1", T0) == markers.CLAIM_ONLY
    del world.provider.apps[original["app_id"]]  # original absent: the copy is the only one
    world.clock.now += 3600
    result = world.ctrl.reconcile()
    assert [r["action"] for r in result] == ["alert"]
    assert copy_app.app_id in world.provider.apps and "destroy" not in world.provider.calls
    assert world.ctrl.store.all("deployments")[0]["app_id"] == original["app_id"]

def bind_fails(world, body=None):
    dep = submit_approved(world.ctrl, body or payload())
    world.provider.fail_next("update", "before")
    done = execute(world, dep)
    assert done["status"] == "created_unbound" and done["reason"] == "bind_pending"
    return dep, done


def test_crash_between_create_and_bind_with_store_lost_is_alerted_and_untouched(world):
    _, done = bind_fails(world)
    restarted = controller(world, store=RecordStore(world.path.with_name("lost.json")))
    assert [r["action"] for r in restarted.reconcile()] == ["alert"]
    assert markers.BOUND_KEY not in app_of(world, done).env
    assert "destroy" not in world.provider.calls


def test_crash_between_create_and_bind_with_store_intact_completes_the_bind(world):
    dep, done = bind_fails(world)
    assert "bind_pending" in alert_reasons(world.ctrl)
    restarted = controller(world)
    assert [r["action"] for r in restarted.reconcile()] == ["bound"]
    assert markers.verified_bound(KEY, app_of(world, done), "team-1")
    assert restarted.get(done["deployment_id"])["status"] == "running"
    assert restarted.get_approval(dep["approval_id"])["state"] == "executed"


def test_valid_unbound_claim_copied_to_a_second_app_never_produces_a_bound_marker(world):
    dep, done = bind_fails(world)
    twin = world.provider.add_foreign("orbit-twin")
    world.provider.copy_markers(done["app_id"], twin.app_id, (markers.CLAIM_KEY,))
    restarted = controller(world)
    assert [r["action"] for r in restarted.reconcile()] == ["alert", "alert"]  # duplicate claims
    assert markers.BOUND_KEY not in app_of(world, done).env
    assert markers.BOUND_KEY not in twin.env


def test_sole_copied_claim_inside_the_reserved_window_is_left_unbound(world):
    dep = submit_approved(world.ctrl, payload())
    world.provider.fail_next("create", "before")
    execute(world, dep)
    appr = world.ctrl.get_approval(dep["approval_id"])
    stored = world.ctrl.get(dep["deployment_id"])
    claim = markers.Claim(appr["claim_id"], "controller-1", "team-1", int(T0),
                          stored["expires_at"], appr["request_hash"])
    planted = world.provider.add_foreign(
        "orbit-planted", {markers.CLAIM_KEY: markers.claim_entry(KEY, claim)})
    assert [r["action"] for r in controller(world).reconcile()] == ["alert"]
    assert markers.BOUND_KEY not in planted.env


def test_recovery_after_a_lost_bind_response_refreshes_the_deployment_record(world):
    dep, done = bind_fails(world)
    app = app_of(world, done)  # the bind landed at the provider but the controller never heard
    claim = markers.parse_claim(KEY, app.env)
    app.spec["env"][markers.BOUND_KEY] = markers.bound_entry(KEY, claim, app.app_id)
    restarted = controller(world)
    assert [r["action"] for r in restarted.reconcile()] == ["adopted"]
    record = restarted.get(done["deployment_id"])
    assert record["status"] == "running" and record["reason"] is None
    assert restarted.get_approval(dep["approval_id"])["state"] == "executed"


def test_an_expired_claim_only_app_is_never_bound_adopted_or_deleted(world):
    dep, done = bind_fails(world)
    world.clock.now += 2 * 3600
    restarted = controller(world)
    assert [r["action"] for r in restarted.reconcile()] == ["alert"]
    assert "left_alone:claim_only:expired" in alert_reasons(restarted)
    assert markers.BOUND_KEY not in app_of(world, done).env
    assert "destroy" not in world.provider.calls and "update" in world.provider.calls
    assert restarted.get(done["deployment_id"])["status"] == "created_unbound"

