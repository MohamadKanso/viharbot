"""Which model each identity uses, read from the services themselves.

The chat page shows a model logo and alias next to each agent. The truth is each service's own
environment, so the web service asks every agent for its alias (GET /model, no inference, no
key) and groups the answers by identity using the approved execution bindings. Nothing is copied
into the web container's configuration, so it cannot drift from what the agents actually run.
"""

from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import urlsplit
from urllib.request import Request

from .execution_bindings import default_bindings
from .litellm_client import urlopen as no_redirect_urlopen

TIMEOUT_SECONDS = 2.5
CACHE_SECONDS = 30.0
_cache: dict[str, Any] = {"at": 0.0, "value": None}
_lock = threading.Lock()


def _alias(url: str) -> str | None:
    """One agent's configured alias, or None if it is unreachable, remote (AgentCore) or unset."""
    if not url or url.startswith("agentcore://"):
        return None
    parts = urlsplit(url)
    try:
        request = Request(f"{parts.scheme}://{parts.netloc}/model")
        with no_redirect_urlopen(request, TIMEOUT_SECONDS) as response:
            data = json.loads(response.read(4096).decode("utf-8"))
        alias = data.get("model_alias")
        return alias if isinstance(alias, str) and alias.strip() else None
    except Exception:
        return None


def collect(service_urls: dict[str, str]) -> dict[str, list[dict[str, str | None]]]:
    """identity id -> [{capability, service, model}], one entry per approved binding."""
    with ThreadPoolExecutor(max_workers=8) as pool:
        aliases = dict(zip(service_urls, pool.map(_alias, service_urls.values())))
    result: dict[str, list[dict[str, str | None]]] = {}
    for binding in default_bindings().bindings:
        if binding.enabled:
            result.setdefault(binding.identity_id, []).append({
                "capability": binding.capability.value,
                "service": binding.service_name,
                "model": aliases.get(binding.service_name),
            })
    planner = os.getenv("CHARLIE_PLANNER_MODEL", "").strip()
    if planner:
        result.setdefault("charlie", []).append(
            {"capability": "planning", "service": "planner", "model": planner})
    return result


def team_models(service_urls: dict[str, str]) -> dict[str, list[dict[str, str | None]]]:
    """`collect`, cached briefly so the page does not poll every agent on each load."""
    with _lock:
        if _cache["value"] is not None and time.monotonic() - _cache["at"] < CACHE_SECONDS:
            return _cache["value"]
    value = collect(service_urls)
    with _lock:
        _cache.update(at=time.monotonic(), value=value)
    return value
