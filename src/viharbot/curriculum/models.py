"""Typed curriculum records. Lesson text is data, never instructions."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

LessonId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")]
Sha = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20000)]
Short = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Difficulty(StrEnum):
    INTRODUCTORY = "introductory"
    INTERMEDIATE = "intermediate"
    ADVANCED = "advanced"


class ExerciseMode(StrEnum):
    SIMULATED = "simulated"  # nothing real is touched
    LIVE_SYSTEM = "live_system"  # runs against a real system


class TargetEnvironment(StrEnum):
    NONE = "none"
    DISPOSABLE = "disposable"  # throwaway clone or sandbox created for training
    STAGE = "stage"
    PRODUCTION = "production"


class Exercise(_Frozen):
    """`mode` is required. A live_system exercise needs an explicit authorization note and may
    never target production, authorized or not."""

    title: Short
    mode: ExerciseMode
    instructions: Text
    target_environment: TargetEnvironment = TargetEnvironment.NONE
    authorization_note: str | None = None

    @model_validator(mode="after")
    def _check_mode(self) -> Self:
        if self.target_environment is TargetEnvironment.PRODUCTION:
            raise ValueError("an exercise may never target production")
        if self.mode is ExerciseMode.SIMULATED:
            if self.target_environment is not TargetEnvironment.NONE:
                raise ValueError("a simulated exercise must have target_environment 'none'")
            return self
        if self.target_environment is TargetEnvironment.NONE:
            raise ValueError("a live_system exercise must name a non-production target")
        if not (self.authorization_note or "").strip():
            raise ValueError("a live_system exercise needs an explicit authorization_note")
        return self


class AssessmentKind(StrEnum):
    QUIZ = "quiz"
    CONCEPTUAL = "conceptual"
    PRACTICAL = "practical"
    ARCHITECTURE_CHALLENGE = "architecture_challenge"
    SCENARIO = "scenario"
    PROGRESSIVE = "progressive"
    FINAL_COMPETENCY = "final_competency"


class Assessment(_Frozen):
    kind: AssessmentKind
    prompt: Text
    criteria: tuple[Short, ...] = Field(min_length=1)


class InstructorReference(_Frozen):
    """Pins a lesson to one approved instructor artifact at one commit and version."""

    artifact_id: Short
    commit: Sha
    version: Short


class Lesson(_Frozen):
    """Exactly the 14 fields in the specification."""

    lesson_id: LessonId
    title: Short
    difficulty: Difficulty
    estimated_minutes: int = Field(ge=5, le=600)
    prerequisites: tuple[LessonId, ...] = ()
    learning_objectives: tuple[Short, ...] = Field(min_length=1)
    core_concepts: tuple[Short, ...] = Field(min_length=1)
    technical_explanation: Text
    practical_example: Text
    hands_on_exercise: Exercise
    knowledge_assessment: Assessment
    expected_outcomes: tuple[Short, ...] = Field(min_length=1)
    instructor_references: tuple[InstructorReference, ...] = ()  # empty is reported, not hidden
    curriculum_version: Short


class Track(_Frozen):
    track_id: LessonId
    title: Short
    status: Literal["proposed"] = "proposed"
    lesson_ids: tuple[LessonId, ...] = ()


class Curriculum(_Frozen):
    version: Short
    status: Literal["proposed"] = "proposed"
    tracks: tuple[Track, ...]


# PROPOSED, NOT APPROVED. The specification text with the eight tracks is not in this repository,
# so these titles are drawn from the topic areas Phase 2 already extracts (learning/extract.py)
# plus the existing Git-fundamentals lesson. A human must reconcile them with the spec and with
# the two onboarding repositories before any lesson is written. They hold no lessons.
PROPOSED_TRACKS: tuple[Track, ...] = tuple(
    Track(track_id=tid, title=title)
    for tid, title in (
        ("git-and-contribution", "Git and the contribution workflow"),
        ("architecture-decisions", "Architecture and decision records"),
        ("agent-identity-governance", "Agent identity and governance"),
        ("inference-and-litellm", "Model inference through LiteLLM"),
        ("orchestration", "Orchestration and delegation"),
        ("security-privacy", "Security and privacy"),
        ("observability-deployment", "Observability and deployment"),
        ("testing-quality", "Testing and quality assurance"),
    )
)
PROPOSED_CURRICULUM = Curriculum(version="0.0.0-proposed", tracks=PROPOSED_TRACKS)
