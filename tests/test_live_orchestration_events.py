"""Events are emitted by the real orchestration loop, at the real boundaries, and only then."""

from __future__ import annotations

import pytest

from live_helpers import FIXTURES, R, FC, S, P, Y, VersionedSend, collect, live_work, types
from orchestration_helpers import C, FIXED_TIME_BUDGET, Clock, Script, delegate, finish
from pipeline_fixtures import RID, FakeSend
from viharbot.deadline import DeadlineExceeded
from viharbot.delegation import (
    PlannerInferenceError, PlannerOutputError, PlannerUnavailableError)
from viharbot.iterative_execution import run_iterative_pipeline
from viharbot.live_events import check_stream, serialize_event
from viharbot.live_hooks import CancelToken, OrchestrationCancelled
from viharbot.live_stream import EventEmitter, drive
from viharbot.orchestration_state import ExecutionBudget

URLS = {"fact_check": "u-f", "source_quality": "u-s", "privacy_review": "u-p", "synthesis": "u-y"}


def run(script, send=None, **kwargs):
    kwargs.setdefault("budget", FIXED_TIME_BUDGET)
    events = collect(live_work(script, send, **kwargs))
    return events, check_stream(events)


def by_type(events, event_type):
    return [e for e in events if e.event_type == event_type]


# ---- a run without a reporter is exactly the run it always was ---------------------------------
def test_events_do_not_change_what_the_orchestration_does():
    def go(emit):
        result = run_iterative_pipeline(
            "q", RID, research_url="u-r", security_url="u-sec", specialist_urls=URLS,
            send=VersionedSend(), emit=emit,
            decide=Script(delegate(R), delegate(S), delegate(R), delegate(S), finish()))
        legacy = result.to_legacy_dict()
        legacy["agent_activity"] = [{**a, "duration_ms": 0} for a in legacy["agent_activity"]]
        o = result.orchestration
        return legacy, (o.status, o.completion_kind, o.completion_reason, o.decision_number,
                        o.rejected_actions, [(s.capability, s.version) for s in o.completed_steps])

    quiet = go(None)
    loud = go(EventEmitter(RID, lambda event: None))
    assert quiet == loud


# ---- planner ----------------------------------------------------------------------------------
def test_each_decision_is_bracketed_by_planner_started_and_a_result():
    events, _ = run(Script(delegate(R), delegate(S), finish()))
    started = by_type(events, "planner_started")
    completed = by_type(events, "planner_completed")
    assert [e.correlation.decision_number for e in started] == [1, 2, 3]
    assert [e.data.action for e in completed] == ["delegate", "delegate", "finish"]
    assert [e.data.capability for e in completed] == ["research", "security_assessment", None]
    for begin, end in zip(started, completed):
        assert end.correlation.parent_event_id == begin.event_id and end.data.duration_ms >= 0


def test_remaining_budget_only_goes_down_across_decisions():
    events, _ = run(Script(delegate(R), delegate(S), finish()))
    remaining = [e.data.remaining_budget_ms for e in by_type(events, "planner_started")]
    assert remaining == sorted(remaining, reverse=True) and 0 < remaining[0] <= 120_000


# ---- delegation and agents --------------------------------------------------------------------
def test_a_delegation_produces_the_documented_chain_with_real_identities_and_versions():
    events, terminal = run(Script(delegate(R), delegate(S), finish()))
    assert terminal == "orchestration_completed"
    chain = [e for e in events if e.correlation.decision_number == 1]
    assert [e.event_type for e in chain] == [
        "planner_started", "planner_completed", "delegation_requested", "delegation_authorized",
        "agent_started", "agent_completed"]
    requested, authorized, started, completed = chain[2], chain[3], chain[4], chain[5]
    assert requested.data.target_identity == "nova" and requested.source.role == "orchestrator"
    assert authorized.source.identity_id == "application" and authorized.source.role == "application"
    assert (authorized.data.service, authorized.data.result_version) == ("research", 1)
    assert started.source.identity_id == "nova" and started.source.display_name == "Nova"
    assert started.source.role == "specialist" and completed.data.validated is True
    assert {e.correlation.execution_id for e in chain[3:]} == {"exec-1"}
    assert [e.correlation.parent_event_id for e in chain[1:]] == [e.event_id for e in chain[:-1]]


def test_charlies_synthesis_is_reported_as_specialist_work_not_orchestration():
    events, _ = run(Script(delegate(R), delegate(S), delegate(Y), finish()))
    synthesis = next(e for e in events if e.event_type == "agent_started" and e.data.capability == "synthesis")
    assert synthesis.source.identity_id == "charlie" and synthesis.source.role == "specialist"


def test_agent_completed_is_emitted_only_after_the_result_is_recorded():
    holder: dict = {}

    def decide(state, context):
        holder["state"] = state
        return script(state, context)

    script = Script(delegate(R), delegate(S), finish())
    observed: list[tuple[str, bool]] = []

    def sink(event):
        if event.event_type == "agent_completed":
            state = holder["state"]
            capability = C(event.data.capability)
            latest = state.latest(capability)
            observed.append((event.event_type, latest is not None and latest.version == event.data.result_version))
        if event.event_type == "agent_started":
            observed.append((event.event_type, holder["state"].latest(C(event.data.capability)) is None
                             or event.data.result_version > 1))

    emitter = EventEmitter(RID, sink)
    drive(emitter, live_work(decide), CancelToken())
    assert observed and all(ok for _, ok in observed)
    assert [name for name, _ in observed] == ["agent_started", "agent_completed"] * 2


def test_a_rejected_delegation_never_starts_an_agent():
    send = VersionedSend()
    events, _ = run(Script(delegate(S), delegate(R), delegate(S), finish()), send)
    rejected = by_type(events, "delegation_rejected")
    assert [(e.data.capability, e.data.rejection_code) for e in rejected] == [
        ("security_assessment", "dependency_unmet")]
    position = events.index(rejected[0])
    assert events[position + 1].event_type == "planner_started"  # nothing started in between
    assert len(by_type(events, "agent_started")) == len(send.calls) == 2
    assert rejected[0].source.identity_id == "application"


def test_a_refused_finish_is_reported_without_a_capability():
    events, _ = run(Script(finish(), delegate(R), delegate(S), finish()))
    refused = by_type(events, "delegation_rejected")[0]
    assert refused.data.capability is None and refused.data.rejection_code == "mandatory_missing"


def test_a_stale_dependency_is_reported_with_the_real_policy_code():
    script = Script(delegate(R), delegate(S), delegate(R), delegate(Y), delegate(S), finish())
    events, _ = run(script, budget=ExecutionBudget(max_decisions=12, max_executions=10))
    codes = [e.data.rejection_code for e in by_type(events, "delegation_rejected")]
    assert codes == ["dependency_stale"]


# ---- dependencies and supersession -------------------------------------------------------------
def test_dependencies_are_recorded_after_completion_with_required_and_optional_inputs():
    events, _ = run(Script(delegate(R), delegate(FC), delegate(S), delegate(P), finish()),
                    budget=ExecutionBudget(max_decisions=12, max_executions=10))
    recorded = by_type(events, "dependency_recorded")
    assert [e.data.consumer.capability for e in recorded] == ["fact_check", "security_assessment", "privacy_review"]
    assert all(e.data.dependencies for e in recorded)  # research itself has none, so it emits nothing
    privacy = recorded[-1].data
    kinds = {(d.capability, d.dependency_type) for d in privacy.dependencies}
    assert kinds == {("research", "required"), ("security_assessment", "required"), ("fact_check", "optional")}
    assert all(d.result_version >= 1 for d in privacy.dependencies)
    parent = next(e for e in events if e.event_id == recorded[0].correlation.parent_event_id)
    assert parent.event_type == "agent_completed"


def test_supersession_names_exactly_what_the_freshness_rules_invalidate():
    script = Script(delegate(R), delegate(FC), delegate(S), delegate(Y), delegate(R), finish())
    events, _ = run(script, budget=ExecutionBudget(max_decisions=12, max_executions=10))
    superseded = by_type(events, "evidence_superseded")
    assert len(superseded) == 1
    data = superseded[0].data
    assert (data.capability, data.superseded_version, data.replacement_version) == ("research", 1, 2)
    assert {(r.capability, r.result_version) for r in data.affected_results} == {
        ("fact_check", 1), ("security_assessment", 1), ("synthesis", 1)}
    assert events.index(superseded[0]) > events.index(
        next(e for e in events if e.event_type == "agent_completed" and e.data.result_version == 2))


def test_a_repeat_that_invalidates_nothing_emits_no_supersession():
    events, _ = run(Script(delegate(R), delegate(S), delegate(FC), delegate(FC), finish()),
                    budget=ExecutionBudget(max_decisions=12, max_executions=10))
    assert by_type(events, "evidence_superseded") == []
    repeated = [e for e in by_type(events, "agent_completed") if e.data.capability == "fact_check"]
    assert [e.data.result_version for e in repeated] == [1, 2]


def test_result_versions_follow_the_real_run_count():
    events, _ = run(Script(delegate(R), delegate(S), delegate(R), delegate(S), finish()))
    versions = [(e.data.capability, e.data.result_version) for e in by_type(events, "delegation_authorized")]
    assert versions == [("research", 1), ("security_assessment", 1), ("research", 2), ("security_assessment", 2)]


# ---- failures ---------------------------------------------------------------------------------
@pytest.mark.parametrize("error,code", [
    (PlannerUnavailableError("no planner model alias is configured"), "planner_unavailable"),
    (PlannerInferenceError("planning inference failed", "TimeoutError"), "planner_inference_failed"),
    (PlannerOutputError("planner returned an invalid action"), "planner_output_invalid"),
    (RuntimeError("SECRET detail from the model client"), "planner_inference_failed"),
])
def test_planner_failures_emit_planner_failed_then_one_orchestration_failed(error, code):
    def decide(state, context):
        raise error

    events, terminal = run(decide)
    assert terminal == "orchestration_failed"
    assert types(events) == ["planner_started", "planner_failed", "orchestration_failed"]
    assert events[1].data.error_code == events[2].data.error_code == code
    assert "SECRET" not in events[2].model_dump_json()
    assert by_type(events, "final_response") == []


def test_an_invalid_agent_response_emits_agent_failed_and_no_final_response():
    send = VersionedSend({"security": lambda r: {**r, "agent": "someone-else"}})
    events, terminal = run(Script(delegate(R), delegate(S)), send)
    assert terminal == "orchestration_failed"
    failed = by_type(events, "agent_failed")[0]
    assert (failed.data.capability, failed.data.error_code) == ("security_assessment", "agent_response_invalid")
    assert events[-1].data.error_code == "agent_response_invalid"
    assert by_type(events, "agent_completed")[-1].data.capability == "research"  # no completion for security
    assert by_type(events, "final_response") == []


def test_a_transport_failure_is_an_execution_failure_not_a_validation_failure():
    def broken(url, payload, request_id, destination):
        raise OSError("connection refused: http://internal-host:8000")

    events, terminal = run(Script(delegate(R)), broken)
    assert terminal == "orchestration_failed"
    assert by_type(events, "agent_failed")[0].data.error_code == "agent_execution_failed"
    assert "internal-host" not in "".join(e.model_dump_json() for e in events)


def test_expiry_without_required_results_ends_in_a_deadline_failure():
    clock = Clock()

    class Slow(FakeSend):
        def __call__(self, *args, **kwargs):
            clock.now += 500
            return super().__call__(*args, **kwargs)

    events, terminal = run(Script(delegate(R)), Slow(), clock=clock)
    assert terminal == "orchestration_failed"
    assert by_type(events, "agent_failed")[0].data.error_code == "deadline_exceeded"
    assert events[-1].data.error_code == "deadline_exceeded"


def test_expiry_with_required_results_completes_under_policy_not_by_charlie():
    clock = Clock()

    class Slow(FakeSend):
        def __call__(self, url, payload, request_id, destination):
            result = super().__call__(url, payload, request_id, destination)
            clock.now += 59
            return result

    events, terminal = run(Script(delegate(R), delegate(S), delegate(FC), delegate(FC)), Slow(), clock=clock)
    assert terminal == "orchestration_completed"
    data = events[-1].data
    assert (data.status, data.completion_kind, data.completion_reason) == (
        "budget_exhausted", "policy_budget", "time_budget")


def test_a_planner_that_times_out_after_the_required_work_completes_under_policy():
    def decide(state, context):
        if state.decision_number == 3:
            raise DeadlineExceeded("request deadline exceeded")
        return [delegate(R), delegate(S)][state.decision_number - 1]

    events, terminal = run(decide)
    assert terminal == "orchestration_completed"
    assert by_type(events, "planner_failed")[0].data.error_code == "deadline_exceeded"
    assert events[-1].data.completion_kind == "policy_budget"


def test_a_late_planner_answer_is_reported_as_rejected_and_never_acted_on():
    clock = Clock()
    script = Script(delegate(R), delegate(S), delegate(FC))

    def decide(state, context):
        if state.decision_number == 3:
            clock.now += 500
        return script(state, context)

    send = VersionedSend()
    events, terminal = run(decide, send, clock=clock)
    assert terminal == "orchestration_completed"
    late = by_type(events, "delegation_rejected")[-1]
    assert late.data.rejection_code == "deadline_exceeded" and late.data.capability == "fact_check"
    assert len(send.calls) == 2 and len(by_type(events, "agent_started")) == 2


# ---- completion -------------------------------------------------------------------------------
def test_charlie_approved_completion_counts_match_the_real_run():
    events, _ = run(Script(delegate(R), delegate(S), finish()))
    data = events[-1].data
    assert (data.status, data.completion_kind, data.completion_reason) == (
        "completed", "charlie_approved", "charlie_finished")
    assert (data.decisions, data.executions) == (3, 2) and data.elapsed_ms >= 0
    assert [e.event_type for e in events[-2:]] == ["final_response", "orchestration_completed"]


def test_budget_exhaustion_without_charlie_is_policy_completion():
    events, _ = run(Script(delegate(R), delegate(S), delegate(FC)), budget=ExecutionBudget(max_executions=2))
    data = events[-1].data
    assert (data.completion_kind, data.completion_reason) == ("policy_budget", "execution_budget")
    assert by_type(events, "delegation_rejected")[-1].data.rejection_code == "execution_budget"


# ---- diagnostics gate --------------------------------------------------------------------------
def test_rationale_and_aliases_appear_only_when_developer_diagnostics_are_on():
    work = live_work(Script(delegate(R, "A bounded structured rationale."), delegate(S), finish()))
    quiet = collect(work, diagnostics=False)
    loud = collect(live_work(Script(delegate(R, "A bounded structured rationale."), delegate(S), finish())),
                   diagnostics=True)
    quiet_text = "".join(serialize_event(e) for e in quiet if e.event_type != "final_response")  # the wire format
    assert "bounded structured rationale" not in quiet_text and '"model_alias"' not in quiet_text
    planner = by_type(loud, "planner_completed")[0].data
    assert planner.rationale == "A bounded structured rationale."
    assert by_type(loud, "agent_completed")[0].data.model_alias == "m-r"


# ---- cancellation -----------------------------------------------------------------------------
def test_cancellation_stops_new_work_and_emits_no_terminal_event():
    token, send = CancelToken(), VersionedSend()
    events: list = []

    def sink(event):
        events.append(event)
        if event.event_type == "agent_completed":
            token.set()  # the client disappears right after the first result

    emitter = EventEmitter(RID, sink)
    with pytest.raises(OrchestrationCancelled):
        drive(emitter, live_work(Script(delegate(R), delegate(S), finish()), send), token)
    assert len(send.calls) == 1  # no second delegation was started
    assert types(events)[-1] == "agent_completed"
    assert emitter.terminal is None
    assert by_type(events, "agent_started")[-1].data.capability == "research"


def test_a_closed_stream_cancels_the_run_through_the_sink():
    from viharbot.live_hooks import StreamClosed

    def sink(event):
        if event.event_type == "planner_completed":
            raise StreamClosed("gone")

    send = VersionedSend()
    with pytest.raises(OrchestrationCancelled):
        drive(EventEmitter(RID, sink), live_work(Script(delegate(R), finish()), send), CancelToken())
    assert send.calls == []


def test_an_event_that_breaks_the_contract_is_a_bug_not_a_silent_skip():
    from viharbot.live_stream import EventContractError

    emitter = EventEmitter(RID, lambda event: None)
    emitter.planner_started(decision_number=1, remaining_ms=1000)
    with pytest.raises(EventContractError):
        emitter.failed("not_a_documented_code")
    emitter.failed("internal_error")
    with pytest.raises(EventContractError, match="terminal"):
        emitter.planner_started(decision_number=2, remaining_ms=1)
    with pytest.raises(EventContractError):
        emitter.failed("internal_error")
