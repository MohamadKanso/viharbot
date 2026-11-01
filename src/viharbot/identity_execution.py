"""Identity-attributed execution on top of the existing typed pipeline.

Identity attribution is bookkeeping. The services keep their own prompts, model aliases and
permissions, so a bound identity's personality prompt is NOT used for inference.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict

from . import demo
from .demo import AgentPipelineError, run_typed_pipeline
from .execution_bindings import ExecutionBindingSet, default_bindings, load_bindings
from .execution_planner import ExecutionPlan, plan_workflow, validate_dispatchable
from .identity import AgentId, Capability, NonEmpty
from .identity_registry import AgentRegistry, default_registry
from .pipeline_state import PipelineState

from .live_flags import flag_on, live_team_enabled

IDENTITY_ROUTING_FLAG = "VIHARBOT_IDENTITY_ROUTING_ENABLED"


def identity_routing_enabled() -> bool:
    """Its own flag, or the live-team umbrella flag, which turns on every layer above it too."""
    return flag_on(IDENTITY_ROUTING_FLAG) or live_team_enabled()


class IdentityExecutionError(RuntimeError):
    """The executed stages did not match the plan."""


class ExecutionRecord(BaseModel):
    """Internal attribution for one executed step. Not part of any public response."""

    model_config = ConfigDict(frozen=True)

    identity_id: AgentId
    capability: Capability
    service_name: NonEmpty
    request_id: str
    duration_ms: float
    status: Literal["complete", "failed"]
    # As reported by the service; the service's configured alias is authoritative.
    model_alias: str | None = None


class IdentityPipelineResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    state: PipelineState
    plan: ExecutionPlan
    records: tuple[ExecutionRecord, ...]

    def to_legacy_dict(self) -> dict[str, Any]:
        """The same result dictionary run_pipeline() returns."""
        return self.state.to_legacy_dict()


class IdentityPipelineError(AgentPipelineError):
    """An AgentPipelineError that also carries the attribution recorded so far."""

    def __init__(self, activity: list[dict], records: tuple[ExecutionRecord, ...]) -> None:
        super().__init__(activity)
        self.records = records


def _build_records(
    plan: ExecutionPlan, activity: list[dict], request_id: str
) -> tuple[ExecutionRecord, ...]:
    by_service = {item["agent"]: item for item in activity}
    return tuple(
        ExecutionRecord(
            identity_id=step.identity_id,
            capability=step.capability,
            service_name=step.service_name,
            request_id=request_id,
            duration_ms=by_service[step.service_name]["duration_ms"],
            status=by_service[step.service_name]["status"],
            model_alias=by_service[step.service_name].get("model_alias"),
        )
        for step in plan.steps
        if step.service_name in by_service  # steps that never ran get no record
    )


def run_identity_pipeline(
    question: str,
    request_id: str,
    *,
    workflow: str = "full_review",
    research_url: str,
    security_url: str,
    specialist_urls: dict[str, str] | None = None,
    send: Callable[[str, dict, str, str], dict] | None = None,
    registry: AgentRegistry | None = None,
    bindings: ExecutionBindingSet | None = None,
) -> IdentityPipelineResult:
    """Plan a named workflow from approved bindings and run it on the existing pipeline."""
    registry = registry or default_registry()
    if bindings is None:
        bindings = default_bindings() if registry is default_registry() else load_bindings(registry)

    urls = {key: url for key, url in (specialist_urls or {}).items() if url}
    available = {"research", "security", *(key.replace("_", "-") for key in urls)}
    plan = plan_workflow(workflow, bindings, registry, available)
    return execute_plan(
        plan,
        question,
        request_id,
        research_url=research_url,
        security_url=security_url,
        specialist_urls=urls,
        send=send,
    )


def execute_plan(
    plan: ExecutionPlan,
    question: str,
    request_id: str,
    *,
    research_url: str,
    security_url: str,
    specialist_urls: dict[str, str] | None = None,
    send: Callable[[str, dict, str, str], dict] | None = None,
) -> IdentityPipelineResult:
    """Run an already validated plan on the existing typed pipeline."""
    validate_dispatchable(plan)
    urls = {key: url for key, url in (specialist_urls or {}).items() if url}
    planned = set(plan.services)
    planned_urls = {key: url for key, url in urls.items() if key.replace("_", "-") in planned}
    try:
        state = run_typed_pipeline(
            question,
            request_id,
            research_url=research_url,
            security_url=security_url,
            specialist_urls=planned_urls,
            send=send or demo.post_json,
        )
    except AgentPipelineError as exc:
        raise IdentityPipelineError(
            exc.activity, _build_records(plan, exc.activity, request_id)
        ) from None

    activity = [item.to_legacy() for item in state.agent_activity]
    if [item["agent"] for item in activity] != list(plan.services):
        raise IdentityExecutionError("executed stages did not match the plan")
    return IdentityPipelineResult(
        state=state, plan=plan, records=_build_records(plan, activity, request_id)
    )
