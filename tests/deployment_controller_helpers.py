"""Shared builders for the deployment controller tests. Offline: FakeProvider, throwaway keys."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from viharbot.deployment_controller.audit import AuditLog
from viharbot.deployment_controller.controller import DeploymentController
from viharbot.deployment_controller.policy import Policy, PolicyDenied
from viharbot.deployment_controller.provider import FakeProvider
from viharbot.deployment_controller.store import RecordStore

T0 = 1_800_000_000.0
KEY = b"throwaway-marker-key-for-tests-only"
POLICY = Policy()
DIGEST = "sha256:" + "a" * 64


class Clock:
    def __init__(self, now: float = T0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class Crash(BaseException):
    """Simulates the process dying mid-operation (not an Exception, so nothing catches it)."""


def payload(n: int = 1, **overrides) -> dict:
    body = {
        "request_id": f"req-{n}", "name": f"orbit-demo-{n}",
        "image": {"registry": POLICY.registry, "repository": "orbit/demo", "digest": DIGEST},
        "size": POLICY.size, "region": POLICY.region, "port": 8080,
        "env": {"LOG_LEVEL": "info", "APP_MODE": "demo"}, "ttl_minutes": 60,
    }
    body.update(overrides)
    return body


def controller(world, store=None, **policy_kwargs) -> DeploymentController:
    return DeploymentController(
        provider=world.provider, store=store or RecordStore(world.path),
        policy=Policy(**policy_kwargs) if policy_kwargs else POLICY, marker_key=KEY,
        audit=AuditLog(), clock=world.clock,
    )


def make_world(tmp_path, **provider_kwargs) -> SimpleNamespace:
    world = SimpleNamespace(
        clock=Clock(), provider=FakeProvider(**provider_kwargs), path=tmp_path / "store.json"
    )
    world.ctrl = controller(world)
    return world


def approve(ctrl, dep, approver="approver"):
    return ctrl.approve(
        dep["approval_id"], approver=approver, reviewed_hash=dep["request_hash"], request_id="r"
    )


def run_all(ctrl, body, requester="orbit") -> dict:
    dep = ctrl.submit(body, caller=requester, request_id="r")
    approve(ctrl, dep)
    return ctrl.execute(dep["approval_id"], caller="approver", request_id="r")


def submit_approved(ctrl, body) -> dict:
    dep = ctrl.submit(body, caller="orbit", request_id="r")
    approve(ctrl, dep)
    return dep



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
