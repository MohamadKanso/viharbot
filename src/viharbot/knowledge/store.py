"""Durable institutional entries as one JSON file each, plus an identifier-only audit log."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

from .models import KnowledgeEntry

_ID = re.compile(r"^[0-9a-f]{32}$")


class KnowledgeStore:
    def __init__(self, directory: Path) -> None:
        self._dir = directory
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, entry_id: str) -> Path:
        if not _ID.match(entry_id):  # also blocks path traversal
            raise ValueError("invalid entry id")
        return self._dir / f"{entry_id}.json"

    def get(self, entry_id: str) -> KnowledgeEntry | None:
        path = self._path(entry_id)
        if not path.is_file():
            return None
        return KnowledgeEntry.model_validate_json(path.read_text(encoding="utf-8"))

    def save(self, entry: KnowledgeEntry) -> KnowledgeEntry:
        fd, tmp = tempfile.mkstemp(dir=self._dir, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(entry.model_dump(mode="json"), handle, indent=2)
        os.replace(tmp, self._path(entry.entry_id))
        return entry

    def list(self) -> list[KnowledgeEntry]:
        return [KnowledgeEntry.model_validate_json(p.read_text(encoding="utf-8"))
                for p in sorted(self._dir.glob("*.json"))]

    def audit(self, **fields: str) -> None:
        """Append identifiers only (who, what, outcome); never claim text."""
        with (self._dir / "audit.log").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(fields, sort_keys=True) + "\n")
