"""The governed interface between agent memories and the institutional store.

`MemoryProvider` is the only way an agent's knowledge reaches this layer, and it returns entries the
agent has already approved; the broker never touches a private memory store. Intake creates
`proposed` entries. Only `promote`, with a configured human approver, produces `validated`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence

from .access import GrantTable
from .models import (NOT_INSTRUCTIONS, PRIMARY_KINDS, Approval, EntryStatus, KnowledgeEntry,
                     ProviderEntry, SourceKind, entry_id_for, now)
from .store import KnowledgeStore


class MemoryProvider(Protocol):
    """Implemented per agent (later, behind ADR-009's service). Phase 3 ships no implementation."""

    agent_id: str

    def approved_entries(self, topic: str | None = None) -> Sequence[ProviderEntry]: ...


class KnowledgeError(Exception):
    pass


@dataclass(frozen=True)
class RetrievedEntry:
    entry_id: str
    topic: str
    claim: str
    label: str  # "VALIDATED" or "UNVERIFIED agent recollection"
    agent_scope: str
    provenance: dict
    derived_from: tuple[str, ...]
    independent_sources: int  # excludes cycles, derived entries and the requester's own output
    approved_by: str | None
    notice: str = NOT_INSTRUCTIONS


class KnowledgeBroker:
    def __init__(self, store: KnowledgeStore, grants: GrantTable, approvers: frozenset[str]) -> None:
        self._store, self._grants, self._approvers = store, grants, approvers

    # Intake -----------------------------------------------------------------------------------
    def ingest(self, provider: MemoryProvider, topic: str | None = None) -> list[KnowledgeEntry]:
        """Take a provider's approved entries in as `proposed`. Never as validated."""
        out: list[KnowledgeEntry] = []
        for item in provider.approved_entries(topic):
            if item.provider_status != "approved":
                self._store.audit(action="ingest", agent=provider.agent_id, outcome="refused_not_approved")
                continue
            if item.classification != "internal":  # constitution 8.4; ADR-009 denies cross-scope confidential
                self._store.audit(action="ingest", agent=provider.agent_id, outcome="refused_classification")
                continue
            if item.provenance.kind is SourceKind.REQUESTER_OUTPUT:
                self._store.audit(action="ingest", agent=provider.agent_id, outcome="refused_requester_output")
                continue
            # The agent scope is the provider's id, so a provider cannot speak for another agent.
            eid = entry_id_for(provider.agent_id, item.topic, item.provenance.source_id, item.claim)
            if self._store.get(eid):  # idempotent; never resets a promoted entry
                continue
            if any(d == eid or self._store.get(d) is None for d in item.derived_from):
                self._store.audit(action="ingest", agent=provider.agent_id, entry=eid, outcome="refused_unknown_parent")
                continue
            out.append(self._store.save(KnowledgeEntry(
                entry_id=eid, topic=item.topic, agent_scope=provider.agent_id, claim=item.claim,
                provenance=item.provenance, derived_from=item.derived_from,
                status=EntryStatus.PROPOSED, proposed_by=provider.agent_id, created_at=now())))
            self._store.audit(action="ingest", agent=provider.agent_id, entry=eid, outcome="proposed")
        return out

    # Provenance and amplification ------------------------------------------------------------
    def independent_sources(self, entry_id: str, requester: str | None = None) -> int:
        """Distinct primary sources behind a claim. Derived entries add none of their own; cycles
        are walked once; the requester's own output and its own agent's entries never count."""
        roots: set[tuple[str, str]] = set()
        seen: set[str] = set()
        stack = [entry_id]
        while stack:
            current = self._store.get(stack.pop())
            if current is None or current.entry_id in seen:
                continue
            seen.add(current.entry_id)
            stack.extend(current.derived_from)
            if current.provenance.kind not in PRIMARY_KINDS:
                continue
            if requester and (current.agent_scope == requester or current.provenance.author == requester):
                continue
            roots.add((current.provenance.kind.value, current.provenance.source_id))
        return len(roots)

    # Promotion --------------------------------------------------------------------------------
    def promote(self, entry_id: str, approver: str, note: str = "") -> KnowledgeEntry:
        entry = self._store.get(entry_id)
        if entry is None:
            raise KnowledgeError("unknown entry")
        agents = self._grants.requesters() | {entry.agent_scope, entry.proposed_by}
        if approver not in self._approvers or approver in agents:
            self._store.audit(action="promote", entry=entry_id, approver=approver, outcome="refused_not_human_approver")
            raise KnowledgeError("promotion needs a configured human approver")
        if entry.status is not EntryStatus.PROPOSED:
            raise KnowledgeError(f"only proposed entries can be promoted, not {entry.status.value}")
        if entry.derived_from:
            parents = [self._store.get(d) for d in entry.derived_from]
            if any(p is None or p.status is not EntryStatus.VALIDATED for p in parents):
                raise KnowledgeError("a derived entry needs validated parents")
        if self.independent_sources(entry_id) < 1:
            self._store.audit(action="promote", entry=entry_id, approver=approver, outcome="refused_no_independent_source")
            raise KnowledgeError("no independent primary source behind this claim")
        done = entry.model_copy(update={"status": EntryStatus.VALIDATED,
                                        "approval": Approval(approver=approver, approved_at=now(), note=note)})
        self._store.save(done)
        self._store.audit(action="promote", entry=entry_id, approver=approver, outcome="validated")
        return done

    def retire(self, entry_id: str, approver: str) -> KnowledgeEntry:
        entry = self._store.get(entry_id)
        if entry is None or approver not in self._approvers:
            raise KnowledgeError("unknown entry or approver")
        done = entry.model_copy(update={"status": EntryStatus.RETIRED})
        self._store.save(done)
        self._store.audit(action="retire", entry=entry_id, approver=approver, outcome="retired")
        return done

    # Retrieval --------------------------------------------------------------------------------
    def retrieve(self, requester: str, topic: str, agent_scope: str | None = None,
                 include_unverified: bool = False) -> list[RetrievedEntry]:
        """Validated entries the requester is granted, each with provenance. Unverified recollections
        appear only when asked for, only under a grant that allows them, and are labelled."""
        out: list[RetrievedEntry] = []
        for e in self._store.list():
            if e.topic != topic or (agent_scope and e.agent_scope != agent_scope):
                continue
            validated = e.status is EntryStatus.VALIDATED
            unverified = e.status in (EntryStatus.OBSERVED, EntryStatus.PROPOSED)
            if not (validated or (include_unverified and unverified)):
                continue
            if not self._grants.allows(requester, e.topic, e.agent_scope, unverified=unverified):
                continue
            out.append(RetrievedEntry(
                entry_id=e.entry_id, topic=e.topic, claim=e.claim, agent_scope=e.agent_scope,
                label="VALIDATED" if validated else "UNVERIFIED agent recollection",
                provenance=e.provenance.model_dump(mode="json"), derived_from=e.derived_from,
                independent_sources=self.independent_sources(e.entry_id, requester),
                approved_by=e.approval.approver if e.approval else None))
        self._store.audit(action="retrieve", requester=requester, topic=topic, returned=str(len(out)))
        return out
