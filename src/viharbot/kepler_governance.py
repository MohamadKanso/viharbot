"""Governance loading and citation checks for Kepler assignments (issue #126).

At the start of every assignment Kepler loads the governing documents from the assignment's
checkout. Loading is all-or-nothing: if any required document is missing, unreadable, empty,
oversized or outside the checkout, no bundle is returned and the assignment must not start.
The loader reads files only. It makes no network, LiteLLM, GitHub or Strands calls, and the
rules here are enforced in code, not by the persona prompt.

Kepler may *propose* a governance change (`GovernanceProposal`) but nothing in this module can
approve or enact one. Paths under `PROTECTED_PATHS` need a proposal, not an edit.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

MAX_DOCUMENT_BYTES = 400_000
_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")


def _extract_headings(text: str) -> tuple[str, ...]:
    """Markdown headings outside fenced code blocks (a fenced '# x' is not a section)."""
    headings: list[str] = []
    fence: str | None = None
    for line in text.splitlines():
        marker = _FENCE.match(line)
        if marker:
            char = marker.group(1)
            if fence is None:
                fence = char
            elif char[0] == fence[0] and len(char) >= len(fence):
                fence = None
            continue
        if fence is None and (match := _HEADING.match(line)):
            headings.append(_normalize(match.group(1)))
    return tuple(headings)
_VERSION = re.compile(r"\bVersion\s+(\d+(?:\.\d+)*)")


class GovernanceLoadError(RuntimeError):
    """A required governance document could not be loaded; the assignment must not proceed."""


class GovernanceCitationError(ValueError):
    """A 'Governance followed' citation does not match the loaded documents."""


class GovernanceModificationError(PermissionError):
    """Kepler attempted to edit a protected governance path directly."""


@dataclass(frozen=True)
class RequiredDocument:
    key: str
    path: str  # relative to the checkout root, POSIX separators
    versioned: bool = True  # whether the document states "Version X.Y" near its top


REQUIRED_DOCUMENTS: tuple[RequiredDocument, ...] = (
    RequiredDocument("charter", "docs/governance/founding-charter-v2.md"),
    RequiredDocument("constitution", "docs/governance/agent-constitution-v1.md"),
    RequiredDocument("architecture", "docs/governance/agent-architecture-v1.1.md"),
    RequiredDocument("skills-registry", "docs/governance/agent-skills-registry-v1.md"),
    RequiredDocument("change-rationale", "docs/governance/change-rationale-policy.md", False),
)

# Direct edits to these need a founder-approved proposal. Matching is by path prefix.
PROTECTED_PATHS: tuple[str, ...] = ("docs/governance/", "docs/adr/", "AGENTS.md")


@dataclass(frozen=True)
class LoadedDocument:
    key: str
    path: str
    version: str | None  # as stated in the document; None only for unversioned documents
    sha256: str  # exact content identity; this is the authoritative version
    headings: tuple[str, ...]


@dataclass(frozen=True)
class GovernanceBundle:
    documents: tuple[LoadedDocument, ...]

    def get(self, key: str) -> LoadedDocument:
        for document in self.documents:
            if document.key == key:
                return document
        raise GovernanceCitationError(f"unknown governance document: {key}")

    def versions(self) -> dict[str, dict[str, str | None]]:
        """Audit record: what was loaded, for the run log and the pull request."""
        return {
            d.key: {"path": d.path, "version": d.version, "sha256": d.sha256}
            for d in self.documents
        }


def load_governance(
    root: Path, required: tuple[RequiredDocument, ...] = REQUIRED_DOCUMENTS
) -> GovernanceBundle:
    """Load every required document from `root`, or raise `GovernanceLoadError`.

    Error messages name the document key and the problem, never file contents.
    """
    base = Path(root).resolve()
    loaded: list[LoadedDocument] = []
    for doc in required:
        try:
            path = (base / doc.path).resolve()
            if base not in path.parents or not path.is_file():
                raise GovernanceLoadError(f"{doc.key}: {doc.path} is missing")
            with path.open("rb") as handle:
                raw = handle.read(MAX_DOCUMENT_BYTES + 1)  # bounded: never read an unbounded file
        except OSError:
            raise GovernanceLoadError(f"{doc.key}: {doc.path} is unreadable") from None
        if len(raw) > MAX_DOCUMENT_BYTES:
            raise GovernanceLoadError(f"{doc.key}: {doc.path} exceeds the size limit")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise GovernanceLoadError(f"{doc.key}: {doc.path} is not valid UTF-8") from None
        if not text.strip():
            raise GovernanceLoadError(f"{doc.key}: {doc.path} is empty")
        version = None
        if doc.versioned:
            match = _VERSION.search("\n".join(text.splitlines()[:15]))
            if match is None:
                raise GovernanceLoadError(f"{doc.key}: {doc.path} states no version")
            version = match.group(1)
        headings = _extract_headings(text)
        loaded.append(
            LoadedDocument(
                key=doc.key,
                path=doc.path,
                version=version,
                sha256=hashlib.sha256(raw).hexdigest(),
                headings=headings,
            )
        )
    return GovernanceBundle(tuple(loaded))


def _normalize(text: str) -> str:
    return " ".join(text.replace("*", "").split()).casefold()


@dataclass(frozen=True)
class GovernanceCitation:
    document: str  # a RequiredDocument key
    section: str  # a heading in that document, e.g. "Section 2.4 — Human Approval"
    evidence: str  # how this change complies; must not be empty


def verify_citations(
    bundle: GovernanceBundle, citations: tuple[GovernanceCitation, ...]
) -> None:
    """Check each citation against the loaded documents; raise on the first problem.

    This proves the cited section exists and evidence was supplied. Whether the evidence
    is true is Sage's independent judgement (#134), not something Kepler can self-certify.
    """
    if not citations:
        raise GovernanceCitationError("at least one governance citation is required")
    for citation in citations:
        document = bundle.get(citation.document)
        wanted = _normalize(citation.section)
        if not wanted or wanted not in document.headings:
            raise GovernanceCitationError(
                f"{citation.document}: section not found: {citation.section!r}"
            )
        if not citation.evidence.strip():
            raise GovernanceCitationError(
                f"{citation.document} / {citation.section}: evidence is required"
            )


def render_governance_followed(
    bundle: GovernanceBundle, citations: tuple[GovernanceCitation, ...]
) -> str:
    """The mandatory 'Governance followed' pull request section, with loaded versions."""
    verify_citations(bundle, citations)
    lines = ["## Governance followed", "", "Documents loaded at assignment start:"]
    for document in bundle.documents:
        stated = f"v{document.version}, " if document.version else ""
        lines.append(f"- `{document.path}` ({stated}sha256 `{document.sha256[:12]}`)")
    lines += ["", "Citations:"]
    for c in citations:
        document = bundle.get(c.document)
        lines.append(f"- `{document.path}` — {c.section}: {c.evidence.strip()}")
    lines += ["", "Sage independently verifies these citations; this section is Kepler's claim."]
    return "\n".join(lines)


@dataclass(frozen=True)
class GovernanceProposal:
    """A suggested governance change. It is always `proposed`; only the founder can adopt it."""

    path: str
    rationale: str
    status: str = "proposed"

    def __post_init__(self) -> None:
        if self.status != "proposed":
            raise GovernanceModificationError("Kepler cannot approve a governance change")
        if not self.rationale.strip():
            raise ValueError("a governance proposal needs a rationale")


def _lexical(path: str) -> str:
    """Normalize a checkout-relative path; raise if it is absolute or escapes the checkout."""
    text = path.replace("\\", "/")
    if not text or text.startswith("/") or re.match(r"^[A-Za-z]:", text) or "\x00" in text:
        raise GovernanceModificationError(f"{path!r} must be a checkout-relative path")
    normalized: list[str] = []
    for part in PurePosixPath(text).parts:
        if part in ("", "."):
            continue
        if part == "..":
            if not normalized:
                raise GovernanceModificationError(f"{path!r} escapes the checkout")
            normalized.pop()
            continue
        normalized.append(part)
    return "/".join(normalized)


def _matches(relative: str) -> bool:
    # Casefolded: macOS and Windows checkouts are usually case-insensitive.
    wanted = relative.casefold()
    return any(
        wanted == p.rstrip("/").casefold() or wanted.startswith(p.casefold())
        for p in PROTECTED_PATHS
    )


def is_protected_path(path: str, root: Path) -> bool:
    """True if `path` (checkout-relative) names a protected path, directly or through a symlink.

    `root` is the trusted checkout root. Absolute and escaping paths raise
    `GovernanceModificationError` (fail closed). The path is checked as written and again
    after symlinks are resolved inside `root`, so an ordinary-looking alias is still caught.
    """
    relative = _lexical(path)
    base = Path(root).resolve()
    target = (base / relative).resolve()
    if target != base and base not in target.parents:
        raise GovernanceModificationError(f"{path!r} resolves outside the checkout")
    resolved = target.relative_to(base).as_posix()
    return _matches(relative) or _matches(resolved)


def require_editable(path: str, root: Path) -> None:
    """Raise unless `path` may be edited directly; protected paths need a proposal."""
    if is_protected_path(path, root):
        raise GovernanceModificationError(
            f"{path} is governance: submit a GovernanceProposal for founder approval"
        )
