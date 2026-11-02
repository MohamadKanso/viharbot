"""The live event contract, schema version 1.0.

Fourteen typed events share one envelope and form a strict discriminated union on `event_type`.
Every event is built from real orchestration facts, validated here, and only then serialized.
Nothing in this module invents activity: it only describes what the application did.

Ordering is by `sequence`, never by timestamp. Unknown fields are rejected everywhere.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from .chat_models import ChatResponse
from .identity_registry import default_registry
from .orchestration_policy import CapabilityName
from .orchestration_view import CompletionReason

SCHEMA_VERSION = "1.0"
APPLICATION_IDENTITY = "application"  # the documented system identity for policy and terminal events
APPLICATION_NAME = "ViharBot"

Service = Literal[
    "research", "fact-check", "source-quality", "security", "privacy-review", "synthesis", "pixel",
    "sage"]
# Pixel's slide generation runs after the loop, so the loop's own CapabilityName does not include it.
# Sage's philosophical_analysis is a loop capability, so CapabilityName already carries it.
EventCapability = CapabilityName | Literal["slide_generation"]

# Sanitized, documented codes. No free text, no exception names, no URLs.
PlannerErrorCode = Literal[
    "planner_unavailable", "planner_inference_failed", "planner_output_invalid", "deadline_exceeded"]
RejectionCode = Literal[
    "not_a_delegation", "unknown_capability", "capability_not_implemented", "no_binding",
    "binding_disabled", "binding_mismatch", "identity_invalid", "service_unavailable",
    "dependency_unmet", "dependency_stale", "repeat_limit", "execution_budget", "time_budget",
    "mandatory_missing", "security_stale", "deadline_exceeded"]
AgentErrorCode = Literal["agent_execution_failed", "agent_response_invalid", "deadline_exceeded"]
FailureCode = Literal[
    "planner_unavailable", "planner_inference_failed", "planner_output_invalid",
    "agent_execution_failed", "agent_response_invalid", "deadline_exceeded",
    "orchestration_incomplete", "cancelled", "internal_error"]

FAILURE_MESSAGES: dict[str, str] = {
    "planner_unavailable": "Charlie's planning is not available.",
    "planner_inference_failed": "Charlie's planning call did not complete.",
    "planner_output_invalid": "Charlie's planning response was not valid.",
    "agent_execution_failed": "An agent call did not complete.",
    "agent_response_invalid": "An agent returned a response that failed validation.",
    "deadline_exceeded": "The request time limit was reached.",
    "orchestration_incomplete": "The orchestration ended without the required results.",
    "cancelled": "The request was cancelled.",
    "internal_error": "The orchestration could not be completed.",
}


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _registered(identity_id: str) -> str:
    if identity_id not in default_registry():
        raise ValueError("identity is not registered")
    return identity_id


class EventSource(Strict):
    identity_id: str
    display_name: str
    role: Literal["orchestrator", "specialist", "application"]

    @model_validator(mode="after")
    def _known_identity(self) -> "EventSource":
        if self.role == "application":
            if (self.identity_id, self.display_name) != (APPLICATION_IDENTITY, APPLICATION_NAME):
                raise ValueError("application events use the system identity")
            return self
        identity = default_registry().get(_registered(self.identity_id))
        if self.display_name != identity.name:
            raise ValueError("display name does not match the registered identity")
        if self.role == "orchestrator" and self.identity_id != "charlie":
            raise ValueError("only Charlie orchestrates")
        return self


class Correlation(Strict):
    decision_number: int | None = Field(default=None, ge=1)
    execution_id: str | None = Field(default=None, pattern=r"^exec-\d+$")
    parent_event_id: str | None = Field(default=None, pattern=r"^evt-\d{6}$")


class ResultRef(Strict):
    identity_id: str
    capability: EventCapability
    result_version: int = Field(ge=1)

    _check = field_validator("identity_id")(lambda v: _registered(v))


class DependencyRef(ResultRef):
    dependency_type: Literal["required", "optional"]


_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")


class _Envelope(Strict):
    schema_version: Literal["1.0"]
    event_id: str = Field(pattern=r"^evt-\d{6}$")
    request_id: str = Field(min_length=1, max_length=128)
    sequence: int = Field(ge=1)
    timestamp: str
    source: EventSource
    correlation: Correlation

    @field_validator("timestamp")
    @classmethod
    def _utc_iso(cls, value: str) -> str:
        if not _TIMESTAMP.match(value):
            raise ValueError("timestamp must be ISO 8601 UTC ending in Z")
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value

    @model_validator(mode="after")
    def _id_matches_sequence(self) -> "_Envelope":
        if int(self.event_id.removeprefix("evt-")) != self.sequence:
            raise ValueError("event_id must encode the sequence number")
        if self.correlation.parent_event_id is not None:
            if int(self.correlation.parent_event_id.removeprefix("evt-")) >= self.sequence:
                raise ValueError("an event can only follow a parent that came before it")
        return self


# ---- event data -------------------------------------------------------------------------------
class PlannerStartedData(Strict):
    status: Literal["running"]
    remaining_budget_ms: int = Field(ge=0)


class PlannerCompletedData(Strict):
    status: Literal["completed"]
    duration_ms: float = Field(ge=0)
    action: Literal["delegate", "finish"]
    capability: EventCapability | None
    # Developer diagnostics only (bounded, validated, never hidden reasoning):
    rationale: str | None = Field(default=None, max_length=300)
    model_alias: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _finish_names_no_capability(self) -> "PlannerCompletedData":
        if (self.action == "finish") != (self.capability is None):
            raise ValueError("finish has no capability; delegate needs one")
        return self


class PlannerFailedData(Strict):
    status: Literal["failed"]
    error_code: PlannerErrorCode
    duration_ms: float = Field(ge=0)


class DelegationRequestedData(Strict):
    status: Literal["requested"]
    capability: EventCapability
    target_identity: str

    _check = field_validator("target_identity")(lambda v: _registered(v))


class DelegationAuthorizedData(Strict):
    status: Literal["authorized"]
    capability: EventCapability
    target_identity: str
    service: Service
    result_version: int = Field(ge=1)

    _check = field_validator("target_identity")(lambda v: _registered(v))


class DelegationRejectedData(Strict):
    status: Literal["rejected"]
    capability: EventCapability | None  # null only when a finish request was refused
    rejection_code: RejectionCode


class AgentStartedData(Strict):
    status: Literal["running"]
    capability: EventCapability
    service: Service
    result_version: int = Field(ge=1)


class AgentCompletedData(Strict):
    status: Literal["completed"]
    capability: EventCapability
    result_version: int = Field(ge=1)
    duration_ms: float = Field(ge=0)
    validated: Literal[True]
    # Developer diagnostics only: the alias the service itself reported using.
    model_alias: str | None = Field(default=None, max_length=200)


class AgentFailedData(Strict):
    status: Literal["failed"]
    capability: EventCapability
    error_code: AgentErrorCode
    duration_ms: float = Field(ge=0)


class DependencyRecordedData(Strict):
    consumer: ResultRef
    dependencies: list[DependencyRef] = Field(min_length=1)


class EvidenceSupersededData(Strict):
    capability: EventCapability
    superseded_version: int = Field(ge=1)
    replacement_version: int = Field(ge=2)
    affected_results: list[ResultRef] = Field(min_length=1)

    @model_validator(mode="after")
    def _replacement_is_newer(self) -> "EvidenceSupersededData":
        if self.replacement_version <= self.superseded_version:
            raise ValueError("the replacement must be a newer version")
        return self


class FinalResponseData(Strict):
    response: ChatResponse

    @field_validator("response", mode="before")
    @classmethod
    def _no_unknown_top_level_fields(cls, value):
        # ChatResponse (frozen public contract) ignores extras; the event contract does not.
        if isinstance(value, dict) and set(value) - set(ChatResponse.model_fields):
            raise ValueError("the response has fields that are not part of ChatResponse")
        return value


class OrchestrationCompletedData(Strict):
    status: Literal["completed", "budget_exhausted"]
    completion_kind: Literal["charlie_approved", "policy_budget"]
    completion_reason: CompletionReason
    decisions: int = Field(ge=0)
    executions: int = Field(ge=0)
    elapsed_ms: float = Field(ge=0)

    @model_validator(mode="after")
    def _kind_status_reason_agree(self) -> "OrchestrationCompletedData":
        charlie = self.completion_kind == "charlie_approved"
        if charlie != (self.status == "completed") or charlie != (self.completion_reason == "charlie_finished"):
            raise ValueError("completion kind, status and reason disagree")
        return self


class OrchestrationFailedData(Strict):
    status: Literal["failed"]
    error_code: FailureCode
    message: str = Field(max_length=200)
    elapsed_ms: float = Field(ge=0)


# ---- the fourteen events ----------------------------------------------------------------------
class PlannerStarted(_Envelope):
    event_type: Literal["planner_started"]
    data: PlannerStartedData


class PlannerCompleted(_Envelope):
    event_type: Literal["planner_completed"]
    data: PlannerCompletedData


class PlannerFailed(_Envelope):
    event_type: Literal["planner_failed"]
    data: PlannerFailedData


class DelegationRequested(_Envelope):
    event_type: Literal["delegation_requested"]
    data: DelegationRequestedData


class DelegationAuthorized(_Envelope):
    event_type: Literal["delegation_authorized"]
    data: DelegationAuthorizedData


class DelegationRejected(_Envelope):
    event_type: Literal["delegation_rejected"]
    data: DelegationRejectedData


class AgentStarted(_Envelope):
    event_type: Literal["agent_started"]
    data: AgentStartedData


class AgentCompleted(_Envelope):
    event_type: Literal["agent_completed"]
    data: AgentCompletedData


class AgentFailed(_Envelope):
    event_type: Literal["agent_failed"]
    data: AgentFailedData


class DependencyRecorded(_Envelope):
    event_type: Literal["dependency_recorded"]
    data: DependencyRecordedData


class EvidenceSuperseded(_Envelope):
    event_type: Literal["evidence_superseded"]
    data: EvidenceSupersededData


class FinalResponse(_Envelope):
    event_type: Literal["final_response"]
    data: FinalResponseData


class OrchestrationCompleted(_Envelope):
    event_type: Literal["orchestration_completed"]
    data: OrchestrationCompletedData


class OrchestrationFailed(_Envelope):
    event_type: Literal["orchestration_failed"]
    data: OrchestrationFailedData


LiveEvent = Annotated[
    Union[
        PlannerStarted, PlannerCompleted, PlannerFailed, DelegationRequested, DelegationAuthorized,
        DelegationRejected, AgentStarted, AgentCompleted, AgentFailed, DependencyRecorded,
        EvidenceSuperseded, FinalResponse, OrchestrationCompleted, OrchestrationFailed,
    ],
    Field(discriminator="event_type"),
]

EVENT_TYPES: tuple[str, ...] = (
    "planner_started", "planner_completed", "planner_failed", "delegation_requested",
    "delegation_authorized", "delegation_rejected", "agent_started", "agent_completed",
    "agent_failed", "dependency_recorded", "evidence_superseded", "final_response",
    "orchestration_completed", "orchestration_failed")
TERMINAL_EVENT_TYPES = frozenset({"orchestration_completed", "orchestration_failed"})

_adapter = TypeAdapter(LiveEvent)


def parse_event(payload: object) -> "LiveEvent":
    """Validate a decoded JSON object against the registry. Raises ValidationError."""
    return _adapter.validate_python(payload)


def serialize_event(event: "LiveEvent") -> str:
    """One line of JSON. Newlines inside strings are escaped, so a frame cannot be split."""
    text = event.model_dump_json(exclude_unset=True)
    assert "\n" not in text and "\r" not in text
    return text


def utc_timestamp(moment: datetime | None = None) -> str:
    moment = moment or datetime.now(timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class StreamInvariantError(ValueError):
    """A sequence of events breaks the documented stream rules."""


def check_stream(events: list, *, require_terminal: bool = True) -> str | None:
    """Verify the rules every stream must satisfy. Returns the terminal event type, or None.

    Rules: one request, sequence starting at 1 with no gaps, ids that match, every parent earlier,
    at most one final_response, exactly one terminal event and nothing after it, completion only
    after final_response, and no final_response on a failed run.
    """
    if not events:
        raise StreamInvariantError("a stream has at least one event")
    request_ids = {event.request_id for event in events}
    if len(request_ids) != 1:
        raise StreamInvariantError("a stream belongs to one request")
    seen: set[str] = set()
    terminal: str | None = None
    final_seen = False
    for index, event in enumerate(events, start=1):
        if event.sequence != index:
            raise StreamInvariantError(f"sequence must run 1..n without gaps (saw {event.sequence} at {index})")
        parent = event.correlation.parent_event_id
        if parent is not None and parent not in seen:
            raise StreamInvariantError("a parent event must appear earlier in the stream")
        seen.add(event.event_id)
        if terminal is not None:
            raise StreamInvariantError("no events may follow a terminal event")
        if event.event_type == "final_response":
            if final_seen:
                raise StreamInvariantError("only one final_response is allowed")
            final_seen = True
        if event.event_type == "orchestration_completed":
            if not final_seen or events[index - 2].event_type != "final_response":
                raise StreamInvariantError("orchestration_completed must immediately follow final_response")
        if event.event_type == "orchestration_failed" and final_seen:
            raise StreamInvariantError("a failed run must not have a final_response")
        if event.event_type in TERMINAL_EVENT_TYPES:
            terminal = event.event_type
    if final_seen and terminal != "orchestration_completed":
        raise StreamInvariantError("a final_response must be followed by orchestration_completed")
    if require_terminal and terminal is None:
        raise StreamInvariantError("a complete stream ends with one terminal event")
    return terminal
