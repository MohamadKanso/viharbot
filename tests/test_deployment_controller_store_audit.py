"""Record store durability and locking, audit event shape, controller construction (part 1)."""

from __future__ import annotations

import json
import logging
import os

import pytest
from deployment_controller_helpers import (
    DIGEST, KEY, execute, make_world, payload, reason_of, run_all, submit, submit_approved,
)

from viharbot.deployment_controller import markers
from viharbot.deployment_controller.audit import AuditLog
from viharbot.deployment_controller.provider import FakeProvider
from viharbot.deployment_controller.store import RecordStore, StoreError


@pytest.fixture
def world(tmp_path):
    return make_world(tmp_path)

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


# --- audit ---
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

def test_store_lock_failure_is_a_store_error(world, monkeypatch):
    monkeypatch.setattr("fcntl.flock", lambda *a: (_ for _ in ()).throw(OSError("no lock")))
    with pytest.raises(StoreError):
        world.ctrl.store.all("approvals")
    monkeypatch.undo()
    assert world.ctrl.store.all("approvals") == []

