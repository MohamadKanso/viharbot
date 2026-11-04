"""JSON invocation client for AgentCore Runtime URLs used by the web coordinator."""

from __future__ import annotations

import json
import os
import threading
from typing import Any
from uuid import uuid4

from .deadline import DeadlineExceeded, RequestDeadline, is_timeout_error

_runtime_client: Any | None = None
_session: Any | None = None
_session_lock = threading.Lock()
_READ_CHUNK_BYTES = 8192


def _make_session() -> Any:
    import boto3

    return boto3.session.Session()


def _deadline_client(timeout: float) -> Any:
    """A client whose timeouts fit one request's remaining time, with retries disabled.

    botocore timeouts are fixed when a client is created and cannot be set per call, so a
    deadline-aware call gets its own client. The shared client keeps its default behavior for
    calls without a deadline. Session and client creation run under a lock because boto3
    sessions are not thread-safe; the resulting client is private to the call.
    """
    from botocore.config import Config

    global _session
    with _session_lock:
        if _session is None:
            _session = _make_session()
        return _session.client(
            "bedrock-agentcore",
            region_name=os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION"),
            config=Config(
                connect_timeout=timeout,
                read_timeout=timeout,
                retries={"total_max_attempts": 1},  # no SDK retries
            ),
        )


def invoke_agentcore(
    runtime_arn: str,
    payload: dict,
    request_id: str,
    *,
    deadline: RequestDeadline | None = None,
) -> dict:
    """Invoke one runtime over AgentCore's authenticated HTTPS data plane.

    With a deadline, the call is bounded by the time remaining and a response that arrives
    after it is rejected. A client-side timeout means this coordinator stops waiting; it does
    not cancel anything running in the remote runtime.
    """
    global _runtime_client
    if deadline is None:
        if _runtime_client is None:
            import boto3

            _runtime_client = boto3.client(
                "bedrock-agentcore",
                region_name=os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION"),
            )
        client = _runtime_client
    else:
        client = _deadline_client(deadline.require_remaining())

    # AgentCore requires runtimeSessionId of at least 33 characters, but a request id may be
    # 1 to 128. The session id is derived (36 + 5 characters); the request id stays in the payload.
    session_id = f"nova-{uuid4()}"
    arguments = dict(
        agentRuntimeArn=runtime_arn,
        runtimeSessionId=session_id,
        qualifier="DEFAULT",
        contentType="application/json",
        accept="application/json",
        payload=json.dumps({"request_id": request_id, "input": payload}).encode("utf-8"),
    )
    if deadline is None:
        response = client.invoke_agent_runtime(**arguments)
        status_code = response.get("statusCode", 200)
        body = response["response"].read().decode("utf-8")
    else:
        try:
            deadline.require_remaining()  # client setup may have used the remaining time
            response = client.invoke_agent_runtime(**arguments)
            deadline.require_remaining()
            status_code = response.get("statusCode", 200)
            stream = response["response"]
            chunks: list[bytes] = []
            while True:
                deadline.require_remaining()
                chunk = stream.read(_READ_CHUNK_BYTES)
                if not chunk:
                    break
                chunks.append(chunk)
            deadline.require_remaining()
            body = b"".join(chunks).decode("utf-8")
        except DeadlineExceeded:
            raise
        except Exception as exc:
            if deadline.expired() and is_timeout_error(exc):
                raise DeadlineExceeded("request deadline exceeded") from None
            raise
    if status_code >= 400:
        raise RuntimeError(f"AgentCore runtime returned HTTP {status_code}")
    result = json.loads(body)
    if not isinstance(result, dict):
        raise ValueError("AgentCore runtime returned a non-object JSON response")
    return result
