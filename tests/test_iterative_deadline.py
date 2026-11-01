from __future__ import annotations

import socket

import pytest

from orchestration_helpers import C, FIXED_TIME_BUDGET, Clock, Script, delegate, finish
from pipeline_fixtures import ORDER, RID, FakeSend, response
from viharbot import iterative_execution
from viharbot.deadline import DeadlineExceeded, RequestDeadline
from viharbot.delegation import PlannerInferenceError
from viharbot.demo import AgentPipelineError, run_pipeline
from viharbot.iterative_execution import OrchestrationFailed, run_iterative_pipeline
from viharbot.orchestration_state import ExecutionBudget

QUESTION = "SENSITIVE-QUESTION-TEXT about the access review"
SERVICE_URLS = {"fact_check": "u-f", "source_quality": "u-s", "privacy_review": "u-p",
                "synthesis": "u-y"}


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("a real network call was attempted")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)


def run(decide, send=None, clock=None, **kwargs):
    send = send or FakeSend()
    kwargs.setdefault("budget", FIXED_TIME_BUDGET)
    return run_iterative_pipeline(
        QUESTION, RID, research_url="u-r", security_url="u-sec",
        specialist_urls=kwargs.pop("specialist_urls", SERVICE_URLS), send=send, decide=decide,
        **({"clock": clock} if clock else {}), **kwargs), send


class TimedSend(FakeSend):
    """Advances the fake clock by `cost` for every call and records the deadline it was given."""

    def __init__(self, clock, cost, **kwargs):
        super().__init__(**kwargs)
        self.clock, self.cost, self.remaining_at_call = clock, cost, []
        self.deadlines = []

    def __call__(self, url, payload, request_id, destination, *, deadline=None):
        self.deadlines.append(deadline)
        self.remaining_at_call.append(None if deadline is None else deadline.remaining())
        result = super().__call__(url, payload, request_id, destination)
        self.clock.now += self.cost
        return result


class TimedScript(Script):
    """A scripted Charlie that spends fake time deciding and records the deadline it got."""

    def __init__(self, clock, cost, *actions):
        super().__init__(*actions)
        self.clock, self.cost, self.deadlines, self.remaining_at_call = clock, cost, [], []

    def __call__(self, state, context):
        self.deadlines.append(context.deadline)
        self.remaining_at_call.append(context.deadline.remaining())
        self.clock.now += self.cost
        return super().__call__(state, context)


# 1. created exactly once per request, never reset
def test_one_deadline_is_created_per_request_and_shared_everywhere(monkeypatch):
    created = []

    def counting(*args, **kwargs):
        deadline = RequestDeadline(*args, **kwargs)
        created.append(deadline)
        return deadline

    monkeypatch.setattr(iterative_execution, "RequestDeadline", counting)
    clock = Clock()
    script = TimedScript(clock, 1.0, delegate(C.RESEARCH), delegate(C.SECURITY_ASSESSMENT), finish())
    send = TimedSend(clock, 1.0)
    run(script, send=send, clock=clock)
    assert len(created) == 1
    assert {id(d) for d in script.deadlines} == {id(created[0])}
    assert {id(d) for d in send.deadlines} == {id(created[0])}
    assert created[0].expires_at == 120.0  # started_at 0 + the 120 s budget, set once


def test_each_request_gets_its_own_independent_deadline():
    clock_a, clock_b = Clock(), Clock()
    script_a = TimedScript(clock_a, 0, delegate(C.RESEARCH), delegate(C.SECURITY_ASSESSMENT), finish())
    script_b = TimedScript(clock_b, 0, delegate(C.RESEARCH), delegate(C.SECURITY_ASSESSMENT), finish())
    run(script_a, send=TimedSend(clock_a, 100.0), clock=clock_a, budget=ExecutionBudget(max_seconds=250))
    run(script_b, send=TimedSend(clock_b, 1.0), clock=clock_b, budget=ExecutionBudget(max_seconds=250))
    assert script_a.deadlines[0] is not script_b.deadlines[0]
    assert script_a.deadlines[0].expires_at == script_b.deadlines[0].expires_at == 250
    assert script_a.remaining_at_call[-1] < script_b.remaining_at_call[-1]  # no shared state


# 2, 3. remaining time only goes down
def test_remaining_time_decreases_between_planning_calls_and_service_calls():
    clock = Clock()
    script = TimedScript(clock, 2.0, delegate(C.RESEARCH), delegate(C.SECURITY_ASSESSMENT), finish())
    send = TimedSend(clock, 5.0)
    run(script, send=send, clock=clock)
    assert script.remaining_at_call == [120.0, 113.0, 106.0]  # 2 s planning + 5 s service each
    assert send.remaining_at_call == [118.0, 111.0]
    assert script.remaining_at_call == sorted(script.remaining_at_call, reverse=True)


# 4, 5. late planner results
def test_a_late_finish_is_not_recorded_as_charlies_approval():
    clock = Clock()
    clock.now = 0
    script = TimedScript(clock, 0, delegate(C.RESEARCH), delegate(C.SECURITY_ASSESSMENT), finish())
    send = TimedSend(clock, 0)

    def late_finish(state, context):
        if state.decision_number == 3:
            clock.now += 200  # Charlie takes longer than the whole budget
        return script(state, context)

    result, _ = run(late_finish, send=send, clock=clock)
    orchestration = result.orchestration
    assert orchestration.completion_kind == "policy_budget"
    assert orchestration.completion_reason == "time_budget"
    assert orchestration.status == "budget_exhausted"
    assert [r.status for r in orchestration.decision_history][-1] == "rejected"
    assert orchestration.decision_history[-1].reason_code == "deadline_exceeded"
    assert "completed" not in [r.status for r in orchestration.decision_history]


def test_a_late_delegation_is_never_executed():
    clock = Clock()
    script = Script(delegate(C.RESEARCH), delegate(C.SECURITY_ASSESSMENT), delegate(C.FACT_CHECK))
    send = TimedSend(clock, 0)

    def late_delegate(state, context):
        if state.decision_number == 3:
            clock.now += 500
        return script(state, context)

    result, _ = run(late_delegate, send=send, clock=clock)
    assert send.order == ["research", "security"]  # fact-check never started
    assert result.orchestration.completion_reason == "time_budget"


# 6. late service results
def test_a_service_that_returns_after_the_deadline_is_discarded():
    clock = Clock()
    send = TimedSend(clock, 0)
    original = send.__call__

    def slow_security(url, payload, request_id, destination, *, deadline=None):
        result = original(url, payload, request_id, destination, deadline=deadline)
        if destination == "security":
            clock.now += 500
        return result

    script = Script(delegate(C.RESEARCH), delegate(C.SECURITY_ASSESSMENT))
    with pytest.raises(DeadlineExceeded):
        run(script, send=slow_security, clock=clock)  # security was required and arrived late


def test_a_late_optional_result_is_discarded_and_the_task_still_completes_by_policy():
    clock = Clock()
    base = FakeSend()

    def send(url, payload, request_id, destination):
        result = base(url, payload, request_id, destination)
        if destination == "fact-check":
            clock.now += 500
        return result

    script = Script(delegate(C.RESEARCH), delegate(C.SECURITY_ASSESSMENT), delegate(C.FACT_CHECK))
    result, _ = run(script, send=send, clock=clock)
    assert [s.capability for s in result.orchestration.completed_steps] == [C.RESEARCH, C.SECURITY_ASSESSMENT]
    assert "fact_check" not in result.to_legacy_dict()
    activity = result.to_legacy_dict()["agent_activity"]
    assert [a["agent"] for a in activity] == ["research", "security"] or \
        activity[-1]["agent"] == "fact-check"  # the late call stays visible in the history
    assert result.orchestration.completion_reason == "time_budget"


# 10, 11. expiry with and without the required results
def test_expiry_without_required_results_raises_a_distinct_deadline_error():
    clock = Clock()
    send = TimedSend(clock, 500.0)
    with pytest.raises(DeadlineExceeded) as caught:
        run(Script(delegate(C.RESEARCH)), send=send, clock=clock)
    assert not isinstance(caught.value, (OrchestrationFailed, PlannerInferenceError, AgentPipelineError))


def test_expiry_with_required_results_completes_by_policy():
    clock = Clock()
    send = TimedSend(clock, 50.0)
    script = Script(delegate(C.RESEARCH), delegate(C.SECURITY_ASSESSMENT), delegate(C.FACT_CHECK),
                    delegate(C.SOURCE_QUALITY))
    result, _ = run(script, send=send, clock=clock)
    assert result.orchestration.completion_kind == "policy_budget"
    assert result.orchestration.completion_reason == "time_budget"
    assert list(result.to_legacy_dict())[2:] == ["research", "security"]


# 12, 13. exception categories
def test_a_planner_that_raises_deadline_exceeded_is_not_turned_into_a_planner_error():
    def expiring(state, context):
        raise DeadlineExceeded("request deadline exceeded")

    with pytest.raises(DeadlineExceeded) as caught:
        run(expiring)
    assert not isinstance(caught.value, PlannerInferenceError)


def test_a_planner_deadline_after_required_results_completes_by_policy():
    calls = []

    def decide(state, context):
        calls.append(state.decision_number)
        if state.decision_number == 3:
            raise DeadlineExceeded("request deadline exceeded")
        return [delegate(C.RESEARCH), delegate(C.SECURITY_ASSESSMENT)][state.decision_number - 1]

    result, _ = run(decide)
    assert result.orchestration.completion_reason == "time_budget"
    assert result.orchestration.completion_kind == "policy_budget"


def test_service_deadline_expiry_is_not_an_agent_pipeline_error():
    clock = Clock()
    send = TimedSend(clock, 500.0)
    with pytest.raises(DeadlineExceeded) as caught:
        run(Script(delegate(C.RESEARCH)), send=send, clock=clock)
    assert not isinstance(caught.value, AgentPipelineError)


def test_ordinary_service_failures_are_still_agent_pipeline_errors():
    send = FakeSend({"research": lambda r: {**r, "request_id": "other"}})
    with pytest.raises(AgentPipelineError):
        run(Script(delegate(C.RESEARCH)), send=send)


# 14. nothing starts after expiry
def test_no_delegation_starts_once_the_deadline_has_passed():
    clock = Clock()
    send = TimedSend(clock, 130.0)
    script = Script(delegate(C.RESEARCH), delegate(C.SECURITY_ASSESSMENT), delegate(C.FACT_CHECK))
    with pytest.raises(DeadlineExceeded):
        run(script, send=send, clock=clock)
    assert len(send.calls) == 1 and len(script.calls) == 1


# 15, 16, 19. compatibility
def test_four_argument_send_callbacks_work_in_the_loop():
    send = FakeSend()
    result, _ = run(Script(delegate(C.RESEARCH), delegate(C.SECURITY_ASSESSMENT), finish()), send=send)
    assert send.order == ["research", "security"]
    assert result.orchestration.status == "completed"


def test_the_fixed_pipeline_is_unchanged_by_the_deadline_work():
    legacy_send = FakeSend()
    old = run_pipeline(QUESTION, RID, research_url="u-r", security_url="u-sec",
                       specialist_urls=SERVICE_URLS, send=legacy_send)
    clock = Clock()
    send = FakeSend()
    script = Script(*[delegate(c) for c in
                      (C.RESEARCH, C.FACT_CHECK, C.SOURCE_QUALITY, C.SECURITY_ASSESSMENT,
                       C.PRIVACY_REVIEW, C.SYNTHESIS)], finish())
    result, _ = run(script, send=send, clock=clock)
    norm = lambda d: {**d, "agent_activity": [{**a, "duration_ms": 0} for a in d["agent_activity"]]}
    assert norm(result.to_legacy_dict()) == norm(old)
    assert send.calls == legacy_send.calls


def test_request_ids_are_unchanged_everywhere():
    clock = Clock()
    seen = []

    def send(url, payload, request_id, destination):
        seen.append(request_id)
        return response(destination)

    result, _ = run(Script(delegate(C.RESEARCH), delegate(C.SECURITY_ASSESSMENT), finish()),
                    send=send, clock=clock)
    assert seen == [RID, RID] and result.orchestration.request_id == RID
    assert result.to_legacy_dict()["request_id"] == RID


def test_deadline_aware_senders_get_the_request_deadline():
    clock = Clock()
    send = TimedSend(clock, 1.0)
    run(Script(delegate(C.RESEARCH), delegate(C.SECURITY_ASSESSMENT), finish()), send=send, clock=clock)
    assert all(isinstance(d, RequestDeadline) for d in send.deadlines)


# logs
def test_logs_carry_timing_and_completion_reason_without_sensitive_text(caplog):
    import json
    import logging

    from viharbot.observability import JsonFormatter

    clock = Clock()
    with caplog.at_level(logging.INFO):
        run(TimedScript(clock, 1.0, delegate(C.RESEARCH, "Reason text."),
                        delegate(C.SECURITY_ASSESSMENT), finish("Enough.")),
            send=TimedSend(clock, 2.0), clock=clock)
    lines = [json.loads(JsonFormatter().format(r)) for r in caplog.records if r.name == "orchestrator"]
    assert all({"elapsed_ms", "remaining_ms"} <= set(l) for l in lines)
    assert [l["elapsed_ms"] for l in lines] == sorted(l["elapsed_ms"] for l in lines)
    assert lines[-1]["completion_reason"] == "charlie_finished"
    assert lines[-1]["completion_status"] == "charlie_approved"
    assert lines[-1]["remaining_ms"] == pytest.approx((120 - 7) * 1000)  # 3 planning s + 2 services x 2 s
    text = json.dumps(lines)
    for secret in ("SENSITIVE", "Reason text.", "Enough."):
        assert secret not in text


def test_deadline_failures_are_logged_with_the_error_type(caplog):
    import json
    import logging

    from viharbot.observability import JsonFormatter

    clock = Clock()
    with caplog.at_level(logging.INFO):
        with pytest.raises(DeadlineExceeded):
            run(Script(delegate(C.RESEARCH)), send=TimedSend(clock, 500.0), clock=clock)
    lines = [json.loads(JsonFormatter().format(r)) for r in caplog.records if r.name == "orchestrator"]
    failed = lines[-1]
    assert failed["error"] == "DeadlineExceeded" and failed["completion_reason"] == "time_budget"
    assert failed["completion_status"] == "incomplete" and failed["request_id"] == RID
