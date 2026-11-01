"""Typed agent identity and runtime configuration.

Identity says who an agent is. Capabilities say what it is suited to do. The preferred
model family is a human-readable preference. Runtime configuration says where an agent
executes. These are kept apart, and none of them grants permissions.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

NonEmpty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
AgentId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,31}$")]
ModelAlias = Annotated[str, StringConstraints(pattern=r"^\S{1,200}$")]


class Capability(StrEnum):
    """Controlled vocabulary. A capability describes suitability, never authorization."""

    RESEARCH = "research"
    FACT_CHECK = "fact_check"
    SOURCE_QUALITY = "source_quality"
    SECURITY_ASSESSMENT = "security_assessment"
    PRIVACY_REVIEW = "privacy_review"
    SYNTHESIS = "synthesis"
    CREATIVE_DESIGN = "creative_design"
    PHILOSOPHICAL_ANALYSIS = "philosophical_analysis"
    LOGISTICS_PLANNING = "logistics_planning"
    AUTOMATION_ENGINEERING = "automation_engineering"
    EVIDENCE_DISCOVERY = "evidence_discovery"
    SLIDE_GENERATION = "slide_generation"


class CapabilityStatus(StrEnum):
    IMPLEMENTED = "implemented"  # an operational service already performs it
    PLANNED = "planned"  # declared only; nothing executes it yet


# "implemented" means an operational service exists for the capability. It does not mean
# the identity that declares it runs that service; identities are not yet connected to
# execution.
CAPABILITY_STATUS: dict[Capability, CapabilityStatus] = {
    capability: (
        CapabilityStatus.IMPLEMENTED
        if capability
        in {
            Capability.RESEARCH,
            Capability.FACT_CHECK,
            Capability.SOURCE_QUALITY,
            Capability.SECURITY_ASSESSMENT,
            Capability.PRIVACY_REVIEW,
            Capability.SYNTHESIS,
            Capability.SLIDE_GENERATION,
            Capability.PHILOSOPHICAL_ANALYSIS,
        }
        else CapabilityStatus.PLANNED
    )
    for capability in Capability
}


def capability_status(capability: Capability) -> CapabilityStatus:
    return CAPABILITY_STATUS[capability]


class AgentIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: AgentId
    name: NonEmpty
    title: NonEmpty
    description: NonEmpty
    personality: NonEmpty
    expertise: Annotated[tuple[NonEmpty, ...], Field(min_length=1)]
    capabilities: Annotated[tuple[Capability, ...], Field(min_length=1)]
    # Human-readable preference only; it is not a deployed or verified model.
    preferred_model_family: NonEmpty
    # LiteLLM alias, if one has been configured. Unset until a real alias exists.
    model_alias: ModelAlias | None = None
    system_prompt: NonEmpty

    @field_validator("capabilities")
    @classmethod
    def _capabilities_unique(cls, value: tuple[Capability, ...]) -> tuple[Capability, ...]:
        if len(set(value)) != len(value):
            raise ValueError("capabilities must not repeat")
        return value


class AgentRuntimeConfig(BaseModel):
    """Where an agent runs. Deliberately separate from AgentIdentity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: AgentId
    runtime_type: Literal["docker", "agentcore", "external"] | None = None
    endpoint: str | None = None
    model_alias: ModelAlias | None = None

    @field_validator("endpoint")
    @classmethod
    def _endpoint_has_no_credentials(cls, value: str | None) -> str | None:
        if value is None:
            return value
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https", "agentcore"} or not parts.netloc:
            raise ValueError("endpoint must be an http, https or agentcore URL")
        if "@" in parts.netloc or parts.query or parts.fragment:
            raise ValueError("endpoint must not embed credentials, query strings or fragments")
        return value
