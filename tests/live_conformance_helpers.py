"""Regenerate the documented event streams from the real orchestration loop (issue #53).

Each recipe drives `run_iterative_pipeline` with mocked inference, through the production
`EventEmitter` and `drive()`, so the result is what the application actually emits. Tests compare it
with the JSON fixtures under tests/fixtures/live_events, which makes a fixture that drifts from the
code fail instead of silently going stale.

    python tests/live_conformance_helpers.py F_full_team      # print the regenerated stream
    python tests/live_conformance_helpers.py F_full_team --write   # rewrite that fixture

Only fixtures this module generates exactly (GENERATED) may be rewritten. The other scenarios are
compared structurally: timestamps and durations are ignored, everything else must match.
"""

from __future__ import annotations

import contextlib
import itertools
import json
import sys
from pathlib import Path
from unittest import mock

from live_helpers import FIXTURES, FC, P, R, S, Y, VersionedSend, deterministic_timestamps, live_work
from orchestration_helpers import C, FIXED_TIME_BUDGET, Script, delegate, finish
from viharbot import delegated_execution, demo, iterative_execution
from viharbot.delegation import PlannerInferenceError
from viharbot.live_events import serialize_event
from viharbot.live_hooks import CancelToken
from viharbot.live_stream import EventEmitter, drive
from viharbot.orchestration_state import ExecutionBudget

SQ = C.SOURCE_QUALITY
# Seven specialists need seven executions; the default allows six (the ceiling is twelve), so no
# request under default settings can populate every optional ChatResponse section.
FULL_TEAM_BUDGET = ExecutionBudget(max_seconds=120.0, max_executions=7)
VOLATILE = {"timestamp", "duration_ms", "elapsed_ms", "remaining_budget_ms"}


def stable(value):
    """The event with its time-dependent fields removed, recursively."""
    if isinstance(value, dict):
        return {k: stable(v) for k, v in value.items() if k not in VOLATILE}
    if isinstance(value, list):
        return [stable(v) for v in value]
    return value


class _ResearchThenPlannerFailure:
    """Charlie delegates Research, then the planning call fails."""

    def __init__(self):
        self.script = Script(delegate(R))

    def __call__(self, state, context):
        if self.script.actions:
            return self.script(state, context)
        raise PlannerInferenceError("planning inference failed", "TimeoutError")


def _recipe(name):
    """(decide, send, diagnostics) for a scenario."""
    if name == "A_successful_request":
        return Script(delegate(R), delegate(S), finish()), VersionedSend(), False
    if name == "A_successful_request_with_diagnostics":
        return (Script(delegate(R, "Research is required before security analysis."),
                       delegate(S, "Assess the risks in the research."),
                       finish("The evidence is sufficient.")), VersionedSend(), True)
    if name == "B_policy_rejection":
        return Script(delegate(S), delegate(R), delegate(S), finish()), VersionedSend(), False
    if name == "C_evidence_supersession":
        return Script(delegate(R), delegate(S), delegate(R), delegate(S), finish()), VersionedSend(), False
    if name == "D_planner_failure":
        return _ResearchThenPlannerFailure(), VersionedSend(), False
    if name == "E_agent_failure":
        return (Script(delegate(R), delegate(S)),
                VersionedSend({"security": lambda r: {**r, "agent": "someone-else"}}), False)
    if name == "F_full_team":
        # Every specialist, Sage included, so the final ChatResponse has every optional section set.
        return (Script(delegate(R), delegate(FC), delegate(SQ), delegate(S), delegate(P),
                       delegate(C.PHILOSOPHICAL_ANALYSIS), delegate(Y), finish()), VersionedSend(), False)
    raise KeyError(name)


# The Sage service is offered to Charlie only where its URL is configured; the full-team run configures it.
WITH_SAGE = {"fact_check": "u-f", "source_quality": "u-s", "privacy_review": "u-p", "synthesis": "u-y",
             "sage": "u-sage"}


# Scenarios whose fixture this module reproduces byte for byte (a fixed clock and timestamps).
GENERATED = {"F_full_team"}
SCENARIOS = ["A_successful_request", "A_successful_request_with_diagnostics", "B_policy_rejection",
             "C_evidence_supersession", "D_planner_failure", "E_agent_failure", "F_full_team"]


def fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


def regenerate(name, *, request_id=None, step=0.25):
    """The events the real loop emits for this scenario, as the dicts a client would parse."""
    decide, send, diagnostics = _recipe(name)
    if request_id is None:
        request_id = fixture(name)["events"][0]["request_id"] if (FIXTURES / f"{name}.json").exists() \
            else "req-200"
    ticks = itertools.count()
    clock = lambda: next(ticks) * step  # noqa: E731 - one deterministic clock for the loop and emitter
    events = []
    emitter = EventEmitter(request_id, events.append, diagnostics=diagnostics, clock=clock,
                           timestamp=deterministic_timestamps())
    full = name == "F_full_team"
    work = live_work(decide, send, request_id=request_id, clock=clock,
                     budget=FULL_TEAM_BUDGET if full else FIXED_TIME_BUDGET,
                     specialist_urls=WITH_SAGE if full else None)
    with _stepped_timers(step):
        drive(emitter, work, CancelToken())
    return [json.loads(serialize_event(e)) for e in events]


class _SteppedTime:
    """Stands in for the `time` module inside the loop's modules: each perf_counter() call advances
    a fixed step, so the planner and agent durations in the events are the same on every run."""

    def __init__(self, step):
        self._ticks, self._step = itertools.count(), step

    def perf_counter(self):
        return next(self._ticks) * self._step


@contextlib.contextmanager
def _stepped_timers(step):
    # Only the names inside these modules are replaced; the real `time` module (which pytest and
    # everything else use) is untouched. demo.py times each agent call (agent_activity durations).
    with mock.patch.object(iterative_execution, "time", _SteppedTime(step)), \
            mock.patch.object(delegated_execution, "time", _SteppedTime(step)), \
            mock.patch.object(demo, "time", _SteppedTime(step)):
        yield


def render(name):
    doc = fixture(name) if (FIXTURES / f"{name}.json").exists() else {
        "scenario": name, "description": DESCRIPTIONS[name]}
    doc = {**{k: v for k, v in doc.items() if k != "events"}, "events": regenerate(name)}
    return json.dumps(doc, indent=1, ensure_ascii=False) + "\n"  # the format every fixture already uses


DESCRIPTIONS = {
    "F_full_team": ("Charlie delegates to all seven specialists (Research, Fact Check, Source Quality, "
                    "Security, Privacy Review, Sage, Synthesis), then finishes. The final_response "
                    "carries a ChatResponse with every optional section populated."),
}


if __name__ == "__main__":  # pragma: no cover - a maintenance tool, not part of the suite
    target = sys.argv[1]
    if "--write" in sys.argv:
        if target not in GENERATED:
            raise SystemExit(f"{target} is not generated exactly; refusing to rewrite it")
        Path(FIXTURES / f"{target}.json").write_text(render(target))
        print("wrote", target)
    else:
        print(render(target))
