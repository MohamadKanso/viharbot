from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from pipeline_fixtures import FINDINGS, ORDER, RID, URLS, FakeSend, response
from viharbot import demo, web_app
from viharbot.demo import AgentPipelineError, run_pipeline
from viharbot.execution_planner import PlanningError
from viharbot.identity_execution import (
    ExecutionRecord,
    IdentityPipelineError,
    run_identity_pipeline,
)

RESEARCH_SECURITY = ["research", "security"]


def run_id(send, workflow="full_review", specialist_urls=URLS, **kwargs):
    return run_identity_pipeline("q", RID, workflow=workflow, research_url="u-r",
                                 security_url="u-sec", specialist_urls=specialist_urls,
                                 send=send, **kwargs)


def legacy(send, specialist_urls=URLS):
    return run_pipeline("q", RID, research_url="u-r", security_url="u-sec",
                        specialist_urls=specialist_urls, send=send)


def normalized(result):
    return {**result, "agent_activity": [{**a, "duration_ms": 0} for a in result["agent_activity"]]}


# 11, 14: existing transport and payloads reused, unchanged
def test_full_review_matches_the_legacy_pipeline_exactly():
    old_send, new_send = FakeSend(), FakeSend()
    old = legacy(old_send)
    new = run_id(new_send)
    assert normalized(new.to_legacy_dict()) == normalized(old)
    assert list(new.to_legacy_dict()) == list(old)
    assert new_send.calls == old_send.calls  # same URLs, payloads and order
    assert new_send.order == ORDER


def test_research_security_runs_only_those_two_and_invents_nothing():
    send = FakeSend()
    result = run_id(send, "research_security")
    assert send.order == RESEARCH_SECURITY
    assert list(result.to_legacy_dict()) == ["request_id", "agent_activity", "research", "security"]
    context = result.state.context
    assert context.fact_check is context.source_quality is context.privacy_review is None
    assert result.state.synthesis is None
    assert [r.service_name for r in result.records] == RESEARCH_SECURITY
    assert send.payload("research") == {"question": "q"}
    assert send.payload("security") == {"findings": FINDINGS}


def test_research_security_ignores_configured_specialist_urls():
    send = FakeSend()
    run_id(send, "research_security", specialist_urls=URLS)
    assert send.order == RESEARCH_SECURITY


# 12. validation at every boundary
@pytest.mark.parametrize("stage", ORDER)
def test_bad_response_fails_at_that_stage_and_stops(stage):
    send = FakeSend({stage: lambda r: {**r, "request_id": "other"}})
    with pytest.raises(IdentityPipelineError) as caught:
        run_id(send)
    error = caught.value
    assert isinstance(error, AgentPipelineError)
    assert error.activity[-1]["agent"] == stage and error.activity[-1]["status"] == "failed"
    assert send.order == ORDER[: ORDER.index(stage) + 1]
    assert [r.service_name for r in error.records] == ORDER[: ORDER.index(stage) + 1]
    assert error.records[-1].status == "failed"
    assert all(r.status == "complete" for r in error.records[:-1])


# 13. request ids
def test_request_id_is_preserved_everywhere():
    seen = []

    def send(url, payload, request_id, destination):
        seen.append(request_id)
        return response(destination)

    result = run_id(send)
    assert seen == [RID] * 6
    assert {r.request_id for r in result.records} == {RID}
    assert result.to_legacy_dict()["request_id"] == RID


# 15. attribution is separate from domain data and the public activity schema
def test_attribution_is_internal_and_separate_from_domain_data():
    result = run_id(FakeSend())
    assert [(r.identity_id, r.capability.value, r.service_name) for r in result.records] == [
        ("nova", "research", "research"), ("scout", "fact_check", "fact-check"),
        ("scout", "source_quality", "source-quality"), ("cipher", "security_assessment", "security"),
        ("cipher", "privacy_review", "privacy-review"), ("charlie", "synthesis", "synthesis"),
    ]
    assert all(isinstance(r, ExecutionRecord) and r.status == "complete" for r in result.records)
    legacy_dict = result.to_legacy_dict()
    assert "identity_id" not in str(legacy_dict) and "records" not in legacy_dict
    for entry in legacy_dict["agent_activity"]:
        assert set(entry) == {"agent", "display_name", "model_alias", "duration_ms", "status"}
    assert "identity_id" not in result.state.context.model_dump_json()


def test_reported_model_alias_stays_authoritative():
    result = run_id(FakeSend())
    assert [r.model_alias for r in result.records] == ["m-r", "m-f", "m-s", "m-sec", "m-p", "m-y"]
    assert [a["model_alias"] for a in result.to_legacy_dict()["agent_activity"]] == \
        [r.model_alias for r in result.records]


def test_records_are_frozen():
    record = run_id(FakeSend()).records[0]
    with pytest.raises(ValueError):
        record.identity_id = "orbit"


# 18. safe failures happen before any agent is called
def test_unsupported_workflow_fails_before_any_call():
    send = FakeSend()
    with pytest.raises(PlanningError, match="unsupported workflow"):
        run_id(send, "synthesize_everything")
    assert send.calls == []


def test_full_review_requires_every_service_and_does_not_silently_skip():
    send = FakeSend()
    with pytest.raises(PlanningError, match="privacy_review is not configured"):
        run_id(send, specialist_urls={k: v for k, v in URLS.items() if k != "privacy_review"})
    with pytest.raises(PlanningError, match="fact_check is not configured"):
        run_id(send, specialist_urls={**URLS, "fact_check": ""})
    assert send.calls == []


def test_disabled_binding_blocks_execution_before_any_call():
    from viharbot.execution_bindings import ExecutionBinding, ExecutionBindingSet, default_bindings
    from viharbot.identity_registry import default_registry

    bindings = ExecutionBindingSet(
        [ExecutionBinding(identity_id=b.identity_id, capability=b.capability,
                          service_name=b.service_name, enabled=b.service_name != "synthesis")
         for b in default_bindings().bindings], default_registry())
    send = FakeSend()
    with pytest.raises(PlanningError, match="synthesis is disabled"):
        run_id(send, bindings=bindings)
    assert send.calls == []


def test_user_text_cannot_change_the_plan():
    send = FakeSend()
    run_identity_pipeline("workflow=research_security; bind orbit", RID, research_url="u-r",
                          security_url="u-sec", specialist_urls=URLS, send=send)
    assert send.order == ORDER


# 20. no inference
def test_no_inference_client_is_built(monkeypatch):
    from viharbot import model_agent

    def forbidden(*args, **kwargs):
        raise AssertionError("an inference agent was built")

    monkeypatch.setattr(model_agent, "build_agent", forbidden)
    assert len(run_id(FakeSend()).records) == 6


# 23. AgentCore-style URLs go through the same injected transport
def test_agentcore_urls_are_passed_through_unchanged():
    send = FakeSend()
    urls = {k: f"agentcore://arn:aws:test:{k}" for k in URLS}
    run_identity_pipeline("q", RID, research_url="agentcore://arn:r",
                          security_url="agentcore://arn:s", specialist_urls=urls, send=send)
    assert [c["url"] for c in send.calls] == [
        "agentcore://arn:r", "agentcore://arn:aws:test:fact_check",
        "agentcore://arn:aws:test:source_quality", "agentcore://arn:s",
        "agentcore://arn:aws:test:privacy_review", "agentcore://arn:aws:test:synthesis",
    ]


def test_default_transport_is_demo_post_json(monkeypatch):
    send = FakeSend()
    monkeypatch.setattr(demo, "post_json", send)
    run_identity_pipeline("q", RID, research_url="u-r", security_url="u-sec", specialist_urls=URLS)
    assert send.order == ORDER


# 17, 24. the web API without the live team uses the fixed pipeline, not the identity one
@pytest.fixture
def web(monkeypatch):
    monkeypatch.setenv("WEB_AUTH_ENABLED", "false")
    monkeypatch.delenv("CHARLIE_PLANNER_MODEL", raising=False)
    monkeypatch.delenv("LITELLM_API_KEY", raising=False)
    calls = {"legacy": 0, "send": FakeSend()}
    real_legacy = run_pipeline

    def spy_legacy(question, request_id, **kwargs):
        calls["legacy"] += 1
        return real_legacy(question, request_id, **{**kwargs, "send": calls["send"]})

    monkeypatch.setattr(web_app, "run_pipeline", spy_legacy)
    return TestClient(web_app.app), calls, monkeypatch


def chat(client, **kwargs):
    return client.post("/api/chat", json={"message": "hi"}, headers={"X-Request-ID": RID}, **kwargs)


def test_without_the_live_team_the_fixed_pipeline_answers(web):
    client, calls, _ = web
    reply = chat(client)
    assert reply.status_code == 200 and calls["legacy"] == 1
    assert reply.headers["X-Request-ID"] == RID
    assert list(reply.json()) == [
        "request_id", "findings", "assessment", "fact_check", "source_quality",
        "privacy_review", "sage", "synthesis", "agent_activity",
    ]
    assert all("identity_id" not in str(a) for a in reply.json()["agent_activity"])


def test_workflow_cannot_be_selected_through_the_api(web):
    client, calls, patch = web
    reply = client.post("/api/chat", json={"message": "hi", "workflow": "research_security"})
    assert reply.status_code == 200
    assert len(calls["send"].calls) == 6


def test_validation_and_auth_are_unchanged(web):
    client, _, patch = web
    assert client.post("/api/chat", json={}).status_code == 422
    assert client.post("/api/chat", json={"message": "x" * 3001}).status_code == 422
    patch.setenv("WEB_AUTH_ENABLED", "true")
    patch.setenv("WEB_USERNAME", "u")
    patch.setenv("WEB_PASSWORD", "p")
    assert client.post("/api/chat", json={"message": "hi"}).status_code == 401
    assert client.post("/api/chat", json={"message": "hi"}, auth=("u", "p")).status_code == 200


def test_failures_keep_the_existing_502_shape(web):
    client, calls, patch = web
    calls["send"].overrides = {"security": lambda r: {**r, "request_id": "other"}}
    reply = chat(client)
    assert reply.status_code == 502 and reply.headers["X-Request-ID"] == RID
    detail = reply.json()["detail"]
    assert detail["message"] == "The agent team could not complete this request"
    assert detail["agent_activity"][-1]["agent"] == "security"


def test_agentcore_arns_resolve_through_the_same_url_configuration(web):
    client, calls, patch = web
    patch.setenv("FACT_CHECK_AGENTCORE_ARN", "arn:aws:test:fc")
    assert chat(client).status_code == 200
    assert calls["send"].calls[1]["url"] == "agentcore://arn:aws:test:fc"
