"""Approval state machine and reserve-then-execute create, with quotas (ADR-007, part 1)."""

from __future__ import annotations

import pytest
from deployment_controller_helpers import (
    KEY, POLICY, T0, Crash, alert_reasons, app_of, approve, controller, execute, make_world,
    payload, reason_of, run_all, submit, submit_approved,
)

from viharbot.deployment_controller import markers
from viharbot.deployment_controller.policy import DeploymentRequest, request_hash
from viharbot.deployment_controller.store import RecordStore


@pytest.fixture
def world(tmp_path):
    return make_world(tmp_path)

def test_changing_an_environment_value_after_approval_invalidates_it(world):
    dep = submit_approved(world.ctrl, payload())
    tampered = world.ctrl.store.get("approvals", dep["approval_id"])
    tampered["request"]["env"]["LOG_LEVEL"] = "debug"
    world.ctrl.store.commit(approvals={dep["approval_id"]: tampered})
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


@pytest.mark.parametrize("reviewed", [
    "\u00e9" * 64,  # non-ASCII: compare_digest on str raised TypeError, a 500 (#176)
    "\u0661" * 64,  # Arabic-Indic digits, which \d would have let through
    "A" * 64, "a" * 63, "a" * 65, "g" * 64, "a" * 64 + "\n", "", None, 64, ["a" * 64],
])
def test_a_malformed_request_hash_is_refused_and_audited_not_a_crash(world, reviewed):
    dep = submit(world, payload())
    assert reason_of(world.ctrl.approve, dep["approval_id"], approver="approver",
                     reviewed_hash=reviewed, request_id="r") == "invalid_request_hash"
    denied = world.ctrl.audit.events[-1]
    assert (denied["action"], denied["decision"], denied["reason"]) == (
        "approve", "deny", "invalid_request_hash")
    assert denied["request_hash"] == dep["request_hash"]  # the stored hash, never the input
    assert world.ctrl.store.get("approvals", dep["approval_id"])["approver"] is None
    approve(world.ctrl, dep)  # nothing was consumed, so the reviewed hash still approves


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
                     reviewed_hash="0" * 64, request_id="r") == "request_hash_mismatch"
    other = world.ctrl.store.get("approvals", dep["approval_id"])
    other["approver"] = "someone-else"
    world.ctrl.store.commit(approvals={dep["approval_id"]: other})
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


# --- reserve-then-execute: create ---
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
    assert reason_of(execute, world, dep, restarted) == "approval_not_pending"
    assert not world.provider.apps


def test_lost_state_after_reservation_refuses_the_old_approval(world, tmp_path):
    dep = submit_approved(world.ctrl, payload())
    world.provider.fail_next("create", "before")
    execute(world, dep)
    wiped = controller(world, store=RecordStore(tmp_path / "wiped.json"))
    assert reason_of(execute, world, dep, wiped) == "unknown_approval"
    assert world.provider.calls.count("create") == 1


# --- quotas ---
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


def test_daily_quota_counts_every_action(tmp_path):
    world = make_world(tmp_path)
    world.ctrl = controller(world, max_concurrent=99, max_creates_per_hour=99,
                            max_actions_per_day=2)
    run_all(world.ctrl, payload(1))
    run_all(world.ctrl, payload(2))
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


def test_two_controllers_on_one_store_consume_an_approval_only_once(world):
    other = controller(world)  # loaded before the approval exists: its cache is stale
    dep = submit_approved(world.ctrl, payload())
    first = execute(world, dep, ctrl=other)
    assert first["status"] == "running"
    assert reason_of(execute, world, dep) == "approval_not_pending"
    assert world.provider.calls.count("create") == 1

