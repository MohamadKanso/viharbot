"""Request body size cap that runs before service authentication (ADR-006 A2, issue #101).

Pure ASGI middleware. For the listed POST paths it refuses with 413 before the app (and so before
the assertion check) runs when ``Content-Length`` is over the limit, and it counts bytes while the
body streams so a chunked or understated body is cut off as soon as the limit is crossed. Other
paths and methods pass through untouched. Only the size is logged, never a body or a header value.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Awaitable, Callable, Mapping

logger = logging.getLogger("body_limit")

LIMIT_ENV = "VIHARBOT_MAX_BODY_BYTES"
# Sized from the largest legitimate specialist request (see docs/service-authentication.md).
DEFAULT_MAX_BODY_BYTES = 262_144
_MIN, _MAX = 1_024, 16 * 1024 * 1024

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]


class BodyLimitConfigError(RuntimeError):
    """The configured limit is not a sane integer."""


class _TooLarge(Exception):
    pass


def max_body_bytes_from_environment(environ: Mapping[str, str] | None = None) -> int:
    env = os.environ if environ is None else environ
    text = env.get(LIMIT_ENV, "").strip()
    if not text:
        return DEFAULT_MAX_BODY_BYTES
    try:
        value = int(text)
    except ValueError:
        raise BodyLimitConfigError(f"{LIMIT_ENV} must be an integer number of bytes") from None
    if not _MIN <= value <= _MAX:
        raise BodyLimitConfigError(f"{LIMIT_ENV} must be between {_MIN} and {_MAX}")
    return value


class BodySizeLimitMiddleware:
    def __init__(
        self, app: Callable[..., Awaitable[None]], *, max_bytes: int, paths: frozenset[str], agent: str
    ) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.paths = paths
        self.agent = agent

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"] not in self.paths:
            await self.app(scope, receive, send)
            return
        declared = _declared_length(scope)
        if declared is not None and declared > self.max_bytes:
            await self._refuse(send, "content_length")
            return

        seen = 0
        started = False
        refused = False

        async def counting_receive() -> dict[str, Any]:
            nonlocal seen, refused
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.max_bytes:
                    if started:
                        raise _TooLarge
                    # Answer 413 now, then abort the read. FastAPI turns a body-read error into
                    # its own 400, so whatever the app sends after this point is dropped.
                    refused = True
                    await self._refuse(send, "stream")
                    raise _TooLarge
            return message

        async def tracking_send(message: dict[str, Any]) -> None:
            nonlocal started
            if refused:
                return
            started = started or message["type"] == "http.response.start"
            await send(message)

        try:
            await self.app(scope, counting_receive, tracking_send)
        except _TooLarge:
            if not refused:
                raise

    async def _refuse(self, send: Send, reason: str) -> None:
        logger.warning(
            "request body too large",
            extra={"agent": self.agent, "reason": reason, "limit_bytes": self.max_bytes},
        )
        payload = json.dumps({"detail": "request body too large"}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(payload)).encode()),
                    (b"connection", b"close"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload})


def _declared_length(scope: Scope) -> int | None:
    for name, value in scope["headers"]:
        if name == b"content-length":
            text = value.strip()
            return int(text) if text.isdigit() else None
    return None
