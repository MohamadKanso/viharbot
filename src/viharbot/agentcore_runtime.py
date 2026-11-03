"""Small HTTP adapter for the Amazon Bedrock AgentCore Runtime contract."""

from __future__ import annotations

import time
from typing import Any, Callable, Type

from fastapi import APIRouter, FastAPI, Header, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from .nova_contract import (
    INSTANCE_HEADER,
    NOT_ACCEPTED_HEADER,
    NovaCallContext,
    NovaEnvelope,
    instance_identity,
    not_accepted_headers,
    reset_call_context,
    set_call_context,
)

_WORK_STARTED = "nova_work_started"


def _refusal(status: int, detail: Any, headers: dict[str, str] | None = None) -> HTTPException:
    """A refusal made before any work started, so it carries the not-accepted marker."""
    return HTTPException(status_code=status, detail=detail, headers=not_accepted_headers(headers))


class NovaRoute(APIRoute):
    """Marks every refusal made before the handler started, leaving FastAPI's dependency graph
    untouched: framework body and parameter validation, authentication dependencies (including
    nested and generator ones) and the adapter's own checks all run inside this wrapper. Once
    the endpoint sets the work-started flag, nothing is marked.
    """

    def get_route_handler(self) -> Callable[[Request], Any]:
        original = super().get_route_handler()

        async def handler(request: Request) -> Any:
            try:
                return await original(request)
            except RequestValidationError as exc:
                if request.scope.get(_WORK_STARTED):
                    raise
                response = await request_validation_exception_handler(request, exc)
                response.headers[NOT_ACCEPTED_HEADER] = "1"
                return response
            except StarletteHTTPException as exc:  # FastAPI's HTTPException is a subclass
                if request.scope.get(_WORK_STARTED):
                    raise
                raise _refusal(exc.status_code, exc.detail, exc.headers) from exc

        return handler


def _admit(
    envelope: dict[str, Any], logical_agent: str
) -> tuple[tuple[str, str], NovaCallContext]:
    """Identity and envelope checks that run before any work."""
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
    values.setdefault("attempt", 1)  # the effective attempt, as the response reports it
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

    @app.get("/ping", include_in_schema=False)
    def ping() -> dict[str, str]:
        return {"status": "Healthy"}

    router = APIRouter(route_class=NovaRoute if logical_agent else APIRoute)

    @router.post("/invocations", include_in_schema=False, dependencies=dependencies or [])
    def invoke(
        envelope: dict[str, Any],
        request: Request,
        x_request_id: str | None = Header(default=None),
    ) -> JSONResponse:
        payload = envelope.get("input", envelope)
        request_id = envelope.get("request_id") or x_request_id
        identity: tuple[str, str] | None = None
        context: NovaCallContext | None = None
        if logical_agent:
            identity, context = _admit(envelope, logical_agent)
        try:
            validated = request_model.model_validate(payload)
        except ValidationError as exc:
            if identity:
                raise _refusal(
                    422, "Invalid invocation payload", {INSTANCE_HEADER: identity[0]}
                ) from exc
            raise HTTPException(status_code=422, detail="Invalid invocation payload") from exc

        # Work starts here: nothing from this point may carry the not-accepted marker.
        request.scope[_WORK_STARTED] = True
        token = set_call_context(context) if context else None
        started = time.perf_counter()
        try:
            response = Response()
            result = handler(validated, response, request_id)
        except StarletteHTTPException as exc:
            if not identity:
                raise
            # A failure after work began: attribute it to this instance, never mark it.
            raise HTTPException(
                status_code=exc.status_code, detail=exc.detail,
                headers={**(exc.headers or {}), INSTANCE_HEADER: identity[0]},
            ) from exc
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

    app.include_router(router)
