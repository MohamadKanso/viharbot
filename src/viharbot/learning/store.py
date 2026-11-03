"""Durable, idempotent ingestion records in a plain directory of JSON files.

One file per source revision (repository, pull request, merge commit), so reprocessing the same
revision finds the same record instead of creating a second artifact. Writes are atomic.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .models import IngestionEvent, IngestState


def event_key(repository: str, pull_request: int, merge_commit_sha: str) -> str:
    return hashlib.sha256(f"{repository}#{pull_request}@{merge_commit_sha}".encode()).hexdigest()[:32]


class IngestionStore:
    def __init__(self, directory: Path) -> None:
        self._dir = directory
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self._dir / f"{key}.json"

    def get(self, key: str) -> IngestionEvent | None:
        path = self._path(key)
        if not path.is_file():
            return None
        return IngestionEvent.model_validate_json(path.read_text(encoding="utf-8"))

    def save(self, event: IngestionEvent) -> IngestionEvent:
        event.updated_at = datetime.now(timezone.utc).isoformat()
        fd, tmp = tempfile.mkstemp(dir=self._dir, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(event.model_dump(mode="json"), handle, indent=2)
        os.replace(tmp, self._path(event.event_key))
        return event

    def record(self, repository: str, pull_request: int, merge_commit_sha: str) -> IngestionEvent:
        """The existing record for this revision, or a new `received` one."""
        key = event_key(repository, pull_request, merge_commit_sha)
        existing = self.get(key)
        if existing:
            return existing
        return self.save(IngestionEvent(
            event_key=key, repository=repository, pull_request=pull_request,
            merge_commit_sha=merge_commit_sha, state=IngestState.RECEIVED, updated_at="",
        ))

    def list(self) -> list[IngestionEvent]:
        return [IngestionEvent.model_validate_json(p.read_text(encoding="utf-8"))
                for p in sorted(self._dir.glob("*.json"))]
