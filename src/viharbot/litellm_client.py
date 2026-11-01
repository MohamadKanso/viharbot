"""Small OpenAI-compatible LiteLLM client with an HTTP credential guard."""

from __future__ import annotations

import ipaddress
import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class LiteLLMError(RuntimeError):
    """A safe-to-log LiteLLM configuration or request error."""


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, new_url):
        return None


_HTTP_OPENER = build_opener(_NoRedirectHandler)


def urlopen(request: Request, timeout: int):
    """Open without following redirects that could leak proxy credentials."""
    return _HTTP_OPENER.open(request, timeout=timeout)


def _configuration() -> tuple[str, str]:
    base_url = os.getenv("LITELLM_BASE_URL", "").strip().rstrip("/")
    api_key = os.getenv("LITELLM_API_KEY", "").strip()
    if not base_url or not api_key:
        raise LiteLLMError("LITELLM_BASE_URL and LITELLM_API_KEY are required")

    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise LiteLLMError("LITELLM_BASE_URL must be an HTTP or HTTPS URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise LiteLLMError("LITELLM_BASE_URL must not contain credentials or query parameters")
    if parsed.scheme == "http":
        host = parsed.hostname.lower()
        allowed_http = host == "localhost"
        try:
            address = ipaddress.ip_address(host)
            private_ranges = (
                ipaddress.ip_network("10.0.0.0/8"),
                ipaddress.ip_network("172.16.0.0/12"),
                ipaddress.ip_network("192.168.0.0/16"),
                ipaddress.ip_network("fc00::/7"),
            )
            allowed_http = address.is_loopback or any(
                address.version == network.version and address in network
                for network in private_ranges
            )
        except ValueError:
            pass
        if not allowed_http:
            raise LiteLLMError(
                "refusing to send API key over public HTTP; use HTTPS or a private IP tunnel"
            )
    return base_url, api_key


def _endpoint(base_url: str, resource: str) -> str:
    if urlsplit(base_url).path.rstrip("/").endswith("/v1"):
        return f"{base_url}/{resource}"
    return f"{base_url}/v1/{resource}"


def litellm_credentials() -> tuple[str, str]:
    """Return a validated OpenAI-compatible API base URL and proxy key."""
    base_url, api_key = _configuration()
    if urlsplit(base_url).path.rstrip("/").endswith("/v1"):
        api_base = base_url
    else:
        api_base = f"{base_url}/v1"
    return api_base, api_key


def _request(resource: str, payload: dict | None = None) -> dict:
    base_url, api_key = _configuration()
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = Request(_endpoint(base_url, resource), data=body, headers=headers)
    try:
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise LiteLLMError(f"LiteLLM returned HTTP {exc.code}") from None
    except (URLError, TimeoutError, OSError, json.JSONDecodeError):
        raise LiteLLMError("LiteLLM request failed or returned invalid JSON") from None


def model_info() -> dict:
    """Read-only routing and price metadata (GET model/info). Not an inference call."""
    return _request("model/info")


def check_connection() -> dict:
    """Call the non-inference OpenAI-compatible models endpoint."""
    return _request("models")


