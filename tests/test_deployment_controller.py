"""Deployment controller core (ADR-007, B2a). Offline and deterministic: FakeProvider, no network."""

from __future__ import annotations

import json
import logging
import os

import pytest
from deployment_controller_helpers import (
    DIGEST, KEY, POLICY, T0, Crash, approve, controller, make_world, payload, replacement,
    run_all, submit_approved,
)

from viharbot.deployment_controller import markers
from viharbot.deployment_controller.audit import AuditLog
from viharbot.deployment_controller.policy import (
    DeploymentRequest, PolicyDenied, build_spec, request_hash,
)
from viharbot.deployment_controller.provider import FakeProvider, ProviderError, ProviderTimeout
from viharbot.deployment_controller.store import RecordStore, StoreError


@pytest.fixture
def world(tmp_path):
    return make_world(tmp_path)


def reason_of(call, *args, **kwargs):
    with pytest.raises(PolicyDenied) as info:
        call(*args, **kwargs)
    return info.value.reason


def submit(world, body, caller="orbit"):
    return world.ctrl.submit(body, caller=caller, request_id="r")


def execute(world, dep, ctrl=None):
    return (ctrl or world.ctrl).execute(dep["approval_id"], caller="approver", request_id="r")


def app_of(world, dep):
    return world.provider.apps[dep["app_id"]]


def alert_reasons(ctrl):
    return [a["reason"] for a in ctrl.alerts]


# --- policy: every denial -----------------------------------------------------------------------

DENIALS = {
    "name_not_allowed": [{"name": "web-demo"}, {"name": "orbit-"}, {"name": "orbit-Bad_Name"},
                         {"name": "orbit-" + "a" * 40}],
    "registry_not_allowed": [{"image": {"registry": "docker.io/x", "repository": "orbit/d",
                                        "digest": DIGEST}}],
    "repository_not_allowed": [{"image": {"registry": POLICY.registry, "repository": "other/d",
                                          "digest": DIGEST}},
                               {"image": {"registry": POLICY.registry, "repository": "orbit/D d",
                                          "digest": DIGEST}}],
    "image_must_be_pinned_by_digest": [
        {"image": {"registry": POLICY.registry, "repository": "orbit/d", "digest": "latest"}},
        {"image": {"registry": POLICY.registry, "repository": "orbit/d", "digest": "sha256:abc"}}],
    "size_not_allowed": [{"size": "4gib"}],
    "region_not_allowed": [{"region": "fra"}],
    "port_not_allowed": [{"port": 0}, {"port": 70000}],
    "env_key_not_allowed": [{"env": {"PATH": "x"}}, {"env": {"ORBIT_CLAIM": "x"}}],
    "env_value_not_allowed": [{"env": {"LOG_LEVEL": "has space"}}, {"env": {"LOG_LEVEL": "a" * 129}}],
    "secret_env_value": [{"env": {"LOG_LEVEL": "my-api-key"}}, {"env": {"LOG_LEVEL": "a" * 40}}],
    "ttl_not_allowed": [{"ttl_minutes": 0}, {"ttl_minutes": 24 * 60 + 1}],
    "invalid_request": [
        {"command": "rm -rf /"}, {"volumes": ["/:/host"]}, {"spec": {"services": []}},
        {"secrets": {"K": "v"}}, {"run_command": "sh"}, {"ttl_minutes": "5"}, {"port": True},
        {"image": {"registry": POLICY.registry, "repository": "orbit/d", "digest": DIGEST,
                   "tag": "latest"}},
        {"request_id": "bad id!"}, {"replaces": {"app_id": "a", "expected_version": 1, "x": 1}},
    ],
}


@pytest.mark.parametrize(
    "reason,overrides", [(r, o) for r, items in DENIALS.items() for o in items]
)
def test_every_allowlist_denial(world, reason, overrides):
    assert reason_of(submit, world, payload(**overrides)) == reason
    assert not world.ctrl.store.all("deployments")


def test_missing_field_and_caller_rules(world):
    body = payload()
    del body["name"]
    assert reason_of(submit, world, body) == "invalid_request"
    assert reason_of(submit, world, payload(), caller="web") == "caller_not_requester"


def test_submit_is_idempotent_and_request_id_cannot_be_reused(world):
    first = submit(world, payload())
    assert submit(world, payload())["deployment_id"] == first["deployment_id"]
    assert reason_of(submit, world, payload(ttl_minutes=30)) == "request_id_reused"


def test_spec_is_built_only_from_the_template(world):
    req = DeploymentRequest.model_validate(payload())
    spec = build_spec(req, {"ORBIT_CLAIM": "c"})
    assert set(spec) == {"template", "name", "region", "size", "image", "port", "env"}
    assert spec["env"]["ORBIT_CLAIM"] == "c"


# --- approval contract --------------------------------------------------------------------------


def test_request_hash_binds_every_field_template_version_and_team(monkeypatch):
    req = DeploymentRequest.model_validate(payload())
    base = request_hash(req, "team-1")
    assert request_hash(req, "team-2") != base
    for change in ({"env": {"LOG_LEVEL": "debug", "APP_MODE": "demo"}}, {"ttl_minutes": 61},
                   {"port": 9090}, {"name": "orbit-other"}):
        other = DeploymentRequest.model_validate(payload(**change))
        assert request_hash(other, "team-1") != base
    monkeypatch.setattr("viharbot.deployment_controller.policy.TEMPLATE_VERSION", "orbit-app-v2")
    assert request_hash(req, "team-1") != base


def test_changing_an_environment_value_after_approval_invalidates_it(world):
    dep = submit_approved(world.ctrl, payload())
    world.ctrl.store.data["approvals"][dep["approval_id"]]["request"]["env"]["LOG_LEVEL"] = "debug"
    assert reason_of(execute, world, dep) == "request_changed_after_approval"
    assert not world.provider.apps
    assert reason_of(execute, world, dep) == "approval_not_pending"


def test_approval_of_a_different_request_hash_is_refused(world):
    dep = submit(world, payload())
    other = request_hash(
        DeploymentRequest.model_validate(payload(env={"LOG_LEVEL": "debug"})), "team-1"
    )
    assert reason_of(world.ctrl.approve, dep["approval_id"], approver="approver",
                     reviewed_hash=other, request_id="r") == "request_hash_mismatch"
    assert reason_of(execute, world, dep) == "not_approved"


def test_unauthorized_approver_and_orbit_cannot_approve(world):
    dep = submit(world, payload())
    for who in ("intruder", "orbit"):
        assert reason_of(approve, world.ctrl, dep, approver=who) == "approver_not_authorized"
    assert reason_of(world.ctrl.approve, "apr_missing", approver="approver",
                     reviewed_hash="x", request_id="r") == "unknown_approval"
    assert not world.provider.apps


def test_requester_in_the_approver_list_still_cannot_approve_its_own_request(tmp_path):
    world = make_world(tmp_path)
    world.ctrl = controller(world, approvers=frozenset({"approver", "orbit"}))
    dep = submit(world, payload())
    assert reason_of(approve, world.ctrl, dep, approver="orbit") == "self_approval_refused"


def test_expired_approval_is_refused_and_marked_expired(world):
    dep = submit_approved(world.ctrl, payload())
    world.clock.now += POLICY.approval_ttl_seconds
    assert reason_of(execute, world, dep) == "approval_expired"
    assert world.ctrl.get(dep["deployment_id"])["status"] == "expired"
    late = submit(world, payload(2))
    world.clock.now += POLICY.approval_ttl_seconds
    assert reason_of(approve, world.ctrl, late) == "approval_expired"


def test_execute_needs_an_approval_that_exists_and_was_granted(world):
    assert reason_of(world.ctrl.execute, "apr_missing", caller="x", request_id="r") == (
        "unknown_approval")
    assert reason_of(execute, world, submit(world, payload())) == "not_approved"
    done = run_all(world.ctrl, payload(2))
    assert reason_of(world.ctrl.approve, done["approval_id"], approver="approver",
                     reviewed_hash=done["request_hash"], request_id="r") == "approval_not_pending"


def test_approving_again_is_a_safe_retry_only_for_the_same_approver_and_hash(world):
    dep = submit_approved(world.ctrl, payload())
    approve(world.ctrl, dep)  # same approver, same hash, still pending
    assert reason_of(world.ctrl.approve, dep["approval_id"], approver="approver",
                     reviewed_hash="wrong", request_id="r") == "request_hash_mismatch"
    world.ctrl.store.data["approvals"][dep["approval_id"]]["approver"] = "someone-else"
    assert reason_of(approve, world.ctrl, dep) == "approval_not_pending"


def test_happy_create_binds_and_a_replayed_approval_is_refused(world):
    dep = run_all(world.ctrl, payload())
    app = app_of(world, dep)
    assert dep["status"] == "running" and dep["expires_at"] == int(T0) + 3600
    claim = markers.verified_bound(KEY, app, "team-1")
    assert claim and claim.claim_id and claim.request_hash == dep["request_hash"]
    assert world.ctrl.get_approval(dep["approval_id"])["state"] == "executed"
    assert reason_of(execute, world, dep) == "approval_not_pending"
    assert world.provider.calls.count("create") == 1
    # an approval cannot be re-presented for a second deployment either
    second = submit(world, payload(2))
    assert reason_of(world.ctrl.execute, dep["approval_id"], caller="a", request_id="r") == (
        "approval_not_pending")
    assert second["status"] == "pending_approval"


# --- reserve-then-execute: create ---------------------------------------------------------------


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


def test_definite_provider_error_on_create_never_returns_to_pending(world):
    world.provider.add_foreign("orbit-demo-1")  # name conflict, a definite refusal
    dep = submit_approved(world.ctrl, payload())
    done = execute(world, dep)
    assert done["status"] == "failed" and done["reason"] == "provider_error_on_create"
    assert world.ctrl.get_approval(dep["approval_id"])["state"] == "reserved"


def test_crash_between_reservation_and_create_means_no_automatic_create(world):
    dep = submit_approved(world.ctrl, payload())

    def die(*_):
        raise Crash

    world.provider.create_app = die
    with pytest.raises(Crash):
        execute(world, dep)
    restarted = controller(world)
    assert restarted.get_approval(dep["approval_id"])["state"] == "reserved"
    restarted.reconcile()
    assert "reservation_unresolved" in alert_reasons(restarted)
    assert reason_of(execute, world, dep, restarted) == "approval_not_pending"
    assert not world.provider.apps


def test_lost_state_after_reservation_refuses_the_old_approval(world, tmp_path):
    dep = submit_approved(world.ctrl, payload())
    world.provider.fail_next("create", "before")
    execute(world, dep)
    wiped = controller(world, store=RecordStore(tmp_path / "wiped.json"))
    assert reason_of(execute, world, dep, wiped) == "unknown_approval"
    assert world.provider.calls.count("create") == 1


# --- reserve-then-execute: replacement ----------------------------------------------------------


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


# --- quotas, TTL ---------------------------------------------------------------------------------


def test_hourly_create_quota(tmp_path):
    world = make_world(tmp_path)
    world.ctrl = controller(world, max_concurrent=99)
    for n in range(1, 6):
        run_all(world.ctrl, payload(n))
    dep = submit_approved(world.ctrl, payload(6))
    assert reason_of(execute, world, dep) == "hourly_quota_exhausted"
    assert world.ctrl.get_approval(dep["approval_id"])["state"] == "pending"  # not consumed
    world.clock.now += 3601
    assert run_all(world.ctrl, payload(7))["status"] == "running"


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


def test_concurrent_cap_uses_the_live_listing_including_foreign_orbit_apps(world):
    world.provider.add_foreign("orbit-someone-else")
    run_all(world.ctrl, payload(1))
    run_all(world.ctrl, payload(2))
    assert reason_of(run_all, world.ctrl, payload(3)) == "concurrent_cap_reached"
    restarted = controller(world, store=RecordStore(world.path.with_name("fresh.json")))
    assert reason_of(run_all, restarted, payload(4)) == "concurrent_cap_reached"  # survives restart


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
                     keep_record=True) == "no_app"
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


def test_message_caps_use_the_policy_limit(tmp_path):
    world = make_world(tmp_path)
    dep = run_all(world.ctrl, payload())
    world.provider.logs[dep["app_id"]] = "\n".join(str(i) for i in range(2000))
    text = world.ctrl.logs(dep["deployment_id"], 10**6, caller="o", request_id="r")
    assert len(text.splitlines()) == POLICY.max_log_lines


# --- store ---------------------------------------------------------------------------------------


def test_a_failed_durable_write_means_no_provider_call_and_the_approval_stays_pending(world):
    dep = submit_approved(world.ctrl, payload())

    def broken(_):
        raise StoreError("disk full")

    world.ctrl.store._write = broken
    assert reason_of(execute, world, dep) == "store_unavailable"
    assert "create" not in world.provider.calls
    assert world.ctrl.get_approval(dep["approval_id"])["state"] == "pending"


def test_store_writes_are_atomic_and_a_corrupt_file_is_treated_as_lost(world, monkeypatch):
    run_all(world.ctrl, payload())
    assert not list(world.path.parent.glob("*.tmp"))
    assert json.loads(world.path.read_text())["approvals"]
    before = world.ctrl.store.all("approvals")
    monkeypatch.setattr("os.replace", lambda *a: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(StoreError):
        world.ctrl.store.commit(approvals={"x": {"approval_id": "x"}})
    assert world.ctrl.store.all("approvals") == before
    monkeypatch.undo()
    world.path.write_text("{corrupt")
    assert RecordStore(world.path).all("approvals") == []


def test_the_rename_is_made_durable_with_a_directory_fsync(world, monkeypatch):
    synced = []
    real = os.fsync
    monkeypatch.setattr("os.fsync", lambda fd: (synced.append(fd), real(fd))[1])
    world.ctrl.store.commit(approvals={"x": {"approval_id": "x"}})
    assert len(synced) == 2  # the temporary file, then the containing directory
    monkeypatch.setattr("os.fsync", lambda fd: (_ for _ in ()).throw(OSError("no")))
    with pytest.raises(StoreError):
        world.ctrl.store.commit(approvals={"y": {"approval_id": "y"}})


# --- audit ---------------------------------------------------------------------------------------


def test_audit_events_carry_the_adr_fields_and_never_secrets(world, caplog):
    caplog.set_level(logging.INFO)
    body = payload(env={"LOG_LEVEL": "sentinel-env-value"})
    dep = run_all(world.ctrl, body)
    world.ctrl.logs(dep["deployment_id"], 5, caller="orbit", request_id="r")
    reason_of(submit, world, payload(2, size="huge"))
    fields = {"timestamp", "request_id", "caller", "action", "decision", "reason", "deployment_id",
              "app_id", "image_digest", "size", "region", "ttl_minutes", "provider_status",
              "duration_ms", "error", "approver", "request_hash"}
    events = world.ctrl.audit.events
    assert all(set(e) == fields for e in events)
    execute_event = next(e for e in events if e["action"] == "execute")
    assert execute_event["approver"] == "approver" and execute_event["app_id"] == dep["app_id"]
    assert execute_event["image_digest"] == DIGEST and execute_event["ttl_minutes"] == 60
    assert execute_event["duration_ms"] is not None and execute_event["request_id"] == "r"
    assert any(e["decision"] == "deny" and e["reason"] == "size_not_allowed" for e in events)
    everything = json.dumps(events) + caplog.text + json.dumps(world.ctrl.alerts)
    assert "sentinel-env-value" not in everything
    assert markers.CLAIM_KEY not in everything and KEY.decode() not in everything


def test_audit_refuses_unknown_fields():
    with pytest.raises(ValueError):
        AuditLog().emit("x", "allow", env={"A": "b"})


def test_controller_requires_a_marker_key(tmp_path):
    with pytest.raises(ValueError):
        from viharbot.deployment_controller.controller import DeploymentController
        from viharbot.deployment_controller.policy import Policy
        DeploymentController(provider=FakeProvider(), store=RecordStore(tmp_path / "s.json"),
                             policy=Policy(), marker_key=b"")
