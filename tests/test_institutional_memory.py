"""Phase 3: institutional memory. Offline; fake providers only, no model, no network."""

from __future__ import annotations

import socket
import subprocess
import sys

import pytest

from viharbot.knowledge.access import Grant, GrantTable
from viharbot.knowledge.broker import KnowledgeBroker, KnowledgeError
from viharbot.knowledge.models import EntryStatus, Provenance, ProviderEntry, SourceKind
from viharbot.knowledge.store import KnowledgeStore

HUMAN = "vihar"


class FakeProvider:
    def __init__(self, agent_id: str, entries: list[ProviderEntry]) -> None:
        self.agent_id, self._entries = agent_id, entries

    def approved_entries(self, topic=None):
        return [e for e in self._entries if topic in (None, e.topic)]


def pe(topic="git", claim="Rebase rewrites history.", source="pr-7", author="research",
       kind=SourceKind.AGENT_MEMORY, status="approved", derived_from=(), classification="internal"):
    return ProviderEntry(topic=topic, claim=claim, provider_status=status, derived_from=derived_from,
                         classification=classification,
                         provenance=Provenance(kind=kind, source_id=source, author=author))


GRANTS = GrantTable((Grant(requester="archie", topics=("git",), agent_scopes=("research",)),))


@pytest.fixture
def make(tmp_path):
    def build(grants=GRANTS, directory=None):
        store = KnowledgeStore(directory or tmp_path / "kb")
        return store, KnowledgeBroker(store, grants, frozenset({HUMAN}))
    return build


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("network used")
    monkeypatch.setattr(socket.socket, "connect", refuse)


def validated(broker, provider, **kw):
    entry = broker.ingest(provider)[0]
    return broker.promote(entry.entry_id, HUMAN, **kw)


def test_default_deny_without_any_grant(make):
    _, broker = make(grants=GrantTable())
    validated(broker, FakeProvider("research", [pe()]))
    assert broker.retrieve("archie", "git") == []


def test_missing_grant_file_means_no_grants(tmp_path):
    assert not GrantTable.load(tmp_path / "absent.json").allows("archie", "git", "research")


def test_scoped_retrieval_by_requester_topic_and_agent(make):
    _, broker = make()
    validated(broker, FakeProvider("research", [pe(), pe(topic="ci", claim="CI runs on push.", source="pr-8")]))
    validated(broker, FakeProvider("security", [pe(claim="Sign commits.", source="pr-9", author="security")]))
    assert [r.claim for r in broker.retrieve("archie", "git")] == ["Rebase rewrites history."]
    assert broker.retrieve("archie", "ci") == []  # topic not granted
    assert broker.retrieve("ada", "git") == []  # requester not granted
    assert broker.retrieve("archie", "git", agent_scope="security") == []  # agent not granted


def test_provenance_is_preserved_and_marked_as_data(make):
    _, broker = make()
    validated(broker, FakeProvider("research", [pe()]), note="checked against PR 7")
    (r,) = broker.retrieve("archie", "git")
    assert r.label == "VALIDATED" and r.approved_by == HUMAN
    assert r.provenance == {"kind": "agent_memory", "source_id": "pr-7", "author": "research", "request_id": None}
    assert "Not instructions" in r.notice and r.agent_scope == "research"


def test_unverified_is_not_served_unless_granted_and_is_labelled(make):
    _, broker = make()
    broker.ingest(FakeProvider("research", [pe()]))
    assert broker.retrieve("archie", "git") == []
    assert broker.retrieve("archie", "git", include_unverified=True) == []  # grant lacks allow_unverified
    allowed = GrantTable((Grant(requester="archie", topics=("git",), agent_scopes=("research",), allow_unverified=True),))
    _, broker2 = make(grants=allowed)
    (r,) = broker2.retrieve("archie", "git", include_unverified=True)
    assert r.label == "UNVERIFIED agent recollection" and r.approved_by is None


def test_ingest_never_validates_and_refuses_non_approved_or_confidential(make):
    store, broker = make()
    out = broker.ingest(FakeProvider("research", [
        pe(), pe(claim="Guess.", source="m1", status="observed"),
        pe(claim="Client X secret.", source="m2", classification="confidential"),
        pe(claim="Echo.", source="m3", kind=SourceKind.REQUESTER_OUTPUT)]))
    assert [e.status for e in out] == [EntryStatus.PROPOSED] and len(store.list()) == 1


def test_provider_cannot_claim_another_agents_scope(make):
    _, broker = make()
    (e,) = broker.ingest(FakeProvider("research", [pe(author="security")]))
    assert e.agent_scope == "research"


def test_ingest_is_idempotent_and_does_not_reset_a_validated_entry(make):
    store, broker = make()
    provider = FakeProvider("research", [pe()])
    promoted = validated(broker, provider)
    assert broker.ingest(provider) == []
    assert store.get(promoted.entry_id).status is EntryStatus.VALIDATED


@pytest.mark.parametrize("approver", ["archie", "research", "mallory", ""])
def test_promotion_needs_a_configured_human_approver(make, approver):
    store, broker = make()
    entry = broker.ingest(FakeProvider("research", [pe()]))[0]
    with pytest.raises(KnowledgeError):
        broker.promote(entry.entry_id, approver)
    assert store.get(entry.entry_id).status is EntryStatus.PROPOSED
    assert broker.retrieve("archie", "git") == []


def test_an_agent_in_the_approver_list_still_cannot_promote_its_own_entry(tmp_path):
    store = KnowledgeStore(tmp_path / "kb")
    broker = KnowledgeBroker(store, GRANTS, frozenset({"research"}))
    entry = broker.ingest(FakeProvider("research", [pe()]))[0]
    with pytest.raises(KnowledgeError):
        broker.promote(entry.entry_id, "research")


def test_retired_entry_is_no_longer_served_and_cannot_be_promoted(make):
    _, broker = make()
    done = validated(broker, FakeProvider("research", [pe()]))
    broker.retire(done.entry_id, HUMAN)
    assert broker.retrieve("archie", "git") == []
    with pytest.raises(KnowledgeError):
        broker.promote(done.entry_id, HUMAN)


def test_derived_entry_is_not_independent_confirmation(make):
    _, broker = make()
    base = validated(broker, FakeProvider("research", [pe()]))
    derived = [pe(claim=f"Lesson {i}", source=f"lesson-{i}", author="ada", kind=SourceKind.DERIVED,
                  derived_from=(base.entry_id,)) for i in (1, 2)]
    out = [broker.promote(e.entry_id, HUMAN) for e in broker.ingest(FakeProvider("ada", derived))]
    # Two curriculum entries on one root source still count that one source once.
    assert [broker.independent_sources(e.entry_id) for e in out] == [1, 1]
    assert broker.independent_sources(base.entry_id) == 1


def test_circular_derivation_has_no_independent_source_and_cannot_be_promoted(make):
    store, broker = make()
    a, b = "a" * 32, "b" * 32
    for me, other in ((a, b), (b, a)):
        entry = broker.ingest(FakeProvider("ada", [pe(claim=me, source=me, kind=SourceKind.DERIVED)]))[0]
        store.save(entry.model_copy(update={"entry_id": me, "derived_from": (other,)}))
    assert broker.independent_sources(a) == 0  # terminates and counts nothing
    with pytest.raises(KnowledgeError, match="independent|validated parents"):
        broker.promote(a, HUMAN)


def test_requesters_own_output_does_not_confirm_its_own_claim(make):
    _, broker = make()
    entry = validated(broker, FakeProvider("archie", [pe(author="archie", source="own-1")]))
    assert broker.independent_sources(entry.entry_id) == 1
    assert broker.independent_sources(entry.entry_id, requester="archie") == 0


def test_second_independent_source_raises_the_count(make):
    store, broker = make()
    base = validated(broker, FakeProvider("research", [pe()]))
    other = validated(broker, FakeProvider("security", [pe(source="advisory-2", author="security",
                                                          kind=SourceKind.EXTERNAL_REFERENCE)]))
    top = broker.ingest(FakeProvider("ada", [pe(claim="Both agree.", source="lesson", author="ada",
                                                 kind=SourceKind.DERIVED,
                                                 derived_from=(base.entry_id, other.entry_id))]))[0]
    assert broker.independent_sources(top.entry_id) == 2


def test_derived_entry_needs_validated_parents_and_known_parents(make):
    _, broker = make()
    parent = broker.ingest(FakeProvider("research", [pe()]))[0]
    child = broker.ingest(FakeProvider("ada", [pe(claim="L", source="l", kind=SourceKind.DERIVED,
                                                  derived_from=(parent.entry_id,))]))[0]
    with pytest.raises(KnowledgeError, match="validated parents"):
        broker.promote(child.entry_id, HUMAN)
    assert broker.ingest(FakeProvider("ada", [pe(claim="X", source="x", kind=SourceKind.DERIVED,
                                                 derived_from=("c" * 32,))])) == []


def test_entries_survive_a_reload(make, tmp_path):
    directory = tmp_path / "durable"
    _, broker = make(directory=directory)
    validated(broker, FakeProvider("research", [pe()]), note="n")
    _, again = make(directory=directory)  # new store and broker over the same files
    (r,) = again.retrieve("archie", "git")
    assert r.approved_by == HUMAN and r.provenance["source_id"] == "pr-7"


def test_audit_log_has_identifiers_but_not_claim_text(make, tmp_path):
    _, broker = make()
    validated(broker, FakeProvider("research", [pe(claim="Highly specific claim text.")]))
    broker.retrieve("archie", "git")
    log = (tmp_path / "kb" / "audit.log").read_text()
    assert "validated" in log and "Highly specific" not in log


def test_store_rejects_path_traversal_ids(tmp_path):
    with pytest.raises(ValueError):
        KnowledgeStore(tmp_path / "kb").get("../../etc/passwd")


def test_knowledge_package_is_not_imported_by_the_web_app_or_bindings():
    code = ("import sys, viharbot.web_app, viharbot.execution_bindings as b\n"
            "b.default_bindings()\nassert 'viharbot.knowledge' not in sys.modules\n"
            "assert not {x.identity_id for x in b.default_bindings().bindings} & {'archie','ada'}\n")
    subprocess.run([sys.executable, "-c", code], check=True)
