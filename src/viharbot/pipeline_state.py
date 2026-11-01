"""Typed pipeline state and legacy payload serializers for the orchestrator."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from .contracts import AgentResponse, SecurityResponse
from .schemas import (
    FactCheckReview,
    FinalSynthesis,
    PrivacyAssessment,
    ResearchFindings,
    SageAnalysis,
    SecurityAssessment,
    SourceQualityReview,
)


class AgentActivity(BaseModel):
    """Execution metadata for one agent call; kept apart from domain data."""

    agent: str
    display_name: str
    model_alias: str | None = None
    duration_ms: float
    status: Literal["complete", "failed"]
    error: str | None = None

    def to_legacy(self) -> dict[str, Any]:
        # Completed calls never carried an `error` key.
        return self.model_dump(exclude={"error"} if self.error is None else set())


class PipelineContext(BaseModel):
    """Normalized domain results. Holds no response envelopes."""

    research: ResearchFindings
    fact_check: FactCheckReview | None = None
    source_quality: SourceQualityReview | None = None
    security: SecurityAssessment | None = None
    privacy_review: PrivacyAssessment | None = None
    sage: SageAnalysis | None = None


class PipelineState(BaseModel):
    request_id: str
    context: PipelineContext
    synthesis: FinalSynthesis | None = None
    # Complete envelopes keyed by legacy result name, in invocation order.
    responses: dict[str, AgentResponse]
    agent_activity: list[AgentActivity] = Field(default_factory=list)

    def to_legacy_dict(self) -> dict[str, Any]:
        """The dictionary run_pipeline() has always returned."""
        return {
            "request_id": self.request_id,
            "agent_activity": [item.to_legacy() for item in self.agent_activity],
            **{name: response.model_dump() for name, response in self.responses.items()},
        }


def legacy_findings_payload(findings: ResearchFindings) -> dict[str, Any]:
    """Outbound payload for the fact-check, source-quality and security agents."""
    return {"findings": findings.model_dump()}


def fact_check_payload(question: str, findings: ResearchFindings) -> dict[str, Any]:
    """Outbound payload for the fact-check agent: the findings plus the original question."""
    return {"question": question, "findings": findings.model_dump()}


def legacy_context_payload(responses: dict[str, AgentResponse]) -> dict[str, Any]:
    """Outbound context for the privacy-review and synthesis agents.

    Specialists contribute their bare result, but security contributes its complete
    response envelope. That mix is the existing wire format and is kept deliberately.
    """
    context: dict[str, Any] = {"research": responses["research"].findings.model_dump()}
    for name, response in responses.items():
        if name == "research":
            continue
        context[name] = (
            response.model_dump() if isinstance(response, SecurityResponse) else response.result
        )
    return context
