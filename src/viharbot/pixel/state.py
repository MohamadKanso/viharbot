"""Per-deck working state: plain JSON files, isolated under Pixel's own memory directory.

This is a scratchpad for one deck run, not retrievable agent memory. It holds the engagement
profile, the manifest, a progress pointer, a one-line digest of finished slides, and the
validated batches. A failed run resumes at the first batch that has no saved file.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .models import DeckManifest, EngagementProfile, SlideBatch

_DECK_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def deck_dir(deck_id: str) -> Path:
    if not _DECK_ID.fullmatch(deck_id):
        raise ValueError("invalid deck_id")
    return Path(os.getenv("MEMORY_DIR", "data")) / "pixel" / deck_id


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def save_plan(
    deck_id: str,
    engagement: EngagementProfile,
    manifest: DeckManifest,
    evidence_paths: list[str],
) -> None:
    state = {
        "engagement": engagement.model_dump(),
        "manifest": manifest.model_dump(),
        "evidence_paths": evidence_paths,
        "next_batch": 0,
        "digest": [],
    }
    _write(deck_dir(deck_id) / "state.json", json.dumps(state, indent=1))


def load_state(deck_id: str) -> dict:
    path = deck_dir(deck_id) / "state.json"
    if not path.is_file():
        raise FileNotFoundError("no saved plan for this deck_id")
    return json.loads(path.read_text(encoding="utf-8"))


def load_manifest(deck_id: str) -> DeckManifest:
    return DeckManifest.model_validate(load_state(deck_id)["manifest"])


def load_engagement(deck_id: str) -> EngagementProfile:
    return EngagementProfile.model_validate(load_state(deck_id)["engagement"])


def save_batch(deck_id: str, index: int, batch: SlideBatch, digest_lines: list[str]) -> None:
    _write(deck_dir(deck_id) / "batches" / f"{index:03d}.json", batch.model_dump_json(indent=1))
    state = load_state(deck_id)
    state["next_batch"] = max(state["next_batch"], index + 1)
    state["digest"] = (state["digest"] + digest_lines)[-40:]
    _write(deck_dir(deck_id) / "state.json", json.dumps(state, indent=1))


def load_batches(deck_id: str) -> list[SlideBatch]:
    folder = deck_dir(deck_id) / "batches"
    if not folder.is_dir():
        return []
    return [
        SlideBatch.model_validate_json(p.read_text(encoding="utf-8"))
        for p in sorted(folder.glob("*.json"))
    ]


def batch_saved(deck_id: str, index: int) -> bool:
    return (deck_dir(deck_id) / "batches" / f"{index:03d}.json").is_file()
