"""The pixel service: generates a slide deck from the team's results over private JSON REST.

Pixel's intelligence is the model: it chooses the slides, writes their text within fixed token
schemas and rates each item. Everything deterministic is code: layout and token schemas, validation
between calls, a hard model-call budget, and the .pptx rendering. All inference goes through
LiteLLM with Pixel's own alias in PIXEL_MODEL. The service refuses to run unless the PPT flag is on.

Limitations: the HTTP interface is unauthenticated like the other private agents. Finished decks
are kept under MEMORY_DIR/pixel/decks (newest PIXEL_MAX_DECKS, default 20) and contain the team's
results, so protect that storage like the other agent memory. Nothing here has been run against a
real model in automated checks.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, Response
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from .contracts import SpecialistResponse
from .inference_config import effective_inference, legacy_inference
from .model_agent import build_agent
from .observability import configure_logging, get_request_id
from .pixel.flag import ppt_generation_enabled
from .pixel.instructions import PIXEL_INSTRUCTIONS
from .pixel.models import EngagementProfile
from .pixel.render import render_html
from .pixel.runner import PixelError, build_deck_in_memory
from .schemas import DeckResult

configure_logging()
logger = logging.getLogger("pixel")
app = FastAPI(title="ViharBot Pixel Slide Agent", version="1.0")

# Result sections from the other agents that may be used as evidence, by name.
EVIDENCE_KEYS = ("research", "fact_check", "source_quality", "security", "privacy_review", "synthesis")
DECK_ID = re.compile(r"^deck-[0-9a-f]{12}$")


class PixelRequest(BaseModel):
    question: str = Field(min_length=1, max_length=6000)
    context: dict[str, Any] = Field(default_factory=dict)


def decks_dir() -> Path:
    return Path(os.getenv("MEMORY_DIR", "data")) / "pixel" / "decks"


def _int_env(name: str, default: int, low: int, high: int) -> int:
    try:
        return min(high, max(low, int(os.getenv(name, str(default)))))
    except ValueError:
        return default


# The page is self-contained (inline CSS, no scripts or assets), so it can be locked down hard.
HTML_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'",
    "X-Content-Type-Options": "nosniff",
}


def prune_decks(keep: int) -> None:
    files = sorted(decks_dir().glob("deck-*.pptx"), key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in files[keep:]:
        stale.unlink(missing_ok=True)
        stale.with_suffix(".html").unlink(missing_ok=True)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "agent": "pixel"}


@app.get("/model")
def model() -> dict[str, str | None]:
    """The configured model alias, so the web UI can show who uses what. Not a secret."""
    return {"agent": "pixel", "model_alias": os.getenv("PIXEL_MODEL", "").strip() or None}


@app.post("/process", response_model=SpecialistResponse)
def process(
    body: PixelRequest,
    response: Response,
    x_request_id: str | None = Header(default=None),
) -> SpecialistResponse:
    request_id = get_request_id(x_request_id) if x_request_id else str(uuid4())
    response.headers["X-Request-ID"] = request_id
    if not ppt_generation_enabled():
        raise HTTPException(status_code=503, detail="PPT generation is disabled",
                            headers={"X-Request-ID": request_id})
    model_alias = os.getenv("PIXEL_MODEL", "").strip()
    started = time.perf_counter()
    inference = legacy_inference(model_alias, PIXEL_INSTRUCTIONS)
    try:
        inference = effective_inference(
            "pixel", legacy_alias=model_alias, legacy_prompt=PIXEL_INSTRUCTIONS
        )
        evidence = {
            name: json.dumps(body.context[name], ensure_ascii=False)
            for name in EVIDENCE_KEYS
            if body.context.get(name)
        }
        deck_id = f"deck-{uuid4().hex[:12]}"
        decks_dir().mkdir(parents=True, exist_ok=True)
        built = build_deck_in_memory(
            deck_id,
            EngagementProfile(scope=body.question[:500]),
            evidence,
            _int_env("PIXEL_TARGET_SLIDES", 6, 1, 30),
            decks_dir() / f"{deck_id}.pptx",
            agent_factory=lambda: build_agent(
                model_alias=inference.model_alias, system_prompt=inference.system_prompt
            ),
            max_model_calls=_int_env("PIXEL_MAX_MODEL_CALLS", 8, 2, 40),
        )
        (decks_dir() / f"{deck_id}.html").write_text(
            render_html(built.payloads, download_url=f"/api/decks/{deck_id}", warnings=built.warnings),
            encoding="utf-8",
        )
        prune_decks(_int_env("PIXEL_MAX_DECKS", 20, 1, 500))
        result = DeckResult(
            deck_id=deck_id,
            slide_count=built.slide_count,
            rejected_slides=built.rejected,
            warnings=built.warnings,
            model_calls=built.model_calls,
        )
        logger.info(
            "pixel completed",
            extra={
                "agent": "pixel",
                "request_id": request_id,
                **inference.log_extra(),
                "model_calls": built.model_calls,
                "input_tokens": built.input_tokens,
                "output_tokens": built.output_tokens,
                "slides": built.slide_count,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            },
        )
        return SpecialistResponse(
            request_id=request_id,
            agent="pixel",
            requested_model_alias=inference.model_alias,
            result=result.model_dump(),
        )
    except Exception as exc:
        logger.error(
            "pixel failed",
            extra={
                "agent": "pixel",
                "request_id": request_id,
                **inference.log_extra(),
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                "error": type(exc).__name__ if not isinstance(exc, PixelError) else str(exc),
            },
        )
        raise HTTPException(
            status_code=502, detail="pixel processing failed", headers={"X-Request-ID": request_id}
        ) from None


@app.get("/decks/{deck_id}.pptx")
def download(deck_id: str) -> FileResponse:
    if not DECK_ID.fullmatch(deck_id):
        raise HTTPException(status_code=404, detail="Deck not found")
    path = decks_dir() / f"{deck_id}.pptx"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Deck not found")
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        filename=f"{deck_id}.pptx",
    )


@app.get("/decks/{deck_id}.html")
def view(deck_id: str) -> HTMLResponse:
    """Pixel's HTML rendition of the deck, in the same colours as the .pptx."""
    if not DECK_ID.fullmatch(deck_id):
        raise HTTPException(status_code=404, detail="Deck not found")
    path = decks_dir() / f"{deck_id}.html"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Deck not found")
    return HTMLResponse(path.read_text(encoding="utf-8"), headers=HTML_HEADERS)
