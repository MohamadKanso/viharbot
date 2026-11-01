from __future__ import annotations

import json
import logging

import pytest

from orchestration_helpers import C, FIXED_TIME_BUDGET, Clock, Forever, Script, delegate, finish
from pipeline_fixtures import FakeSend, response
from viharbot import web_app
from viharbot.deadline import DeadlineExceeded
from viharbot.iterative_execution import OrchestrationFailed, run_iterative_pipeline
from viharbot.observability import JsonFormatter
from viharbot.orchestration_planner import DecisionContext, build_decision_message
from viharbot.orchestration_state import ExecutionBudget
from orchestration_helpers import new_state, make_step

RID = "req-1"
URLS = {"fact_check": "u-f", "source_quality": "u-s", "privacy_review": "u-p", "synthesis": "u-y"}
R, FC, SQ, S, P, Y = (C.RESEARCH, C.FACT_CHECK, C.SOURCE_QUALITY, C.SECURITY_ASSESSMENT,
                      C.PRIVACY_REVIEW, C.SYNTHESIS)
ALL_SIX = [R, FC, SQ, S, P, Y]


def run(decide, send=None, **kwargs):
    send = send or FakeSend()
    kwargs.setdefault("budget", FIXED_TIME_BUDGET)
    return run_iterative_pipeline(
        "q", RID, research_url="u-r", security_url="u-sec", specialist_urls=URLS, send=send,
        decide=decide, **kwargs), send


def plan(*capabilities, end=True):
    return Script(*[delegate(c) for c in capabilities], *([finish()] if end else []))


# 1, 2. Charlie's own completion
def test_charlie_finishing_with_valid_required_results_is_charlie_approved():
    result, _ = run(plan(R, S))
    orchestration = result.orchestration
    assert (orchestration.status, orchestration.completion_kind, orchestration.completion_reason) == (
        "completed", "charlie_approved", "charlie_finished")
    assert orchestration.decision_history[-1].status == "completed"


def test_finish_before_the_required_results_exist_is_rejected():
    script = Script(finish(), delegate(R), delegate(S), finish())
    result, _ = run(script)
    assert result.orchestration.decision_history[0].reason_code == "mandatory_missing"
    assert result.orchestration.completion_kind == "charlie_approved"


# 3, 4, 5. execution budget exhausted: Charlie still evaluates the last result
def test_charlie_evaluates_the_final_result_after_six_capabilities():
    script = plan(*ALL_SIX)
    result, send = run(script)
    assert len(send.calls) == 6 and len(script.calls) == 7  # a seventh decision: the evaluation
    assert script.calls[-1]["allowed"] == [] and script.calls[-1]["finish_allowed"] is True
    assert result.orchestration.completion_kind == "charlie_approved"
    assert result.orchestration.completion_reason == "charlie_finished"
    assert result.orchestration.status == "completed"


def test_the_final_evaluation_prompt_shows_the_last_observation_and_restricts_to_finish():
    state = new_state(original_question="q")
    state.decision_number = 7
    for capability in ALL_SIX:
        state.completed_steps.append(make_step(capability))
    message = build_decision_message(state, DecisionContext(final_evaluation=True, finish_allowed=True))
    assert "FINAL EVALUATION" in message and "Only finish is allowed" in message
    assert '"capability":"synthesis"' in message  # Charlie sees the last result
    assert "allowed to delegate now: none" in message


def test_nothing_is_delegated_after_the_execution_budget_is_used_up():
    script = Script(*[delegate(c) for c in (R, S)], delegate(FC), delegate(SQ))
    result, send = run(script, budget=ExecutionBudget(max_executions=2))
    assert send.order == ["research", "security"]
    assert len(script.calls) == 3 and script.actions == [delegate(SQ)]  # one evaluation only
    assert result.orchestration.completion_kind == "policy_budget"
    assert result.orchestration.completion_reason == "execution_budget"
    assert result.orchestration.decision_history[-1].reason_code == "execution_budget"


def test_policy_completion_is_never_labelled_as_charlies():
    result, _ = run(Script(delegate(R), delegate(S), delegate(FC)),
                    budget=ExecutionBudget(max_executions=2))
    assert result.orchestration.completion_kind == "policy_budget"
    assert "completed" not in [r.status for r in result.orchestration.decision_history]
    assert result.orchestration.status == "budget_exhausted"


# 6, 7. decision budget
def test_decision_budget_with_required_results_completes_by_policy():
    result, _ = run(plan(R, S, FC, SQ, end=False), budget=ExecutionBudget(max_decisions=3))
    assert result.orchestration.completion_reason == "decision_budget"
    assert result.orchestration.completion_kind == "policy_budget"
    assert list(result.to_legacy_dict())[2:] == ["research", "fact_check", "security"]


def test_decision_budget_without_required_results_fails():
    with pytest.raises(OrchestrationFailed) as caught:
        run(plan(R, FC, end=False), budget=ExecutionBudget(max_decisions=2))
    assert caught.value.code == "decision_budget"


def test_the_decision_budget_is_never_exceeded_by_the_final_evaluation():
    script = plan(R, S, end=False)
    result, _ = run(script, budget=ExecutionBudget(max_executions=2, max_decisions=2))
    assert len(script.calls) == 2 and result.orchestration.decision_number == 2
    assert result.orchestration.completion_reason == "decision_budget"


# 8, 9, 10. time budget
class TimedSend(FakeSend):
    def __init__(self, clock, cost):
        super().__init__()
        self.clock, self.cost = clock, cost

    def __call__(self, *args, **kwargs):
        result = super().__call__(*args, **kwargs)
        self.clock.now += self.cost
        return result


def test_time_budget_with_required_results_completes_by_policy():
    clock = Clock()
    result, _ = run(plan(R, S, FC, end=False), send=TimedSend(clock, 59.0), clock=clock)
    assert result.orchestration.completion_reason == "time_budget"
    assert result.orchestration.completion_kind == "policy_budget"


def test_time_budget_without_required_results_raises_the_deadline_error():
    clock = Clock()
    with pytest.raises(DeadlineExceeded):
        run(plan(R, S, end=False), send=TimedSend(clock, 130.0), clock=clock)


def test_a_late_finish_after_the_deadline_is_not_charlie_approved():
    clock = Clock()
    script = plan(R, S)

    def decide(state, context):
        if state.decision_number == 3:
            clock.now += 500
        return script(state, context)

    result, _ = run(decide, clock=clock)
    assert result.orchestration.completion_kind == "policy_budget"
    assert result.orchestration.completion_reason == "time_budget"


# 11. the recorded reason matches what happened
@pytest.mark.parametrize("budget,script,reason,kind", [
    (None, plan(R, S), "charlie_finished", "charlie_approved"),
    (ExecutionBudget(max_decisions=2), plan(R, S, end=False), "decision_budget", "policy_budget"),
    (ExecutionBudget(max_executions=2), Script(delegate(R), delegate(S), delegate(FC)),
     "execution_budget", "policy_budget"),
    (ExecutionBudget(max_decisions=12), Script(delegate(R), delegate(S), delegate(FC), delegate(FC),
                                              delegate(FC), delegate(FC), delegate(FC), delegate(FC)),
     "rejection_limit", "policy_budget"),
])
def test_completion_reason_and_kind_are_recorded_accurately(budget, script, reason, kind):
    result, _ = run(script, **({"budget": budget} if budget else {}))
    assert (result.orchestration.completion_reason, result.orchestration.completion_kind) == (reason, kind)


def test_incomplete_runs_record_that_they_failed():
    captured = {}

    class Spy(Script):
        def __call__(self, state, context):
            captured["state"] = state
            return super().__call__(state, context)

    with pytest.raises(OrchestrationFailed):
        run(Spy(finish(), finish()))
    assert captured["state"].status == "failed"
    assert captured["state"].completion_kind == "incomplete"
    assert captured["state"].completion_reason == "rejection_limit"


# logs distinguish the three kinds
def completion_line(caplog):
    lines = [json.loads(JsonFormatter().format(r)) for r in caplog.records if r.name == "orchestrator"]
    return lines[-1], lines


def test_logs_distinguish_charlie_policy_and_incomplete_endings(caplog):
    with caplog.at_level(logging.INFO):
        run(plan(R, S))
    charlie, _ = completion_line(caplog)
    assert (charlie["completion_status"], charlie["completion_reason"]) == ("charlie_approved", "charlie_finished")
    caplog.clear()
    with caplog.at_level(logging.INFO):
        run(Script(delegate(R), delegate(S), delegate(FC)), budget=ExecutionBudget(max_executions=2))
    policy, _ = completion_line(caplog)
    assert (policy["completion_status"], policy["completion_reason"]) == ("policy_budget", "execution_budget")
    caplog.clear()
    with caplog.at_level(logging.INFO):
        with pytest.raises(OrchestrationFailed):
            run(Script(finish(), finish()))
    incomplete, _ = completion_line(caplog)
    assert incomplete["completion_status"] == "incomplete" and incomplete["error"] == "rejection_limit"
    assert "stale_excluded" not in incomplete


def test_logs_report_how_many_stale_results_were_excluded(caplog):
    with caplog.at_level(logging.INFO):
        run(plan(R, S, Y, R, S), budget=ExecutionBudget(max_decisions=12, max_executions=10))
    final, _ = completion_line(caplog)
    assert final["stale_excluded"] == 1


# 13. public API; 14. no infinite loops
def test_the_public_response_is_unchanged_for_every_completion_kind():
    for script, budget in ((plan(R, S), None),
                           (Script(delegate(R), delegate(S), delegate(FC)), ExecutionBudget(max_executions=2))):
        result, _ = run(script, **({"budget": budget} if budget else {}))
        body = result.to_legacy_dict()
        assert list(body)[:2] == ["request_id", "agent_activity"]
        web_app.ChatResponse.model_validate({
            "request_id": body["request_id"], "findings": body["research"]["findings"],
            "assessment": body["security"]["assessment"], "agent_activity": body["agent_activity"]})
        assert "completion" not in json.dumps(body) and "orchestration" not in json.dumps(body)


@pytest.mark.parametrize("action", [delegate(R), delegate(S), finish(), delegate(Y), delegate(FC)])
@pytest.mark.parametrize("budget", [ExecutionBudget(), ExecutionBudget(max_executions=2),
                                    ExecutionBudget(max_decisions=3, max_repeats_per_capability=0)])
def test_no_charlie_can_loop_forever(action, budget):
    forever, send = Forever(action), FakeSend()
    try:
        run(forever, send=send, budget=budget)
    except (OrchestrationFailed, DeadlineExceeded):
        pass
    assert forever.calls <= budget.max_decisions
    assert len(send.calls) <= budget.max_executions
