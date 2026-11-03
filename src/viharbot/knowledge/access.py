"""Explicit grant table. Default deny: no grant, no entries. Grants are reviewed configuration;
the broker offers no way to add one."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict


class Grant(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    requester: str
    topics: tuple[str, ...]  # exact topics; there is no wildcard
    agent_scopes: tuple[str, ...]  # whose approved entries the requester may retrieve
    allow_unverified: bool = False  # may also see labelled, non-validated recollections


class GrantTable:
    def __init__(self, grants: tuple[Grant, ...] = ()) -> None:
        self._grants = tuple(grants)

    @classmethod
    def load(cls, path: Path) -> "GrantTable":
        """A missing file means no grants at all."""
        if not path.is_file():
            return cls()
        return cls(tuple(Grant.model_validate(g) for g in json.loads(path.read_text(encoding="utf-8"))))

    def allows(self, requester: str, topic: str, agent_scope: str, *, unverified: bool = False) -> bool:
        return any(g.requester == requester and topic in g.topics and agent_scope in g.agent_scopes
                   and (g.allow_unverified or not unverified) for g in self._grants)

    def requesters(self) -> frozenset[str]:
        return frozenset(g.requester for g in self._grants)
