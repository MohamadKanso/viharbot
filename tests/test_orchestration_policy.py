from __future__ import annotations

import socket
import subprocess
import sys

import pytest
from pydantic import ValidationError

from orchestration_helpers import (
    C, URLS_BY_SERVICE, delegate, finish, make_step, new_state, with_steps)
from viharbot import identity
from viharbot.execution_bindings import ExecutionBinding, ExecutionBindingSet, default_bindings
from viharbot.identity import CapabilityStatus
from viharbot.identity_registry import AgentRegistry, default_registry
from viharbot.orchestration_policy import (
    HARD_DEPENDENCIES,
    MANDATORY,
    ActionRejected,
    OrchestrationAction,
    allowed_capabilities,
    authorize_action,
    check_completion,
    input_versions,
)
from viharbot.orchestration_state import ExecutionBudget

REGISTRY, BINDINGS = default_registry(), default_bindings()
SERVICES = set(URLS_BY_SERVICE)


def authorize(action, state, bindings=BINDINGS, services=SERVICES, registry=REGISTRY, **kw):
    return authorize_action(action, state, registry, bindings, services, **kw)


def rejected(action, state, **kw):
    with pytest.raises(ActionRejected) as caught:
        authorize(action, state, **kw)
    return caught.value.code


def research_done():
    return with_steps(make_step(C.RESEARCH))


def research_security_done():
    return with_steps(make_step(C.RESEARCH), make_step(C.SECURITY_ASSESSMENT, 1, {"research": 1}))


# 4. the action model
def test_valid_actions():
    assert delegate(C.RESEARCH).capability == "research"
    assert delegate(C.FACT_CHECK, unresolved_question="Is claim 2 supported?").unresolved_question
    assert finish().capability is None


@pytest.mark.parametrize("kwargs", [
    dict(action="delegate", rationale="x"),                                  # no capability
    dict(action="finish", rationale="x", capability="research"),             # finish + capability
    dict(action="finish", rationale="x", unresolved_question="q"),           # finish + question
    dict(action="explore", rationale="x"),                                   # unknown action
    dict(action="delegate", capability="research", rationale=""),            # empty rationale
    dict(action="delegate", capability="research", rationale="x" * 301),
    dict(action="delegate", capability="research", rationale="x", unresolved_question="q" * 201),
    dict(action="delegate", capability="research", rationale="x", unresolved_question=""),
])
def test_malformed_action_combinations_are_rejected(kwargs):
    with pytest.raises(ValidationError):
        OrchestrationAction(**kwargs)


@pytest.mark.parametrize("capability", [
    "automation_engineering", "logistics_planning", "creative_design",
    "evidence_discovery", "shell", "RESEARCH", "", "research; security_assessment", 7,
])
def test_unknown_and_planned_capabilities_cannot_be_named(capability):
    with pytest.raises(ValidationError):
        OrchestrationAction(action="delegate", capability=capability, rationale="x")


@pytest.mark.parametrize("field,value", [
    ("service_name", "research"), ("service_url", "http://evil"), ("url", "http://evil"),
    ("endpoint", "http://evil"), ("model_alias", "evil/model"), ("runtime_arn", "arn:aws:x"),
    ("tool", "shell"), ("tools", ["shell"]), ("command", "rm -rf /"), ("credentials", "k"),
    ("identity_id", "orbit"), ("permissions", ["all"]), ("steps", ["research"]),
])
def test_actions_cannot_carry_executable_details(field, value):
    with pytest.raises(ValidationError):
        OrchestrationAction(action="delegate", capability="research", rationale="x", **{field: value})


def test_action_schema_is_flat_strict_and_has_no_executable_fields():
    schema = OrchestrationAction.model_json_schema()
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"action", "capability", "rationale", "unresolved_question"}
    with pytest.raises(ValidationError):
        delegate(C.RESEARCH).action = "finish"


# 6, 7. dependencies
def test_dependency_table_is_explicit():
    assert HARD_DEPENDENCIES == {
        C.RESEARCH: (), C.FACT_CHECK: (C.RESEARCH,), C.SOURCE_QUALITY: (C.RESEARCH,),
        C.SECURITY_ASSESSMENT: (C.RESEARCH,),
        C.PRIVACY_REVIEW: (C.RESEARCH, C.SECURITY_ASSESSMENT),
        C.PHILOSOPHICAL_ANALYSIS: (C.RESEARCH,),
        C.SYNTHESIS: (C.RESEARCH, C.SECURITY_ASSESSMENT),
    }
    assert MANDATORY == (C.RESEARCH, C.SECURITY_ASSESSMENT)


def test_research_is_authorized_first_and_resolves_the_approved_binding():
    result = authorize(delegate(C.RESEARCH), new_state())
    assert (result.capability, result.identity_id, result.service_name) == (
        C.RESEARCH, "nova", "research")


@pytest.mark.parametrize("capability", [
    C.FACT_CHECK, C.SOURCE_QUALITY, C.SECURITY_ASSESSMENT, C.PRIVACY_REVIEW, C.SYNTHESIS])
def test_nothing_else_runs_before_research(capability):
    assert rejected(delegate(capability), new_state()) == "dependency_unmet"


@pytest.mark.parametrize("capability", [C.PRIVACY_REVIEW, C.SYNTHESIS])
def test_privacy_and_synthesis_need_security_too(capability):
    assert rejected(delegate(capability), research_done()) == "dependency_unmet"
    assert authorize(delegate(capability), research_security_done()).capability is capability


@pytest.mark.parametrize("capability,identity_id", [
    (C.FACT_CHECK, "scout"), (C.SOURCE_QUALITY, "scout"), (C.SECURITY_ASSESSMENT, "cipher")])
def test_research_unlocks_its_dependents_with_the_bound_identity(capability, identity_id):
    assert authorize(delegate(capability), research_done()).identity_id == identity_id


# 3, 5, 10. bindings, services, implementation status
def test_disabled_missing_and_mismatched_bindings_block_authorization():
    disabled = ExecutionBindingSet(
        [ExecutionBinding(identity_id=b.identity_id, capability=b.capability,
                          service_name=b.service_name, enabled=b.capability is not C.RESEARCH)
         for b in BINDINGS.bindings], REGISTRY)
    assert rejected(delegate(C.RESEARCH), new_state(), bindings=disabled) == "binding_disabled"
    empty = ExecutionBindingSet([], REGISTRY)
    assert rejected(delegate(C.RESEARCH), new_state(), bindings=empty) == "no_binding"


def test_unavailable_service_and_invalid_identity_block_authorization():
    assert rejected(delegate(C.RESEARCH), new_state(), services=SERVICES - {"research"}) \
        == "service_unavailable"
    without_nova = AgentRegistry([i for i in REGISTRY.list() if i.id != "nova"])
    assert rejected(delegate(C.RESEARCH), new_state(), registry=without_nova) == "identity_invalid"


def test_planned_or_unknown_capabilities_cannot_be_authorized(monkeypatch):
    forged = OrchestrationAction.model_construct(action="delegate", capability="shell",
                                                 rationale="x", unresolved_question=None)
    assert rejected(forged, new_state()) == "unknown_capability"
    planned = OrchestrationAction.model_construct(
        action="delegate", capability="automation_engineering", rationale="x",
        unresolved_question=None)
    assert rejected(planned, new_state()) == "unknown_capability"
    monkeypatch.setitem(identity.CAPABILITY_STATUS, C.RESEARCH, CapabilityStatus.PLANNED)
    assert rejected(delegate(C.RESEARCH), new_state()) == "capability_not_implemented"
    assert rejected(finish(), new_state()) == "not_a_delegation"


def test_non_action_objects_are_rejected():
    for bad in ({"action": "delegate", "capability": "research"}, None, "research"):
        assert rejected(bad, new_state()) == "not_a_delegation"


# 9. repeat, execution and time limits
def test_repeat_limit_allows_one_run_plus_two_repeats():
    state = with_steps(*(make_step(C.RESEARCH, v) for v in (1, 2)))
    assert authorize(delegate(C.RESEARCH), state).capability is C.RESEARCH  # third run
    state.completed_steps.append(make_step(C.RESEARCH, 3))
    assert rejected(delegate(C.RESEARCH), state) == "repeat_limit"
    strict = with_steps(make_step(C.RESEARCH), execution_budget=ExecutionBudget(max_repeats_per_capability=0))
    assert rejected(delegate(C.RESEARCH), strict) == "repeat_limit"


def test_execution_and_time_budgets_are_enforced():
    state = with_steps(make_step(C.RESEARCH), make_step(C.SECURITY_ASSESSMENT, 1, {"research": 1}),
                       execution_budget=ExecutionBudget(max_executions=2))
    assert rejected(delegate(C.FACT_CHECK), state) == "execution_budget"
    assert rejected(delegate(C.RESEARCH), new_state(), seconds_left=0) == "time_budget"
    assert rejected(delegate(C.RESEARCH), new_state(), seconds_left=-5) == "time_budget"
    assert authorize(delegate(C.RESEARCH), new_state(), seconds_left=0.5)


def test_model_confidence_is_not_an_input_to_authorization():
    assert "confidence" not in OrchestrationAction.model_fields


# 12, 13, 14. completion policy
def test_completion_requires_the_mandatory_results():
    assert check_completion(new_state()) == "mandatory_missing"
    assert check_completion(research_done()) == "mandatory_missing"
    assert check_completion(research_security_done()) is None


def test_completion_does_not_require_optional_results():
    state = research_security_done()
    assert state.latest(C.SYNTHESIS) is None and check_completion(state) is None


def test_completion_rejects_security_that_is_stale_against_newer_research():
    state = research_security_done()
    state.completed_steps.append(make_step(C.RESEARCH, 2))
    assert check_completion(state) == "security_stale"
    state.completed_steps.append(make_step(C.SECURITY_ASSESSMENT, 2, {"research": 2}))
    assert check_completion(state) is None


# the planner's view of what is possible
def test_allowed_capabilities_track_dependencies():
    names = lambda state: [c.value for c in allowed_capabilities(state, REGISTRY, BINDINGS, SERVICES)]
    assert names(new_state()) == ["research"]
    assert names(research_done()) == [
        "research", "fact_check", "source_quality", "security_assessment"]
    # These services have no sage address, so Sage is not offered (see the Sage tests below).
    assert names(research_security_done()) == [
        c.value for c in HARD_DEPENDENCIES if c is not C.PHILOSOPHICAL_ANALYSIS]


def test_input_versions_record_provenance():
    state = with_steps(make_step(C.RESEARCH, 1), make_step(C.RESEARCH, 2),
                       make_step(C.FACT_CHECK, 1, {"research": 2}))
    assert input_versions(C.SECURITY_ASSESSMENT, state) == {"research": 2}
    assert input_versions(C.SYNTHESIS, state) == {"research": 2, "fact_check": 1}
    assert input_versions(C.RESEARCH, state) == {}


# 24. nothing real is contacted
def test_policy_makes_no_network_calls(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    assert authorize(delegate(C.RESEARCH), new_state())


def test_policy_modules_do_not_import_inference_clients():
    code = (
        "import sys\n"
        "import viharbot.orchestration_state, viharbot.orchestration_policy\n"
        "import viharbot.orchestration_planner, viharbot.iterative_execution\n"
        "bad = [m for m in ('strands', 'litellm', 'boto3', 'openai') if m in sys.modules]\n"
        "assert not bad, bad\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
