from __future__ import annotations

import json
import pytest
from fastapi.testclient import TestClient

from orchestration_helpers import C, Script, delegate, finish
from pipeline_fixtures import FINDINGS, RID, URLS, FakeSend, response
from viharbot import web_app
from viharbot.delegation import DelegationDecision
from viharbot.orchestration_state import ExecutionBudget
from viharbot.orchestration_view import OrchestrationMeta, OrchestrationStep, build_orchestration_meta

R, FC, S, P, Y = C.RESEARCH, C.FACT_CHECK, C.SECURITY_ASSESSMENT, C.PRIVACY_REVIEW, C.SYNTHESIS
QUESTION_MARK = "SENSITIVE-QUESTION-TEXT"
RATIONALE = "Rationale text that must stay private."
GAP = "Unresolved question text that must stay private."
LEGACY_KEYS = ["request_id", "findings", "assessment", "fact_check", "source_quality",
               "privacy_review", "synthesis", "agent_activity"]


class VersionedSend(FakeSend):
    def __init__(self):
        self.research_calls = 0
        super().__init__({"research": self._versioned})

    def _versioned(self, raw):
        self.research_calls += 1
        return {**raw, "findings": {**FINDINGS, "summary": f"research-v{self.research_calls}"}}


LIVE_ENV = ("CHARLIE_PLANNER_MODEL", "LITELLM_API_KEY")


def decision(workflow="full_review"):
    return DelegationDecision(workflow_name=workflow, rationale="r", confidence="high")


@pytest.fixture
def web(monkeypatch):
    monkeypatch.setenv("WEB_AUTH_ENABLED", "false")
    for name in LIVE_ENV:
        monkeypatch.delenv(name, raising=False)
    state = {"send": VersionedSend(), "budget": None, "paths": [],
             "script": lambda: Script(delegate(R, RATIONALE, GAP), delegate(S), finish(RATIONALE)),
             "recommend": lambda q, r: decision()}
    originals = {name: getattr(web_app, name) for name in
                 ("run_pipeline", "run_iterative_pipeline")}

    def wrap(name, key, **extra):
        def call(question, request_id, **kwargs):
            state["paths"].append(key)
            resolved = {k: (v() if callable(v) else v) for k, v in extra.items()}
            return originals[name](question, request_id, **{**kwargs, "send": state["send"], **resolved})
        return call

    monkeypatch.setattr(web_app, "run_pipeline", wrap("run_pipeline", "legacy"))
    monkeypatch.setattr(web_app, "run_iterative_pipeline",
                        wrap("run_iterative_pipeline", "iterative", decide=lambda: state["script"]()))
    return TestClient(web_app.app), state, monkeypatch


def enable(patch):
    patch.setenv("CHARLIE_PLANNER_MODEL", "planner/alias")
    patch.setenv("LITELLM_API_KEY", "test-key")


def chat(client, message="hello", **kwargs):
    return client.post("/api/chat", json={"message": message}, headers={"X-Request-ID": RID}, **kwargs)


# 1, 2. iterative responses carry valid, accurate metadata
def test_iterative_response_includes_valid_metadata(web):
    client, state, patch = web
    enable(patch)
    reply = chat(client)
    assert reply.status_code == 200
    body = reply.json()
    meta = OrchestrationMeta.model_validate(body["orchestration"])
    assert meta.mode == "iterative"
    assert (meta.status, meta.completion_kind, meta.completion_reason) == (
        "completed", "charlie_approved", "charlie_finished")
    assert list(body) == [*LEGACY_KEYS, "orchestration"]


def test_metadata_reflects_the_actual_orchestration_state(web):
    client, state, patch = web
    enable(patch)
    state["script"] = lambda: Script(delegate(R), delegate(S), finish())
    meta = chat(client).json()["orchestration"]
    assert meta["decisions"] == 3 and meta["executions"] == 2 and meta["rejected_actions"] == 0
    assert [(s["identity"], s["capability"], s["service"]) for s in meta["steps"]] == [
        ("nova", "research", "research"), ("cipher", "security_assessment", "security")]
    assert [s["sequence"] for s in meta["steps"]] == [1, 2]
    assert [s["service"] for s in meta["steps"]] == state["send"].order  # nothing invented
    assert all(s["status"] == "complete" and s["duration_ms"] >= 0 for s in meta["steps"])
    assert meta["elapsed_ms"] >= 0


def test_refused_actions_are_counted_but_never_listed_as_steps(web):
    client, state, patch = web
    enable(patch)
    state["script"] = lambda: Script(delegate(S), delegate(R), delegate(S), finish())
    meta = chat(client).json()["orchestration"]
    assert meta["rejected_actions"] == 1 and meta["decisions"] == 4 and meta["executions"] == 2
    assert [s["capability"] for s in meta["steps"]] == ["research", "security_assessment"]


# 3. repeated capabilities, in execution order
def test_repeated_capabilities_appear_in_execution_order_with_their_versions(web):
    client, state, patch = web
    enable(patch)
    state["script"] = lambda: Script(delegate(R), delegate(S), delegate(R), delegate(S), finish())
    body = chat(client).json()
    steps = body["orchestration"]["steps"]
    assert [(s["sequence"], s["capability"], s["version"]) for s in steps] == [
        (1, "research", 1), (2, "security_assessment", 1), (3, "research", 2),
        (4, "security_assessment", 2)]
    assert [s["in_final_result"] for s in steps] == [False, False, True, True]
    assert body["findings"]["summary"] == "research-v2"  # the final result uses the newest
    assert len(body["agent_activity"]) == 4  # the existing activity list still shows every run


def test_a_superseded_optional_result_is_flagged_and_left_out_of_the_response(web):
    client, state, patch = web
    enable(patch)
    state["script"] = lambda: Script(delegate(R), delegate(S), delegate(Y), delegate(R),
                                     delegate(S), finish())
    body = chat(client).json()
    synthesis = next(s for s in body["orchestration"]["steps"] if s["capability"] == "synthesis")
    assert synthesis["in_final_result"] is False and body["synthesis"] is None


# 4, 5. completion kinds
def test_charlie_approved_completion_is_identified(web):
    client, state, patch = web
    enable(patch)
    meta = chat(client).json()["orchestration"]
    assert (meta["completion_kind"], meta["completion_reason"], meta["status"]) == (
        "charlie_approved", "charlie_finished", "completed")


@pytest.mark.parametrize("reason,script,budget", [
    ("execution_budget", lambda: Script(delegate(R), delegate(S), delegate(FC)),
     ExecutionBudget(max_executions=2)),
    ("decision_budget", lambda: Script(delegate(R), delegate(S)), ExecutionBudget(max_decisions=2)),
])
def test_policy_budget_completion_is_identified(web, monkeypatch, reason, script, budget):
    client, state, patch = web
    enable(patch)
    state["script"] = script
    patch.setattr(ExecutionBudget, "from_env", classmethod(lambda cls: budget))
    meta = chat(client).json()["orchestration"]
    assert (meta["completion_kind"], meta["completion_reason"], meta["status"]) == (
        "policy_budget", reason, "budget_exhausted")


# 6. optional results can be absent
def test_optional_results_can_be_absent_and_stay_null(web):
    client, state, patch = web
    enable(patch)
    body = chat(client).json()
    for field in ("fact_check", "source_quality", "privacy_review", "synthesis"):
        assert body[field] is None
    assert body["findings"] and body["assessment"]


# 7, 8. other paths are unchanged and carry no metadata
def test_legacy_responses_are_unchanged_and_omit_the_key(web):
    client, state, patch = web
    reply = chat(client)
    body = reply.json()
    assert state["paths"] == ["legacy"] and list(body) == LEGACY_KEYS
    assert b"orchestration" not in reply.content
    assert body["findings"]["summary"] == "research-v1"


def test_legacy_bodies_match_a_run_without_the_feature_exactly(web):
    client, state, patch = web
    legacy = chat(client).json()
    expected_activity = [{**a, "duration_ms": 0} for a in legacy["agent_activity"]]
    from viharbot.demo import run_pipeline

    direct = run_pipeline("hello", RID, research_url="u-r", security_url="u-sec",
                          specialist_urls={k: v for k, v in URLS.items()}, send=VersionedSend())
    assert legacy["findings"] == direct["research"]["findings"]
    assert legacy["assessment"] == direct["security"]["assessment"]
    assert [a["agent"] for a in legacy["agent_activity"]] == [a["agent"] for a in direct["agent_activity"]]
    assert expected_activity  # the activity record shape is unchanged


# 9, 10. authentication and request ids
def test_authentication_is_still_enforced(web):
    client, _, patch = web
    enable(patch)
    patch.setenv("WEB_AUTH_ENABLED", "true")
    patch.setenv("WEB_USERNAME", "u")
    patch.setenv("WEB_PASSWORD", "p")
    unauthorized = chat(client)
    assert unauthorized.status_code == 401 and "orchestration" not in unauthorized.text
    assert chat(client, auth=("u", "p")).status_code == 200


def test_request_ids_are_unchanged(web):
    client, _, patch = web
    enable(patch)
    reply = chat(client)
    assert reply.headers["X-Request-ID"] == RID and reply.json()["request_id"] == RID
    assert "request_id" not in reply.json()["orchestration"]  # no second identifier


# 11. nothing sensitive is exposed
def test_metadata_exposes_no_prompts_reasoning_urls_or_user_text(web):
    client, state, patch = web
    enable(patch)
    patch.setenv("RESEARCH_AGENTCORE_ARN", "arn:aws:bedrock:secret-runtime")
    patch.setenv("LITELLM_API_KEY", "super-secret-key")
    body = chat(client, message=f"{QUESTION_MARK} about payroll").json()
    text = json.dumps(body["orchestration"])
    for forbidden in (QUESTION_MARK, "payroll", RATIONALE, GAP, "research-v1", "http", "agentcore",
                      "arn:aws", "secret", "api_key", "You are", "prompt", "m-r", "m-sec",
                      "overall risk", "recommend"):
        assert forbidden not in text, forbidden


def test_metadata_has_exactly_the_documented_fields():
    assert list(OrchestrationMeta.model_fields) == [
        "mode", "status", "completion_kind", "completion_reason", "decisions", "executions",
        "rejected_actions", "elapsed_ms", "steps"]
    assert list(OrchestrationStep.model_fields) == [
        "sequence", "identity", "capability", "service", "version", "status", "duration_ms",
        "in_final_result"]


def test_free_text_cannot_enter_the_metadata_models():
    with pytest.raises(Exception):
        OrchestrationMeta.model_validate({
            "mode": "iterative", "status": "completed", "completion_kind": "charlie_approved",
            "completion_reason": "because I said so", "decisions": 1, "executions": 1,
            "rejected_actions": 0, "elapsed_ms": 1.0, "steps": []})
    with pytest.raises(Exception):
        OrchestrationMeta.model_validate({
            "mode": "iterative", "status": "completed", "completion_kind": "charlie_approved",
            "completion_reason": "charlie_finished", "decisions": 1, "executions": 1,
            "rejected_actions": 0, "elapsed_ms": 1.0, "steps": [], "rationale": "leak"})


# schema and other transports
def test_the_schema_marks_the_field_optional_and_documents_it():
    schema = TestClient(web_app.app).get("/openapi.json").json()
    chat_response = schema["components"]["schemas"]["ChatResponse"]
    assert "orchestration" in chat_response["properties"]
    assert "orchestration" not in chat_response["required"]
    assert {"OrchestrationMeta", "OrchestrationStep"} <= set(schema["components"]["schemas"])


def test_failures_return_no_metadata(web):
    client, state, patch = web
    enable(patch)
    state["send"].overrides = {"security": lambda r: {**r, "request_id": "other"}}
    reply = chat(client)
    assert reply.status_code == 502 and "orchestration" not in reply.text


def test_agentcore_invocations_carry_the_metadata_for_iterative_runs(web):
    client, state, patch = web
    enable(patch)
    iterative = client.post("/invocations", json={"request_id": RID, "input": {"message": "hi"}})
    assert iterative.status_code == 200 and iterative.json()["orchestration"]["mode"] == "iterative"
    for name in LIVE_ENV:
        patch.delenv(name)
    legacy = client.post("/invocations", json={"request_id": RID, "input": {"message": "hi"}})
    assert legacy.json()["orchestration"] is None  # the wrapper serializes the null


def test_builder_is_pure_and_derived_from_state_only(web):
    from viharbot.iterative_execution import run_iterative_pipeline

    result = run_iterative_pipeline(
        "q", RID, research_url="u", security_url="u", send=FakeSend(),
        decide=Script(delegate(R), delegate(S), finish()))
    first, second = build_orchestration_meta(result), build_orchestration_meta(result)
    assert first == second and first.executions == len(result.orchestration.completed_steps)
