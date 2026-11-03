"""Two-phase HMAC ownership markers bound to the immutable app id and team (ADR-007).

Claim: HMAC over the claim fields, written into the spec at creation. Bound: HMAC over the claim
fields and the provider's app id, written once the id is known. Only a bound marker that verifies
for this app id and this team is ownership evidence; a claim alone, or a marker copied from another
app, never is.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import asdict, dataclass

from .provider import ProviderApp

CLAIM_KEY = "ORBIT_CLAIM"
BOUND_KEY = "ORBIT_BOUND"

BOUND_EXPIRED = "bound_expired"
BOUND_LIVE = "bound_live"
CLAIM_ONLY = "claim_only"
UNOWNED = "unowned"  # missing, invalid, foreign team or tampered


@dataclass(frozen=True)
class Claim:
    claim_id: str
    instance_id: str
    team_id: str
    created_at: int
    expires_at: int
    request_hash: str


def _mac(key: bytes, label: str, claim: Claim, app_id: str = "") -> str:
    body = json.dumps(asdict(claim), sort_keys=True, separators=(",", ":"))
    return hmac.new(key, f"{label}|{body}|{app_id}".encode(), hashlib.sha256).hexdigest()


def claim_entry(key: bytes, claim: Claim) -> str:
    return json.dumps({**asdict(claim), "mac": _mac(key, "claim", claim)}, sort_keys=True)


def bound_entry(key: bytes, claim: Claim, app_id: str) -> str:
    return _mac(key, "bound", claim, app_id)


def parse_claim(key: bytes, env: dict[str, str]) -> Claim | None:
    """The claim, only if its HMAC verifies. Anything malformed is None."""
    try:
        data = json.loads(env[CLAIM_KEY])
        mac = data.pop("mac")
        claim = Claim(**data)
        if not isinstance(mac, str) or not hmac.compare_digest(mac.encode("utf-8", "replace"), _mac(key, "claim", claim).encode()):
            return None
        return claim
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def verified_bound(key: bytes, app: ProviderApp, team_id: str) -> Claim | None:
    """The claim if the bound marker verifies for this app id and this team."""
    claim = parse_claim(key, app.env)
    if claim is None or claim.team_id != team_id or app.team_id != team_id:
        return None
    presented = app.env.get(BOUND_KEY)
    if not isinstance(presented, str):
        return None
    if not hmac.compare_digest(presented.encode("utf-8", "replace"), bound_entry(key, claim, app.app_id).encode()):
        return None
    return claim


def classify(key: bytes, app: ProviderApp, team_id: str, now: float) -> str:
    claim = verified_bound(key, app, team_id)
    if claim is not None:
        return BOUND_EXPIRED if now >= claim.expires_at else BOUND_LIVE
    claim = parse_claim(key, app.env)
    if claim is not None and claim.team_id == team_id and app.team_id == team_id:
        return CLAIM_ONLY  # includes a bound marker copied from another app
    return UNOWNED
