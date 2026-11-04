"""Outbound service assertions (ADR-006, step A2b): mint a caller assertion for one HTTP request.

The callee side is ``service_auth``. This module only decides whether to attach an
``X-Viharbot-Assertion`` header to a plain HTTP call and builds it with ``service_auth``'s own
canonicalization and minting code (nothing is re-implemented here).

Issuer location: option A of ADR-006 ("Where the issuer's key lives") is accepted (founder decision,
2026-11-03): the web/orchestrator process holds the key and signs, via ``InProcessIssuer``.
``AssertionIssuer`` is kept as the seam so a separate internal signer (option B) can replace it
later without changing any caller. docs/service-authentication.md says what this does not change.

Off by default. With ``VIHARBOT_OUTBOUND_AUTH`` unset nothing here runs and requests are unchanged.
When it is on and a header cannot be produced, ``OutboundAuthError`` is raised and the caller must
not send the request (fail closed; there is no unauthenticated fallback).
"""

from __future__ import annotations

import logging
import os
from typing import Mapping, Protocol
from urllib.parse import urlsplit

from . import service_auth

logger = logging.getLogger("outbound_auth")

ENABLE_ENV = "VIHARBOT_OUTBOUND_AUTH"
KID_ENV = "VIHARBOT_SIGNING_KID"
SUBJECT_ENV = "VIHARBOT_SERVICE_SUBJECT"
KEY_FILE_ENV = "VIHARBOT_SIGNING_KEY_FILE"  # a path; otherwise VIHARBOT_SIGNING_KEY holds the key
DEFAULT_SUBJECT = "web"
OUTBOUND_LIFETIME_SECONDS = 30
# ``eng`` and ``cls`` are required claims but are not enforced until A3 (#95); these are placeholders.
PLACEHOLDER_ENGAGEMENT = "unscoped"
PLACEHOLDER_CLASSIFICATION = "unclassified"

# Destination service (the ``aud``) to the capability its authenticated route requires. Only the
# shared specialist factory enforces assertions today (``specialist_agent.SPECIALIST_CAPABILITY``).
# Research, Security and Pixel do not verify assertions yet, so no token is minted for them.
SPECIALIST_CAPABILITY = "specialist.process"
DESTINATION_CAPABILITIES: dict[str, str] = {
    "fact-check": SPECIALIST_CAPABILITY,
    "source-quality": SPECIALIST_CAPABILITY,
    "privacy-review": SPECIALIST_CAPABILITY,
    "synthesis": SPECIALIST_CAPABILITY,
}


class OutboundAuthError(RuntimeError):
    """Minting is on but no assertion could be produced. The message is safe to log."""


class AssertionIssuer(Protocol):
    """Mints one assertion bound to one request. Implementations must never log key material."""

    def issue(
        self, *, audience: str, capability: str, method: str, target: str, body: bytes
    ) -> str: ...


class InProcessIssuer:
    """Option A: this process holds the signing key and signs directly."""

    def __init__(self, *, kid: str, subject: str, key_path: str | None = None) -> None:
        self._kid = kid
        self._subject = subject
        self._key_path = key_path  # a path, not the key; the key is read on every issue()

    def issue(
        self, *, audience: str, capability: str, method: str, target: str, body: bytes
    ) -> str:
        return service_auth.mint_assertion(
            kid=self._kid,
            sub=self._subject,
            audience=audience,
            capability=capability,
            engagement=PLACEHOLDER_ENGAGEMENT,
            classification=PLACEHOLDER_CLASSIFICATION,
            method=method,
            target=target,
            body=body,
            key_path=self._key_path,
            lifetime=OUTBOUND_LIFETIME_SECONDS,
        )


def issuer_from_environment(environ: Mapping[str, str] | None = None) -> AssertionIssuer | None:
    """None when outbound minting is off (the default). Raises OutboundAuthError when misconfigured."""
    env = os.environ if environ is None else environ
    setting = env.get(ENABLE_ENV, "").strip().lower()
    if setting in ("", "0", "off", "false"):
        return None
    if setting not in ("1", "on", "true"):
        raise OutboundAuthError(f"{ENABLE_ENV} must be on or off")
    kid = env.get(KID_ENV, "").strip()
    if not kid:
        raise OutboundAuthError(f"{KID_ENV} is required when outbound authentication is on")
    subject = env.get(SUBJECT_ENV, "").strip() or DEFAULT_SUBJECT
    return InProcessIssuer(kid=kid, subject=subject, key_path=env.get(KEY_FILE_ENV, "").strip() or None)


def outbound_headers(
    url: str,
    *,
    destination: str,
    method: str,
    body: bytes,
    request_id: str = "",
    issuer: AssertionIssuer | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Headers to add to an outbound request: ``{}`` when minting is off or the destination does
    not verify assertions; otherwise ``{X-Viharbot-Assertion: token}``.

    ``mth``, ``tgt`` and ``req_sha256`` come from the actual request: the method, the URL's path and
    query exactly as sent, and the exact body bytes. Raises OutboundAuthError (fail closed).
    """
    try:
        issuer = issuer if issuer is not None else issuer_from_environment(environ)
    except OutboundAuthError:
        _log(request_id, destination, "error", "misconfigured")
        raise
    if issuer is None:
        return {}
    capability = DESTINATION_CAPABILITIES.get(destination)
    if capability is None:
        return {}
    parts = urlsplit(url)
    target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    try:
        token = issuer.issue(
            audience=destination, capability=capability, method=method, target=target, body=body
        )
    except Exception as exc:  # unreadable or invalid key, bad target, signer failure: all fail closed
        _log(request_id, destination, "error", type(exc).__name__)
        raise OutboundAuthError("could not mint a service assertion") from None
    _log(request_id, destination, "minted", "ok")
    return {service_auth.ASSERTION_HEADER: token}


def _log(request_id: str, destination: str, decision: str, reason: str) -> None:
    log = logger.info if decision == "minted" else logger.error
    log(
        "outbound assertion",
        extra={
            "agent": "orchestrator",
            "request_id": request_id,
            "destination_agent": destination,
            "decision": decision,
            "reason": reason,
        },
    )
