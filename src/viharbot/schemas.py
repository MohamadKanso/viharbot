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
