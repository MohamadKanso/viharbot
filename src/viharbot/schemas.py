"""Typed JSON contracts exchanged by ViharBot agents."""

from typing import Literal

from pydantic import BaseModel, Field


class Finding(BaseModel):
    claim: str = Field(description="One concise research finding")
    evidence: str = Field(description="Reasoning or source detail supporting the claim")
    confidence: Literal["low", "medium", "high"]


class ResearchFindings(BaseModel):
    summary: str
    findings: list[Finding]
    sources: list[str] = Field(description="Sources or references, if known")
    uncertainties: list[str]


class SecurityAssessment(BaseModel):
    overall_risk: Literal["low", "medium", "high"]
    implications: list[str]
    recommendations: list[str]
    uncertainties: list[str]


class ClaimReview(BaseModel):
    claim: str
    status: Literal[
        "supported_by_supplied_evidence",
        "contradicted_by_supplied_evidence",
        "mixed_evidence",
        "insufficient_evidence",
    ]
    rationale: str
    follow_up: str
    # Optional so older results and other agents validate unchanged. The reviewer proposes the
    # quote; the application sets citation_verified by checking it against the supplied evidence.
    evidence_quote: str | None = Field(default=None, max_length=400)
    citation_verified: bool | None = None


class FactCheckReview(BaseModel):
    summary: str
    claims: list[ClaimReview]
    limitations: list[str]


class SourceReview(BaseModel):
    source: str
    apparent_quality: Literal["strong", "moderate", "weak", "unknown"]
    rationale: str
    checks_to_perform: list[str]


class SourceQualityReview(BaseModel):
    summary: str
    sources: list[SourceReview]
    limitations: list[str]


class PrivacyAssessment(BaseModel):
    overall_risk: Literal["low", "medium", "high"]
    sensitive_data_categories: list[str]
    concerns: list[str]
    recommendations: list[str]
    processing_note: str


class FinalSynthesis(BaseModel):
    answer: str
    key_points: list[str]
    confidence: Literal["low", "medium", "high"]
    caveats: list[str]


class DeckResult(BaseModel):
    """What the pixel service reports about a generated slide deck. No slide text."""

    deck_id: str
    slide_count: int
    rejected_slides: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    model_calls: int


# ---- Sage: philosophical analysis ---------------------------------------------------------------
# Every claim says what kind of claim it is, so the reader can tell a finding from a judgment.
SageClaimType = Literal[
    "evidence_supported",  # a supplied context section states it (named in evidence_refs)
    "reasoned_interpretation",  # an inference from the evidence or the question
    "normative_judgment",  # a value judgment, resting on a stated framework or assumption
    "recommendation",  # a proposed action
]


class SageStakeholder(BaseModel):
    name: str = Field(max_length=120)
    interest: str = Field(max_length=400)
    potential_impact: str = Field(max_length=400)
    impact_direction: Literal["benefit", "harm", "mixed", "unclear"]


class SageClaim(BaseModel):
    """Shared shape of a typed claim. `evidence_refs` name supplied context sections only."""

    statement: str = Field(max_length=600)
    claim_type: SageClaimType
    evidence_refs: list[str] = Field(default_factory=list, max_length=6)


class SageEthicalConsideration(SageClaim):
    framework: str | None = Field(
        default=None, max_length=120, description="The ethical framework or assumption relied on"
    )


class SageHumanImpact(SageClaim):
    affected_group: str = Field(max_length=120)


class SageTradeOff(SageClaim):
    competing_values: list[str] = Field(min_length=2, max_length=4)


class SageMitigation(BaseModel):
    action: str = Field(max_length=400)
    addresses: str = Field(max_length=300)


class SageAnalysis(BaseModel):
    """Sage's structured result: perspective on a request, never evidence of its own."""

    executive_summary: str = Field(max_length=1500)
    stakeholders: list[SageStakeholder] = Field(max_length=12)
    ethical_considerations: list[SageEthicalConsideration] = Field(max_length=12)
    human_and_organizational_impacts: list[SageHumanImpact] = Field(max_length=12)
    risks_and_tradeoffs: list[SageTradeOff] = Field(max_length=10)
    recommended_mitigations: list[SageMitigation] = Field(max_length=12)
    assumptions_and_uncertainties: list[str] = Field(max_length=14)
    recommendations_for_charlie: list[str] = Field(max_length=8)
