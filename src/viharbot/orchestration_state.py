"""Per-request state for iterative orchestration.

State lives only in memory for one request and is never exposed through any API. Validated
results are kept as a version history per capability, so a repeated capability never
overwrites earlier evidence.
"""

from __future__ import annotations

import json
import os
import time
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict, Field

from .contracts import AgentResponse
from .identity import Capability
from .schemas import (
    FactCheckReview,
    FinalSynthesis,
    PrivacyAssessment,
    ResearchFindings,
    SecurityAssessment,
    SourceQualityReview,
)

# The six implemented capabilities, in the order the fixed pipeline runs them.
CANONICAL_ORDER: tuple[Capability, ...] = (
    Capability.RESEARCH,
    Capability.FACT_CHECK,
    Capability.SOURCE_QUALITY,
    Capability.SECURITY_ASSESSMENT,
    Capability.PRIVACY_REVIEW,
    Capability.SYNTHESIS,
)
# Capability -> the key used in the legacy result dictionary and outbound context.
LEGACY_KEYS: dict[Capability, str] = {
    Capability.RESEARCH: "research",
    Capability.FACT_CHECK: "fact_check",
    Capability.SOURCE_QUALITY: "source_quality",
    Capability.SECURITY_ASSESSMENT: "security",
    Capability.PRIVACY_REVIEW: "privacy_review",
    Capability.SYNTHESIS: "synthesis",
}

MAX_DECISION_HISTORY = 32
MAX_PENDING_GAPS = 5
MAX_SUMMARY_CHARS = 600
MAX_GAPS_PER_OBSERVATION = 5
MAX_GAP_CHARS = 200
MAX_OBSERVATION_CHARS = 2000
MAX_REJECTED_ACTIONS = 2

# Hard ceilings: environment overrides can never exceed these.
_CEILINGS = {"decisions": 16, "executions": 12, "repeats": 3, "seconds": 300.0}


def _bounded(name: str, default, minimum, ceiling, cast):
    try:
        value = cast(os.getenv(name, ""))
    except ValueError:
        return default
    if value != value:  # NaN
        return default
    return min(max(value, minimum), ceiling)


class ExecutionBudget(BaseModel):
    model_config = ConfigDict(frozen=True)

    max_decisions: int = Field(default=8, ge=1)
    max_executions: int = Field(default=6, ge=1)
    max_repeats_per_capability: int = Field(default=2, ge=0)
    # 240 s. Research on gpt-6.1-sol alone takes about 35 s; with the other agents and the planner's
    # own calls, 120 s ran out before synthesis in a real run (issue #68). The ceiling stays 300 s.
    max_seconds: float = Field(default=240.0, gt=0)
    max_rejected_actions: int = Field(default=MAX_REJECTED_ACTIONS, ge=1)

    @classmethod
    def from_env(cls) -> "ExecutionBudget":
        """Defaults, optionally overridden by environment, always clamped to hard ceilings."""
        return cls(
            max_decisions=_bounded("VIHARBOT_ORCH_MAX_DECISIONS", 8, 1, _CEILINGS["decisions"], int),
            max_executions=_bounded(
                "VIHARBOT_ORCH_MAX_EXECUTIONS", 6, 1, _CEILINGS["executions"], int
            ),
            max_repeats_per_capability=_bounded(
                "VIHARBOT_ORCH_MAX_REPEATS", 2, 0, _CEILINGS["repeats"], int
            ),
            max_seconds=_bounded(
                "VIHARBOT_ORCH_MAX_SECONDS", 240.0, 1.0, _CEILINGS["seconds"], float
            ),
        )


class StepResult(BaseModel):
    """One validated execution of one capability."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    capability: Capability
    version: int = Field(ge=1)  # 1 for the first run of this capability, 2 for the next...
    identity_id: str
    service_name: str
    decision_number: int
    duration_ms: float
    envelope: AgentResponse
    domain: BaseModel
    # Versions of the input capabilities this run used (provenance, and staleness checks).
    based_on: dict[str, int] = Field(default_factory=dict)


class DecisionRecord(BaseModel):
    """Bounded, content-free record of one decision. It holds no user or agent text."""

    model_config = ConfigDict(frozen=True)

    decision_number: int
    requested_action: Literal["delegate", "finish"]
    capability: Capability | None = None
    status: Literal["rejected", "executed", "failed", "completed"]
    reason_code: str | None = None
    duration_ms: float | None = None


class Observation(BaseModel):
    """What Charlie sees after an action. Derived from validated output; treated as data."""

    model_config = ConfigDict(frozen=True)

    capability: Capability | None = None
    identity_id: str | None = None
    status: Literal["complete", "rejected"]
    result_summary: str = Field(max_length=MAX_SUMMARY_CHARS)
    evidence_gaps: tuple[str, ...] = Field(default=(), max_length=MAX_GAPS_PER_OBSERVATION)
    execution_duration_ms: float | None = None
    stale: bool = False

    def as_prompt_json(self) -> str:
        text = self.model_dump_json(exclude_none=True)
        if len(text) <= MAX_OBSERVATION_CHARS:
            return text
        shrunk = self.model_copy(update={"evidence_gaps": (), "result_summary": self.result_summary[:300]})
        return shrunk.model_dump_json(exclude_none=True)[:MAX_OBSERVATION_CHARS]


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _gaps(items: list[str]) -> tuple[str, ...]:
    return tuple(_clip(item, MAX_GAP_CHARS) for item in items if str(item).strip())[
        :MAX_GAPS_PER_OBSERVATION
    ]


def build_observation(step: StepResult, stale: bool = False) -> Observation:
    """Summarize a validated result. Only reports what the agent returned, not verification."""
    domain = step.domain
    if isinstance(domain, ResearchFindings):
        summary, gaps = domain.summary, list(domain.uncertainties)
    elif isinstance(domain, FactCheckReview):
        summary = domain.summary
        gaps = [
            f"{claim.status}: {claim.claim}"
            for claim in domain.claims
            if claim.status in ("insufficient_evidence", "mixed_evidence", "contradicted_by_supplied_evidence")
        ] + list(domain.limitations)
    elif isinstance(domain, SourceQualityReview):
        summary = domain.summary
        gaps = [f"unknown quality: {s.source}" for s in domain.sources if s.apparent_quality == "unknown"]
        gaps += list(domain.limitations)
    elif isinstance(domain, SecurityAssessment):
        summary = f"overall risk {domain.overall_risk}; " + "; ".join(domain.recommendations[:3])
        gaps = list(domain.uncertainties)
    elif isinstance(domain, PrivacyAssessment):
        summary = f"overall privacy risk {domain.overall_risk}; {len(domain.concerns)} concern(s)"
        gaps = list(domain.concerns)
    elif isinstance(domain, FinalSynthesis):
        summary = f"confidence {domain.confidence}; {domain.answer}"
        gaps = list(domain.caveats)
    else:  # pragma: no cover - every implemented capability is handled above
        summary, gaps = "result received", []
    return Observation(
        capability=step.capability,
        identity_id=step.identity_id,
        status="complete",
        result_summary=_clip(summary, MAX_SUMMARY_CHARS),
        evidence_gaps=_gaps(gaps),
        execution_duration_ms=step.duration_ms,
        stale=stale,
    )


def rejection_notice(code: str) -> Observation:
    """A fixed, application-written notice. It never contains model or user text."""
    return Observation(status="rejected", result_summary=f"The application rejected your last action: {code}.")


class OrchestrationState(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    request_id: str
    original_question: str
    current_goal: str = "Answer the user's request."
    iteration: int = 0  # delegated executions started
    decision_number: int = 0  # planning decisions requested
    completed_steps: list[StepResult] = Field(default_factory=list)
    pending_gaps: list[str] = Field(default_factory=list)
    decision_history: list[DecisionRecord] = Field(default_factory=list)
    execution_budget: ExecutionBudget = Field(default_factory=ExecutionBudget)
    rejected_actions: int = 0
    status: Literal["running", "completed", "budget_exhausted", "failed"] = "running"
    started_at: float = 0.0
    # Why and how the task ended. "charlie_approved": Charlie asked to finish and policy
    # agreed. "policy_budget": the application completed it because a limit was reached.
    # "incomplete": it ended without the required results.
    completion_reason: str | None = None
    elapsed_ms: float | None = None  # total orchestration time, set when the run ends
    completion_kind: Literal["charlie_approved", "policy_budget", "incomplete"] | None = None

    def latest(self, capability: Capability) -> StepResult | None:
        runs = [s for s in self.completed_steps if s.capability is capability]
        return runs[-1] if runs else None

    def run_count(self, capability: Capability) -> int:
        return sum(1 for s in self.completed_steps if s.capability is capability)

    def is_stale(self, step: StepResult, hard_dependencies: tuple[Capability, ...]) -> bool:
        for dependency in hard_dependencies:
            latest = self.latest(dependency)
            if latest is None or step.based_on.get(dependency.value) != latest.version:
                return True
        return False

    def record(self, record: DecisionRecord) -> None:
        self.decision_history.append(record)
        del self.decision_history[:-MAX_DECISION_HISTORY]

    def add_gap(self, gap: str | None) -> None:
        if gap and gap not in self.pending_gaps:
            self.pending_gaps.append(_clip(gap, MAX_GAP_CHARS))
            del self.pending_gaps[:-MAX_PENDING_GAPS]

    def elapsed(self, clock: Callable[[], float] = time.monotonic) -> float:
        return clock() - self.started_at

    def remaining(self, clock: Callable[[], float] = time.monotonic) -> dict[str, float | int]:
        budget = self.execution_budget
        return {
            "decisions": max(budget.max_decisions - self.decision_number, 0),
            "executions": max(budget.max_executions - len(self.completed_steps), 0),
            "seconds": round(max(budget.max_seconds - self.elapsed(clock), 0.0), 1),
        }


def to_json(value: object) -> str:  # small helper kept here so prompts stay deterministic
    return json.dumps(value, sort_keys=True, separators=(",", ":"))
