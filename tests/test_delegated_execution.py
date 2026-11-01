from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient

from pipeline_fixtures import FINDINGS, ORDER, RID, URLS, FakeSend, response
from viharbot import delegated_execution, delegation_planner, demo, model_agent, web_app
from viharbot.delegated_execution import (
    run_delegated_pipeline,
)
from viharbot.delegation import (
    DelegationDecision,
    DelegationRejected,
    PlannerInferenceError,
    PlannerUnavailableError,
)
from viharbot.demo import AgentPipelineError, run_pipeline
from viharbot.execution_bindings import ExecutionBinding, ExecutionBindingSet, default_bindings
from viharbot.execution_planner import PlanningError
from viharbot.identity_execution import IdentityPipelineError
from viharbot.identity_registry import default_registry
from viharbot.observability import JsonFormatter

QUESTION = "SENSITIVE-QUESTION-TEXT about our quarterly access review"
RESEARCH_SECURITY = ["research", "security"]


def rec(workflow="full_review", **kwargs):
    return DelegationDecision(workflow_name=workflow, rationale="Reason text.", confidence="high",
                              **kwargs)


_DEFAULT = object()


class Recommender:
    def __init__(self, decision=_DEFAULT, error=None):
        self.decision = rec() if decision is _DEFAULT else decision
        self.error = error
        self.calls = []

    def __call__(self, question, request_id):
        self.calls.append((question, request_id))
        if self.error:
            raise self.error
        return self.decision


def norm(body):
    return {**body, "agent_activity": [{**a, "duration_ms": 0} for a in body["agent_activity"]]}


def run(send, recommend, specialist_urls=URLS, **kwargs):
    return run_delegated_pipeline(
        QUESTION, RID, research_url="u-r", security_url="u-sec", specialist_urls=specialist_urls,
        send=send, recommend=recommend, **kwargs)


# 11, 13. full_review: six stages, in order, identical to the existing paths
def test_full_review_executes_the_six_stages_in_order():
    send, recommender = FakeSend(), Recommender()
    result = run(send, recommender)
    assert send.order == ORDER
    assert [r.service_name for r in result.result.records] == ORDER
    assert result.recommended_workflow == result.approved_workflow == "full_review"


def test_full_review_matches_the_legacy_pipeline_outputs_and_payloads():
    old_send, new_send = FakeSend(), FakeSend()
    old = run_pipeline(QUESTION, RID, research_url="u-r", security_url="u-sec",
                       specialist_urls=URLS, send=old_send)
    new = run(new_send, Recommender()).to_legacy_dict()
    assert norm(new) == norm(old)
    assert new_send.calls == old_send.calls


# 12, 17. research_security: two stages, nothing fabricated
def test_research_security_executes_two_stages_and_invents_nothing():
    send = FakeSend()
    result = run(send, Recommender(rec("research_security")))
    assert send.order == RESEARCH_SECURITY
    legacy = result.to_legacy_dict()
    assert list(legacy) == ["request_id", "agent_activity", "research", "security"]
    state = result.result.state
    assert state.context.fact_check is state.context.source_quality is None
    assert state.context.privacy_review is None and state.synthesis is None
    assert [a["agent"] for a in legacy["agent_activity"]] == RESEARCH_SECURITY
    assert send.payload("security") == {"findings": FINDINGS}


# 14. request ids; 15. existing validation reused
def test_request_id_reaches_the_planner_and_every_agent():
    seen, recommender = [], Recommender()

    def send(url, payload, request_id, destination):
        seen.append(request_id)
        return response(destination)

    result = run(send, recommender)
    assert recommender.calls == [(QUESTION, RID)]
    assert seen == [RID] * 6
    assert {r.request_id for r in result.result.records} == {RID}


@pytest.mark.parametrize("stage", ORDER)
def test_malformed_agent_responses_fail_at_the_existing_validation_boundary(stage):
    send = FakeSend({stage: lambda r: {**r, "agent": "someone-else"}})
    with pytest.raises(IdentityPipelineError) as caught:
        run(send, Recommender())
    assert isinstance(caught.value, AgentPipelineError)
    assert caught.value.activity[-1]["agent"] == stage
    assert send.order == ORDER[: ORDER.index(stage) + 1]


# 8, 9, 10. invalid plans fail before any agent runs
@pytest.mark.parametrize("bad", [
    {"workflow_name": "full_review", "rationale": "x", "confidence": "high"}, None, "full_review",
])
def test_invalid_recommendations_fail_before_execution(bad):
    send = FakeSend()
    with pytest.raises(DelegationRejected):
        run(send, Recommender(decision=bad))
    assert send.calls == []


def test_unapproved_workflow_fails_before_execution():
    forged = DelegationDecision.model_construct(workflow_name="shell", rationale="x",
                                                confidence="high")
    send = FakeSend()
    with pytest.raises(DelegationRejected):
        run(send, Recommender(decision=forged))
    assert send.calls == []


def test_disabled_binding_blocks_execution_before_any_call():
    bindings = ExecutionBindingSet(
        [ExecutionBinding(identity_id=b.identity_id, capability=b.capability,
                          service_name=b.service_name, enabled=b.service_name != "security")
         for b in default_bindings().bindings], default_registry())
    for workflow in ("full_review", "research_security"):
        send = FakeSend()
        with pytest.raises(PlanningError, match="security_assessment is disabled"):
            run(send, Recommender(rec(workflow)), bindings=bindings)
        assert send.calls == []


def test_missing_services_block_execution_before_any_call():
    send = FakeSend()
    with pytest.raises(PlanningError, match="synthesis is not configured"):
        run(send, Recommender(), specialist_urls={k: v for k, v in URLS.items() if k != "synthesis"})
    assert send.calls == []
    # research_security does not need the specialists.
    run(send, Recommender(rec("research_security")), specialist_urls={})
    assert send.order == RESEARCH_SECURITY


# 13, 22. planner failures: controlled, no fallback, no execution
@pytest.mark.parametrize("error,expected", [
    (PlannerInferenceError("planning inference failed", "TimeoutError"), PlannerInferenceError),
    (PlannerUnavailableError("no planner model alias is configured"), PlannerUnavailableError),
    (RuntimeError("boom with SENSITIVE-QUESTION-TEXT"), PlannerInferenceError),
])
def test_planner_failures_never_fall_back_or_execute(error, expected):
    send = FakeSend()
    with pytest.raises(expected) as caught:
        run(send, Recommender(error=error))
    assert send.calls == []  # no silent full_review
    assert "SENSITIVE" not in str(caught.value)


def test_default_planner_without_an_alias_is_unavailable_and_builds_no_agent(monkeypatch):
    monkeypatch.delenv(delegation_planner.PLANNER_MODEL_ENV, raising=False)
    monkeypatch.setenv("SYNTHESIS_MODEL", "svc/synthesis-alias")

    def forbidden(**kwargs):
        raise AssertionError("an agent was built")

    monkeypatch.setattr(model_agent, "build_agent", forbidden)
    send = FakeSend()
    with pytest.raises(PlannerUnavailableError):
        run_delegated_pipeline(QUESTION, RID, research_url="u-r", security_url="u-sec",
                               specialist_urls=URLS, send=send)
    assert send.calls == []


# 23, 24. bounded: one planning call, one execution, no recursion
def test_one_planning_call_and_one_execution_per_request(monkeypatch):
    recommender, executions = Recommender(), []
    real = delegated_execution.execute_plan
    monkeypatch.setattr(delegated_execution, "execute_plan",
                        lambda *a, **k: executions.append(a[0].workflow_name) or real(*a, **k))
    send = FakeSend()
    run(send, recommender)
    assert len(recommender.calls) == 1 and executions == ["full_review"]
    assert send.order.count("research") == 1


def test_a_failed_execution_is_not_retried_or_replanned():
    recommender = Recommender()
    send = FakeSend({"security": lambda r: {**r, "request_id": "other"}})
    with pytest.raises(AgentPipelineError):
        run(send, recommender)
    assert len(recommender.calls) == 1
    assert send.order == ORDER[:4]


def test_planning_runs_before_and_separately_from_execution():
    events = []

    def recommend(question, request_id):
        events.append("plan")
        return rec()

    def send(url, payload, request_id, destination):
        events.append(destination)
        return response(destination)

    result = run(send, recommend)
    assert events == ["plan", *ORDER]
    assert result.planning_duration_ms >= 0 and result.execution_duration_ms >= 0


# 30. logs: recommended vs executed, nothing sensitive
def records_of(caplog):
    return [json.loads(JsonFormatter().format(r)) for r in caplog.records
            if r.name == "orchestrator" and r.getMessage().startswith("deleg")]


def test_success_logs_distinguish_recommended_from_executed(caplog):
    with caplog.at_level(logging.INFO):
        run(FakeSend(), Recommender(rec("research_security")))
    lines = records_of(caplog)
    assert [l["delegation_status"] for l in lines] == ["approved", "executed"]
    approved, executed = lines
    assert "execution_duration_ms" not in approved  # not claimed before it ran
    assert approved["recommended_workflow"] == approved["approved_workflow"] == "research_security"
    assert executed["execution_duration_ms"] >= 0 and executed["planning_duration_ms"] >= 0
    assert executed["request_id"] == RID and executed["planner_identity"] == "charlie"
    text = json.dumps(lines)
    for secret in ("SENSITIVE", "Reason text.", "RECOMMEND"):
        assert secret not in text


def test_failure_logs_never_claim_execution(caplog):
    with caplog.at_level(logging.INFO):
        with pytest.raises(DelegationRejected):
            run(FakeSend(), Recommender(decision=DelegationDecision.model_construct(
                workflow_name="shell", rationale="x", confidence="high")))
        with pytest.raises(AgentPipelineError):
            run(FakeSend({"research": lambda r: {**r, "request_id": "other"}}), Recommender())
    statuses = [l["delegation_status"] for l in records_of(caplog)]
    assert statuses == ["rejected", "approved", "execution_failed"]
    assert "executed" not in statuses


# 25, 26. nothing real is contacted
def test_default_transport_is_still_demo_post_json_and_no_inference_runs(monkeypatch):
    send = FakeSend()
    monkeypatch.setattr(demo, "post_json", send)

    def forbidden(**kwargs):
        raise AssertionError("an inference agent was built")

    monkeypatch.setattr(model_agent, "build_agent", forbidden)
    run_delegated_pipeline(QUESTION, RID, research_url="u-r", security_url="u-sec",
                           specialist_urls=URLS, recommend=Recommender())
    assert send.order == ORDER
