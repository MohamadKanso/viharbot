"""Kepler identity and governance loading (issue #126). Reads repository files only; no model,
network or GitHub calls."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from viharbot.identity import Capability, CapabilityStatus, capability_status
from viharbot.identity_registry import default_registry
from viharbot.kepler_governance import (
    REQUIRED_DOCUMENTS,
    GovernanceCitation,
    GovernanceCitationError,
    GovernanceLoadError,
    GovernanceModificationError,
    GovernanceProposal,
    is_protected_path,
    load_governance,
    render_governance_followed,
    require_editable,
    verify_citations,
)

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(
    not (ROOT / "docs" / "governance").exists(), reason="docs/ is not in this environment"
)


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    for doc in REQUIRED_DOCUMENTS:
        target = tmp_path / doc.path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / doc.path, target)
    return tmp_path


def test_kepler_identity_is_declared_but_planned_and_grants_nothing():
    kepler = default_registry().get("kepler")
    assert kepler.title == "The Architect of Harmony"
    assert kepler.capabilities == (Capability.SOFTWARE_ENGINEERING,)
    assert capability_status(Capability.SOFTWARE_ENGINEERING) is CapabilityStatus.PLANNED
    assert kepler.model_alias is None  # independent of any model or runtime
    prompt = kepler.system_prompt.lower()
    for phrase in ("untrusted data", "never push to main", "instructor-only", "propose"):
        assert phrase in prompt


def test_shipped_governance_loads_with_versions_and_hashes():
    bundle = load_governance(ROOT)
    versions = bundle.versions()
    assert set(versions) == {d.key for d in REQUIRED_DOCUMENTS}
    assert versions["constitution"]["version"] == "1.0"
    assert versions["charter"]["version"] == "2.0"
    assert versions["change-rationale"]["version"] is None
    assert all(len(v["sha256"]) == 64 for v in versions.values())


def test_loading_is_deterministic_and_tracks_content(checkout: Path):
    first = load_governance(checkout)
    assert load_governance(checkout) == first
    path = checkout / "docs/governance/agent-constitution-v1.md"
    path.write_text(path.read_text() + "\nAmended.\n")
    assert load_governance(checkout).get("constitution").sha256 != first.get("constitution").sha256


@pytest.mark.parametrize("doc", REQUIRED_DOCUMENTS, ids=lambda d: d.key)
def test_missing_document_fails_closed(checkout: Path, doc):
    (checkout / doc.path).unlink()
    with pytest.raises(GovernanceLoadError, match=doc.key):
        load_governance(checkout)


def test_empty_unversioned_oversized_and_non_utf8_documents_fail_closed(checkout: Path):
    path = checkout / "docs/governance/agent-constitution-v1.md"
    original = path.read_bytes()
    for content, message in (
        (b"  \n", "empty"),
        (b"# No version stated here\n", "no version"),
        (b"x" * 400_001, "size limit"),
        (b"\xff\xfe# Version 1.0", "UTF-8"),
    ):
        path.write_bytes(content)
        with pytest.raises(GovernanceLoadError, match=message):
            load_governance(checkout)
    path.write_bytes(original)
    assert load_governance(checkout)


def test_symlink_escaping_the_checkout_is_rejected(checkout: Path, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / "charter.md"
    outside.write_text("# Version 2.0\n")
    path = checkout / "docs/governance/founding-charter-v2.md"
    path.unlink()
    path.symlink_to(outside)
    with pytest.raises(GovernanceLoadError, match="charter"):
        load_governance(checkout)


def test_errors_never_echo_document_contents(checkout: Path):
    path = checkout / "docs/governance/agent-constitution-v1.md"
    path.write_text("SECRET-TOKEN-123 no version here")
    with pytest.raises(GovernanceLoadError) as info:
        load_governance(checkout)
    assert "SECRET-TOKEN-123" not in str(info.value)


def test_valid_citations_render_a_governance_followed_section():
    bundle = load_governance(ROOT)
    citations = (
        GovernanceCitation("constitution", "Section 2.4 — Human Approval", "Founder merges."),
        GovernanceCitation("change-rationale", "Principle", "PR explains why."),
    )
    text = render_governance_followed(bundle, citations)
    assert text.startswith("## Governance followed")
    assert "docs/governance/agent-constitution-v1.md" in text and "sha256" in text
    assert "Section 2.4" in text and "Sage independently verifies" in text


@pytest.mark.parametrize(
    "citation",
    [
        GovernanceCitation("constitution", "Section 99.9 — Invented", "x"),
        GovernanceCitation("nonexistent", "Section 2.4 — Human Approval", "x"),
        GovernanceCitation("constitution", "Section 2.4 — Human Approval", "   "),
        GovernanceCitation("constitution", "", "x"),
    ],
)
def test_bad_citations_are_rejected(citation):
    with pytest.raises(GovernanceCitationError):
        verify_citations(load_governance(ROOT), (citation,))


def test_at_least_one_citation_is_required():
    with pytest.raises(GovernanceCitationError):
        verify_citations(load_governance(ROOT), ())


def test_kepler_proposes_but_cannot_approve_governance_changes():
    assert GovernanceProposal("docs/governance/agent-constitution-v1.md", "why").status == "proposed"
    with pytest.raises(GovernanceModificationError):
        GovernanceProposal("docs/governance/agent-constitution-v1.md", "why", status="approved")
    with pytest.raises(ValueError):
        GovernanceProposal("docs/governance/agent-constitution-v1.md", "  ")


@pytest.mark.parametrize(
    "path",
    [
        "docs/governance/agent-constitution-v1.md",
        "docs/adr/ADR-006-workload-identity-and-authorization.md",
        "AGENTS.md",
        "agents.md",  # case-insensitive filesystems
        "Docs/Governance/x.md",
        "./docs/governance/../governance/founding-charter-v2.md",
        "docs\\governance\\x.md",
        "src/../AGENTS.md",
    ],
)
def test_protected_paths_need_a_proposal(checkout: Path, path):
    assert is_protected_path(path, checkout)
    with pytest.raises(GovernanceModificationError):
        require_editable(path, checkout)


@pytest.mark.parametrize("path", ["src/viharbot/kepler_governance.py", "docs/architecture.md",
                                  "docs/governance-notes.md", "tests/test_x.py"])
def test_ordinary_paths_are_editable(checkout: Path, path):
    assert not is_protected_path(path, checkout)
    require_editable(path, checkout)


@pytest.mark.parametrize(
    "path", ["/etc/passwd", "/abs/checkout/AGENTS.md", "C:\\x\\AGENTS.md", "../outside.md",
             "docs/../../outside.md", "", "a\x00b"],
)
def test_absolute_and_escaping_paths_fail_closed(checkout: Path, path):
    with pytest.raises(GovernanceModificationError):
        require_editable(path, checkout)


def test_the_absolute_path_of_a_protected_file_is_rejected_not_allowed(checkout: Path):
    with pytest.raises(GovernanceModificationError):
        require_editable(str(checkout / "AGENTS.md"), checkout)


def test_symlink_alias_of_a_protected_path_is_protected(checkout: Path):
    (checkout / "notes").symlink_to(checkout / "docs" / "governance", target_is_directory=True)
    (checkout / "innocent.md").symlink_to(checkout / "docs/governance/agent-constitution-v1.md")
    assert is_protected_path("notes/anything.md", checkout)
    assert is_protected_path("innocent.md", checkout)
    with pytest.raises(GovernanceModificationError):
        require_editable("innocent.md", checkout)


def test_symlink_pointing_outside_the_checkout_fails_closed(checkout: Path, tmp_path_factory):
    outside = tmp_path_factory.mktemp("elsewhere")
    (checkout / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(GovernanceModificationError):
        require_editable("link/file.md", checkout)


def test_document_reads_are_bounded(checkout: Path, monkeypatch):
    from viharbot import kepler_governance as kg

    path = checkout / "docs/governance/agent-constitution-v1.md"
    path.write_bytes(b"x" * 5_000_000)
    reads: list[int] = []
    real_open = Path.open

    def spy(self, *args, **kwargs):
        handle = real_open(self, *args, **kwargs)
        if self == path.resolve():
            original = handle.read
            handle.read = lambda n=-1: (reads.append(n), original(n))[1]
        return handle

    monkeypatch.setattr(Path, "open", spy)
    with pytest.raises(GovernanceLoadError, match="size limit"):
        load_governance(checkout)
    assert reads == [kg.MAX_DOCUMENT_BYTES + 1]  # one bounded read, never read-everything


def test_headings_inside_fenced_code_are_not_sections(checkout: Path):
    path = checkout / "docs/governance/agent-constitution-v1.md"
    path.write_text(
        path.read_text()
        + "\n```\n## Section 99.1 — Fenced Example\n```\n"
        + "~~~md\n## Section 99.2 — Tilde Example\n~~~\n"
        + "\n## Section 99.3 — Real Heading\n"
    )
    bundle = load_governance(checkout)
    for fenced in ("Section 99.1 — Fenced Example", "Section 99.2 — Tilde Example"):
        with pytest.raises(GovernanceCitationError):
            verify_citations(bundle, (GovernanceCitation("constitution", fenced, "x"),))
    verify_citations(
        bundle, (GovernanceCitation("constitution", "Section 99.3 — Real Heading", "x"),)
    )
    # headings after a closed fence, and the original ones, still count
    verify_citations(
        bundle, (GovernanceCitation("constitution", "Section 2.4 — Human Approval", "x"),)
    )
