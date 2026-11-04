"""The one app-safety guard: every path goes through it, and the review findings it closes."""

from __future__ import annotations

import inspect

import pytest
from deployment_controller_helpers import (
    KEY, T0, Crash, alert_reasons, controller, execute, make_world, payload, replacement, run_all,
    submit_approved,
)

from viharbot.deployment_controller import markers
from viharbot.deployment_controller.app_safety import ACTIONS
from viharbot.deployment_controller.controller import DeploymentController
from viharbot.deployment_controller.policy import DeploymentRequest, PolicyDenied, build_spec


@pytest.fixture
def world(tmp_path):
    return make_world(tmp_path, conditional_update=False)


def crash_in_finish(world):
    def die(*_args, **_kwargs):
        raise Crash

    world.ctrl._finish = die


def second_replacement(world, running):
    app = world.provider.apps[running["app_id"]]
    body = payload(1, request_id="rep-second", env={"LOG_LEVEL": "warn"},
                   replaces={"app_id": app.app_id, "expected_version": app.version})
    return submit_approved(world.ctrl, body)


# --- HIGH: a new replacement while an earlier one is unresolved -----------------------------------


def refused_second(world, running):
    dep, calls = second_replacement(world, running), len(world.provider.calls)
    with pytest.raises(PolicyDenied) as refused:
        execute(world, dep)
    assert refused.value.reason == "app_has_unresolved_outcome"
    assert "update" not in world.provider.calls[calls:]  # no provider write
    assert world.ctrl.get_approval(dep["approval_id"])["state"] == "pending"  # not consumed
    assert "replace_refused:unresolved_outcome" in alert_reasons(world.ctrl)
    assert any(e["action"] == "execute" and e["decision"] == "deny" for e in world.ctrl.audit.events)


def test_a_new_replacement_is_refused_while_an_earlier_one_is_failed_uncertain(world):
    running, dep = replacement(world)
    world.provider.before_update = lambda p, app_id: p.bump_version(app_id)
    assert execute(world, dep)["status"] == "failed_uncertain"
    world.provider.before_update = None
    refused_second(world, running)


def test_a_new_replacement_is_refused_while_an_earlier_one_is_reserved_after_a_crash(world):
    running, dep = replacement(world)
    crash_in_finish(world)
    with pytest.raises(Crash):
        execute(world, dep)
    world.ctrl = controller(world)
    refused_second(world, running)


# --- MEDIUM: a rejected crashed replacement is never reported adopted --------------------------


def test_a_rejected_crashed_replacement_keeps_its_app_and_is_never_adopted_later(world):
    running, dep = replacement(world)
    crash_in_finish(world)
    with pytest.raises(Crash):
        execute(world, dep)
    stored = controller(world).get(dep["deployment_id"])
    assert stored["status"] == "reserved" and stored["app_id"] == running["app_id"]  # at reservation
    world.provider.bump_version(running["app_id"])
    restarted = controller(world)
    assert [r["action"] for r in restarted.reconcile()] == ["recovery_rejected"]
    for _ in range(2):  # every later pass, from the saved state, stays denied and uncertain
        restarted = controller(world)
        assert [r["action"] for r in restarted.reconcile()] == ["left_uncertain"]
        assert [e["decision"] for e in restarted.audit.events if e["action"] == "adopt"] == ["deny"]
    assert not [d for d in restarted.store.all("deployments") if d["kind"] == "adopted"]
    assert restarted.get(dep["deployment_id"])["status"] == "failed_uncertain"


# --- MEDIUM: claim-only recovery never revives a record a person resolved --------------------------


@pytest.mark.parametrize(("status", "action"), [("failed_uncertain", "bound"), ("failed", "alert")])
def test_claim_only_recovery_respects_a_manual_resolution(world, status, action):
    dep = submit_approved(world.ctrl, payload())
    world.provider.fail_next("create", "before")
    execute(world, dep)
    appr = world.ctrl.get_approval(dep["approval_id"])
    record = world.ctrl.get(dep["deployment_id"])
    claim = markers.Claim(appr["claim_id"], "controller-1", "team-1", int(T0),
                          record["expires_at"], appr["request_hash"])
    req = DeploymentRequest.model_validate(appr["request"])
    app = world.provider.create_app(  # the create did land after all, with the approved spec
        req.name, build_spec(req, {markers.CLAIM_KEY: markers.claim_entry(KEY, claim)}))
    world.provider._operations[claim.claim_id] = app.app_id  # evidence appears later
    record["status"] = status  # the documented manual step marks it failed
    world.ctrl.store.commit(deployments={record["deployment_id"]: record})
    assert [r["action"] for r in controller(world).reconcile()] == [action]
    expected = "running" if action == "bound" else "failed"
    assert controller(world).get(dep["deployment_id"])["status"] == expected


# --- every entry point goes through the guard before any provider mutation ----------------------


def watch_provider(world):
    """Fail the test if a mutating provider call has no prior allowed guard decision for its app."""
    ctrl = world.ctrl

    def checked(name):
        real = getattr(world.provider, name)

        def call(app_id, *args, **kwargs):
            mine = [d for d in ctrl.guard_log if d.app_id == app_id and d.allowed]
            assert mine, f"{name}({app_id}) with no prior guard decision"
            ctrl.guard_log.remove(mine[0])  # one decision per mutation
            return real(app_id, *args, **kwargs)
        return call

    world.provider.update_app = checked("update_app")
    world.provider.destroy_app = checked("destroy_app")


def test_the_watch_catches_an_unguarded_mutation(world):
    dep = run_all(world.ctrl, payload())
    watch_provider(world)
    world.ctrl.guard_log.clear()
    with pytest.raises(AssertionError, match="no prior guard decision"):
        world.provider.destroy_app(dep["app_id"])


def test_every_public_entry_point_is_classified_and_guards_before_it_mutates(world):
    world.ctrl = controller(world, max_creates_per_hour=50, max_actions_per_day=100,
                            max_concurrent=50)
    watch_provider(world)
    nonmutating = {"submit", "approve", "get", "get_approval", "logs"}
    guarded = {"execute", "destroy", "reconcile"}  # exercised below
    public = {n for n, _ in inspect.getmembers(DeploymentController, inspect.isfunction)
              if not n.startswith("_")}
    assert public == nonmutating | guarded, "classify the new entry point (and guard it)"
    ctrl, provider = world.ctrl, world.provider
    for keep in (True, False):  # create + bind, then stop and delete
        dep = run_all(ctrl, payload(10 + keep))
        ctrl.destroy(dep["deployment_id"], caller="orbit", request_id="r", keep_record=keep)
    _, rep = replacement(world, 20)  # replace
    assert execute(world, rep)["status"] == "running"
    run_all(ctrl, payload(30, ttl_minutes=1))  # sweep destroy
    run_all(ctrl, payload(31))  # adopt
    world.clock.now += 120
    assert "destroyed" in [r["action"] for r in ctrl.reconcile()]
    world.clock.now = T0
    provider.fail_next("update", "before")  # bind interrupted: claim-only, recovered by reconcile
    stuck = run_all(ctrl, payload(40))
    assert stuck["status"] == "created_unbound"
    assert "bound" in [r["action"] for r in ctrl.reconcile()]
    assert {a for a in ACTIONS} >= {"stop", "delete", "sweep_destroy", "replace", "bind", "adopt",
                                    "recover_create", "recover_replace"}
    used = {d.action for d in ctrl.guard_log} | {"stop", "delete", "replace", "bind"}
    assert used <= set(ACTIONS)
