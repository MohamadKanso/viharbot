"""Deterministic authorization for orchestration actions.

Everything here is pure: no model calls, no network, no agent invocation. A model's action is
only ever a proposal. authorize_action() and check_completion() decide what may happen.
"""

from __future__ import annotations

import json
from collections.abc import Collection
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .execution_bindings import OPERATIONAL_SERVICES, ExecutionBindingSet
from .identity import Capability, CapabilityStatus, capability_status
from .identity_registry import AgentRegistry
from .orchestration_state import (
    CANONICAL_ORDER,
    LEGACY_KEYS,
    OrchestrationState,
    StepResult,
)
from .pipeline_state import fact_check_payload, legacy_context_payload, legacy_findings_payload
from .sage import MAX_SECTION_CHARS
from .schemas import ResearchFindings

CapabilityName = Literal[
    "research",
    "fact_check",
    "source_quality",
    "security_assessment",
    "privacy_review",
    "philosophical_analysis",
    "synthesis",
]

# Inputs a capability cannot run without. Anything else it uses is optional context.
HARD_DEPENDENCIES: dict[Capability, tuple[Capability, ...]] = {
    Capability.RESEARCH: (),
    Capability.FACT_CHECK: (Capability.RESEARCH,),
    Capability.SOURCE_QUALITY: (Capability.RESEARCH,),
    Capability.SECURITY_ASSESSMENT: (Capability.RESEARCH,),
    Capability.PRIVACY_REVIEW: (Capability.RESEARCH, Capability.SECURITY_ASSESSMENT),
    # Sage needs research. It does not need security, so it can run whenever research exists;
    # security and the other results are read when they are fresh (see SOFT_INPUTS).
    Capability.PHILOSOPHICAL_ANALYSIS: (Capability.RESEARCH,),
    Capability.SYNTHESIS: (Capability.RESEARCH, Capability.SECURITY_ASSESSMENT),
}
# Optional inputs that are included when present, matching the fixed pipeline's payloads.
SOFT_INPUTS: dict[Capability, tuple[Capability, ...]] = {
    Capability.PRIVACY_REVIEW: (Capability.FACT_CHECK, Capability.SOURCE_QUALITY),
    # A Sage result that read an older security or privacy result than the current one is stale,
    # so it is not shown beside findings it never saw.
    Capability.PHILOSOPHICAL_ANALYSIS: (
        Capability.FACT_CHECK,
        Capability.SOURCE_QUALITY,
        Capability.SECURITY_ASSESSMENT,
        Capability.PRIVACY_REVIEW,
    ),
    Capability.SYNTHESIS: (
        Capability.FACT_CHECK,
        Capability.SOURCE_QUALITY,
        Capability.PRIVACY_REVIEW,
        Capability.PHILOSOPHICAL_ANALYSIS,
    ),
}
# The /api/chat contract cannot be answered without these.
MANDATORY: tuple[Capability, ...] = (Capability.RESEARCH, Capability.SECURITY_ASSESSMENT)


def step_is_fresh(state: OrchestrationState, step: StepResult) -> bool:
    """True if every input this result consumed is still current.

    Required inputs must be present and equal to the latest version. Optional inputs count
    only while they are themselves fresh: if a fresh optional input now exists at a different
    version than the one consumed (newer, or never consumed), the result is stale. The
    recorded input versions are the source of truth, not the result's own version number.
    """
    capability = step.capability
    for dependency in HARD_DEPENDENCIES[capability]:
        latest = state.latest(dependency)
        if latest is None or step.based_on.get(dependency.value) != latest.version:
            return False
    for optional in SOFT_INPUTS.get(capability, ()):
        current = fresh_latest(state, optional)
        if current is not None and step.based_on.get(optional.value) != current.version:
            return False
    return True


def fresh_latest(state: OrchestrationState, capability: Capability) -> StepResult | None:
    """The latest result for a capability, only if it is still fresh."""
    step = state.latest(capability)
    return step if step is not None and step_is_fresh(state, step) else None


def fresh_results(state: OrchestrationState) -> dict[Capability, StepResult]:
    """The results that may appear in a final response, in the fixed pipeline order."""
    return {
        capability: step
        for capability in CANONICAL_ORDER
        if (step := fresh_latest(state, capability)) is not None
    }


class OrchestrationAction(BaseModel):
    """A recommendation from Charlie. It can name a capability and nothing executable.

    There is deliberately no field for services, URLs, runtime identifiers, model aliases,
    tools, commands, credentials or permissions.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    action: Literal["delegate", "finish"]
    capability: CapabilityName | None = None
    rationale: str = Field(min_length=1, max_length=300)
    unresolved_question: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def _combination_is_valid(self) -> "OrchestrationAction":
        if self.action == "delegate" and self.capability is None:
            raise ValueError("delegate requires a capability")
        if self.action == "finish" and (
            self.capability is not None or self.unresolved_question is not None
        ):
            raise ValueError("finish must not name a capability or an unresolved question")
        return self


class ActionRejected(Exception):
    """The application refused an action. `code` is a fixed, content-free reason."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class AuthorizedAction(BaseModel):
    model_config = ConfigDict(frozen=True)

    capability: Capability
    identity_id: str
    service_name: str


def authorize_action(
    action: OrchestrationAction,
    state: OrchestrationState,
    registry: AgentRegistry,
    bindings: ExecutionBindingSet,
    available_services: Collection[str],
    *,
    seconds_left: float | None = None,
) -> AuthorizedAction:
    """Authorize one delegation. Raises ActionRejected with a fixed code, or returns it."""
    if not isinstance(action, OrchestrationAction) or action.action != "delegate":
        raise ActionRejected("not_a_delegation")
    try:
        capability = Capability(action.capability)
    except ValueError:
        raise ActionRejected("unknown_capability") from None
    if capability not in HARD_DEPENDENCIES:
        raise ActionRejected("unknown_capability")
    if capability_status(capability) is not CapabilityStatus.IMPLEMENTED:
        raise ActionRejected("capability_not_implemented")

    binding = bindings.lookup(capability)
    if binding is None:
        raise ActionRejected("no_binding")
    if not binding.enabled:
        raise ActionRejected("binding_disabled")
    if binding.service_name != OPERATIONAL_SERVICES[capability]:
        raise ActionRejected("binding_mismatch")
    if binding.identity_id not in registry or capability not in registry.get(
        binding.identity_id
    ).capabilities:
        raise ActionRejected("identity_invalid")
    if binding.service_name not in available_services:
        raise ActionRejected("service_unavailable")

    for dependency in HARD_DEPENDENCIES[capability]:
        latest = state.latest(dependency)
        if latest is None:
            raise ActionRejected("dependency_unmet")
        if not step_is_fresh(state, latest):
            raise ActionRejected("dependency_stale")

    budget = state.execution_budget
    if state.run_count(capability) >= 1 + budget.max_repeats_per_capability:
        raise ActionRejected("repeat_limit")
    if len(state.completed_steps) >= budget.max_executions:
        raise ActionRejected("execution_budget")
    if seconds_left is not None and seconds_left <= 0:
        raise ActionRejected("time_budget")
    return AuthorizedAction(
        capability=capability, identity_id=binding.identity_id, service_name=binding.service_name
    )


def check_completion(state: OrchestrationState) -> str | None:
    """None when the task may complete, otherwise a fixed reason code.

    Completion needs the results /api/chat requires, and security must be based on the latest
    research. Optional specialist results may be absent. Nothing is fabricated.
    """
    for capability in MANDATORY:
        if state.latest(capability) is None:
            return "mandatory_missing"
    if not step_is_fresh(state, state.latest(Capability.SECURITY_ASSESSMENT)):
        return "security_stale"
    return None


def allowed_capabilities(
    state: OrchestrationState,
    registry: AgentRegistry,
    bindings: ExecutionBindingSet,
    available_services: Collection[str],
    *,
    seconds_left: float | None = None,
) -> tuple[Capability, ...]:
    """Capabilities the application would authorize right now, in canonical order."""
    allowed = []
    for capability in CANONICAL_ORDER:
        probe = OrchestrationAction(action="delegate", capability=capability.value, rationale="probe")
        try:
            authorize_action(probe, state, registry, bindings, available_services,
                             seconds_left=seconds_left)
        except ActionRejected:
            continue
        allowed.append(capability)
    return tuple(allowed)


def input_versions(capability: Capability, state: OrchestrationState) -> dict[str, int]:
    """Versions of the inputs a run would use now: its provenance.

    Required inputs are always the latest. Optional inputs are used only while fresh.
    """
    versions = {
        c.value: state.latest(c).version
        for c in HARD_DEPENDENCIES[capability]
        if state.latest(c) is not None
    }
    for optional in SOFT_INPUTS.get(capability, ()):
        current = fresh_latest(state, optional)
        if current is not None:
            versions[optional.value] = current.version
    return versions


def _context_responses(state: OrchestrationState, capabilities: tuple[Capability, ...]) -> dict[str, Any]:
    """Envelopes for the outbound context, keyed like the legacy result dict and always in the
    fixed pipeline order. Required inputs are the latest; optional ones only if still fresh."""
    required = {Capability.RESEARCH, Capability.SECURITY_ASSESSMENT}
    responses: dict[str, Any] = {}
    for capability in CANONICAL_ORDER:
        if capability not in capabilities:
            continue
        step = state.latest(capability) if capability in required else fresh_latest(state, capability)
        if step is not None:
            responses[LEGACY_KEYS[capability]] = step.envelope
    return responses


# The result sections Sage may read, keyed as its request names them.
_SAGE_SECTIONS: tuple[tuple[str, Capability], ...] = (
    ("research", Capability.RESEARCH),
    ("fact_check", Capability.FACT_CHECK),
    ("source_quality", Capability.SOURCE_QUALITY),
    ("security", Capability.SECURITY_ASSESSMENT),
    ("privacy_review", Capability.PRIVACY_REVIEW),
)


def sage_payload(question: str, state: OrchestrationState) -> dict[str, Any]:
    """Outbound payload for Sage: the question and the fresh results it is authorized to read.

    Bare domain results only, never envelopes. A section too large for Sage's request limit is
    left out rather than truncated mid-structure; Sage is told which sections it received.
    """
    context: dict[str, Any] = {}
    for key, capability in _SAGE_SECTIONS:
        step = state.latest(capability) if capability is Capability.RESEARCH else fresh_latest(
            state, capability)
        if step is None:
            continue
        section = step.domain.model_dump()
        if len(json.dumps(section, ensure_ascii=False)) <= MAX_SECTION_CHARS:
            context[key] = section
    return {"question": question, "context": context}


def build_payload(capability: Capability, state: OrchestrationState) -> dict[str, Any]:
    """Outbound payload for one capability, identical to the fixed pipeline's payloads."""
    question = state.original_question
    if capability is Capability.RESEARCH:
        return {"question": question}
    research = state.latest(Capability.RESEARCH)
    findings: ResearchFindings = research.domain  # type: ignore[assignment]
    if capability is Capability.FACT_CHECK:
        return fact_check_payload(question, findings)
    if capability in (Capability.SOURCE_QUALITY, Capability.SECURITY_ASSESSMENT):
        return legacy_findings_payload(findings)
    if capability is Capability.PHILOSOPHICAL_ANALYSIS:
        return sage_payload(question, state)
    context_inputs = (
        Capability.RESEARCH,
        Capability.FACT_CHECK,
        Capability.SOURCE_QUALITY,
        Capability.SECURITY_ASSESSMENT,
    )
    context = legacy_context_payload(_context_responses(state, context_inputs))
    if capability is Capability.PRIVACY_REVIEW:
        return {"question": question, "context": context}
    privacy = fresh_latest(state, Capability.PRIVACY_REVIEW)
    sage = fresh_latest(state, Capability.PHILOSOPHICAL_ANALYSIS)
    synthesis_context = {**context, "privacy_review": privacy.envelope.result if privacy else None}
    if sage is not None:  # only when Sage ran on current evidence; the key is absent otherwise
        synthesis_context["sage"] = sage.envelope.result
    return {"question": question, "context": synthesis_context}
