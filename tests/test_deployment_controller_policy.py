"""Deployment controller policy: allowlist denials, submit rules, spec template, request hash."""

from __future__ import annotations

import pytest
from deployment_controller_helpers import (
    DIGEST, POLICY, make_world, payload, reason_of, submit,
)

from viharbot.deployment_controller.policy import DeploymentRequest, build_spec, request_hash


@pytest.fixture
def world(tmp_path):
    return make_world(tmp_path)

# --- policy: every denial ---
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


