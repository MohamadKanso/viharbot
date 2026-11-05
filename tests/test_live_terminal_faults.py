"""Fault injection for the terminal outcome of /api/chat/stream (issue #22, audit F1 and F2).

Inference is mocked throughout (Script and VersionedSend); nothing here touches a network.
"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest

from live_helpers import R, S, Script, VersionedSend, delegate, finish, live_work
from test_live_stream import permits_available, wait_until, workers
from viharbot.live_events import FAILURE_MESSAGES, TERMINAL_EVENT_TYPES, serialize_event
from viharbot.live_hooks import CancelToken, OrchestrationCancelled
from viharbot.live_stream import (
    MAX_CONCURRENT_STREAMS, EventContractError, EventEmitter, StreamPermit, drive, event_stream)

SECRET = "SECRET-TOKEN-do-not-leak-0xC0FFEE"
RID = "req-fault"


def good_work():
    return live_work(Script(delegate(R, "Research first."), delegate(S), finish()), VersionedSend(),
                     request_id=RID)


def bad_completion(inner):
    """The real run, but with a completion summary the schema rejects (kind disagrees with status)."""
    def work(emitter, cancel):
        response, summary = inner(emitter, cancel)
        return response, {**summary, "completion_kind": "policy_budget"}
    return work


def run_to_the_end(work, cancel=None):
    """Read a stream to its natural end, as a connected client would. Returns the raw bytes."""
    cancel, permit = cancel or CancelToken(), StreamPermit.acquire()
    chunks = []

    async def scenario():
        async for chunk in event_stream(request_id=RID, work=work, permit=permit, cancel=cancel):
            chunks.append(chunk)

    asyncio.run(scenario())
    return b"".join(chunks)


def types_in(body: bytes) -> list[str]:
    return [line.split(": ", 1)[1] for line in body.decode().splitlines() if line.startswith("event: ")]


def drive_into(work):
    events = []
    emitter = EventEmitter(RID, events.append)
    return emitter, events


# ---- F1: the closing event cannot be sent after the answer ------------------------------------
def test_the_unmodified_run_ends_with_exactly_one_terminal_after_the_final_response():
    kinds = types_in(run_to_the_end(good_work()))
    assert kinds[-2:] == ["final_response", "orchestration_completed"]
    assert sum(k in TERMINAL_EVENT_TYPES for k in kinds) == 1


def test_a_contract_error_while_completing_leaves_no_contradicting_terminal_event(caplog):
    caplog.set_level(logging.INFO, logger="web")
    body = run_to_the_end(bad_completion(good_work()))
    kinds = types_in(body)
    # The answer went out, then the stream ended. No orchestration_failed (it would contradict the
    # answer) and no second or substitute terminal event.
    assert kinds[-1] == "final_response"
    assert not [k for k in kinds if k in TERMINAL_EVENT_TYPES]
    # Everything it did send is still in strict sequence.
    sequences = [json.loads(line.split(": ", 1)[1])["sequence"]
                 for line in body.decode().splitlines() if line.startswith("data: ")]
    assert sequences == list(range(1, len(sequences) + 1))


def test_that_failure_is_logged_as_an_error_with_fixed_codes_only(caplog):
    caplog.set_level(logging.INFO, logger="web")
    body = run_to_the_end(bad_completion(good_work()))
    ended = [r for r in caplog.records if r.getMessage() == "live stream ended"]
    assert len(ended) == 1
    record = ended[0]
    assert record.levelno == logging.ERROR
    assert (record.action_status, record.error) == ("no_terminal_event", "terminal_event_not_sent")
    assert record.request_id == RID
    # No exception text (which names the schema problem) and no event content in the log or on the wire.
    everything = " ".join(str(v) for v in vars(record).values()) + body.decode()
    for leak in ("disagree", "invalid orchestration_completed", "Traceback", "EventContractError"):
        assert leak not in everything


def test_the_stream_runner_still_releases_everything_after_that_failure():
    run_to_the_end(bad_completion(good_work()))
    assert wait_until(lambda: not workers(RID))
    assert permits_available() == MAX_CONCURRENT_STREAMS


def test_drive_raises_instead_of_inventing_a_terminal_after_the_answer():
    emitter, events = drive_into(None)
    with pytest.raises(EventContractError):
        drive(emitter, bad_completion(good_work()), CancelToken())
    assert emitter.final_sent and emitter.terminal is None
    assert events[-1].event_type == "final_response"
    assert not [e for e in events if e.event_type in TERMINAL_EVENT_TYPES]


# ---- failures before the answer: exactly one sanitized orchestration_failed -------------------
def test_a_failure_before_the_answer_is_one_sanitized_terminal_and_no_answer():
    def work(emitter, cancel):
        raise RuntimeError(f"upstream said {SECRET} at https://internal.example/path")

    emitter, events = drive_into(work)
    drive(emitter, work, CancelToken())
    kinds = [e.event_type for e in events]
    assert kinds == ["orchestration_failed"]
    failed = events[0].data
    assert failed.error_code == "internal_error" and failed.message == FAILURE_MESSAGES["internal_error"]
    wire = serialize_event(events[0])
    assert SECRET not in wire and "internal.example" not in wire and "RuntimeError" not in wire


def test_a_failure_through_the_stream_sends_no_exception_text():
    def work(emitter, cancel):
        raise RuntimeError(SECRET)

    body = run_to_the_end(work)
    assert types_in(body) == ["orchestration_failed"]
    assert SECRET not in body.decode()


# ---- at most one terminal, and nothing after it ----------------------------------------------
SUMMARY = {"status": "completed", "completion_kind": "charlie_approved",
           "completion_reason": "charlie_finished", "decisions": 1, "executions": 1}


def an_answered_emitter():
    work = good_work()
    emitter, events = drive_into(work)
    response, _ = work(emitter, CancelToken())
    emitter.final_response(response)
    return emitter, events


def test_nothing_can_be_emitted_after_a_completion():
    emitter, events = an_answered_emitter()
    emitter.completed(**SUMMARY)
    count = len(events)
    for attempt in (lambda: emitter.completed(**SUMMARY), lambda: emitter.failed("internal_error"),
                    lambda: emitter.planner_started(decision_number=9, remaining_ms=1)):
        with pytest.raises(EventContractError):
            attempt()
    assert len(events) == count and [e.event_type for e in events].count("orchestration_completed") == 1


def test_nothing_can_be_emitted_after_a_failure():
    emitter, events = drive_into(None)
    emitter.failed("deadline_exceeded")
    count = len(events)
    for attempt in (lambda: emitter.failed("internal_error"), lambda: emitter.completed(**SUMMARY),
                    lambda: emitter.planner_started(decision_number=1, remaining_ms=1)):
        with pytest.raises(EventContractError):
            attempt()
    assert len(events) == count == 1


def test_a_failure_after_the_answer_is_refused_by_the_emitter_itself():
    emitter, events = an_answered_emitter()
    count = len(events)
    with pytest.raises(EventContractError):
        emitter.failed("internal_error")
    assert len(events) == count and emitter.terminal is None


# ---- F2: the cancelled code ------------------------------------------------------------------
def test_cancelled_is_declared_but_reserved_and_the_emitter_refuses_it():
    assert "cancelled" in FAILURE_MESSAGES  # still part of the declared schema (see #53)
    emitter, events = drive_into(None)
    with pytest.raises(EventContractError):
        emitter.failed("cancelled")
    assert events == [] and emitter.terminal is None


def test_a_cancelled_run_emits_no_terminal_event_and_never_says_cancelled(caplog):
    caplog.set_level(logging.INFO, logger="web")

    def work(emitter, cancel):
        emitter.planner_started(decision_number=1, remaining_ms=1000)
        raise OrchestrationCancelled("the client is gone")

    body = run_to_the_end(work)
    assert types_in(body) == ["planner_started"]
    assert b"cancelled" not in body and b"orchestration_failed" not in body
    ended = [r for r in caplog.records if r.getMessage() == "live stream ended"]
    assert len(ended) == 1 and ended[0].error == "cancelled" and ended[0].action_status == "no_terminal_event"
    assert ended[0].levelno == logging.INFO  # a disconnect is normal, not an error
    assert wait_until(lambda: not workers(RID)) and permits_available() == MAX_CONCURRENT_STREAMS


def test_drive_lets_cancellation_propagate_without_a_terminal():
    def work(emitter, cancel):
        raise OrchestrationCancelled("stop")

    emitter, events = drive_into(work)
    with pytest.raises(OrchestrationCancelled):
        drive(emitter, work, CancelToken())
    assert events == [] and emitter.terminal is None
