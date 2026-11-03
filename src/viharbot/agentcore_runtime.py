"""Small HTTP adapter for the Amazon Bedrock AgentCore Runtime contract."""

from __future__ import annotations

import functools
import inspect
import time
from typing import Any, Callable, Type

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from .nova_contract import (
    INSTANCE_HEADER,
    NovaCallContext,
    NovaEnvelope,
    instance_identity,
    not_accepted_headers,
    reset_call_context,
    set_call_context,
)


def _refusal(status: int, detail: Any, headers: dict[str, str] | None = None) -> HTTPException:
    """A refusal made before any work started, so it carries the not-accepted marker."""
    return HTTPException(status_code=status, detail=detail, headers=not_accepted_headers(headers))


def _marked(dependency: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap an auth dependency so its refusals carry the marker (it runs before the handler)."""
    if inspect.iscoroutinefunction(dependency):
        @functools.wraps(dependency)
        async def awrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return await dependency(*args, **kwargs)
            except HTTPException as exc:
                raise _refusal(exc.status_code, exc.detail, exc.headers) from exc
        return awrapper

    @functools.wraps(dependency)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return dependency(*args, **kwargs)
        except HTTPException as exc:
            raise _refusal(exc.status_code, exc.detail, exc.headers) from exc
    return wrapper


def _mark_dependencies(dependencies: list[Any]) -> list[Any]:
    return [Depends(_marked(item.dependency)) for item in dependencies]


def _admit(
    envelope: dict[str, Any], logical_agent: str
) -> tuple[tuple[str, str], NovaCallContext]:
    """Identity and envelope checks that run before any work; each refusal is marked."""
    try:
        identity = instance_identity()
    except ValueError as exc:  # startup refusal: this instance is misconfigured
        raise _refusal(503, "Nova instance identity is not configured") from exc
    stamp = {INSTANCE_HEADER: identity[0]}
    try:
        parsed = NovaEnvelope.model_validate(envelope)
    except ValidationError as exc:
        raise _refusal(422, "Invalid invocation envelope", stamp) from exc
    if parsed.logical_agent not in (None, logical_agent):
        raise _refusal(422, "Invalid invocation envelope", stamp)
    values = parsed.model_dump(exclude_none=True)
    values["logical_agent"] = logical_agent
    return identity, NovaCallContext(
        runtime_instance=identity[0], runtime_kind=identity[1], **values)


def add_agentcore_routes(
    app: FastAPI,
    request_model: Type[BaseModel],
    handler: Callable[[BaseModel, Response, str | None], Any],
    *,
    dependencies: list[Any] | None = None,
    logical_agent: str | None = None,
) -> None:
    """Expose the existing JSON handler through AgentCore's /ping and /invocations.

    With ``logical_agent`` set (Nova), the adapter also validates the optional ADR-008 envelope,
    stamps the instance identity on the response and marks pre-work refusals with
    ``X-Nova-Not-Accepted: 1``. Without it the behavior is unchanged.
    """
    used = dependencies or []
    if logical_agent:
        used = _mark_dependencies(used)

    @app.get("/ping", include_in_schema=False)
    def ping() -> dict[str, str]:
        return {"status": "Healthy"}

    @app.post("/invocations", include_in_schema=False, dependencies=used)
    def invoke(
        envelope: dict[str, Any],
        x_request_id: str | None = Header(default=None),
    ) -> JSONResponse:
        payload = envelope.get("input", envelope)
        request_id = envelope.get("request_id") or x_request_id
        identity: tuple[str, str] | None = None
        context: NovaCallContext | None = None
        if logical_agent:
            identity, context = _admit(envelope, logical_agent)
        try:
            request = request_model.model_validate(payload)
        except ValidationError as exc:
            if identity:
                raise _refusal(
                    422, "Invalid invocation payload", {INSTANCE_HEADER: identity[0]}
                ) from exc
            raise HTTPException(status_code=422, detail="Invalid invocation payload") from exc

        # Work starts here: nothing below may carry the not-accepted marker.
        token = set_call_context(context) if context else None
        started = time.perf_counter()
        try:
            response = Response()
            result = handler(request, response, request_id)
        finally:
            if token is not None:
                reset_call_context(token)
        headers = dict(response.headers)
        if request_id:
            headers.setdefault("X-Request-ID", request_id)
        content = jsonable_encoder(result)
        if identity and context:
            if isinstance(content, dict):  # additive only; existing fields are never replaced
                content.setdefault("runtime_instance", identity[0])
                content.setdefault("runtime_kind", identity[1])
                content.setdefault(
                    "duration_ms", round((time.perf_counter() - started) * 1000, 2))
                content.setdefault("attempt", context.attempt or 1)
            headers[INSTANCE_HEADER] = identity[0]
        return JSONResponse(content=content, status_code=response.status_code, headers=headers)
