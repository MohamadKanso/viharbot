"""A small JSON-file record store with atomic, durable writes. No database.

Assumes a POSIX filesystem where fsync of the containing directory persists a rename.
"""

from __future__ import annotations

import copy
import fcntl
import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path


class StoreError(Exception):
    """A durable write failed. Callers must not make a provider call after this."""


class RecordStore:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = threading.RLock()
        self._lockfile = self._path.with_name(self._path.name + ".lock")
        self._depth = 0
        self._fd: int | None = None
        self.data: dict = {}
        self._reload()

    def _reload(self) -> None:
        self.data = {"approvals": {}, "deployments": {}, "actions": []}
        try:
            loaded = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and all(k in loaded for k in self.data):
                self.data = loaded
        except (OSError, ValueError):
            pass  # missing or corrupt is treated exactly as lost: no pending approvals exist

    @contextmanager
    def locked(self):
        """Exclusive access across threads, store instances and processes.

        An advisory file lock is taken, then the file is re-read, so a read-modify-write (such as
        reserving an approval) always starts from the latest committed state. Reentrant.
        """
        with self._lock:
            if self._depth == 0:
                try:
                    self._fd = os.open(self._lockfile, os.O_RDWR | os.O_CREAT, 0o600)
                    fcntl.flock(self._fd, fcntl.LOCK_EX)
                except OSError as exc:
                    if self._fd is not None:
                        os.close(self._fd)
                        self._fd = None
                    raise StoreError("store lock failed") from exc
                self._reload()
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
                if self._depth == 0 and self._fd is not None:
                    os.close(self._fd)  # closing the descriptor releases the lock
                    self._fd = None

    def get(self, section: str, key: str) -> dict | None:
        with self.locked():
            record = self.data[section].get(key)
        return copy.deepcopy(record) if record is not None else None

    def all(self, section: str) -> list[dict]:
        with self.locked():
            return [copy.deepcopy(r) for r in self.data[section].values()]

    def commit(
        self, *, approvals: dict | None = None, deployments: dict | None = None,
        actions: list[dict] | None = None, remove_deployments: tuple[str, ...] = (),
    ) -> None:
        """Apply several changes in one atomic write; on failure nothing changes."""
        with self.locked():
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
            # Make the rename itself durable, or a crash could bring back an older pending approval.
            directory = os.open(self._path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError as exc:
            raise StoreError("store write failed") from exc
