"""The public chat request and response models, shared by /api/chat and the live event stream.

They live here, not in web_app.py, so the event contract can reuse the real ChatResponse without
importing the web application. web_app re-exports them, so existing imports are unchanged.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .orchestration_view import OrchestrationMeta
from .schemas import (
    FactCheckReview,
    FinalSynthesis,
    PrivacyAssessment,
    ResearchFindings,
    SageAnalysis,
    SecurityAssessment,
    SourceQualityReview,
)


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=500)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=3000)
    history: list[ChatTurn] = Field(default_factory=list, max_length=4)


class AgentActivity(BaseModel):
    agent: str
    display_name: str
    model_alias: str | None = None
    duration_ms: float
    status: Literal["complete", "failed"]


class DeckInfo(BaseModel):
    """Public result of the optional slide-deck step. Never contains slide text."""

    status: Literal["complete", "failed"]
    deck_id: str | None = None
    slide_count: int | None = None
    download_url: str | None = None
    view_url: str | None = None
    rejected_slides: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None


class ChatResponse(BaseModel):
    request_id: str
    findings: ResearchFindings
    assessment: SecurityAssessment
    fact_check: FactCheckReview | None = None
    source_quality: SourceQualityReview | None = None
    privacy_review: PrivacyAssessment | None = None
    # Present only when Charlie delegated philosophical analysis and the result is current.
    sage: SageAnalysis | None = None
    synthesis: FinalSynthesis | None = None
    agent_activity: list[AgentActivity]
    # Present only for iterative orchestration; omitted from every other response.
    orchestration: OrchestrationMeta | None = None
    # Present only when the PPT flag is on and the request asked for a deck.
    deck: DeckInfo | None = None
