from __future__ import annotations

import pytest
from pydantic import ValidationError

from orchestration_helpers import C, RID, make_step, new_state, with_steps
from pipeline_fixtures import FINDINGS, RESPONSES
from viharbot.orchestration_policy import HARD_DEPENDENCIES
from viharbot.orchestration_state import (
    MAX_DECISION_HISTORY,
    MAX_GAP_CHARS,
    MAX_GAPS_PER_OBSERVATION,
    MAX_OBSERVATION_CHARS,
    MAX_PENDING_GAPS,
    MAX_SUMMARY_CHARS,
    DecisionRecord,
    ExecutionBudget,
    Observation,
    build_observation,
    rejection_notice,
)

ALL = [C.RESEARCH, C.FACT_CHECK, C.SOURCE_QUALITY, C.SECURITY_ASSESSMENT, C.PRIVACY_REVIEW,
       C.SYNTHESIS]


# 9. budgets
def test_default_budgets_match_the_approved_values():
    budget = ExecutionBudget()
    assert (budget.max_decisions, budget.max_executions, budget.max_repeats_per_capability,
            budget.max_seconds) == (8, 6, 2, 240.0)


@pytest.mark.parametrize("env,expected", [
    ({}, (8, 6, 2, 240.0)),
    ({"VIHARBOT_ORCH_MAX_DECISIONS": "4", "VIHARBOT_ORCH_MAX_EXECUTIONS": "3",
      "VIHARBOT_ORCH_MAX_REPEATS": "0", "VIHARBOT_ORCH_MAX_SECONDS": "30"}, (4, 3, 0, 30.0)),
    ({"VIHARBOT_ORCH_MAX_DECISIONS": "9999", "VIHARBOT_ORCH_MAX_EXECUTIONS": "9999",
      "VIHARBOT_ORCH_MAX_REPEATS": "99", "VIHARBOT_ORCH_MAX_SECONDS": "99999"}, (16, 12, 3, 300.0)),
    ({"VIHARBOT_ORCH_MAX_DECISIONS": "0", "VIHARBOT_ORCH_MAX_EXECUTIONS": "-3",
      "VIHARBOT_ORCH_MAX_REPEATS": "-1", "VIHARBOT_ORCH_MAX_SECONDS": "0"}, (1, 1, 0, 1.0)),
    ({"VIHARBOT_ORCH_MAX_DECISIONS": "abc", "VIHARBOT_ORCH_MAX_SECONDS": "nan"}, (8, 6, 2, 240.0)),
])
def test_budget_environment_overrides_are_bounded(monkeypatch, env, expected):
    for name in ("DECISIONS", "EXECUTIONS", "REPEATS", "SECONDS"):
        monkeypatch.delenv(f"VIHARBOT_ORCH_MAX_{name}", raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    budget = ExecutionBudget.from_env()
    assert (budget.max_decisions, budget.max_executions, budget.max_repeats_per_capability,
            budget.max_seconds) == expected


def test_budget_is_immutable_and_validated():
    with pytest.raises(ValidationError):
        ExecutionBudget().max_decisions = 99
    for bad in ({"max_decisions": 0}, {"max_executions": 0}, {"max_seconds": 0},
                {"max_repeats_per_capability": -1}):
        with pytest.raises(ValidationError):
            ExecutionBudget(**bad)


# 3. state: provenance, versions, isolation
def test_repeated_capabilities_keep_every_version_and_latest_wins():
    first = make_step(C.RESEARCH, 1)
    second = make_step(C.RESEARCH, 2, overrides=lambda r: {
        **r, "findings": {**FINDINGS, "summary": "second"}})
    state = with_steps(first, second)
    assert [s.version for s in state.completed_steps if s.capability is C.RESEARCH] == [1, 2]
    assert state.latest(C.RESEARCH) is second and state.run_count(C.RESEARCH) == 2
    assert first.domain.summary == "s"  # earlier evidence is not overwritten
    assert state.latest(C.SECURITY_ASSESSMENT) is None


def test_staleness_follows_input_versions():
    research1 = make_step(C.RESEARCH, 1)
    security = make_step(C.SECURITY_ASSESSMENT, 1, {"research": 1})
    deps = HARD_DEPENDENCIES[C.SECURITY_ASSESSMENT]
    state = with_steps(research1, security)
    assert state.is_stale(security, deps) is False
    state.completed_steps.append(make_step(C.RESEARCH, 2))
    assert state.is_stale(security, deps) is True
    assert state.is_stale(make_step(C.SECURITY_ASSESSMENT, 2, {"research": 2}), deps) is False
    assert state.is_stale(make_step(C.SECURITY_ASSESSMENT, 3, {}), deps) is True


def test_state_is_isolated_per_request():
    one, two = new_state(), new_state()
    one.completed_steps.append(make_step(C.RESEARCH))
    one.record(DecisionRecord(decision_number=1, requested_action="finish", status="rejected"))
    one.add_gap("gap")
    assert two.completed_steps == [] and two.decision_history == [] and two.pending_gaps == []


def test_decision_history_and_gaps_are_bounded_and_content_free():
    state = new_state()
    for number in range(MAX_DECISION_HISTORY + 10):
        state.record(DecisionRecord(decision_number=number, requested_action="finish",
                                    status="rejected", reason_code="mandatory_missing"))
    assert len(state.decision_history) == MAX_DECISION_HISTORY
    assert state.decision_history[-1].decision_number == MAX_DECISION_HISTORY + 9
    assert set(DecisionRecord.model_fields) == {
        "decision_number", "requested_action", "capability", "status", "reason_code", "duration_ms"}
    for index in range(MAX_PENDING_GAPS + 4):
        state.add_gap(f"gap {index} " + "x" * 500)
    state.add_gap(None)
    assert len(state.pending_gaps) == MAX_PENDING_GAPS
    assert all(len(g) <= MAX_GAP_CHARS for g in state.pending_gaps)
    state.add_gap(state.pending_gaps[0])
    assert len(state.pending_gaps) == MAX_PENDING_GAPS  # no duplicates


def test_remaining_budget_reflects_usage_and_never_goes_negative():
    state = new_state()
    assert state.remaining(lambda: 0.0) == {
        "decisions": 8, "executions": 6, "seconds": ExecutionBudget().max_seconds}
    state.decision_number = 20
    state.completed_steps.extend(make_step(C.RESEARCH, v) for v in range(1, 9))
    assert state.remaining(lambda: 500.0) == {"decisions": 0, "executions": 0, "seconds": 0.0}


def test_orchestration_state_is_not_part_of_any_public_schema():
    from viharbot import web_app

    # Only the sanitized view is public; the state object itself never is.
    from viharbot.orchestration_state import OrchestrationState
    from viharbot.orchestration_view import OrchestrationMeta

    assert web_app.ChatResponse.model_fields["orchestration"].annotation == OrchestrationMeta | None
    private = {"original_question", "current_goal", "pending_gaps", "decision_history",
               "completed_steps", "execution_budget", "request_id", "started_at"}
    assert not private & set(OrchestrationMeta.model_fields)  # no free text, no raw state
    assert list(web_app.ChatResponse.model_fields) == [
        "request_id", "findings", "assessment", "fact_check", "source_quality",
        "privacy_review", "sage", "synthesis", "agent_activity",
        "orchestration", "deck"]


# 8, 17. observations
@pytest.mark.parametrize("capability", ALL)
def test_observations_are_derived_from_validated_output(capability):
    step = make_step(capability)
    observation = build_observation(step)
    assert observation.capability is capability and observation.status == "complete"
    assert observation.identity_id == step.identity_id
    assert observation.result_summary and observation.execution_duration_ms == 1.0
    assert "request_id" not in observation.model_dump_json()  # no raw envelope data


def test_observation_content_matches_the_validated_result():
    assert "overall risk low" in build_observation(make_step(C.SECURITY_ASSESSMENT)).result_summary
    assert build_observation(make_step(C.RESEARCH)).evidence_gaps == ("u",)
    assert "mixed_evidence" in build_observation(make_step(C.FACT_CHECK)).evidence_gaps[0]
    assert build_observation(make_step(C.SOURCE_QUALITY)).evidence_gaps[0] == "unknown quality: x"


def test_observations_are_bounded_even_for_huge_agent_output():
    huge = "A" * 20000

    def inflate(raw):
        findings = {**FINDINGS, "summary": huge, "uncertainties": [huge] * 40}
        return {**raw, "findings": findings}

    observation = build_observation(make_step(C.RESEARCH, overrides=inflate))
    assert len(observation.result_summary) <= MAX_SUMMARY_CHARS
    assert len(observation.evidence_gaps) <= MAX_GAPS_PER_OBSERVATION
    assert all(len(g) <= MAX_GAP_CHARS for g in observation.evidence_gaps)
    assert len(observation.as_prompt_json()) <= MAX_OBSERVATION_CHARS
    with pytest.raises(ValidationError):
        Observation(status="complete", result_summary="x" * (MAX_SUMMARY_CHARS + 1))
    with pytest.raises(ValidationError):
        Observation(status="complete", result_summary="x",
                    evidence_gaps=("g",) * (MAX_GAPS_PER_OBSERVATION + 1))


def test_stale_flag_and_fixed_rejection_notices():
    assert build_observation(make_step(C.SECURITY_ASSESSMENT), stale=True).stale is True
    notice = rejection_notice("dependency_unmet")
    assert notice.status == "rejected" and notice.capability is None
    assert notice.result_summary == "The application rejected your last action: dependency_unmet."


def test_observations_do_not_claim_external_verification():
    for capability in ALL:
        text = build_observation(make_step(capability)).model_dump_json().lower()
        assert "externally verified" not in text and "independently verified" not in text
