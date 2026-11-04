"""Fresh clock before reserve and create, team checks on bind, uniform fake-provider faults."""

from __future__ import annotations

import pytest
from deployment_controller_helpers import (
    POLICY, alert_reasons, app_of, execute, make_world, payload, reason_of, replacement,
    submit_approved,
)

from viharbot.deployment_controller.provider import FakeProvider, ProviderTimeout

TTL = POLICY.approval_ttl_seconds


@pytest.fixture
def world(tmp_path):
    return make_world(tmp_path)


def slow_list(world, seconds):
    def hook(op):
        if op == "list":
            world.clock.now += seconds
    world.provider.on_call = hook


def test_expiry_crossed_during_a_slow_quota_read_reserves_and_creates_nothing(world):
    dep = submit_approved(world.ctrl, payload())
    slow_list(world, TTL + 1)
    assert reason_of(execute, world, dep) == "approval_expired"
    assert world.ctrl.get(dep["deployment_id"])["status"] == "expired"
    assert world.ctrl.get_approval(dep["approval_id"])["state"] == "expired"
    assert "create" not in world.provider.calls and not world.provider.apps
    assert world.ctrl.store.data["actions"] == []  # nothing reserved


def test_expiry_exactly_at_the_boundary_before_reservation_is_refused(world):
    dep = submit_approved(world.ctrl, payload())
    slow_list(world, TTL)  # the clock lands exactly on expires_at
    assert world.clock.now + TTL == world.ctrl.get_approval(dep["approval_id"])["expires_at"]
    assert reason_of(execute, world, dep) == "approval_expired"
    assert "create" not in world.provider.calls


def test_a_not_yet_expired_approval_still_succeeds_after_a_slow_read(world):
    dep = submit_approved(world.ctrl, payload())
    slow_list(world, TTL - 1)
    assert execute(world, dep)["status"] == "running"


def test_expiry_during_the_durable_reservation_write_makes_no_provider_call(world):
    dep = submit_approved(world.ctrl, payload())
    commit = world.ctrl.store.commit

    def slow_commit(**changes):
        commit(**changes)
        if any(a["state"] == "reserved" for a in changes.get("approvals", {}).values()):
            world.clock.now += TTL

    world.ctrl.store.commit = slow_commit
    done = execute(world, dep)
    assert done["status"] == "expired" and "create" not in world.provider.calls
    assert world.ctrl.get_approval(dep["approval_id"])["state"] == "expired"
    assert reason_of(execute, world, dep) == "approval_not_pending"  # terminal, not retryable


def foreign_team_on_get(world, team):
    def hook(op):
        if op == "get":
            for app in world.provider.apps.values():
                app.team_id = team
            world.provider.on_call = None
    world.provider.on_call = hook


@pytest.mark.parametrize("conditional", [True, False])
@pytest.mark.parametrize("team", ["team-2", ""])
def test_bind_refuses_a_foreign_or_missing_team_and_alerts(tmp_path, conditional, team):
    world = make_world(tmp_path, conditional_update=conditional)
    foreign_team_on_get(world, team)
    done = execute(world, submit_approved(world.ctrl, payload()))
    assert done["status"] == "failed_uncertain" and done["reason"] == "foreign_team"
    assert "bind_refused:foreign_team" in alert_reasons(world.ctrl)
    assert "update" not in world.provider.calls
    assert "ORBIT_BOUND" not in app_of(world, done).env


def test_bind_with_the_matching_team_passes(world):
    assert execute(world, submit_approved(world.ctrl, payload()))["status"] == "running"


def test_bind_refuses_an_update_result_for_another_team(world):
    original = world.provider.update_app

    def update(*args, **kwargs):
        result = original(*args, **kwargs)
        result.team_id = "team-2"
        return result

    world.provider.update_app = update
    done = execute(world, submit_approved(world.ctrl, payload()))
    assert done["status"] == "failed_uncertain" and done["reason"] == "foreign_team"
    assert "bind_failed_uncertain:foreign_team" in alert_reasons(world.ctrl)


@pytest.mark.parametrize("op", ["get", "logs", "destroy", "list", "create", "update"])
def test_after_fault_raises_for_every_operation(op):
    provider = FakeProvider()
    app = provider.add_foreign("orbit-x")
    calls = {
        "get": lambda: provider.get_app(app.app_id),
        "logs": lambda: provider.read_logs(app.app_id, 1),
        "destroy": lambda: provider.destroy_app(app.app_id),
        "list": provider.list_apps,
        "create": lambda: provider.create_app("orbit-y", {"env": {}}),
        "update": lambda: provider.update_app(app.app_id, {"env": {"A": "1"}}),
    }
    provider.fail_next(op, "after")
    with pytest.raises(ProviderTimeout):
        calls[op]()
    if op == "destroy":
        assert app.app_id not in provider.apps  # applied remotely, response lost
    if op == "create":
        assert "orbit-y" in {a.name for a in provider.apps.values()}
    if op == "update":
        assert provider.apps[app.app_id].spec["env"] == {"A": "1"}
    if op not in ("destroy", "create"):
        calls[op]()  # the fault was one-shot


# --- part 2 paths: replacement and timeout recovery share the same clock and team logic ---

def test_replacement_approval_expiring_during_a_slow_preflight_applies_nothing(world):
    running, dep = replacement(world)
    world.provider.on_call = lambda op: setattr(world.clock, "now", world.clock.now + TTL) \
        if op == "get" else None  # the preflight read is the slow one
    assert reason_of(execute, world, dep) == "approval_expired"
    assert world.provider.calls.count("update") == 1  # only the original bind
    assert world.ctrl.get_approval(dep["approval_id"])["state"] == "expired"


@pytest.mark.parametrize("conditional", [True, False])
def test_replacement_refuses_a_result_for_another_team(tmp_path, conditional):
    world = make_world(tmp_path, conditional_update=conditional)
    running, dep = replacement(world)
    original_update = world.provider.update_app

    def update(*args, **kwargs):
        result = original_update(*args, **kwargs)
        result.team_id = "team-2"
        world.provider.apps[result.app_id].team_id = "team-2"  # the read-back agrees
        return result

    world.provider.update_app = update
    done = execute(world, dep)
    assert done["status"] == "failed_uncertain" and done["reason"] == "foreign_team"
    assert "replace_failed_uncertain:foreign_team" in alert_reasons(world.ctrl)


def test_timeout_recovery_bind_refuses_a_foreign_team(world):
    dep = submit_approved(world.ctrl, payload())
    world.provider.fail_next("create", "after")
    foreign_team_on_get(world, "team-2")
    done = execute(world, dep)
    assert done["status"] == "failed_uncertain" and done["reason"] == "foreign_team"
    assert world.provider.calls.count("update") == 0
    assert world.ctrl.get_approval(dep["approval_id"])["state"] == "reserved"
