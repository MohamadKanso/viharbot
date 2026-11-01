"""The Sage service: routing through LiteLLM, validation, failure handling and logging. Offline."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from viharbot import model_agent, sage_agent
from viharbot.sage import build_sage_prompt, sage_system_prompt, SageRequest
from viharbot.schemas import (
    SageAnalysis,
    SageEthicalConsideration,
    SageHumanImpact,
    SageMitigation,
    SageStakeholder,
    SageTradeOff,
)

RID = "req-sage-1"
KEY = "SECRET-LITELLM-KEY-123"


def claim(**kwargs):
    return {"statement": "A statement.", "claim_type": "reasoned_interpretation", **kwargs}


def valid_analysis(**overrides) -> SageAnalysis:
    base = dict(
        executive_summary="Summary.",
        stakeholders=[SageStakeholder(name="Staff", interest="Fairness", potential_impact="Monitored",
                                      impact_direction="mixed")],
        ethical_considerations=[SageEthicalConsideration(**claim())],
        human_and_organizational_impacts=[SageHumanImpact(**claim(), affected_group="Staff")],
        risks_and_tradeoffs=[SageTradeOff(**claim(), competing_values=["safety", "privacy"])],
        recommended_mitigations=[SageMitigation(action="Publish a policy", addresses="Trust")],
        assumptions_and_uncertainties=["Unknown staff attitudes."],
        recommendations_for_charlie=["Weigh proportionality."],
    )
    return SageAnalysis(**{**base, **overrides})


class FakeAgent:
    """Stands in for a Strands agent: records the call and returns a scripted outcome."""

    def __init__(self, result=None, error=None, usage=None):
        self.result, self.error, self.usage = result, error, usage
        self.calls = []

    def __call__(self, prompt, structured_output_model=None):
        self.calls.append((prompt, structured_output_model))
        if self.error:
            raise self.error
        metrics = SimpleNamespace(accumulated_usage=self.usage) if self.usage is not None else None
        return SimpleNamespace(structured_output=self.result, metrics=metrics)


@pytest.fixture
def service(monkeypatch, tmp_path):
    """The app with every environment value pinned and build_agent replaced by a recorder."""
    monkeypatch.setenv("MEMORY_DIR", str(tmp_path))
    monkeypatch.setenv("SAGE_MODEL", "test/sage-alias")
    monkeypatch.delenv("SAGE_MODEL_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setenv("LITELLM_API_KEY", KEY)
    state = SimpleNamespace(agent=FakeAgent(result=valid_analysis()), builds=[], memory=tmp_path)

    def build(**kwargs):
        state.builds.append(kwargs)
        return state.agent

    monkeypatch.setattr(sage_agent, "build_agent", build)
    state.client = TestClient(sage_agent.app, raise_server_exceptions=False)
    return state


def post(service, payload=None, **kwargs):
    payload = {"question": "Should we deploy autonomous agents?"} if payload is None else payload
    return service.client.post("/process", json=payload, headers={"X-Request-ID": RID}, **kwargs)


# ---- basic routes -----------------------------------------------------------------------------
def test_health_and_model_routes(service, monkeypatch):
    assert service.client.get("/health").json() == {"status": "ok", "agent": "sage"}
    assert service.client.get("/model").json() == {"agent": "sage", "model_alias": "test/sage-alias"}
    monkeypatch.delenv("SAGE_MODEL")
    assert service.client.get("/model").json() == {"agent": "sage", "model_alias": None}


# ---- success: routing, prompt, envelope, memory -------------------------------------------------
def test_a_request_returns_a_validated_analysis_in_the_specialist_envelope(service):
    reply = post(service, {"question": "Should we deploy?", "context": {"research": {"summary": "s"}}})
    assert reply.status_code == 200 and reply.headers["X-Request-ID"] == RID
    body = reply.json()
    assert (body["request_id"], body["agent"], body["requested_model_alias"]) == (RID, "sage", "test/sage-alias")
    assert SageAnalysis.model_validate(body["result"]) == valid_analysis()


def test_the_model_is_called_through_build_agent_with_sages_identity_prompt_and_bounded_settings(service):
    post(service, {"question": "Should we deploy?", "context": {"research": {"summary": "s"}}})
    (build,) = service.builds
    assert build["model_alias"] == "test/sage-alias"
    assert build["system_prompt"] == sage_system_prompt()
    assert build["timeout"] == 80.0 and build["max_retries"] == 0  # no multiplied retries
    (prompt, output_model), = service.agent.calls
    assert output_model is SageAnalysis
    assert prompt == build_sage_prompt(SageRequest(question="Should we deploy?",
                                                   context={"research": {"summary": "s"}}))


def test_the_request_really_reaches_litellm_through_the_existing_client_settings(monkeypatch, service):
    """No stub for build_agent here: it runs for real, with the Strands classes replaced, to show
    the gateway settings (proxy mode, base URL, key, timeout, retries) that would be used."""
    seen = {}

    class FakeModel:
        def __init__(self, model_id, client_args):
            seen.update(model_id=model_id, client_args=client_args)

    class FakeStrandsAgent:
        def __init__(self, model, system_prompt, callback_handler):
            seen.update(system_prompt=system_prompt)

        def __call__(self, prompt, structured_output_model=None):
            return SimpleNamespace(structured_output=valid_analysis(), metrics=None)

    monkeypatch.setattr(sage_agent, "build_agent", model_agent.build_agent)
    monkeypatch.setattr(model_agent, "LiteLLMModel", FakeModel)
    monkeypatch.setattr(model_agent, "Agent", FakeStrandsAgent)
    monkeypatch.setenv("LITELLM_BASE_URL", "https://litellm.example.test")
    assert post(service).status_code == 200
    assert seen["model_id"] == "test/sage-alias" and seen["system_prompt"] == sage_system_prompt()
    args = seen["client_args"]
    assert args["use_litellm_proxy"] is True and args["api_key"] == KEY
    assert args["api_base"] == "https://litellm.example.test/v1"
    assert args["timeout"] == 80.0 and args["max_retries"] == 0


def test_the_result_is_written_to_sages_own_memory_directory(service):
    post(service)
    lines = (service.memory / "sage" / "memory.jsonl").read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[0])
    assert record["request_id"] == RID and record["result"]["executive_summary"] == "Summary."


def test_a_missing_request_id_is_generated(service):
    reply = service.client.post("/process", json={"question": "q"})
    assert reply.status_code == 200 and reply.headers["X-Request-ID"]
    assert reply.json()["request_id"] == reply.headers["X-Request-ID"]


# ---- the guard runs in the service -------------------------------------------------------------
def test_a_claim_citing_context_that_was_not_supplied_is_downgraded_in_the_response(service):
    service.agent.result = valid_analysis(ethical_considerations=[
        SageEthicalConsideration(**claim(claim_type="evidence_supported", evidence_refs=["security"]))])
    body = post(service, {"question": "q", "context": {"research": {"summary": "s"}}}).json()
    only = body["result"]["ethical_considerations"][0]
    assert only["claim_type"] == "reasoned_interpretation" and only["evidence_refs"] == []
    assert "downgraded" in body["result"]["assumptions_and_uncertainties"][0]


# ---- only the authorized context is accepted ---------------------------------------------------
@pytest.mark.parametrize("payload", [
    {},  # no question
    {"question": " "},
    {"question": "q", "context": {"synthesis": {"answer": "a"}}},
    {"question": "q", "extra": 1},
    {"question": "q", "context": {"research": {"blob": "x" * 20_001}}},
])
def test_requests_outside_the_contract_are_refused_before_any_model_call(service, payload):
    assert post(service, payload).status_code == 422
    assert service.builds == []


# ---- the model timeout -------------------------------------------------------------------------
@pytest.mark.parametrize("value,expected", [
    (None, 80.0), ("30", 30.0), ("999", 180.0), ("1", 5.0), ("-4", 5.0),
    ("abc", 80.0), ("nan", 80.0), ("inf", 180.0), ("", 80.0),
])
def test_the_model_timeout_is_clamped_and_falls_back_on_bad_values(monkeypatch, value, expected):
    monkeypatch.delenv("SAGE_MODEL_TIMEOUT_SECONDS", raising=False)
    if value is not None:
        monkeypatch.setenv("SAGE_MODEL_TIMEOUT_SECONDS", value)
    assert sage_agent.model_timeout() == expected


def test_the_configured_timeout_reaches_the_model_call(service, monkeypatch):
    monkeypatch.setenv("SAGE_MODEL_TIMEOUT_SECONDS", "42")
    post(service)
    assert service.builds[0]["timeout"] == 42.0


# ---- failures are sanitized ---------------------------------------------------------------------
def assert_sanitized_502(reply, secret):
    assert reply.status_code == 502
    assert reply.json() == {"detail": "sage processing failed"}
    assert reply.headers["X-Request-ID"] == RID
    assert secret not in reply.text


def test_a_model_failure_is_a_sanitized_502_that_leaks_nothing(service, caplog):
    service.agent.error = RuntimeError("provider said: secret detail about litellm.example")
    with caplog.at_level(logging.INFO):
        reply = post(service, {"question": "private question text"})
    assert_sanitized_502(reply, "secret detail")
    failed = [r for r in caplog.records if r.getMessage() == "sage failed"]
    assert len(failed) == 1 and failed[0].error == "RuntimeError"
    everything = " ".join(r.getMessage() + str(getattr(r, "error", "")) for r in caplog.records)
    assert "secret detail" not in everything and "private question text" not in everything
    assert not (service.memory / "sage").exists()  # nothing is remembered for a failed run


@pytest.mark.parametrize("bad_result", [None, {"executive_summary": "a dict, not a model"}, "text"])
def test_a_result_that_is_not_a_sage_analysis_is_a_502(service, bad_result):
    service.agent.result = bad_result
    assert_sanitized_502(post(service), "executive_summary")


def test_a_missing_model_alias_fails_closed_with_a_502_and_no_model_call(service, monkeypatch):
    monkeypatch.delenv("SAGE_MODEL")
    monkeypatch.setattr(sage_agent, "build_agent", model_agent.build_agent)  # the real check
    assert_sanitized_502(post(service), "alias")


def test_a_timeout_from_the_model_client_is_a_502(service):
    service.agent.error = TimeoutError("read timed out after 80 s")
    assert_sanitized_502(post(service), "timed out")


# ---- observability -----------------------------------------------------------------------------
def test_a_successful_run_logs_alias_duration_and_tokens_only_when_reported(service, caplog):
    service.agent.usage = {"inputTokens": 1200, "outputTokens": 340, "totalTokens": 1540}
    with caplog.at_level(logging.INFO):
        post(service, {"question": "private question text"})
    (record,) = [r for r in caplog.records if r.getMessage() == "sage completed"]
    assert record.request_id == RID and record.model_alias == "test/sage-alias"
    assert record.duration_ms >= 0 and (record.input_tokens, record.output_tokens) == (1200, 340)
    assert "private question text" not in " ".join(r.getMessage() for r in caplog.records)


def test_token_counts_are_not_invented_when_the_client_reports_none(service, caplog):
    service.agent.usage = None
    with caplog.at_level(logging.INFO):
        post(service)
    (record,) = [r for r in caplog.records if r.getMessage() == "sage completed"]
    assert not hasattr(record, "input_tokens") and not hasattr(record, "output_tokens")


def test_the_api_key_never_appears_in_a_response_or_a_log(service, caplog):
    with caplog.at_level(logging.DEBUG):
        reply = post(service)
    service.agent.error = RuntimeError("boom")
    with caplog.at_level(logging.DEBUG):
        failure = post(service)
    assert KEY not in reply.text and KEY not in failure.text
    assert KEY not in " ".join(r.getMessage() + str(getattr(r, "error", "")) for r in caplog.records)


# ---- packaging ---------------------------------------------------------------------------------
def test_the_service_is_packaged_like_the_other_agents():
    root = Path(__file__).resolve().parent.parent
    dockerfile = (root / "Dockerfile.sage").read_text(encoding="utf-8")
    assert "viharbot.sage_agent:app" in dockerfile and "USER 1001:1001" in dockerfile
    compose = (root / "compose.yaml").read_text(encoding="utf-8")
    block = compose[compose.index("\n  sage:\n"):compose.index("\n  orchestrator:")]
    assert "dockerfile: Dockerfile.sage" in block and "healthcheck:" in block
    assert "ports:" not in block  # never published to the host
    assert "SAGE_MODEL: ${SAGE_MODEL:-gpt-6.1-sol}" in block
    assert "LITELLM_API_KEY: ${LITELLM_API_KEY:-}" in block
