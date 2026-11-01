"""Agent response envelopes and boundary validation shared by agents and the orchestrator."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from .schemas import (
    DeckResult,
    FactCheckReview,
    FinalSynthesis,
    PrivacyAssessment,
    ResearchFindings,
    SecurityAssessment,
    SourceQualityReview,
)


class ResearchResponse(BaseModel):
    request_id: str
    agent: str = "research"
    requested_model_alias: str
    findings: ResearchFindings


class SecurityResponse(BaseModel):
    request_id: str
    agent: str = "security"
    requested_model_alias: str
    assessment: SecurityAssessment


class SpecialistResponse(BaseModel):
    request_id: str
    agent: str
    requested_model_alias: str
    result: dict[str, Any]


SPECIALIST_RESULT_MODELS: dict[str, type[BaseModel]] = {
    "fact-check": FactCheckReview,
    "source-quality": SourceQualityReview,
    "privacy-review": PrivacyAssessment,
    "synthesis": FinalSynthesis,
    "pixel": DeckResult,
}

AgentResponse = ResearchResponse | SecurityResponse | SpecialistResponse


class ResponseContractError(ValueError):
    """An agent response did not match the expected contract."""


def validate_agent_response(
    destination: str, raw: Any, request_id: str
) -> tuple[AgentResponse, BaseModel]:
    """Validate an agent's response envelope and its nested domain payload.

    Returns the typed envelope and the validated domain model. The error text never
    includes response content.
    """
    if not isinstance(raw, dict):
        raise ResponseContractError("response is not a JSON object")

    envelope: AgentResponse
    if destination == "research":
        envelope = ResearchResponse.model_validate(raw)
        domain: BaseModel = envelope.findings
    elif destination == "security":
        envelope = SecurityResponse.model_validate(raw)
        domain = envelope.assessment
    elif destination in SPECIALIST_RESULT_MODELS:
        envelope = SpecialistResponse.model_validate(raw)
        domain = SPECIALIST_RESULT_MODELS[destination].model_validate(envelope.result)
    else:
        raise ResponseContractError("unknown agent")

    if envelope.request_id != request_id:
        raise ResponseContractError("response request_id does not match the request")
    # The envelope models default `agent`, so check the raw payload to require it.
    if raw.get("agent") != destination:
        raise ResponseContractError("response agent does not match the destination")
    return envelope, domain
