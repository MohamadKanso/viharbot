"""FastAPI surface for the deployment controller (ADR-007 decision 2).

Authentication reuses service_auth: every route except /health needs a verified assertion carrying
the route's capability, and the caller's registry subject must also be named in controller policy
(requesters for deployment routes, approvers for approval routes). There is no switch to turn
authentication off; ``create_app`` requires an authenticator. This module is not wired into
compose, Dockerfiles or deploy files, and there is no module-level app.

Known limitations: no background sweeper is scheduled (call ``reconcile`` at startup, which the
lifespan does, and periodically), and approvals have no UI; the approve route is the minimal
interface ADR-007 leaves open.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response

from ..observability import get_request_id
from ..service_auth import Caller, ServiceAuthenticator, require_assertion
from .controller import DeploymentController
from .policy import PolicyDenied
from .provider import ProviderError

REQUEST_CAPABILITY = "deployment.request"
APPROVE_CAPABILITY = "deployment.approve"
AUDIENCE = "deployment-controller"


def create_app(controller: DeploymentController, authenticator: ServiceAuthenticator) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            controller.reconcile(request_id="startup")
        except ProviderError:
            controller._alert("startup_reconcile_failed", request_id="startup")
        yield

    app = FastAPI(title="ViharBot Deployment Controller", version="0.1", lifespan=lifespan)
    app.state.controller = controller

    def ask(caller: Caller = Depends(require_assertion(authenticator, REQUEST_CAPABILITY))) -> str:
        if caller.sub not in controller.policy.requesters:
            raise HTTPException(403, "authentication failed")
        return caller.sub

    def approve_as(
        caller: Caller = Depends(require_assertion(authenticator, APPROVE_CAPABILITY)),
    ) -> str:
        if caller.sub not in controller.policy.approvers:
            raise HTTPException(403, "authentication failed")
        return caller.sub

    def rid(request: Request, response: Response) -> str:
        value = get_request_id(request.headers.get("x-request-id")) or str(uuid4())
        response.headers["X-Request-ID"] = value
        return value

    def run(call: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return call(*args, **kwargs)
        except PolicyDenied as denied:
            raise HTTPException(denied.status, denied.reason) from None

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "agent": "deployment-controller"}

    @app.post("/v1/deployments")
    def create(
        payload: dict = Body(...), caller: str = Depends(ask), request_id: str = Depends(rid)
    ) -> dict:
        return run(controller.submit, payload, caller=caller, request_id=request_id)

    @app.get("/v1/deployments/{deployment_id}")
    def read(deployment_id: str, caller: str = Depends(ask)) -> dict:
        return run(controller.get, deployment_id)

    @app.get("/v1/deployments/{deployment_id}/logs")
    def logs(
        deployment_id: str, lines: int = 100, caller: str = Depends(ask),
        request_id: str = Depends(rid),
    ) -> dict:
        text = run(controller.logs, deployment_id, lines, caller=caller, request_id=request_id)
        return {"deployment_id": deployment_id, "logs": text}

    @app.post("/v1/deployments/{deployment_id}:stop")
    def stop(deployment_id: str, caller: str = Depends(ask), request_id: str = Depends(rid)) -> dict:
        return run(controller.destroy, deployment_id, caller=caller, request_id=request_id,
                   keep_record=True)

    @app.delete("/v1/deployments/{deployment_id}")
    def delete(
        deployment_id: str, caller: str = Depends(ask), request_id: str = Depends(rid)
    ) -> dict:
        return run(controller.destroy, deployment_id, caller=caller, request_id=request_id,
                   keep_record=False)

    @app.get("/v1/approvals/{approval_id}")
    def review(approval_id: str, approver: str = Depends(approve_as)) -> dict:
        """What the approver reviews: the full request, including every environment value."""
        return run(controller.get_approval, approval_id)

    @app.post("/v1/approvals/{approval_id}:approve")
    def approve(
        approval_id: str, payload: dict = Body(...), approver: str = Depends(approve_as),
        request_id: str = Depends(rid),
    ) -> dict:
        """Approve exactly the request whose hash the approver reviewed, then execute it."""
        def go() -> dict:
            controller.approve(approval_id, approver=approver,
                               reviewed_hash=payload.get("request_hash"),
                               request_id=request_id)
            return controller.execute(approval_id, caller=approver, request_id=request_id)

        return run(go)

    return app
