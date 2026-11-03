"""Service-to-service authentication (ADR-006, step A2): verify and mint caller assertions.

Scope is authentication only. A verified assertion proves which service signed a request, that the
request was bound to that signature, and that it was not replayed. It does NOT prove the caller is
authorized by a delegation grant: the optional ``dlg`` claim is ignored here (issue #95; A3 makes it
mandatory). Limitations of this step are listed in docs/service-authentication.md.

Verification profile (docs/adr/ADR-006): EdDSA only; ``kid`` resolved only from the local registry
file; ``typ`` is ``viharbot-assertion+jwt``; ``iss`` and ``sub`` must equal the subject the registry
binds to the ``kid``; short lifetime, bounded clock skew; request binding over method, canonical
target and body hash; audience binding; a bounded per-process ``jti`` replay cache.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping
from uuid import uuid4

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from fastapi import HTTPException, Request

from .observability import get_request_id

logger = logging.getLogger("service_auth")

ASSERTION_HEADER = "X-Viharbot-Assertion"
ASSERTION_TYP = "viharbot-assertion+jwt"
ALGORITHM = "EdDSA"
REQUIRED_CLAIMS = (
    "iss", "sub", "aud", "exp", "iat", "jti", "cap", "eng", "cls", "mth", "tgt", "req_sha256",
)
MAX_LIFETIME_SECONDS = 60
CLOCK_SKEW_SECONDS = 10
REGISTRY_REFRESH_SECONDS = 60
UNKNOWN_KID_RELOAD_SECONDS = 10
REGISTRY_GRACE_SECONDS = 15 * 60
MAX_REPLAY_ENTRIES = 10_000
MAX_TOKEN_BYTES = 8192
MAX_JTI_LENGTH = 128
MAX_JSON_NESTING = 16

ENABLE_ENV = "VIHARBOT_SERVICE_AUTH"
REGISTRY_ENV = "VIHARBOT_KEY_REGISTRY"
SIGNING_KEY_ENV = "VIHARBOT_SIGNING_KEY"

_UNRESERVED = frozenset(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
)
_PERCENT = re.compile(rb"%([0-9A-Fa-f]{2})")


class AuthError(Exception):
    """A request is refused. ``reason`` is a fixed code that is safe to log."""

    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


class ServiceAuthConfigError(RuntimeError):
    """Authentication is enabled but cannot run safely; the service must not start."""


@dataclass(frozen=True)
class Caller:
    """The verified caller. Built only from a verified token and the local registry."""

    sub: str
    kid: str
    capability: str
    engagement: str
    classification: str


# --- encoding helpers -------------------------------------------------------------------------


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    if not isinstance(text, str) or not re.fullmatch(r"[A-Za-z0-9_-]*", text):
        raise ValueError("not base64url")
    if len(text) % 4 == 1:
        raise ValueError("bad base64url length")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# --- canonical request target -----------------------------------------------------------------


def _check_printable(raw: bytes) -> None:
    if any(b < 0x21 or b > 0x7E for b in raw):
        raise AuthError(400, "target_not_printable_ascii")


def _check_path(path: bytes) -> None:
    if not path.startswith(b"/"):
        raise AuthError(400, "target_path_not_absolute")
    for match in _PERCENT.finditer(path):
        digits = match.group(1)
        if digits != digits.upper():
            raise AuthError(400, "target_noncanonical_percent_encoding")
        value = int(digits, 16)
        if value in _UNRESERVED:
            raise AuthError(400, "target_noncanonical_percent_encoding")
        if value in (0x2F, 0x5C, 0x00):  # / \ NUL ('.' is unreserved and caught above)
            raise AuthError(400, "target_encoded_separator")
    if re.search(rb"%(?![0-9A-Fa-f]{2})", path):
        raise AuthError(400, "target_malformed_percent_encoding")
    segments = path.split(b"/")[1:]
    if segments == [b""]:  # the root path "/"
        return
    for index, segment in enumerate(segments):
        if segment == b"" and index != len(segments) - 1:
            raise AuthError(400, "target_empty_segment")
        if segment in (b".", b".."):
            raise AuthError(400, "target_dot_segment")


def _canonical_query(query: bytes) -> bytes:
    if b";" in query:
        raise AuthError(400, "target_semicolon_in_query")
    pairs = []
    for pair in query.split(b"&"):
        if pair == b"":
            raise AuthError(400, "target_empty_query_pair")
        name, sep, value = pair.partition(b"=")
        pairs.append(_canonical_component(name) + sep + _canonical_component(value))
    return b"&".join(pairs)


def _canonical_component(part: bytes) -> bytes:
    """Upper-case percent-escape hex and reject encoded unreserved characters.

    Literal bytes are kept as received and never re-encoded, so a literal ``+`` stays distinct from
    ``%2B`` and ``%20`` (applications decode them differently) and canonicalization never changes
    which request the application sees.
    """
    if re.search(rb"%(?![0-9A-Fa-f]{2})", part):
        raise AuthError(400, "target_malformed_percent_encoding")

    def upper(match: re.Match[bytes]) -> bytes:
        value = int(match.group(1), 16)
        if value in _UNRESERVED:
            raise AuthError(400, "target_noncanonical_percent_encoding")
        return b"%" + match.group(1).upper()

    return _PERCENT.sub(upper, part)


def canonical_target(raw_path: bytes, query_string: bytes | None) -> str:
    """Canonical ``tgt`` from the target exactly as received (no path normalization).

    ``query_string`` is None when the request had no ``?``. Raises AuthError(400) for any target the
    ADR says to reject.
    """
    _check_printable(raw_path)
    if query_string is not None:
        _check_printable(query_string)
    if b"?" in raw_path or b"#" in raw_path:
        raise AuthError(400, "target_unexpected_delimiter")
    _check_path(raw_path)
    target = raw_path
    if query_string is not None:
        target += b"?" + _canonical_query(query_string)
    return target.decode("ascii")


def split_target(target: str) -> tuple[bytes, bytes | None]:
    """Split a raw ``path?query`` string the way the ASGI server does (first ``?``)."""
    raw = target.encode("latin-1", errors="replace")
    path, sep, query = raw.partition(b"?")
    return path, (query if sep else None)


# --- key registry -----------------------------------------------------------------------------


@dataclass(frozen=True)
class KeyEntry:
    kid: str
    sub: str
    key: Ed25519PublicKey
    capabilities: frozenset[str]


def parse_registry(text: str) -> dict[str, KeyEntry]:
    """Parse registry TOML. Any defect raises ValueError so a bad file is never half-applied.

    Format: ``[[keys]]`` tables with ``kid``, ``sub``, ``public_key`` (base64url, the 32 raw
    Ed25519 bytes) and ``capabilities`` (list of strings). Public keys only.
    """
    try:
        data = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, RecursionError) as exc:
        raise ValueError("registry is not valid TOML") from exc
    items = data.get("keys", [])
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise ValueError("keys must be a list of tables")
    entries: dict[str, KeyEntry] = {}
    for item in items:
        kid, sub, caps = item.get("kid"), item.get("sub"), item.get("capabilities")
        if not isinstance(kid, str) or not kid or not isinstance(sub, str) or not sub:
            raise ValueError("each key needs a non-empty kid and sub")
        if kid in entries:
            raise ValueError("duplicate kid")
        if not isinstance(caps, list) or not all(isinstance(c, str) and c for c in caps):
            raise ValueError("capabilities must be a list of strings")
        encoded = item.get("public_key")
        if not isinstance(encoded, str):
            raise ValueError("public_key must be a string")
        raw = b64url_decode(encoded)
        if len(raw) != 32:
            raise ValueError("public_key must be 32 raw Ed25519 bytes")
        entries[kid] = KeyEntry(kid, sub, Ed25519PublicKey.from_public_bytes(raw), frozenset(caps))
    return entries


class KeyRegistry:
    """Local registry of public keys with bounded staleness (ADR-006 propagation bound)."""

    def __init__(self, path: str | Path, clock: Callable[[], float] = time.time) -> None:
        self._path = Path(path)
        self._clock = clock
        self._lock = threading.Lock()
        self._keys: dict[str, KeyEntry] = {}
        self._last_attempt = 0.0
        self._last_success = 0.0

    def _load(self) -> bool:
        self._last_attempt = self._clock()
        try:
            keys = parse_registry(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, AttributeError, RecursionError) as exc:
            logger.warning(
                "key registry refresh failed",
                extra={"agent": "service_auth", "reason": type(exc).__name__},
            )
            return False
        if not keys:
            logger.warning(
                "key registry refresh failed",
                extra={"agent": "service_auth", "reason": "no_keys"},
            )
            return False
        self._keys = keys
        self._last_success = self._last_attempt
        return True

    def load_initial(self) -> None:
        with self._lock:
            if not self._load():
                raise ServiceAuthConfigError(
                    "service authentication is enabled but the key registry is missing, invalid "
                    "or has no keys"
                )

    def lookup(self, kid: str) -> KeyEntry | None:
        """Return the entry for ``kid``; raise AuthError(503) once the registry is too stale."""
        with self._lock:
            now = self._clock()
            if now - self._last_attempt >= REGISTRY_REFRESH_SECONDS:
                self._load()
            if now - self._last_success > REGISTRY_GRACE_SECONDS:
                raise AuthError(503, "key_registry_unavailable")
            entry = self._keys.get(kid)
            if entry is None and now - self._last_attempt >= UNKNOWN_KID_RELOAD_SECONDS:
                self._load()
                if now - self._last_success > REGISTRY_GRACE_SECONDS:
                    raise AuthError(503, "key_registry_unavailable")
                entry = self._keys.get(kid)
            return entry


# --- replay cache -----------------------------------------------------------------------------


class ReplayCache:
    """Per-process ``jti`` cache. Entries live until expiry plus skew; the size is bounded.

    A token is valid while ``now <= exp + skew`` (see ``_check_claims``), so an entry is retained
    and still detects a replay at that same instant and is evicted only strictly after it.

    When full of unexpired entries it refuses new tokens (fail closed) rather than evicting one,
    because evicting an unexpired entry would let that token be replayed.
    """

    def __init__(self, max_entries: int = MAX_REPLAY_ENTRIES) -> None:
        self._max = max_entries
        self._seen: dict[tuple[str, str], float] = {}
        self._lock = threading.Lock()

    def check_and_add(self, sub: str, jti: str, expires_at: float, now: float) -> None:
        with self._lock:
            key = (sub, jti)
            held_until = self._seen.get(key)
            if held_until is not None and held_until >= now:
                raise AuthError(401, "replayed_token")
            if len(self._seen) >= self._max:
                self._seen = {k: v for k, v in self._seen.items() if v >= now}
                if len(self._seen) >= self._max:
                    raise AuthError(503, "replay_cache_full")
            self._seen[key] = expires_at + CLOCK_SKEW_SECONDS


# --- verification -----------------------------------------------------------------------------


def _json_object(segment: str) -> dict[str, Any]:
    raw = b64url_decode(segment)
    depth = deepest = 0
    for byte in raw:  # cheap bound on nesting before the recursive parser sees it
        if byte in b"[{":
            depth += 1
            deepest = max(deepest, depth)
        elif byte in b"]}":
            depth -= 1
    if deepest > MAX_JSON_NESTING:
        raise ValueError("nesting too deep")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("not an object")
    return value


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


class ServiceAuthenticator:
    """Verifies caller assertions for one service (the audience)."""

    def __init__(
        self,
        *,
        audience: str,
        registry: KeyRegistry,
        clock: Callable[[], float] = time.time,
        replay: ReplayCache | None = None,
    ) -> None:
        self.audience = audience
        self._registry = registry
        self._clock = clock
        self._replay = replay or ReplayCache()

    def verify(
        self, token: str | None, *, method: str, raw_path: bytes, query: bytes | None, body: bytes
    ) -> Caller:
        if not token:
            raise AuthError(401, "missing_token")
        if len(token.encode("utf-8", errors="replace")) > MAX_TOKEN_BYTES:
            raise AuthError(401, "malformed_token")
        try:
            header_b64, payload_b64, sig_b64 = token.split(".")
            header = _json_object(header_b64)
            signature = b64url_decode(sig_b64)
            claims = _json_object(payload_b64)
        except (ValueError, TypeError, binascii.Error, RecursionError, MemoryError):
            raise AuthError(401, "malformed_token") from None
        # jku, x5u, x5c, jwk and every other header are ignored: only alg, typ and kid are read.
        if header.get("alg") != ALGORITHM:
            raise AuthError(401, "algorithm_not_allowed")
        if header.get("typ") != ASSERTION_TYP:
            raise AuthError(401, "wrong_token_type")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise AuthError(401, "missing_kid")
        entry = self._registry.lookup(kid)
        if entry is None:
            raise AuthError(401, "unknown_kid")
        try:
            entry.key.verify(signature, f"{header_b64}.{payload_b64}".encode("ascii"))
        except InvalidSignature:
            raise AuthError(401, "bad_signature") from None
        # Only the verified payload is read from here on.
        now = self._clock()
        self._check_claims(claims, entry, now)
        capability = claims["cap"]
        if capability not in entry.capabilities:
            raise AuthError(403, "capability_not_permitted")
        self._check_binding(claims, method, raw_path, query, body)
        self._replay.check_and_add(entry.sub, claims["jti"], claims["exp"], now)
        return Caller(entry.sub, kid, capability, claims["eng"], claims["cls"])

    def _check_claims(self, claims: dict[str, Any], entry: KeyEntry, now: float) -> None:
        for name in REQUIRED_CLAIMS:
            if name not in claims:
                raise AuthError(401, "missing_claim")
        for name in ("iss", "sub", "aud", "jti", "cap", "eng", "cls", "mth", "tgt", "req_sha256"):
            if not isinstance(claims[name], str) or not claims[name]:
                raise AuthError(401, "malformed_claim")
        if not _is_int(claims["exp"]) or not _is_int(claims["iat"]):
            raise AuthError(401, "malformed_claim")
        if len(claims["jti"]) > MAX_JTI_LENGTH:
            raise AuthError(401, "malformed_claim")
        # `dlg` is optional and ignored in A2 (issue #95); it is neither read nor trusted.
        if claims["iss"] != entry.sub or claims["sub"] != entry.sub:
            raise AuthError(401, "subject_mismatch")
        if claims["aud"] != self.audience:
            raise AuthError(401, "wrong_audience")
        if claims["exp"] <= claims["iat"] or claims["exp"] - claims["iat"] > MAX_LIFETIME_SECONDS:
            raise AuthError(401, "invalid_lifetime")
        if claims["iat"] > now + CLOCK_SKEW_SECONDS:
            raise AuthError(401, "not_yet_valid")
        if now > claims["exp"] + CLOCK_SKEW_SECONDS:
            raise AuthError(401, "expired")

    def _check_binding(
        self, claims: dict[str, Any], method: str, raw_path: bytes, query: bytes | None, body: bytes
    ) -> None:
        target = canonical_target(raw_path, query)
        if claims["mth"] != method.upper():
            raise AuthError(401, "binding_mismatch")
        if not hmac.compare_digest(claims["tgt"].encode(), target.encode()):
            raise AuthError(401, "binding_mismatch")
        digest = hashlib.sha256(body).hexdigest()
        if not hmac.compare_digest(claims["req_sha256"].encode(), digest.encode()):
            raise AuthError(401, "binding_mismatch")


# --- FastAPI integration ----------------------------------------------------------------------


def _log_decision(
    audience: str, request_id: str, decision: str, reason: str, caller: Caller | None, target: str
) -> None:
    log = logger.info if decision == "allow" else logger.warning
    log(
        "service authentication decision",
        extra={
            "agent": audience,
            "request_id": request_id,
            "decision": decision,
            "reason": reason,
            "caller": caller.sub if caller else None,
            "capability": caller.capability if caller else None,
            "target": target,
        },
    )


def require_assertion(authenticator: ServiceAuthenticator) -> Callable[[Request], Any]:
    """Build a FastAPI dependency. Identity comes only from the ``X-Viharbot-Assertion`` token."""

    async def dependency(request: Request) -> Caller:
        request_id = get_request_id(request.headers.get("x-request-id")) or str(uuid4())
        path = request.scope.get("raw_path")
        raw_path = path if isinstance(path, bytes) else b""
        query_bytes = request.scope.get("query_string", b"")
        # ASGI cannot tell "no ?" from "empty ?"; an empty query is signed as no query.
        query = query_bytes if query_bytes else None
        logged_target = raw_path.decode("ascii", errors="replace")[:200].split("?")[0]
        try:
            body = await request.body()
            caller = authenticator.verify(
                request.headers.get(ASSERTION_HEADER),
                method=request.method,
                raw_path=raw_path,
                query=query,
                body=body,
            )
        except AuthError as error:
            _log_decision(
                authenticator.audience, request_id, "deny", error.reason, None, logged_target
            )
            raise HTTPException(
                status_code=error.status,
                detail="authentication failed",
                headers={"X-Request-ID": request_id},
            ) from None
        _log_decision(authenticator.audience, request_id, "allow", "verified", caller, logged_target)
        return caller

    return dependency


def authenticator_from_environment(
    audience: str,
    environ: Mapping[str, str] | None = None,
    clock: Callable[[], float] = time.time,
) -> ServiceAuthenticator | None:
    """None when authentication is off (the default). Raises when it is on but cannot run."""
    env = os.environ if environ is None else environ
    setting = env.get(ENABLE_ENV, "").strip().lower()
    if setting in ("", "0", "off", "false"):
        return None
    if setting not in ("1", "on", "true"):
        raise ServiceAuthConfigError(f"{ENABLE_ENV} must be on or off")
    path = env.get(REGISTRY_ENV, "").strip()
    if not path:
        raise ServiceAuthConfigError(f"{REGISTRY_ENV} is required when authentication is on")
    registry = KeyRegistry(path, clock)
    registry.load_initial()
    return ServiceAuthenticator(audience=audience, registry=registry, clock=clock)


# --- signing side -----------------------------------------------------------------------------


def load_signing_key(key_path: str | None = None, env_var: str = SIGNING_KEY_ENV) -> Ed25519PrivateKey:
    """Load a private key at call time from a file path or an environment variable.

    Accepts an unencrypted PKCS8 PEM or the base64url 32-byte seed. The key is never logged and the
    error messages never include it.
    """
    material = Path(key_path).read_bytes() if key_path else os.environ.get(env_var, "").encode()
    material = material.strip()
    if not material:
        raise ServiceAuthConfigError("no signing key configured")
    try:
        try:
            seed = b64url_decode(material.decode("ascii"))
        except (ValueError, UnicodeDecodeError):
            seed = b""
        if len(seed) == 32:  # a PEM never decodes as base64url (it has spaces and newlines)
            key = Ed25519PrivateKey.from_private_bytes(seed)
        else:
            key = load_pem_private_key(material, password=None)
    except (ValueError, TypeError):
        raise ServiceAuthConfigError("signing key could not be loaded") from None
    if not isinstance(key, Ed25519PrivateKey):
        raise ServiceAuthConfigError("signing key must be Ed25519")
    return key


def mint_assertion(
    *,
    kid: str,
    sub: str,
    audience: str,
    capability: str,
    engagement: str,
    classification: str,
    method: str,
    target: str,
    body: bytes = b"",
    key_path: str | None = None,
    lifetime: int = MAX_LIFETIME_SECONDS,
    now: float | None = None,
    jti: str | None = None,
) -> str:
    """Mint a caller assertion for exactly one request. ``target`` is the raw ``path?query``."""
    if not 0 < lifetime <= MAX_LIFETIME_SECONDS:
        raise ValueError("lifetime must be between 1 and 60 seconds")
    raw_path, query = split_target(target)
    tgt = canonical_target(raw_path, query)
    issued = int(time.time() if now is None else now)
    header = {"alg": ALGORITHM, "typ": ASSERTION_TYP, "kid": kid}
    claims = {
        "iss": sub,
        "sub": sub,
        "aud": audience,
        "exp": issued + lifetime,
        "iat": issued,
        "jti": jti or uuid4().hex,
        "cap": capability,
        "eng": engagement,
        "cls": classification,
        "mth": method.upper(),
        "tgt": tgt,
        "req_sha256": hashlib.sha256(body).hexdigest(),
    }

    def segment(value: dict[str, Any]) -> str:
        return b64url_encode(json.dumps(value, separators=(",", ":"), sort_keys=True).encode())

    signing_input = f"{segment(header)}.{segment(claims)}"
    signature = load_signing_key(key_path).sign(signing_input.encode("ascii"))
    return f"{signing_input}.{b64url_encode(signature)}"
