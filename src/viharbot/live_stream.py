"""Request-scoped live events: the emitter, SSE framing and the cancellable stream runner.

One EventEmitter exists per request. It owns the sequence counter, validates every event against
the schema before it leaves, and enforces the terminal rules, so a stream can never contain
events after a terminal one, two terminals, or a final_response on a failed run. The runner executes
the orchestration on its own thread and hands events to the response through a bounded queue.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections import Counter
from typing import Any, AsyncIterator, Callable

from pydantic import ValidationError

from .chat_models import ChatResponse
from .deadline import DeadlineExceeded
from .delegation import PlannerInferenceError, PlannerOutputError, PlannerUnavailableError
from .demo import AgentPipelineError
from .identity_registry import default_registry
from .iterative_execution import OrchestrationFailed as IncompleteOrchestration
from .iterative_execution import agent_error_code
from .live_events import (
    APPLICATION_IDENTITY,
    APPLICATION_NAME,
    FAILURE_MESSAGES,
    SCHEMA_VERSION,
    TERMINAL_EVENT_TYPES,
    parse_event,
    serialize_event,
    utc_timestamp,
)
from .live_hooks import CancelToken, LiveReporter, OrchestrationCancelled, StreamClosed

logger = logging.getLogger("web")

QUEUE_SIZE = 256  # far above any real run (a few dozen events); a full queue means a stuck client
SINK_TIMEOUT_SECONDS = 10.0
KEEPALIVE_SECONDS = 15.0
MAX_CONCURRENT_STREAMS = 8
DIAGNOSTICS_FLAG = "VIHARBOT_DEV_DIAGNOSTICS_ENABLED"
# `cancelled` is declared in the schema but can never be delivered: a run is cancelled only when the
# client has disconnected or stopped reading, so there is nobody to send it to. The emitter refuses
# it, which keeps the documented behaviour ("a cancelled run emits no terminal event") enforced.
NEVER_EMITTED_FAILURE_CODES = frozenset({"cancelled"})

_streams = threading.BoundedSemaphore(MAX_CONCURRENT_STREAMS)


def dev_diagnostics_enabled() -> bool:
    return os.getenv(DIAGNOSTICS_FLAG, "false").strip().lower() in {"1", "true", "yes"}


class EventContractError(RuntimeError):
    """The application tried to emit an event that breaks the schema or the terminal rules."""


class EventEmitter(LiveReporter):
    def __init__(
        self,
        request_id: str,
        sink: Callable[[Any], None],
        *,
        diagnostics: bool = False,
        clock: Callable[[], float] = time.monotonic,
        timestamp: Callable[[], str] = utc_timestamp,
    ) -> None:
        self.request_id = request_id
        self._sink = sink
        self._diagnostics = diagnostics
        self._clock = clock
        self._timestamp = timestamp
        self._started = clock()
        self._sequence = 0
        self._ids: dict[tuple[str, int], str] = {}
        self._last_id: str | None = None
        self._terminal: str | None = None
        self._final_sent = False
        self.counts: Counter[str] = Counter()

    # ---- helpers -------------------------------------------------------------------------
    @staticmethod
    def _source(identity_id: str, role: str) -> dict[str, str]:
        if role == "application":
            return {"identity_id": APPLICATION_IDENTITY, "display_name": APPLICATION_NAME, "role": role}
        return {"identity_id": identity_id, "display_name": default_registry().get(identity_id).name, "role": role}

    def _emit(
        self, event_type: str, source: dict[str, str], data: dict[str, Any], *,
        decision: int | None = None, execution: str | None = None, parent: str | None = None,
    ) -> str:
        if self._terminal is not None:
            raise EventContractError("no events may follow a terminal event")
        sequence = self._sequence + 1
        payload = {
            "schema_version": SCHEMA_VERSION,
            "event_id": f"evt-{sequence:06d}",
            "request_id": self.request_id,
            "sequence": sequence,
            "timestamp": self._timestamp(),
            "event_type": event_type,
            "source": source,
            "correlation": {"decision_number": decision, "execution_id": execution, "parent_event_id": parent},
            "data": data,
        }
        try:
            event = parse_event(payload)
        except ValidationError as exc:  # a bug in the application, never user input
            raise EventContractError(f"invalid {event_type} event ({exc.error_count()} errors)") from None
        self._sequence = sequence
        self._last_id = event.event_id
        self.counts[event_type] += 1
        if event_type in TERMINAL_EVENT_TYPES:
            self._terminal = event_type
        self._sink(event)
        return event.event_id

    def _remember(self, kind: str, decision: int, event_id: str) -> None:
        self._ids[(kind, decision)] = event_id

    def _recall(self, decision: int, *kinds: str) -> str | None:
        for kind in kinds:
            if (kind, decision) in self._ids:
                return self._ids[(kind, decision)]
        return None

    def _charlie(self) -> dict[str, str]:
        return self._source("charlie", "orchestrator")

    # ---- planner ---------------------------------------------------------------------------
    def planner_started(self, *, decision_number: int, remaining_ms: int) -> None:
        event_id = self._emit("planner_started", self._charlie(),
                              {"status": "running", "remaining_budget_ms": max(int(remaining_ms), 0)},
                              decision=decision_number)
        self._remember("planner_started", decision_number, event_id)

    def planner_completed(self, *, decision_number, duration_ms, action, capability, rationale, model_alias):
        data: dict[str, Any] = {"status": "completed", "duration_ms": round(max(duration_ms, 0), 2),
                                "action": action, "capability": capability}
        if self._diagnostics:
            if rationale:
                data["rationale"] = rationale
            if model_alias:
                data["model_alias"] = model_alias
        event_id = self._emit("planner_completed", self._charlie(), data, decision=decision_number,
                              parent=self._recall(decision_number, "planner_started"))
        self._remember("planner_completed", decision_number, event_id)

    def planner_failed(self, *, decision_number, error_code, duration_ms):
        self._emit("planner_failed", self._charlie(),
                   {"status": "failed", "error_code": error_code, "duration_ms": round(max(duration_ms, 0), 2)},
                   decision=decision_number, parent=self._recall(decision_number, "planner_started"))

    # ---- delegation ------------------------------------------------------------------------
    def delegation_requested(self, *, decision_number, capability, target_identity):
        event_id = self._emit(
            "delegation_requested", self._charlie(),
            {"status": "requested", "capability": capability, "target_identity": target_identity},
            decision=decision_number, parent=self._recall(decision_number, "planner_completed"))
        self._remember("delegation_requested", decision_number, event_id)

    def delegation_authorized(self, *, decision_number, execution_id, capability, target_identity, service, result_version):
        event_id = self._emit(
            "delegation_authorized", self._source(APPLICATION_IDENTITY, "application"),
            {"status": "authorized", "capability": capability, "target_identity": target_identity,
             "service": service, "result_version": result_version},
            decision=decision_number, execution=execution_id,
            parent=self._recall(decision_number, "delegation_requested", "planner_completed"))
        self._remember("delegation_authorized", decision_number, event_id)

    def delegation_rejected(self, *, decision_number, capability, rejection_code):
        self._emit(
            "delegation_rejected", self._source(APPLICATION_IDENTITY, "application"),
            {"status": "rejected", "capability": capability, "rejection_code": rejection_code},
            decision=decision_number,
            parent=self._recall(decision_number, "delegation_requested", "planner_completed"))

    # ---- agents ----------------------------------------------------------------------------
    def agent_started(self, *, decision_number, execution_id, identity_id, capability, service, result_version):
        event_id = self._emit(
            "agent_started", self._source(identity_id, "specialist"),
            {"status": "running", "capability": capability, "service": service, "result_version": result_version},
            decision=decision_number, execution=execution_id,
            parent=self._recall(decision_number, "delegation_authorized"))
        self._remember("agent_started", decision_number, event_id)

    def agent_completed(self, *, decision_number, execution_id, identity_id, capability, result_version, duration_ms, model_alias):
        data: dict[str, Any] = {"status": "completed", "capability": capability, "result_version": result_version,
                                "duration_ms": round(max(duration_ms, 0), 2), "validated": True}
        if self._diagnostics and model_alias:
            data["model_alias"] = model_alias
        event_id = self._emit("agent_completed", self._source(identity_id, "specialist"), data,
                              decision=decision_number, execution=execution_id,
                              parent=self._recall(decision_number, "agent_started"))
        self._remember("agent_completed", decision_number, event_id)

    def agent_failed(self, *, decision_number, execution_id, identity_id, capability, error_code, duration_ms):
        self._emit("agent_failed", self._source(identity_id, "specialist"),
                   {"status": "failed", "capability": capability, "error_code": error_code,
                    "duration_ms": round(max(duration_ms, 0), 2)},
                   decision=decision_number, execution=execution_id,
                   parent=self._recall(decision_number, "agent_started"))

    def dependency_recorded(self, *, decision_number, execution_id, consumer, dependencies):
        self._emit("dependency_recorded", self._source(consumer["identity_id"], "specialist"),
                   {"consumer": consumer, "dependencies": dependencies},
                   decision=decision_number, execution=execution_id,
                   parent=self._recall(decision_number, "agent_completed"))

    def evidence_superseded(self, *, decision_number, execution_id, capability, superseded_version, replacement_version, affected_results):
        self._emit("evidence_superseded", self._source(APPLICATION_IDENTITY, "application"),
                   {"capability": capability, "superseded_version": superseded_version,
                    "replacement_version": replacement_version, "affected_results": affected_results},
                   decision=decision_number, execution=execution_id,
                   parent=self._recall(decision_number, "agent_completed"))

    # ---- stream-level events (emitted by the stream layer, never by the loop) ---------------
    def final_response(self, response: ChatResponse) -> None:
        if self._final_sent:
            raise EventContractError("exactly one final_response is allowed")
        self._emit("final_response", self._source(APPLICATION_IDENTITY, "application"),
                   {"response": response.model_dump(mode="json", exclude_unset=True)}, parent=self._last_id)
        self._final_sent = True

    def completed(self, *, status, completion_kind, completion_reason, decisions, executions) -> None:
        if not self._final_sent:
            raise EventContractError("orchestration_completed must follow final_response")
        self._emit("orchestration_completed", self._source(APPLICATION_IDENTITY, "application"),
                   {"status": status, "completion_kind": completion_kind, "completion_reason": completion_reason,
                    "decisions": decisions, "executions": executions,
                    "elapsed_ms": round((self._clock() - self._started) * 1000, 1)}, parent=self._last_id)

    def failed(self, error_code: str) -> None:
        if error_code not in FAILURE_MESSAGES:
            raise EventContractError("orchestration_failed needs a documented error code")
        if error_code in NEVER_EMITTED_FAILURE_CODES:
            raise EventContractError("this failure code is reserved and is never sent")
        if self._final_sent:
            raise EventContractError("a run that produced a final_response cannot fail")
        self._emit("orchestration_failed", self._source(APPLICATION_IDENTITY, "application"),
                   {"status": "failed", "error_code": error_code, "message": FAILURE_MESSAGES[error_code],
                    "elapsed_ms": round((self._clock() - self._started) * 1000, 1)}, parent=self._last_id)

    @property
    def terminal(self) -> str | None:
        return self._terminal

    @property
    def final_sent(self) -> bool:
        return self._final_sent


def classify_failure(exc: BaseException) -> str:
    """Map an exception to a documented, sanitized failure code. Never uses the message."""
    if isinstance(exc, DeadlineExceeded):
        return "deadline_exceeded"
    if isinstance(exc, PlannerUnavailableError):
        return "planner_unavailable"
    if isinstance(exc, PlannerOutputError):
        return "planner_output_invalid"
    if isinstance(exc, PlannerInferenceError):
        return "planner_inference_failed"
    if isinstance(exc, AgentPipelineError):
        return agent_error_code(exc)
    if isinstance(exc, IncompleteOrchestration):
        return "orchestration_incomplete"
    return "internal_error"


def drive(emitter: EventEmitter, work: "Work", cancel: CancelToken) -> None:
    """Run the orchestration and finish the stream: exactly one terminal event, always.

    On success: final_response, then orchestration_completed. On any failure: orchestration_failed
    with a sanitized code and no final_response. A cancelled run emits no terminal event, because
    nobody is listening. This is the sequencing the live endpoint uses, in one place.

    One case cannot end in a terminal event: the answer has been sent but orchestration_completed
    could not be (an application bug, an EventContractError). orchestration_failed would contradict
    the answer already on the wire, and a second terminal is forbidden, so nothing more is sent and
    EventContractError is raised. The stream then closes without a terminal event, the client
    reports it as unverified, and the server logs it (see `event_stream`).
    """
    try:
        response, summary = work(emitter, cancel)
        emitter.final_response(response)
        emitter.completed(**summary)
    except OrchestrationCancelled:
        raise
    except BaseException as exc:  # noqa: BLE001 - every failure becomes one sanitized terminal event
        if emitter.final_sent or emitter.terminal is not None:
            raise EventContractError("the closing event could not be sent after the answer") from None
        emitter.failed(classify_failure(exc))


def format_sse(event: Any) -> bytes:
    """One SSE frame: id, event name (equal to event_type), one JSON line, then a blank line."""
    return f"id: {event.event_id}\nevent: {event.event_type}\ndata: {serialize_event(event)}\n\n".encode("utf-8")


class StreamPermit:
    """A slot in the live-stream cap. Release is idempotent so every exit path can call it."""

    def __init__(self) -> None:
        self._held = True
        self._lock = threading.Lock()

    @classmethod
    def acquire(cls) -> "StreamPermit | None":
        return cls() if _streams.acquire(blocking=False) else None

    def release(self) -> None:
        with self._lock:
            if self._held:
                self._held = False
                _streams.release()


_DONE = object()
Work = Callable[[EventEmitter, CancelToken], "tuple[ChatResponse, dict[str, Any]]"]


async def event_stream(
    *, request_id: str, work: Work, permit: StreamPermit, cancel: CancelToken,
    diagnostics: bool = False, queue_size: int = QUEUE_SIZE, sink_timeout: float = SINK_TIMEOUT_SECONDS,
    keepalive: float = KEEPALIVE_SECONDS,
) -> AsyncIterator[bytes]:
    """Run `work` on its own thread and yield SSE frames as its events happen.

    A disconnected client cancels this generator; the `finally` block then sets the cancel token,
    so the loop starts no further delegation. A call already in flight runs to its own timeout.
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue(maxsize=queue_size)
    started = time.perf_counter()
    outcome: dict[str, Any] = {"terminal": None, "events": 0}

    def put(item: Any) -> None:
        if cancel.is_set():
            raise StreamClosed("the stream is closed")
        future = asyncio.run_coroutine_threadsafe(queue.put(item), loop)
        try:
            future.result(timeout=sink_timeout)
        except Exception:
            future.cancel()
            cancel.set()
            raise StreamClosed("the stream is not being read") from None

    emitter = EventEmitter(request_id, put, diagnostics=diagnostics)

    def worker() -> None:
        try:
            drive(emitter, work, cancel)
        except OrchestrationCancelled:
            outcome["cancelled"] = True
        except EventContractError:  # a bug; the stream still ends cleanly, and it is logged below
            outcome["contract_error"] = True
        finally:
            outcome["terminal"] = emitter.terminal
            outcome["events"] = sum(emitter.counts.values())
            def signal_done() -> None:
                try:
                    queue.put_nowait(_DONE)  # a safety net: a terminal event normally ends the stream
                except asyncio.QueueFull:
                    pass

            try:
                loop.call_soon_threadsafe(signal_done)
            except RuntimeError:  # the event loop is already closed; nobody is listening
                pass

    thread = threading.Thread(target=worker, name=f"live-{request_id[:8]}", daemon=True)
    thread.start()
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=keepalive)
            except asyncio.TimeoutError:
                yield b": keep-alive\n\n"  # a comment, not an event; keeps proxies from idling out
                continue
            if item is _DONE:
                return
            yield format_sse(item)
            if item.event_type in TERMINAL_EVENT_TYPES:
                return
    finally:
        cancel.set()
        permit.release()
        while not queue.empty():  # unblock a producer waiting on a full queue
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        # Fixed codes only: never the exception text, an event body or the question.
        logger.log(logging.ERROR if outcome.get("contract_error") else logging.INFO, "live stream ended", extra={
            "agent": "web", "request_id": request_id,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            "action_status": outcome.get("terminal") or "no_terminal_event",
            **({"error": "cancelled"} if outcome.get("cancelled") else {}),
            **({"error": "terminal_event_not_sent"} if outcome.get("contract_error") else {})})
