"""Bind safety: version condition, claim expiry checked right before the write, log bytes."""

from __future__ import annotations

import pytest
from deployment_controller_helpers import (
    controller, make_world, payload, run_all, submit_approved,
)

from viharbot.deployment_controller import markers

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
    assert "bind_pending" in alerts(world.ctrl)


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


# --- claim expiry is rechecked immediately before every bind ---
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


def test_expiry_during_a_slow_bind_read_is_never_bound(world):
    """The claim is live when the bind starts but expires while get_app is in flight."""
    dep = submit_approved(world.ctrl, payload())
    claim_expiry = int(world.clock.now) + 3600
    original = world.provider.get_app

    def slow_get(app_id):
        result = original(app_id)
        world.clock.now = claim_expiry + 1  # the read itself crossed the expiry
        return result
    world.provider.get_app = slow_get
    done = go(world, dep)
    assert_expired_not_bound(world, done["deployment_id"], dep["approval_id"])


def test_the_expiry_boundary_during_a_slow_bind_read_is_exclusive(tmp_path):
    for conditional in (True, False):
        for offset, bound in ((-1, True), (0, False)):  # the expiry instant is already late
            home = tmp_path / f"{conditional}{offset}"
            home.mkdir()
            world = make_world(home, conditional_update=conditional)
            dep = submit_approved(world.ctrl, payload())
            claim_expiry = int(world.clock.now) + 3600
            original = world.provider.get_app

            def slow_get(app_id, original=original, world=world, at=claim_expiry + offset):
                result = original(app_id)
                world.clock.now = at
                return result
            world.provider.get_app = slow_get
            done = go(world, dep)
            if bound:
                assert done["status"] == "running"
                assert markers.BOUND_KEY in world.provider.apps[done["app_id"]].env
            else:
                assert_expired_not_bound(world, done["deployment_id"], dep["approval_id"])


@pytest.mark.parametrize("conditional", [True, False])
@pytest.mark.parametrize("edit", [
    lambda spec: spec["image"].update(digest="sha256:" + "b" * 64),
    lambda spec: spec["env"].update(LOG_LEVEL="debug"),
    lambda spec: spec.update(port=9999),
])
def test_a_spec_changed_before_the_bind_read_is_never_bound(tmp_path, conditional, edit):
    world = make_world(tmp_path, conditional_update=conditional)
    dep = submit_approved(world.ctrl, payload())
    after_call(world.provider, "create_app", lambda: edit(world.provider.apps["app-0001"].spec))
    done = go(world, dep)  # the claim is intact, but the spec is no longer the approved one
    assert done["status"] == "failed_uncertain" and done["reason"] == "unexpected_writer"
    assert "bind_failed_uncertain:spec_changed" in alerts(world.ctrl)
    assert markers.BOUND_KEY not in world.provider.apps["app-0001"].env
    assert "update" not in world.provider.calls
    assert world.ctrl.get_approval(dep["approval_id"])["state"] == "reserved"


def test_expiry_crossed_while_the_update_is_in_flight_is_not_undone(world):
    """The guarantee is dispatch-before-expiry: nothing here can stop a write already sent."""
    dep = submit_approved(world.ctrl, payload())
    original = world.provider.update_app

    def slow_update(*args, **kwargs):
        result = original(*args, **kwargs)
        world.clock.now += TWO_HOURS  # the write completes after the claim expired
        return result
    world.provider.update_app = slow_update
    done = go(world, dep)
    assert done["status"] == "running" and markers.BOUND_KEY in world.provider.apps[
        done["app_id"]].env
    assert world.clock.now >= done["expires_at"]  # bound and expired: left to the TTL sweep


# --- log cap is bytes ---
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
