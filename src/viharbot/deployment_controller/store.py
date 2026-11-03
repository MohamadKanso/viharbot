"""A small JSON-file record store with atomic, durable writes. No database."""

from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path


class StoreError(Exception):
    """A durable write failed. Callers must not make a provider call after this."""


class RecordStore:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self.data: dict = {"approvals": {}, "deployments": {}, "actions": []}
        try:
            loaded = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and all(k in loaded for k in self.data):
                self.data = loaded
        except (OSError, ValueError):
            pass  # missing or corrupt is treated exactly as lost: no pending approvals exist

    def get(self, section: str, key: str) -> dict | None:
        record = self.data[section].get(key)
        return copy.deepcopy(record) if record is not None else None

    def all(self, section: str) -> list[dict]:
        return [copy.deepcopy(r) for r in self.data[section].values()]

    def commit(
        self, *, approvals: dict | None = None, deployments: dict | None = None,
        actions: list[dict] | None = None, remove_deployments: tuple[str, ...] = (),
    ) -> None:
        """Apply several changes in one atomic write; on failure nothing changes."""
        with self._lock:
            new = copy.deepcopy(self.data)
            new["approvals"].update(copy.deepcopy(approvals or {}))
            new["deployments"].update(copy.deepcopy(deployments or {}))
            for key in remove_deployments:
                new["deployments"].pop(key, None)
            new["actions"].extend(actions or [])
            self._write(new)
            self.data = new

    def _write(self, data: dict) -> None:
        tmp = self._path.with_name(self._path.name + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(data, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self._path)
        except OSError as exc:
            raise StoreError("store write failed") from exc
