"""Authenticated end-to-end through the FastAPI TestClient with the FakeProvider. No network.

Authentication is the existing service_auth dependency with throwaway Ed25519 keys.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from deployment_controller_helpers import make_world, payload, run_all
from fastapi.testclient import TestClient

from viharbot import service_auth as sa
from viharbot.deployment_controller.api import (
    APPROVE_CAPABILITY, AUDIENCE, REQUEST_CAPABILITY, create_app,
)

IDENTITIES = {  # kid -> (sub, capabilities)
    "orbit-1": ("orbit", [REQUEST_CAPABILITY]),
    "approver-1": ("approver", [APPROVE_CAPABILITY]),
    "web-1": ("web", [REQUEST_CAPABILITY]),  # authenticates, but is not a requester in policy
    "approver-2": ("intruder", [APPROVE_CAPABILITY]),
}


def b64(key, private):
    fmt = serialization.PrivateFormat.Raw if private else serialization.PublicFormat.Raw
    enc = serialization.Encoding.Raw
    if private:
        return sa.b64url_encode(key.private_bytes(enc, fmt, serialization.NoEncryption()))
    return sa.b64url_encode(key.public_key().public_bytes(enc, fmt))


@pytest.fixture
def api(tmp_path):
    world = make_world(tmp_path)
    keys, blocks = {}, []
    for kid, (sub, caps) in IDENTITIES.items():
        key = Ed25519PrivateKey.generate()
        seed = tmp_path / f"{kid}.key"
        seed.write_text(b64(key, True))
        keys[kid] = str(seed)
        listed = ", ".join(f'"{c}"' for c in caps)
        blocks.append(f'[[keys]]\nkid="{kid}"\nsub="{sub}"\npublic_key="{b64(key, False)}"\n'
                      f"capabilities=[{listed}]\n")
    registry_path = tmp_path / "registry.toml"
    registry_path.write_text("\n".join(blocks))
    registry = sa.KeyRegistry(registry_path, world.clock)
    registry.load_initial()
    auth = sa.ServiceAuthenticator(audience=AUDIENCE, registry=registry, clock=world.clock)
    app = create_app(world.ctrl, auth)
    return SimpleNamespace(world=world, keys=keys, app=app, client=TestClient(app))


def call(api, who, method, target, body=None, capability=None, **mint):
    kid = {"orbit": "orbit-1", "approver": "approver-1", "web": "web-1",
           "intruder": "approver-2"}[who]
    raw = json.dumps(body).encode() if body is not None else b""
    token = sa.mint_assertion(
        kid=kid, sub=IDENTITIES[kid][0], audience=AUDIENCE,
        capability=capability or IDENTITIES[kid][1][0], engagement="e", classification="internal",
        method=method, target=target, body=raw, key_path=api.keys[kid], now=api.world.clock.now,
        **mint,
    )
    headers = {sa.ASSERTION_HEADER: token, "content-type": "application/json"}
    return api.client.request(method, target, content=raw, headers=headers), token


def test_full_flow_create_review_approve_logs_replace_stop_delete(api, caplog):
    caplog.set_level(logging.INFO)
    body = payload(env={"LOG_LEVEL": "sentinel-env-value"})
    created, orbit_token = call(api, "orbit", "POST", "/v1/deployments", body)
    assert created.status_code == 200 and created.json()["status"] == "pending_approval"
    dep = created.json()
    assert call(api, "orbit", "GET", f"/v1/deployments/{dep['deployment_id']}")[0].json()[
        "status"] == "pending_approval"

    review, _ = call(api, "approver", "GET", f"/v1/approvals/{dep['approval_id']}")
    assert review.json()["request"]["env"] == {"LOG_LEVEL": "sentinel-env-value"}
    approve_target = f"/v1/approvals/{dep['approval_id']}:approve"
    done, _ = call(api, "approver", "POST", approve_target, {"request_hash": dep["request_hash"]})
    assert done.status_code == 200 and done.json()["status"] == "running"
    app_id = done.json()["app_id"]
    api.world.provider.logs[app_id] = "\n".join(f"l{i}" for i in range(1000))

    logs, _ = call(api, "orbit", "GET", f"/v1/deployments/{dep['deployment_id']}/logs?lines=3")
    assert logs.json()["logs"] == "l997\nl998\nl999"

    replace = payload(request_id="rep-1", env={"LOG_LEVEL": "debug"},
                      replaces={"app_id": app_id, "expected_version": 2})
    second = call(api, "orbit", "POST", "/v1/deployments", replace)[0].json()
    call(api, "approver", "POST", f"/v1/approvals/{second['approval_id']}:approve",
         {"request_hash": second["request_hash"]})
    assert api.world.provider.apps[app_id].env["LOG_LEVEL"] == "debug"

    stopped, _ = call(api, "orbit", "POST", f"/v1/deployments/{dep['deployment_id']}:stop")
    assert stopped.json()["status"] == "stopped" and app_id not in api.world.provider.apps
    deleted, _ = call(api, "orbit", "DELETE", f"/v1/deployments/{dep['deployment_id']}")
    assert deleted.status_code == 200  # already stopped: the record is removed
    assert api.client.get("/health").json()["status"] == "ok"

    everything = caplog.text + json.dumps(api.world.ctrl.audit.events)
    assert orbit_token not in everything and "sentinel-env-value" not in everything


def test_delete_removes_the_record(api):
    dep = run_all(api.world.ctrl, payload())
    response, _ = call(api, "orbit", "DELETE", f"/v1/deployments/{dep['deployment_id']}")
    assert response.status_code == 200
    assert call(api, "orbit", "GET", f"/v1/deployments/{dep['deployment_id']}")[0].status_code == 404


def test_authentication_and_identity_rules(api):
    assert api.client.post("/v1/deployments", json=payload()).status_code == 401
    # the existing dependency refuses a capability the route does not accept
    wrong, _ = call(api, "approver", "POST", "/v1/deployments", payload(),
                    capability=APPROVE_CAPABILITY)
    assert wrong.status_code == 403
    # a registry-valid caller that policy does not name as the requester
    assert call(api, "web", "POST", "/v1/deployments", payload())[0].status_code == 403
    assert call(api, "web", "GET", "/v1/deployments/dep_x")[0].status_code == 403
    # an approver identity cannot request, and an unlisted approver cannot approve
    assert call(api, "approver", "POST", "/v1/deployments", payload(),
                capability=REQUEST_CAPABILITY)[0].status_code == 403
    dep = call(api, "orbit", "POST", "/v1/deployments", payload())[0].json()
    target = f"/v1/approvals/{dep['approval_id']}:approve"
    body = {"request_hash": dep["request_hash"]}
    assert call(api, "intruder", "POST", target, body)[0].status_code == 403
    assert call(api, "intruder", "GET", f"/v1/approvals/{dep['approval_id']}")[0].status_code == 403
    # Orbit cannot approve its own request: its registry entry has no approve capability
    assert call(api, "orbit", "POST", target, body, capability=APPROVE_CAPABILITY)[0].status_code == 403
    assert not api.world.provider.apps


def test_policy_denials_and_state_errors_map_to_http_statuses(api):
    bad, _ = call(api, "orbit", "POST", "/v1/deployments", payload(command="sh"))
    assert bad.status_code == 422 and bad.json()["detail"] == "invalid_request"
    dep = call(api, "orbit", "POST", "/v1/deployments", payload())[0].json()
    target = f"/v1/approvals/{dep['approval_id']}:approve"
    assert call(api, "approver", "POST", target, {"request_hash": "0" * 64})[0].status_code == 409
    assert call(api, "approver", "POST", target, {"request_hash": "wrong"})[0].status_code == 422
    assert call(api, "approver", "POST", target, {})[0].status_code == 422
    assert call(api, "approver", "POST", "/v1/approvals/apr_none:approve",
                {"request_hash": "x"})[0].status_code == 404
    assert call(api, "approver", "GET", "/v1/approvals/apr_none")[0].status_code == 404
    ok = call(api, "approver", "POST", target, {"request_hash": dep["request_hash"]})[0]
    assert ok.status_code == 200
    replay = call(api, "approver", "POST", target, {"request_hash": dep["request_hash"]})[0]
    assert replay.status_code == 409  # a consumed approval cannot be replayed
    assert call(api, "orbit", "GET", "/v1/deployments/dep_none")[0].status_code == 404


def test_lifespan_runs_the_startup_reconciliation(api):
    dep = run_all(api.world.ctrl, payload(ttl_minutes=1))
    api.world.clock.now += 120
    with TestClient(api.app):
        pass
    assert dep["app_id"] not in api.world.provider.apps


def test_startup_reconciliation_failure_raises_an_alert_and_the_service_still_starts(api):
    api.world.provider.fail_next("list", "before")
    with TestClient(api.app) as client:
        assert client.get("/health").status_code == 200
    assert "startup_reconcile_failed" in [a["reason"] for a in api.world.ctrl.alerts]


def test_operation_lookup_failure_during_immediate_recovery_returns_a_normal_response(api):
    created = call(api, "orbit", "POST", "/v1/deployments", payload())[0].json()
    api.world.provider.fail_next("create", "after")
    api.world.provider.fail_next("find", "before")
    response, _ = call(api, "approver", "POST", f"/v1/approvals/{created['approval_id']}:approve",
                       {"request_hash": created["request_hash"]})
    assert response.status_code == 200
    assert response.json()["status"] == "failed_uncertain"
    assert response.json()["reason"] == "reconcile_unavailable"
    assert "create_outcome_unknown:reconcile_unavailable" in [
        a["reason"] for a in api.world.ctrl.alerts]
    assert api.world.provider.calls.count("create") == 1


def test_a_malformed_request_hash_is_a_clean_422_and_is_audited(api, caplog):
    caplog.set_level(logging.INFO)
    dep = call(api, "orbit", "POST", "/v1/deployments", payload())[0].json()
    target = f"/v1/approvals/{dep['approval_id']}:approve"
    good = dep["request_hash"]
    for bad in ("\u00e9" * 64, good.upper(), good[:-1], good + "0", 7, None, [good]):
        got = call(api, "approver", "POST", target, {"request_hash": bad})[0]
        assert got.status_code == 422 and got.json()["detail"] == "invalid_request_hash", bad
        denied = api.world.ctrl.audit.events[-1]
        assert (denied["action"], denied["reason"]) == ("approve", "invalid_request_hash")
    assert not api.world.provider.apps
    logged = caplog.text + json.dumps(api.world.ctrl.audit.events, ensure_ascii=False)
    assert "\u00e9" * 64 not in logged and good.upper() not in logged  # never the input
    ok = call(api, "approver", "POST", target, {"request_hash": good})[0]
    assert ok.status_code == 200 and ok.json()["status"] == "running"
