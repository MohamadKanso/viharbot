"""Authenticate as the read-only reader GitHub App. This module is the ONLY place that sends a
non-GET request, and the only such request is the token exchange for the app's own installation.

The flow: read the app's secret (id, installation id, private key), sign a 10-minute JWT, exchange
it for a one-hour installation token scoped to one repository, and **refuse the token if it carries
any permission beyond the reader's read-only set**. A token that is broader than expected is never
used, because the founder's promise is that the monitor holds no write credential.

Keys and tokens are never logged and never appear in an error message.
"""

from __future__ import annotations

import base64
import datetime
import json
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

API = "https://api.github.com"
EXPECTED_READER_PERMISSIONS = {
    "actions": "read", "checks": "read", "contents": "read", "issues": "read",
    "metadata": "read", "pull_requests": "read", "statuses": "read",
}
REQUIRED_SECRET_KEYS = ("app_id", "installation_id", "private_key_pem")
REFRESH_MARGIN_SECONDS = 300


class AuthError(RuntimeError):
    """Authentication failed. The message is fixed wording and contains no key or token."""


class UnsafeCredential(AuthError):
    """The token is broader than the reader may hold, so it was refused."""


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def make_jwt(app_id: int | str, pem: str, now: datetime.datetime) -> str:
    """A GitHub App JWT: RS256, issued 60 seconds in the past, valid at most 10 minutes."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    issued = int(now.timestamp())
    header = {"alg": "RS256", "typ": "JWT"}
    claims = {"iat": issued - 60, "exp": issued + 540, "iss": str(app_id)}
    signing_input = ".".join(
        _b64url(json.dumps(part, separators=(",", ":")).encode()) for part in (header, claims))
    try:
        key = serialization.load_pem_private_key(pem.encode(), password=None)
        signature = key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
    except (ValueError, TypeError, AttributeError):
        raise AuthError("the app's private key could not be used") from None
    return f"{signing_input}.{_b64url(signature)}"


def load_secret_from_aws(client, secret_id: str) -> dict:
    """Read and validate the app secret through an injected Secrets Manager client."""
    try:
        text = client.get_secret_value(SecretId=secret_id)["SecretString"]
        secret = json.loads(text)
    except Exception:  # any AWS or parse error: say nothing about its content
        raise AuthError("the reader secret could not be read") from None
    if not isinstance(secret, dict) or any(not secret.get(k) for k in REQUIRED_SECRET_KEYS):
        raise AuthError("the reader secret is incomplete")
    return secret


class InstallationTokens:
    """Mints and caches the reader's installation token."""

    def __init__(
        self,
        secret_loader: Callable[[], dict],
        repo: str,
        clock: Callable[[], datetime.datetime],
        opener: Callable[..., Any] = urllib.request.urlopen,
    ) -> None:
        self._load, self._repo, self._clock, self._open = secret_loader, repo, clock, opener
        self._token: str | None = None
        self._expires: datetime.datetime | None = None

    def token(self) -> str:
        now = self._clock()
        if (self._token and self._expires
                and (self._expires - now).total_seconds() > REFRESH_MARGIN_SECONDS):
            return self._token
        secret = self._load()
        app_jwt = make_jwt(secret["app_id"], secret["private_key_pem"], now)
        owner_name = self._repo.split("/", 1)[1]
        request = urllib.request.Request(  # noqa: S310 - fixed https URL
            f"{API}/app/installations/{int(secret['installation_id'])}/access_tokens",
            data=json.dumps({"repositories": [owner_name]}).encode(), method="POST",
            headers={"Authorization": f"Bearer {app_jwt}", "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "viharbot-kepler",
                     "Content-Type": "application/json"},
        )
        try:
            with self._open(request, timeout=20) as response:
                issued = json.loads(response.read())
        except urllib.error.HTTPError as error:
            raise AuthError(f"GitHub refused the token exchange (HTTP {error.code})") from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            raise AuthError("GitHub could not be reached for the token exchange") from None
        if issued.get("permissions") != EXPECTED_READER_PERMISSIONS:
            raise UnsafeCredential("the installation token's permissions are not exactly the "
                                   "reader's read-only set, so it was refused")
        names = sorted(r.get("name") for r in issued.get("repositories", []))
        if names != [owner_name]:
            raise UnsafeCredential("the installation token reaches more than the one repository")
        token, expires = issued.get("token"), issued.get("expires_at")
        try:
            expiry = datetime.datetime.strptime(expires, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=datetime.timezone.utc)
        except (TypeError, ValueError):
            raise AuthError("GitHub returned an unusable token expiry") from None
        if not isinstance(token, str) or not token:
            raise AuthError("GitHub returned no token")
        self._token, self._expires = token, expiry
        return token
