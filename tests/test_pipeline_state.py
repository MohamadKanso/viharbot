from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from pipeline_fixtures import FINDINGS, ORDER, RID, URLS, FakeSend, response
from viharbot import demo, web_app
from viharbot.demo import AgentPipelineError, run_pipeline, run_typed_pipeline
from viharbot.pipeline_state import PipelineState
from viharbot.schemas import (
    FactCheckReview,
    FinalSynthesis,
    PrivacyAssessment,
    ResearchFindings,
    SecurityAssessment,
    SourceQualityReview,
)


def run(send, specialist_urls=URLS):
    return run_pipeline("q", RID, research_url="u-r", security_url="u-sec",
                        specialist_urls=specialist_urls, send=send)


def run_typed(send, specialist_urls=URLS):
    return run_typed_pipeline("q", RID, research_url="u-r", security_url="u-sec",
                              specialist_urls=specialist_urls, send=send)


# A. successful execution
def test_full_run_state_is_typed_and_ordered():
    send = FakeSend()
    state = run_typed(send)
    assert isinstance(state, PipelineState)
    assert send.order == ORDER
    ctx = state.context
    assert isinstance(ctx.research, ResearchFindings)
    assert isinstance(ctx.fact_check, FactCheckReview)
    assert isinstance(ctx.source_quality, SourceQualityReview)
    assert isinstance(ctx.security, SecurityAssessment)
    assert isinstance(ctx.privacy_review, PrivacyAssessment)
    assert isinstance(state.synthesis, FinalSynthesis)
    assert [a.agent for a in state.agent_activity] == ORDER
    assert all(a.status == "complete" and a.error is None for a in state.agent_activity)


def test_legacy_return_shape_and_activity_records():
    result = run(FakeSend())
    assert list(result) == [
        "request_id", "agent_activity", "research", "fact_check", "source_quality",
        "security", "privacy_review", "synthesis",
    ]
    assert result["research"] == response("research")
    assert result["security"] == response("security")
    assert [a["agent"] for a in result["agent_activity"]] == ORDER
    assert [a["display_name"] for a in result["agent_activity"]] == [
        "Research", "Fact Check", "Source Quality", "Security", "Privacy Review", "Synthesis",
    ]
    assert [a["model_alias"] for a in result["agent_activity"]] == [
        "m-r", "m-f", "m-s", "m-sec", "m-p", "m-y",
    ]
    for entry in result["agent_activity"]:
        assert set(entry) == {"agent", "display_name", "model_alias", "duration_ms", "status"}


# E. optional specialists
@pytest.mark.parametrize("skipped", list(URLS))
def test_missing_optional_url_skips_that_agent(skipped):
    send = FakeSend()
    result = run(send, {k: v for k, v in URLS.items() if k != skipped})
    assert skipped not in result
    assert skipped.replace("_", "-") not in send.order
    assert [a["agent"] for a in result["agent_activity"]] == send.order


def test_no_specialists_keeps_only_research_and_security():
    for urls in (None, {}):
        send = FakeSend()
        result = run(send, urls)
        assert send.order == ["research", "security"]
        assert list(result) == ["request_id", "agent_activity", "research", "security"]


def test_research_and_security_are_required():
    with pytest.raises(AgentPipelineError):
        run(FakeSend({"research": lambda r: (_ for _ in ()).throw(OSError("down"))}))
    with pytest.raises(AgentPipelineError):
        run(FakeSend({"security": lambda r: (_ for _ in ()).throw(OSError("down"))}))


def test_synthesis_gets_null_privacy_review_when_unconfigured():
    send = FakeSend()
    run(send, {k: v for k, v in URLS.items() if k != "privacy_review"})
    context = send.payload("synthesis")["context"]
    assert "privacy_review" in context and context["privacy_review"] is None


# F. normalization: no envelopes inside the typed context
def test_context_holds_domain_models_only():
    context = run_typed(FakeSend()).context
    for name in ("research", "fact_check", "source_quality", "security", "privacy_review"):
        dumped = getattr(context, name).model_dump()
        assert "request_id" not in dumped and "requested_model_alias" not in dumped


def test_response_envelopes_are_retained_separately():
    state = run_typed(FakeSend())
    assert state.responses["security"].requested_model_alias == "m-sec"
    assert list(state.responses) == [
        "research", "fact_check", "source_quality", "security", "privacy_review", "synthesis",
    ]


# G. legacy outbound payloads
def test_outbound_payloads_match_the_legacy_wire_format():
    send = FakeSend()
    run(send)
    assert send.payload("research") == {"question": "q"}
    for name in ("source-quality", "security"):
        assert send.payload(name) == {"findings": FINDINGS}
    # Fact Check also receives the question, so it can judge relevance (it is not evidence).
    assert send.payload("fact-check") == {"question": "q", "findings": FINDINGS}
    privacy = send.payload("privacy-review")
    assert privacy["question"] == "q"
    context = privacy["context"]
    assert list(context) == ["research", "fact_check", "source_quality", "security"]
    assert context["research"] == FINDINGS
    assert context["fact_check"] == response("fact-check")["result"]
    assert context["source_quality"] == response("source-quality")["result"]
    assert context["security"] == response("security")  # full envelope, as before
    synthesis = send.payload("synthesis")
    assert list(synthesis["context"]) == [*context, "privacy_review"]
    assert {k: v for k, v in synthesis["context"].items() if k != "privacy_review"} == context
    assert synthesis["context"]["privacy_review"] == response("privacy-review")["result"]


def test_every_call_carries_the_original_request_id():
    seen = []

    def send(url, payload, request_id, destination):
        seen.append(request_id)
        return response(destination)

    run(send)
    assert seen == [RID] * 6


# H. failure handling
def test_unreachable_agent_is_recorded_and_stops_the_pipeline():
    send = FakeSend({"security": lambda r: (_ for _ in ()).throw(TimeoutError())})
    with pytest.raises(AgentPipelineError) as caught:
        run(send)
    assert [(a["agent"], a["status"]) for a in caught.value.activity] == [
        ("research", "complete"), ("fact-check", "complete"),
        ("source-quality", "complete"), ("security", "failed"),
    ]
    assert caught.value.activity[-1]["error"] == "TimeoutError"
    assert send.order == ORDER[:4]


# J. transport independence: only the injected send differs
def test_http_and_agentcore_style_urls_behave_the_same():
    http = run(FakeSend(), {k: f"http://{v}" for k, v in URLS.items()})
    core = run(FakeSend(), {k: f"agentcore://arn:{v}" for k, v in URLS.items()})
    strip = lambda r: [{**a, "duration_ms": 0} for a in r["agent_activity"]]
    assert strip(http) == strip(core)
    assert {k: v for k, v in http.items() if k != "agent_activity"} == {
        k: v for k, v in core.items() if k != "agent_activity"
    }


def test_post_json_response_is_validated_through_agentcore_path(monkeypatch):
    import viharbot.agentcore_client as agentcore

    seen = {}

    def fake_invoke(arn, payload, request_id):
        seen["arn"], seen["payload"] = arn, payload
        return response("research")

    monkeypatch.setattr(agentcore, "invoke_agentcore", fake_invoke)
    reply = demo.post_json("agentcore://arn:x", {"question": "q"}, RID, "research")
    assert seen == {"arn": "arn:x", "payload": {"question": "q"}}
    assert reply == response("research")


# I. public API regression (pipeline mocked at the injected-send boundary)
@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("WEB_AUTH_ENABLED", "false")
    real = demo.run_pipeline

    def fake_run(question, request_id, **kwargs):
        return real(question, request_id, **{**kwargs, "send": FakeSend()})

    monkeypatch.setattr(web_app, "run_pipeline", fake_run)
    return TestClient(web_app.app)


def test_chat_accepts_valid_request_and_keeps_response_contract(client):
    reply = client.post("/api/chat", json={"message": "hi"}, headers={"X-Request-ID": RID})
    assert reply.status_code == 200
    assert reply.headers["X-Request-ID"] == RID
    body = reply.json()
    assert list(body) == [
        "request_id", "findings", "assessment", "fact_check", "source_quality",
        "privacy_review", "sage", "synthesis", "agent_activity",
    ]
    assert body["request_id"] == RID
    assert all("error" not in a for a in body["agent_activity"])
    web_app.ChatResponse.model_validate(body)


def test_chat_optional_fields_remain_nullable(monkeypatch):
    monkeypatch.setenv("WEB_AUTH_ENABLED", "false")
    monkeypatch.setattr(
        web_app, "run_pipeline",
        lambda q, rid, **kw: run_pipeline(q, rid, research_url="u", security_url="u",
                                          specialist_urls=None, send=FakeSend()),
    )
    body = TestClient(web_app.app).post("/api/chat", json={"message": "hi"}).json()
    assert body["fact_check"] is None and body["synthesis"] is None


@pytest.mark.parametrize("payload", [
    {}, {"message": ""}, {"message": "x" * 3001},
    {"message": "hi", "history": [{"role": "system", "content": "x"}]},
    {"message": "hi", "history": [{"role": "user", "content": "x"}] * 5},
])
def test_chat_rejects_invalid_requests(client, payload):
    assert client.post("/api/chat", json=payload).status_code == 422


def test_chat_basic_auth_is_unchanged(client, monkeypatch):
    monkeypatch.setenv("WEB_AUTH_ENABLED", "true")
    monkeypatch.setenv("WEB_USERNAME", "u")
    monkeypatch.setenv("WEB_PASSWORD", "p")
    assert client.post("/api/chat", json={"message": "hi"}).status_code == 401
    assert client.post("/api/chat", json={"message": "hi"}, auth=("u", "p")).status_code == 200


def test_chat_pipeline_failure_still_returns_502_with_activity(monkeypatch):
    monkeypatch.setenv("WEB_AUTH_ENABLED", "false")
    send = FakeSend({"security": lambda r: {**r, "request_id": "other"}})
    monkeypatch.setattr(
        web_app, "run_pipeline",
        lambda q, rid, **kw: run_pipeline(q, rid, research_url="u", security_url="u",
                                          specialist_urls=None, send=send),
    )
    reply = TestClient(web_app.app).post("/api/chat", json={"message": "hi"},
                                         headers={"X-Request-ID": RID})
    assert reply.status_code == 502
    assert reply.headers["X-Request-ID"] == RID
    detail = reply.json()["detail"]
    assert detail["message"] == "The agent team could not complete this request"
    assert detail["agent_activity"][-1]["agent"] == "security"
    assert detail["agent_activity"][-1]["status"] == "failed"
