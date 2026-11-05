"""Run real orchestrations through the production streaming sequence, with mocked inference."""

from __future__ import annotations

import itertools
import json
from pathlib import Path

from orchestration_helpers import C, Script, delegate, finish
from pipeline_fixtures import FINDINGS, FakeSend
from viharbot import web_app
from viharbot.live_events import EVENT_TYPES, check_stream, parse_event
from viharbot.live_hooks import CancelToken
from viharbot.live_stream import EventEmitter, drive
from viharbot.iterative_execution import run_iterative_pipeline
from viharbot.orchestration_view import build_orchestration_meta

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "live_events"
URLS = {"fact_check": "u-f", "source_quality": "u-s", "privacy_review": "u-p", "synthesis": "u-y"}
R, FC, SQ, S, P, Y = (C.RESEARCH, C.FACT_CHECK, C.SOURCE_QUALITY, C.SECURITY_ASSESSMENT,
                      C.PRIVACY_REVIEW, C.SYNTHESIS)


class VersionedSend(FakeSend):
    """Every research call differs, so repeated versions are distinguishable."""

    def __init__(self, overrides=None):
        self.research_calls = 0
        super().__init__({"research": self._versioned, **(overrides or {})})

    def _versioned(self, raw):
        self.research_calls += 1
        return {**raw, "findings": {**FINDINGS, "summary": f"research-v{self.research_calls}"}}


def live_work(decide, send=None, *, request_id="req-123", question="hello", specialist_urls=None, **kwargs):
    """The same closure the route builds: run the loop, assemble the response, summarize."""
    send = send or VersionedSend()

    def work(emitter, cancel):
        iterative = run_iterative_pipeline(
            question, request_id, research_url="u-r", security_url="u-sec",
            specialist_urls=specialist_urls or URLS,
            send=send, decide=decide, emit=emitter, cancel=cancel, **kwargs)
        response = web_app.build_chat_response(
            iterative.to_legacy_dict(), request_id, build_orchestration_meta(iterative))
        state = iterative.orchestration
        return response, {
            "status": state.status, "completion_kind": state.completion_kind,
            "completion_reason": state.completion_reason, "decisions": state.decision_number,
            "executions": len(state.completed_steps)}

    return work


def collect(work, *, request_id="req-123", diagnostics=False, timestamp=None, cancel=None):
    """Drive a run to its end and return the events exactly as a client would receive them."""
    events: list = []
    kwargs = {"timestamp": timestamp} if timestamp else {}
    emitter = EventEmitter(request_id, events.append, diagnostics=diagnostics, **kwargs)
    drive(emitter, work, cancel or CancelToken())
    return events


def types(events) -> list[str]:
    return [event.event_type for event in events]


def deterministic_timestamps():
    ticks = itertools.count()
    return lambda: f"2026-10-27T19:00:{next(ticks) // 4:02d}.{(next(ticks) % 4) * 250:03d}Z"
