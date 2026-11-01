"""Model-assisted workflow selection with application-side authorization.

Charlie's recommendation is untrusted. It is validated by deterministic policy, turned into an
ExecutionPlan by the existing planner, and only then executed on the existing pipeline. This is
not autonomous orchestration: one planning call, one predefined workflow, no replanning.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Callable

from pydantic import BaseModel

from . import delegation_planner
from .delegation import (
    DelegationDecision,
    DelegationError,
    PlannerInferenceError,
    validate_delegation,
)
from .execution_bindings import ExecutionBindingSet, default_bindings, load_bindings
from .identity_execution import IdentityPipelineResult, execute_plan
from .identity_registry import AgentRegistry, default_registry

logger = logging.getLogger("orchestrator")


class DelegatedPipelineResult(BaseModel):
    result: IdentityPipelineResult
    recommended_workflow: str
    approved_workflow: str
    planning_duration_ms: float
    execution_duration_ms: float

    def to_legacy_dict(self) -> dict:
        """The same result dictionary run_pipeline() returns."""
        return self.result.to_legacy_dict()


def _log(level: int, message: str, request_id: str, **fields: object) -> None:
    logger.log(
        level,
        message,
        extra={
            "agent": "orchestrator",
            "request_id": request_id,
            "planner_identity": delegation_planner.PLANNER_IDENTITY,
            "model_alias": delegation_planner.planner_alias() or None,
            **fields,
        },
    )


def run_delegated_pipeline(
    question: str,
    request_id: str,
    *,
    research_url: str,
    security_url: str,
    specialist_urls: dict[str, str] | None = None,
    send: Callable[[str, dict, str, str], dict] | None = None,
    recommend: Callable[[str, str], DelegationDecision] | None = None,
    registry: AgentRegistry | None = None,
    bindings: ExecutionBindingSet | None = None,
) -> DelegatedPipelineResult:
    """Recommend, authorize, then execute. Any failure before execution runs no agent."""
    registry = registry or default_registry()
    if bindings is None:
        bindings = default_bindings() if registry is default_registry() else load_bindings(registry)
    recommend = recommend or delegation_planner.recommend_workflow

    planning_started = time.perf_counter()
    try:
        decision = recommend(question, request_id)  # the only planning call per request
    except DelegationError:
        raise
    except Exception as exc:
        raise PlannerInferenceError("planning inference failed", type(exc).__name__) from None
    planning_ms = round((time.perf_counter() - planning_started) * 1000, 2)

    urls = {key: url for key, url in (specialist_urls or {}).items() if url}
    available = {"research", "security", *(key.replace("_", "-") for key in urls)}
    try:
        plan = validate_delegation(decision, registry, bindings, available)
    except Exception as exc:
        _log(
            logging.WARNING,
            "delegation rejected",
            request_id,
            recommended_workflow=getattr(decision, "workflow_name", None),
            planning_duration_ms=planning_ms,
            delegation_status="rejected",
            error=type(exc).__name__,
        )
        raise
    _log(
        logging.INFO,
        "delegation approved",
        request_id,
        recommended_workflow=decision.workflow_name,
        approved_workflow=plan.workflow_name,
        planning_duration_ms=planning_ms,
        delegation_status="approved",
    )

    execution_started = time.perf_counter()
    try:
        result = execute_plan(
            plan,
            question,
            request_id,
            research_url=research_url,
            security_url=security_url,
            specialist_urls=urls,
            send=send,
        )
    except Exception as exc:
        _log(
            logging.ERROR,
            "delegated execution failed",
            request_id,
            recommended_workflow=decision.workflow_name,
            approved_workflow=plan.workflow_name,
            planning_duration_ms=planning_ms,
            execution_duration_ms=round((time.perf_counter() - execution_started) * 1000, 2),
            delegation_status="execution_failed",
            error=type(exc).__name__,
        )
        raise
    execution_ms = round((time.perf_counter() - execution_started) * 1000, 2)
    # "executed" is only claimed here, after the workflow completed.
    _log(
        logging.INFO,
        "delegated execution completed",
        request_id,
        recommended_workflow=decision.workflow_name,
        approved_workflow=plan.workflow_name,
        planning_duration_ms=planning_ms,
        execution_duration_ms=execution_ms,
        delegation_status="executed",
    )
    return DelegatedPipelineResult(
        result=result,
        recommended_workflow=decision.workflow_name,
        approved_workflow=plan.workflow_name,
        planning_duration_ms=planning_ms,
        execution_duration_ms=execution_ms,
    )
