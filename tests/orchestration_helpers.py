"""Shared helpers for the orchestration tests."""

from __future__ import annotations

from pipeline_fixtures import RID, response
from viharbot.contracts import validate_agent_response
from viharbot.execution_bindings import OPERATIONAL_SERVICES, default_bindings
from viharbot.identity import Capability
from viharbot.orchestration_policy import OrchestrationAction
from viharbot.orchestration_state import ExecutionBudget, OrchestrationState, StepResult

C = Capability

# The tests that simulate time (a fake clock advancing per call) assume a 120 s budget. They pin it
# here instead of reading the production default, which is a separate decision and may change.
FIXED_TIME_BUDGET = ExecutionBudget(max_seconds=120.0)
SERVICE = dict(OPERATIONAL_SERVICES)
URLS_BY_SERVICE = {"research": "u-r", "security": "u-sec", "fact-check": "u-f",
                   "source-quality": "u-s", "privacy-review": "u-p", "synthesis": "u-y"}


def delegate(capability, rationale="Because.", unresolved_question=None):
    value = capability.value if isinstance(capability, Capability) else capability
    return OrchestrationAction(action="delegate", capability=value, rationale=rationale,
                               unresolved_question=unresolved_question)


def finish(rationale="Enough evidence."):
    return OrchestrationAction(action="finish", rationale=rationale)


def new_state(**kwargs):
    fields = {"request_id": RID, "original_question": "q", "started_at": 0.0, **kwargs}
    return OrchestrationState(**fields)


def make_step(capability, version=1, based_on=None, overrides=None):
    service = SERVICE[capability]
    raw = {**response(service), "request_id": RID}
    if overrides:
        raw = overrides(raw)
    envelope, domain = validate_agent_response(service, raw, RID)
    return StepResult(
        capability=capability, version=version,
        identity_id=default_bindings().lookup(capability).identity_id, service_name=service,
        decision_number=version, duration_ms=1.0, envelope=envelope, domain=domain,
        based_on=based_on or {})


def with_steps(*steps, **kwargs):
    state = new_state(**kwargs)
    state.completed_steps.extend(steps)
    return state


class Script:
    """A scripted Charlie. Records every context it was shown."""

    def __init__(self, *actions):
        self.actions = list(actions)
        self.calls = []

    def __call__(self, state, context):
        self.calls.append({
            "decision": state.decision_number,
            "completed": [(s.capability.value, s.version) for s in state.completed_steps],
            "allowed": [c.value for c in context.allowed_capabilities],
            "finish_allowed": context.finish_allowed,
            "notice": context.notice.result_summary if context.notice else None,
        })
        if not self.actions:
            raise AssertionError("the script ran out of actions")
        return self.actions.pop(0)


class Forever:
    """A Charlie that never stops asking for the same action."""

    def __init__(self, action):
        self.action, self.calls = action, 0

    def __call__(self, state, context):
        self.calls += 1
        return self.action


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now
