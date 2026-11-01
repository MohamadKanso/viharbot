"""The web service's optional slide-deck step, run after the agent pipeline.

Pixel is bound to the `slide_generation` capability (execution_config/bindings.toml) but is not a
stage of the typed pipeline: the pipeline runs unchanged, then, only if the PPT flag is on and
the request asks for a deck, the pixel service is called with the team's results as evidence.
A failure here never fails the chat; it is reported in `deck` and in the agent activity.
"""

from __future__ import annotations

import functools
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request

import os

from .chat_models import DeckInfo
from .demo import AgentPipelineError, invoke_agent, post_json
from .execution_bindings import default_bindings
from .identity import Capability
from .identity_registry import default_registry
from .litellm_client import urlopen as no_redirect_urlopen
from .pipeline_state import AgentActivity
from .pixel.flag import ppt_generation_enabled  # noqa: F401  (re-exported for web_app)
from .schemas import DeckResult

PIXEL_TIMEOUT_SECONDS = 300
MAX_DECK_BYTES = 25 * 1024 * 1024
DECK_ID = re.compile(r"^deck-[0-9a-f]{12}$")
# Deliberately narrow and deterministic: a request names a deck, slides or a presentation file.
DECK_INTENT = re.compile(r"\b(slide\s*deck|slides|power\s?point|pptx?|presentation)\b", re.IGNORECASE)


def wants_deck(message: str) -> bool:
    return bool(DECK_INTENT.search(message))


def pixel_url() -> str:
    return os.getenv("PIXEL_URL", "http://pixel:8000/process")


def pixel_authorized() -> bool:
    """Only an enabled repository binding for a registered identity authorizes the step."""
    binding = default_bindings().lookup(Capability.SLIDE_GENERATION)
    return bool(binding and binding.enabled and binding.identity_id in default_registry())


def _evidence(pipeline_result: dict[str, Any]) -> dict[str, Any]:
    def result(key: str) -> Any:
        return (pipeline_result.get(key) or {}).get("result")

    return {
        "research": (pipeline_result.get("research") or {}).get("findings"),
        "fact_check": result("fact_check"),
        "source_quality": result("source_quality"),
        "security": (pipeline_result.get("security") or {}).get("assessment"),
        "privacy_review": result("privacy_review"),
        "synthesis": result("synthesis"),
    }


def run_pixel_stage(
    question: str, pipeline_result: dict[str, Any], request_id: str, *, send=None
) -> tuple[DeckInfo, dict[str, Any] | None]:
    """Call the pixel service. Returns the public deck info and an activity row, if one ran."""
    if not pixel_authorized():
        return DeckInfo(status="failed", error="Slide generation is not authorized"), None
    send = send or functools.partial(post_json, timeout_cap=PIXEL_TIMEOUT_SECONDS)
    activity: list[AgentActivity] = []
    try:
        _, domain = invoke_agent(
            send,
            pixel_url(),
            {"question": question, "context": _evidence(pipeline_result)},
            request_id,
            "pixel",
            activity,
        )
    except AgentPipelineError:
        return (
            DeckInfo(status="failed", error="The slide deck could not be generated"),
            activity[-1].to_legacy() if activity else None,
        )
    deck = DeckResult.model_validate(domain)
    return (
        DeckInfo(
            status="complete",
            deck_id=deck.deck_id,
            slide_count=deck.slide_count,
            download_url=f"/api/decks/{deck.deck_id}",
            rejected_slides=deck.rejected_slides,
            warnings=deck.warnings,
        ),
        activity[-1].to_legacy(),
    )


def fetch_deck(deck_id: str) -> bytes | None:
    """The finished .pptx from the pixel service, or None if it does not exist."""
    if not DECK_ID.fullmatch(deck_id):
        return None
    parts = urlsplit(pixel_url())
    request = Request(f"{parts.scheme}://{parts.netloc}/decks/{deck_id}.pptx")
    try:
        with no_redirect_urlopen(request, 30) as response:
            data = response.read(MAX_DECK_BYTES + 1)
    except (HTTPError, URLError, TimeoutError, OSError):
        return None
    return data if 0 < len(data) <= MAX_DECK_BYTES else None
