"""Tiny orchestrator that delegates over private JSON REST endpoints."""

from __future__ import annotations

import functools
import inspect
import json
import logging
import os
import time
from typing import Callable
from urllib.request import Request, urlopen
from uuid import uuid4

from pydantic import BaseModel

from .contracts import AgentResponse, validate_agent_response
from .deadline import DeadlineExceeded, RequestDeadline, bounded_timeout, is_timeout_error
from .litellm_client import urlopen as no_redirect_urlopen
from .observability import configure_logging
from .outbound_auth import OutboundAuthError, outbound_headers
from .pipeline_state import (
    AgentActivity,
    PipelineContext,
    PipelineState,
    legacy_context_payload,
    fact_check_payload,
    legacy_findings_payload,
)

configure_logging()
logger = logging.getLogger("orchestrator")

AGENT_LABELS = {
    "research": "Research",
    "fact-check": "Fact Check",
    "source-quality": "Source Quality",
    "security": "Security",
    "privacy-review": "Privacy Review",
    "synthesis": "Synthesis",
    "pixel": "Pixel",
    "sage": "Sage",
}


class AgentPipelineError(RuntimeError):
    def __init__(self, activity: list[dict]) -> None:
        super().__init__("an agent call failed")
        self.activity = activity


REST_TIMEOUT_CAP_SECONDS = 90
_READ_CHUNK_BYTES = 8192


def _read_body(response, deadline: RequestDeadline | None) -> bytes:
    """Read the response body. With a deadline, read in chunks and stop if it passes.

    Socket timeouts apply to each read, not to the whole body, so the chunked read is what
    lets a slow response be abandoned. It is cooperative: a single blocked read can still
    last up to the socket timeout.
    """
    if deadline is None:
        return response.read()
    chunks: list[bytes] = []
    while True:
        deadline.require_remaining()
        chunk = response.read(_READ_CHUNK_BYTES)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def post_json(
    url: str,
    payload: dict,
    request_id: str,
    destination: str,
    *,
    deadline: RequestDeadline | None = None,
    timeout_cap: float = REST_TIMEOUT_CAP_SECONDS,
) -> dict:
    started = time.perf_counter()

    def log_failure(exc: BaseException) -> None:
        logger.error(
            "agent call failed",
            extra={
                "agent": "orchestrator",
                "request_id": request_id,
                "destination_agent": destination,
                "model_alias": None,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                "error": type(exc).__name__,
            },
        )

    def log_success() -> None:
        logger.info(
            "agent call completed",
            extra={
                "agent": "orchestrator",
                "request_id": request_id,
                "destination_agent": destination,
                "model_alias": None,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            },
        )

    if url.startswith("agentcore://"):
        from .agentcore_client import invoke_agentcore

        try:
            runtime_arn = url.removeprefix("agentcore://")
            if deadline is None:
                result = invoke_agentcore(runtime_arn, payload, request_id)
            else:
                result = invoke_agentcore(runtime_arn, payload, request_id, deadline=deadline)
        except Exception as exc:
            log_failure(exc)
            raise
        log_success()
        return result

    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "X-Request-ID": request_id}
    try:
        # Off by default (adds nothing). When on, a failure here stops the call: no unsigned fallback.
        assertion = outbound_headers(
            url, destination=destination, method="POST", body=data, request_id=request_id
        )
    except OutboundAuthError as exc:
        log_failure(exc)
        raise
    headers.update(assertion)
    # A signed request never follows redirects, so the token cannot be forwarded to another origin.
    opener = no_redirect_urlopen if assertion else urlopen
    request = Request(url, data=data, headers=headers)
    try:
        timeout = bounded_timeout(timeout_cap, deadline)
        with opener(request, timeout=timeout) as response:
            if deadline is not None:
                deadline.require_remaining()  # the response arrived after the deadline
            result = json.loads(_read_body(response, deadline).decode("utf-8"))
        if deadline is not None:
            deadline.require_remaining()  # the body finished arriving after the deadline
    except DeadlineExceeded as exc:
        log_failure(exc)
        raise
    except Exception as exc:
        if deadline is not None and deadline.expired() and is_timeout_error(exc):
            expired = DeadlineExceeded("request deadline exceeded")
            log_failure(expired)
            raise expired from None
        log_failure(exc)
        raise
    log_success()
    return result


def run_pipeline(
    question: str,
    request_id: str,
    *,
    research_url: str,
    security_url: str,
    specialist_urls: dict[str, str] | None = None,
    send: Callable[[str, dict, str, str], dict] = post_json,
) -> dict:
    return run_typed_pipeline(
        question,
        request_id,
        research_url=research_url,
        security_url=security_url,
        specialist_urls=specialist_urls,
        send=send,
    ).to_legacy_dict()


def _bind_deadline(
    send: Callable[..., dict], deadline: RequestDeadline | None
) -> Callable[..., dict]:
    """Pass the deadline only to senders that declare a `deadline` keyword."""
    if deadline is None:
        return send
    try:
        accepts = "deadline" in inspect.signature(send).parameters
    except (TypeError, ValueError):
        accepts = False
    return functools.partial(send, deadline=deadline) if accepts else send


def invoke_agent(
    send: Callable[[str, dict, str, str], dict],
    url: str,
    payload: dict,
    request_id: str,
    destination: str,
    activity: list[AgentActivity],
    *,
    deadline: RequestDeadline | None = None,
) -> tuple[AgentResponse, BaseModel]:
    """Call one service, validate its response and record the activity.

    This is the smallest unit of pipeline execution: one capability, one validated response.
    With a deadline, nothing starts after it has passed and a late response is discarded.
    DeadlineExceeded is recorded in the activity and re-raised unchanged, never wrapped.
    """
    started = time.perf_counter()
    try:
        if deadline is not None:
            deadline.require_remaining()
        raw = _bind_deadline(send, deadline)(url, payload, request_id, destination)
        if deadline is not None:
            deadline.require_remaining()  # a late result is never accepted as a success
        envelope, domain = validate_agent_response(destination, raw, request_id)
    except DeadlineExceeded:
        activity.append(
            AgentActivity(
                agent=destination,
                display_name=AGENT_LABELS.get(destination, destination),
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
                status="failed",
                error="DeadlineExceeded",
            )
        )
        raise
    except Exception as exc:
        activity.append(
            AgentActivity(
                agent=destination,
                display_name=AGENT_LABELS.get(destination, destination),
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
                status="failed",
                error=type(exc).__name__,
            )
        )
        raise AgentPipelineError([item.to_legacy() for item in activity]) from None
    activity.append(
        AgentActivity(
            agent=destination,
            display_name=AGENT_LABELS.get(destination, destination),
            model_alias=envelope.requested_model_alias,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
            status="complete",
        )
    )
    return envelope, domain


def run_typed_pipeline(
    question: str,
    request_id: str,
    *,
    research_url: str,
    security_url: str,
    specialist_urls: dict[str, str] | None = None,
    send: Callable[[str, dict, str, str], dict] = post_json,
) -> PipelineState:
    activity: list[AgentActivity] = []

    def invoke(
        url: str, payload: dict, destination: str
    ) -> tuple[AgentResponse, BaseModel]:
        return invoke_agent(send, url, payload, request_id, destination, activity)

    research, findings = invoke(research_url, {"question": question}, "research")
    responses: dict[str, AgentResponse] = {"research": research}
    context = PipelineContext(research=findings)

    for name in ("fact_check", "source_quality"):
        url = (specialist_urls or {}).get(name)
        if url:
            payload = (
                fact_check_payload(question, findings)
                if name == "fact_check"
                else legacy_findings_payload(findings)
            )
            responses[name], domain = invoke(url, payload, name.replace("_", "-"))
            setattr(context, name, domain)

    responses["security"], context.security = invoke(
        security_url, legacy_findings_payload(findings), "security"
    )

    legacy_context = legacy_context_payload(responses)
    privacy_url = (specialist_urls or {}).get("privacy_review")
    if privacy_url:
        responses["privacy_review"], context.privacy_review = invoke(
            privacy_url,
            {"question": question, "context": legacy_context},
            "privacy-review",
        )

    synthesis = None
    synthesis_url = (specialist_urls or {}).get("synthesis")
    if synthesis_url:
        privacy_response = responses.get("privacy_review")
        responses["synthesis"], synthesis = invoke(
            synthesis_url,
            {
                "question": question,
                "context": {
                    **legacy_context,
                    "privacy_review": privacy_response.result if privacy_response else None,
                },
            },
            "synthesis",
        )

    return PipelineState(
        request_id=request_id,
        context=context,
        synthesis=synthesis,
        responses=responses,
        agent_activity=activity,
    )


def main() -> None:
    question = os.getenv(
        "RESEARCH_QUESTION", "Summarize general safety practices for bicycle maintenance."
    )
    result = run_pipeline(
        question,
        str(uuid4()),
        research_url=os.getenv("RESEARCH_URL", "http://research:8000/research"),
        security_url=os.getenv("SECURITY_URL", "http://security:8000/assess"),
        specialist_urls={
            "fact_check": os.getenv(
                "FACT_CHECK_URL", "http://fact-check:8000/process"
            ),
            "source_quality": os.getenv(
                "SOURCE_QUALITY_URL", "http://source-quality:8000/process"
            ),
            "privacy_review": os.getenv(
                "PRIVACY_REVIEW_URL", "http://privacy-review:8000/process"
            ),
            "synthesis": os.getenv("SYNTHESIS_URL", "http://synthesis:8000/process"),
        },
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
