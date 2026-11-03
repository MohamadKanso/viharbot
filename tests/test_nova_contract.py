"""C2a: Nova envelope, instance identity, session id and the not-accepted marker (offline)."""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel

from deadline_helpers import deadline_with, install_agentcore
from viharbot import agentcore_runtime, research_agent
from viharbot.agentcore_client import invoke_agentcore
from viharbot.nova_contract import nova_log_fields
from viharbot.observability import JsonFormatter
from viharbot.schemas import ResearchFindings

ALIAS = "svc/test-alias"
FINDINGS = {"summary": "s", "findings": [], "sources": [], "uncertainties": []}


@pytest.fixture
def nova(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMORY_DIR", str(tmp_path))
    monkeypatch.setenv("RESEARCH_MODEL", ALIAS)
    monkeypatch.delenv("NOVA_INSTANCE_ID", raising=False)
    monkeypatch.delenv("NOVA_RUNTIME", raising=False)
    built = []

    def build_agent(*, model_alias, system_prompt):
        built.append(model_alias)
        return lambda prompt, *, structured_output_model: SimpleNamespace(
            structured_output=ResearchFindings.model_validate(FINDINGS))

    monkeypatch.setattr(research_agent, "build_agent", build_agent)
    return TestClient(research_agent.app), built


def invoke(client, **envelope):
    return client.post("/invocations", json={"input": {"question": "q"}, **envelope})


# 1. session id
@pytest.mark.parametrize("request_id", ["a", "req-1", "x" * 128])
def test_runtime_session_id_is_valid_for_any_request_id_and_request_id_stays_in_payload(
        monkeypatch, request_id):
    fake = install_agentcore(monkeypatch)
    deadline, _ = deadline_with(30.0)
    invoke_agentcore("arn:x", {"question": "q"}, request_id, deadline=deadline)
    invoke_agentcore("arn:x", {"question": "q"}, request_id, deadline=deadline)
    first, second = (i["runtimeSessionId"] for i in fake.invocations)
    assert len(first) >= 33 and first.startswith("nova-") and first != second
    assert json.loads(fake.invocations[0]["payload"])["request_id"] == request_id


# 2. envelope
def test_bare_invocation_still_works_and_is_stamped_with_defaults(nova):
    client, built = nova
    result = invoke(client, request_id="r-1")
    body = result.json()
    assert result.status_code == 200 and built == [ALIAS]
    assert body["request_id"] == "r-1" and body["findings"]["summary"] == "s"
    assert body["runtime_instance"] == "nova-local-01" and body["runtime_kind"] == "local"
    assert body["attempt"] == 1 and body["duration_ms"] >= 0
    assert result.headers["X-Nova-Instance"] == "nova-local-01"
    assert "X-Nova-Not-Accepted" not in result.headers


def test_full_envelope_is_accepted_and_carried_to_logs(nova, monkeypatch, caplog):
    client, _ = nova
    monkeypatch.setenv("NOVA_INSTANCE_ID", "nova-agentcore-01")
    monkeypatch.setenv("NOVA_RUNTIME", "agentcore")
    envelope = dict(
        request_id="r-2", logical_agent="nova", contract_version="1", attempt=2,
        deadline_ms=5000, attempt_max_output_tokens=1000, attempt_max_cost_usd=100000,
        total_max_output_tokens=2000, total_max_cost_usd=200000,
        idempotency_key="k-1", operation_class="idempotent")
    with caplog.at_level(logging.INFO, logger="research"):
        result = invoke(client, **envelope)
    assert result.status_code == 200
    assert result.json()["attempt"] == 2
    assert result.json()["runtime_instance"] == "nova-agentcore-01"
    record = next(r for r in caplog.records if r.getMessage() == "research completed")
    logged = json.loads(JsonFormatter().format(record))
    assert logged["logical_agent"] == "nova" and logged["attempt"] == 2
    assert logged["runtime_instance"] == "nova-agentcore-01"
    assert logged["runtime_kind"] == "agentcore"
    assert logged["operation_class"] == "idempotent" and logged["total_max_cost_usd"] == 200000
    assert nova_log_fields() == {}  # the context does not leak past the invocation


@pytest.mark.parametrize("bad", [
    {"logical_agent": "other"}, {"logical_agent": "Nova!"}, {"contract_version": "2"},
    {"attempt": 0}, {"attempt": 3}, {"attempt": True}, {"attempt": "1"},
    {"deadline_ms": 0}, {"deadline_ms": 900001}, {"deadline_ms": 1.5},
    {"attempt_max_output_tokens": -1}, {"attempt_max_output_tokens": 1000001},
    {"attempt_max_cost_usd": 1.5}, {"total_max_cost_usd": 100000001},
    {"total_max_output_tokens": "5"}, {"idempotency_key": ""},
    {"idempotency_key": "bad key"}, {"operation_class": "stateful"},
    {"operation_class": "side-effecting"},
])
def test_invalid_envelope_values_are_refused_before_any_work(nova, bad):
    client, built = nova
    result = invoke(client, **bad)
    assert result.status_code == 422 and built == []
    assert result.headers["X-Nova-Not-Accepted"] == "1"


# 3. identity
@pytest.mark.parametrize("name,value", [
    ("NOVA_RUNTIME", "lambda"), ("NOVA_INSTANCE_ID", "bad id!")])
def test_malformed_identity_is_a_marked_startup_refusal(nova, monkeypatch, name, value):
    client, built = nova
    monkeypatch.setenv(name, value)
    result = invoke(client)
    assert result.status_code == 503 and built == []
    assert result.headers["X-Nova-Not-Accepted"] == "1"


# 4. the not-accepted marker: both sides of the rule
def test_payload_validation_failure_is_marked_not_accepted(nova):
    client, built = nova
    result = client.post("/invocations", json={"input": {"question": ""}})
    assert result.status_code == 422 and built == []
    assert result.headers["X-Nova-Not-Accepted"] == "1"
    assert result.headers["X-Nova-Instance"] == "nova-local-01"


def test_failure_after_work_started_is_never_marked(nova, monkeypatch):
    client, built = nova

    def failing(*, model_alias, system_prompt):
        def agent(prompt, *, structured_output_model):
            raise RuntimeError("boom")
        return agent

    monkeypatch.setattr(research_agent, "build_agent", failing)
    result = invoke(client, request_id="r-3", attempt=1)
    assert result.status_code == 502
    assert "X-Nova-Not-Accepted" not in result.headers


def test_auth_refusal_is_marked_and_the_handler_does_not_run():
    class Body(BaseModel):
        q: str

    def deny() -> None:
        raise HTTPException(status_code=401, detail="no", headers={"WWW-Authenticate": "x"})

    async def adeny() -> None:
        raise HTTPException(status_code=403, detail="no")

    calls = []
    for dependency, status in ((deny, 401), (adeny, 403)):
        app = FastAPI()
        agentcore_runtime.add_agentcore_routes(
            app, Body, lambda *a: calls.append(a), dependencies=[Depends(dependency)],
            logical_agent="nova")
        result = TestClient(app).post("/invocations", json={"input": {"q": "x"}})
        assert result.status_code == status
        assert result.headers["X-Nova-Not-Accepted"] == "1"
    assert calls == []


def test_allowed_auth_dependency_and_non_nova_adapters_are_unchanged():
    class Body(BaseModel):
        q: str

    def allow() -> None:
        return None

    app = FastAPI()
    agentcore_runtime.add_agentcore_routes(
        app, Body, lambda b, r, rid: {"ok": b.q}, dependencies=[Depends(allow)],
        logical_agent="nova")
    assert TestClient(app).post("/invocations", json={"input": {"q": "x"}}).json()["ok"] == "x"

    plain = FastAPI()  # no logical_agent: no envelope, no identity, no marker
    agentcore_runtime.add_agentcore_routes(plain, Body, lambda b, r, rid: {"ok": b.q})
    result = TestClient(plain).post("/invocations", json={"input": {"q": ""}, "attempt": 99})
    assert result.status_code == 200 and result.json() == {"ok": ""}
    assert "X-Nova-Instance" not in result.headers
    bad = TestClient(plain).post("/invocations", json={"input": {}})
    assert bad.status_code == 422 and "X-Nova-Not-Accepted" not in bad.headers


def test_non_object_result_is_passed_through_with_the_instance_header():
    class Body(BaseModel):
        q: str

    app = FastAPI()
    agentcore_runtime.add_agentcore_routes(
        app, Body, lambda b, r, rid: [1, 2], logical_agent="nova")
    result = TestClient(app).post("/invocations", json={"input": {"q": "x"}})
    assert result.json() == [1, 2] and result.headers["X-Nova-Instance"] == "nova-local-01"


def test_existing_response_fields_are_never_overwritten():
    class Body(BaseModel):
        q: str

    app = FastAPI()
    agentcore_runtime.add_agentcore_routes(
        app, Body, lambda b, r, rid: {"attempt": 7, "duration_ms": 1.0}, logical_agent="nova")
    body = TestClient(app).post("/invocations", json={"input": {"q": "x"}}).json()
    assert body["attempt"] == 7 and body["duration_ms"] == 1.0
