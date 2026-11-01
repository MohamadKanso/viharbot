"""Typed contracts for the Pixel slide pipeline. No model, network or file access here."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

SlideId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")]
DeckId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")]
Status = Literal["CRITICAL", "HIGH", "MEDIUM", "LOW", "NOT_ASSESSED"]
ClaimStatus = Literal["implemented", "proposed"]

GAP = "[to be supplied]"


class Cell(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    tokens: tuple[str, ...]
    badges: tuple[str, ...] = ()


class LayoutSpec(BaseModel):
    """One layout: the only tokens and badges a slide of this layout may carry."""

    model_config = ConfigDict(frozen=True)

    layout_id: str
    theme: Literal["dark", "light"]
    purpose: str
    grid_cols: int = 3
    # The first N cells are laid out as one compact band across the top; the rest fill the grid.
    header_cells: int = 0
    word_limits: dict[str, int] = Field(default_factory=dict)
    cells: tuple[Cell, ...]

    @property
    def tokens(self) -> tuple[str, ...]:
        """Header tokens first, then every cell token, in order."""
        return ("SLIDE_TAG", "SLIDE_TITLE", *(t for c in self.cells for t in c.tokens))

    @property
    def badge_tokens(self) -> tuple[str, ...]:
        return tuple(b for c in self.cells for b in c.badges)


class EngagementProfile(BaseModel):
    """Per-deck context supplied by a person. Treated as untrusted text in prompts."""

    model_config = ConfigDict(extra="forbid")

    scope: str = Field(max_length=500)
    audience: str = Field(default="CISO and board", max_length=200)
    risk_appetite: str | None = Field(default=None, max_length=300)
    mandates: list[Annotated[str, StringConstraints(max_length=120)]] = Field(
        default_factory=list, max_length=12
    )


class SlideStub(BaseModel):
    slide_id: SlideId
    layout_id: str
    title: str = Field(max_length=120)
    purpose: str = Field(max_length=200)
    frameworks: list[str] = Field(default_factory=list, max_length=6)
    evidence_refs: list[str] = Field(default_factory=list, max_length=6)


class Section(BaseModel):
    section_id: SlideId
    title: str = Field(max_length=120)
    theme: Literal["dark", "light"]
    slides: list[SlideStub]


class DeckManifest(BaseModel):
    deck_id: DeckId
    version: str = "0.1"
    sections: list[Section]

    def stubs(self) -> list[SlideStub]:
        return [s for section in self.sections for s in section.slides]


class SlidePayload(BaseModel):
    slide_id: SlideId
    layout_id: str
    tokens: dict[str, str]
    status_overrides: dict[str, Status] = Field(default_factory=dict)
    claim_status: ClaimStatus
    evidence_refs: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class SlideBatch(BaseModel):
    slides: list[SlidePayload]


class Issue(BaseModel):
    slide_id: str
    severity: Literal["error", "warning"]
    message: str


class RenderResult(BaseModel):
    deck_id: str
    accepted: list[str]
    rejected: list[str]
    issues: list[Issue]
