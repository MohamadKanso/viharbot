"""Typed, versioned persona specification.

A persona describes how an agent approaches and communicates about work. It is descriptive
configuration only. It cannot grant tools, scopes, endpoints, model aliases or approvals:
every model forbids unknown fields, so a persona file that tries to carry one is rejected.
Identity (who) lives in `agent_identities/`; the persona (how) lives here; authorization
(what is allowed) lives in the application's bindings and policy and is never read from here.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from ..identity import AgentId

PERSONA_SCHEMA_VERSION = "1.0"

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=700)]
Short = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
Line = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Term = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]
SemVer = Annotated[str, StringConstraints(pattern=r"^\d{1,4}\.\d{1,5}\.\d{1,6}$")]
# A bare file name from the approved `images/` directory. Never a path or URL.
AvatarFile = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,80}\.(png|jpg|svg)$")]


class PersonaStatus(StrEnum):
    # Persona lifecycle only (aligned with #161). "implemented" and "activated" are deliberately
    # absent: whether an agent runs is decided by approved bindings and founder activation,
    # never by a field a persona file can set.
    DRAFT = "draft"  # authored, not yet submitted for review
    REVIEW = "review"  # submitted for founder review
    APPROVED = "approved"  # founder-approved; requires an approval reference


class CollaborationPattern(StrEnum):
    """How this agent prefers to work with a peer. Never a trust level or an access right."""

    REQUESTS_REVIEW_FROM = "requests_review_from"
    REVIEWS = "reviews"
    CHALLENGES = "challenges"
    DEFERS_TO = "defers_to"
    CONSULTS = "consults"
    HANDS_OFF_TO = "hands_off_to"


def _unique(values: tuple[str, ...], what: str) -> tuple[str, ...]:
    lowered = [value.lower() for value in values]
    if len(set(lowered)) != len(lowered):
        raise ValueError(f"{what} must not repeat")
    return values


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Provenance(_Strict):
    persona_version: SemVer
    schema_version: Literal["1.0"]
    status: PersonaStatus
    # Repository documents this persona was derived from. Informational, never fetched.
    sources: Annotated[tuple[Short, ...], Field(min_length=1, max_length=12)]
    # Required for `approved`, and forbidden before it, so an unapproved persona cannot pose as approved.
    approval_ref: Short | None = None

    @model_validator(mode="after")
    def _approval_matches_status(self) -> Provenance:
        if self.status is PersonaStatus.APPROVED and not self.approval_ref:
            raise ValueError("an approved persona needs an approval_ref")
        if self.status is not PersonaStatus.APPROVED and self.approval_ref:
            raise ValueError("only an approved persona may carry an approval_ref")
        return self


class Traits(_Strict):
    # Adjectives taken from the approved identity file; the registry checks they still appear there.
    identity_traits: Annotated[tuple[Term, ...], Field(min_length=2, max_length=8)]
    # Epic #157 design direction. Additive: it never replaces an identity trait.
    design_direction_traits: Annotated[tuple[Term, ...], Field(min_length=1, max_length=8)]
    behavioral_tendencies: Annotated[tuple[Line, ...], Field(min_length=2, max_length=8)]

    @field_validator("identity_traits", "design_direction_traits", "behavioral_tendencies")
    @classmethod
    def _no_repeats(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, "traits")


class Communication(_Strict):
    tone: Text
    sentence_style: Text
    preferred_vocabulary: Annotated[tuple[Term, ...], Field(min_length=4, max_length=16)]
    avoid: Annotated[tuple[Line, ...], Field(min_length=1, max_length=8)]
    # Illustrations of voice for reviewers and evaluation. They are not response templates and
    # must never be used to shape a structured or machine-readable message.
    illustrative_examples: Annotated[tuple[Text, ...], Field(min_length=1, max_length=4)]

    @field_validator("preferred_vocabulary", "illustrative_examples")
    @classmethod
    def _no_repeats(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, "communication entries")


class Reasoning(_Strict):
    approach: Text
    decision_preferences: Annotated[tuple[Line, ...], Field(min_length=2, max_length=6)]
    evidence_standard: Text


class PeerPattern(_Strict):
    agent_id: AgentId
    pattern: CollaborationPattern
    description: Text


class Disagreement(_Strict):
    # Constitution section 5.5: disagreement must rest on evidence. The schema accepts only true.
    # This is a configuration invariant only: it does not check that evidence is present,
    # relevant or true. That validation, and any behavioral evaluation, are future controls.
    requires_evidence: Literal[True]
    style: Text
    resolution_preference: Text
    escalation: Text


class SelfAssessment(_Strict):
    uncertainty_reporting: Text
    confidence_vocabulary: Annotated[tuple[Term, ...], Field(min_length=2, max_length=6)]


class ModelPreference(_Strict):
    # Descriptive only, and it must equal the identity's preferred_model_family. There is
    # deliberately no alias field: model selection stays with central LiteLLM governance.
    family: Short
    rationale: Text


class VisualIdentity(_Strict):
    avatar_file: AvatarFile
    alt_text: Text


class Persona(_Strict):
    agent_id: AgentId
    provenance: Provenance
    biography: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1200)]
    values: Annotated[tuple[Line, ...], Field(min_length=3, max_length=8)]
    principles: Annotated[tuple[Text, ...], Field(min_length=2, max_length=8)]
    traits: Traits
    communication: Communication
    reasoning: Reasoning
    strengths: Annotated[tuple[Line, ...], Field(min_length=2, max_length=8)]
    limitations: Annotated[tuple[Line, ...], Field(min_length=2, max_length=8)]
    collaboration: Annotated[tuple[PeerPattern, ...], Field(min_length=1, max_length=10)]
    disagreement: Disagreement
    self_assessment: SelfAssessment
    model_preference: ModelPreference
    visual_identity: VisualIdentity

    @field_validator("values", "principles", "strengths", "limitations")
    @classmethod
    def _no_repeats(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, "list entries")

    @model_validator(mode="after")
    def _peers_are_valid(self) -> Persona:
        seen: set[tuple[str, CollaborationPattern]] = set()
        for peer in self.collaboration:
            if peer.agent_id == self.agent_id:
                raise ValueError("collaboration must not name the agent itself")
            key = (peer.agent_id, peer.pattern)
            if key in seen:
                raise ValueError("collaboration entries must not repeat")
            seen.add(key)
        return self

    def content_digest(self) -> str:
        """SHA-256 of the canonical content, for provenance and change detection."""
        canonical = json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
