"""Typed records for the learning pipeline. Source text is data, never instructions."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Repo = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")]
Sha = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]


class ChangedPath(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    path: str
    status: str
    additions: int = 0
    deletions: int = 0


class PRSnapshot(BaseModel):
    """What was retrieved. Patches are deliberately not kept; only paths and counts are."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    repository: Repo
    number: int = Field(ge=1)
    title: str
    body: str
    merged_at: datetime
    merge_commit_sha: Sha
    base_ref: str
    files: tuple[ChangedPath, ...]
    commit_subjects: tuple[str, ...] = ()
    discussion: tuple[str, ...] = ()  # review and issue comments, untrusted
    software_version: str = "unknown"
    files_omitted: int = 0  # > 0 means the evidence is incomplete


class Verification(StrEnum):
    UNVERIFIED = "unverified"  # extracted from one source, no human or second-source check
    EVIDENCE_CHECKED = "evidence_checked"  # every claim maps to a retrieved source
    APPROVED = "approved"  # a human merged the instructor PR; only a human sets this


class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    question: str
    answer: str  # "Not stated in the evidence." when nothing supports an answer
    basis: str  # "author_statement" | "verified_in_diff" | "not_available"
    source: str


class LearningArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    artifact_id: str
    topic: str
    topics: tuple[str, ...]
    learning_objectives: tuple[str, ...]  # drafts for human refinement
    source_repository: Repo
    pull_request: int
    merge_commit_sha: Sha
    source_references: tuple[str, ...]
    extracted_on: str
    software_version: str
    verification: Verification
    prerequisites: tuple[str, ...]
    curriculum_placement: str
    claims: tuple[Claim, ...]
    evidence_complete: bool
    discussion_excerpts: tuple[str, ...] = ()


class IngestState(StrEnum):
    RECEIVED = "received"
    PROCESSING = "processing"
    EXTRACTED = "extracted"
    PROPOSED = "proposed"
    FAILED = "failed"


class IngestionEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_key: str
    repository: Repo
    pull_request: int
    merge_commit_sha: Sha
    state: IngestState
    attempts: int = 0
    artifact_id: str | None = None
    proposal_url: str | None = None
    error: str | None = None  # an exception class name only, never a message or response body
    updated_at: str
