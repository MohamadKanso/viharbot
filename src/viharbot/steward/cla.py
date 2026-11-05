"""The steward's view of the CLA, built on the repository's own gate.

The rules live in `.github/scripts/cla_gate.py`. This module loads that script and calls its
functions, so Kepler cannot disagree with the merge check about who is covered. It adds only what
the gate deliberately does not do: it reads acceptance *comments* (through the gate's own
`check_comment`) and reports a valid comment as "claimed, awaiting the founder's record", never as
acceptance. A comment is a request, not a record.

If the gate cannot be loaded, nothing here guesses: the caller gets `ClaGateUnavailable`.
"""

from __future__ import annotations

import datetime
import hashlib
import importlib.util
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

_GATE_ENV = "KEPLER_CLA_GATE_DIR"
_loaded: dict[str, ModuleType] = {}


class ClaGateUnavailable(RuntimeError):
    """The CLA gate script or its registry module could not be loaded."""


def default_gate_dir() -> Path:
    override = os.environ.get(_GATE_ENV)
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[3] / ".github" / "scripts"


def load_gate(directory: Path | None = None) -> ModuleType:
    base = Path(directory) if directory else default_gate_dir()
    key = str(base.resolve())
    if key in _loaded:
        return _loaded[key]
    script = base / "cla_gate.py"
    if not script.is_file() or not (base / "contributor_registry.py").is_file():
        raise ClaGateUnavailable("cla_gate.py or contributor_registry.py was not found")
    # The gate imports its sibling module by putting its own directory on sys.path.
    spec = importlib.util.spec_from_file_location("cla_gate", script)
    if spec is None or spec.loader is None:
        raise ClaGateUnavailable("cla_gate.py could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules["cla_gate"] = module  # the gate's dataclasses resolve annotations through it
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # a broken gate must never be treated as a pass
        raise ClaGateUnavailable(f"cla_gate.py failed to load ({type(exc).__name__})") from None
    _loaded[key] = module
    return module


def gate_fingerprint(directory: Path | None = None) -> str:
    """SHA-256 of the gate code, recorded in the audit trail so a verdict names its judge."""
    base = Path(directory) if directory else default_gate_dir()
    digest = hashlib.sha256()
    for name in ("cla_gate.py", "contributor_registry.py"):
        try:
            digest.update((base / name).read_bytes())
        except OSError:
            raise ClaGateUnavailable(f"{name} could not be read") from None
    return digest.hexdigest()


@dataclass(frozen=True)
class ClaAssessment:
    record_readable: bool
    legal_version: str | None
    pr_covered: bool  # the gate's verdict for EVERY contributor on the pull request
    author_covered: bool  # judged by the gate's own `judge_person` for the author alone
    author_state: str  # covered, exempt, not_recorded, older_version, withdrawn, hold, unreadable
    claim: str  # none, valid (exact, unedited, by the author), or invalid_only
    maintainer_request_at: str | None  # when the founder asked for the CLA, if they did
    author_replied_after_request: bool  # a non-acceptance comment by the author after the request
    identity_unresolved: bool  # some contributor identity could not be matched to an account
    lookup_failed: bool  # GitHub could not resolve a login, so identity is unconfirmed


def _parse_time(value: str | None) -> datetime.datetime | None:
    try:
        return datetime.datetime.strptime(value or "", "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc)
    except ValueError:
        return None


def assess_cla(
    gate: ModuleType, files, pr: dict, comments: list[dict], resolve, today: datetime.date,
    repo: str,
) -> ClaAssessment:
    """Judge the pull request's CLA position using only the gate's functions."""
    unreadable = ClaAssessment(False, None, False, False, "unreadable", "none", None, False,
                               False, False)
    if files.base_record is None or files.base_exceptions is None or files.base_agreement is None:
        return unreadable
    terms, rows, record_errors = gate.parse_record(files.base_record)
    rules, rule_errors = gate.parse_exceptions(files.base_exceptions)
    founders, problems = gate.founders_from(files.base_registry)
    if terms is None or record_errors or rule_errors or problems:
        return unreadable

    lookup_failed = False
    try:
        whole = gate.evaluate(files, pr, resolve, today)
        pr_covered = whole.ok
    except gate.IdentityLookup:
        lookup_failed, pr_covered = True, False

    # The author alone, judged by the gate's own rule.
    author = pr["author"]
    person = gate.Person(author["id"], author["login"], author.get("type", "User"),
                         "pull request author")
    exemptions: dict[int, list] = {}
    for rule in rules:
        exemptions.setdefault(rule.account_id, []).append(rule)
    founder_names = {name.lower() for name in founders.values()}
    verdict = gate.Result()
    gate.judge_person(person, pr, terms, rows, exemptions, founders, founder_names, today, verdict)
    author_covered = not verdict.errors and bool(verdict.covered or verdict.exemptions)
    mine = [row for row in rows if row.account_id == author["id"]]
    if author_covered:
        state = "exempt" if verdict.exemptions and not verdict.covered else "covered"
    elif any(row.status == "withdrawn" for row in mine):
        state = "withdrawn"
    elif any(row.version == terms.legal and row.eligibility in gate.HOLDS for row in mine):
        state = "hold"
    elif mine and not any(row.version == terms.legal for row in mine):
        state = "older_version"
    else:
        state = "not_recorded"

    # Acceptance comments: only the gate's exact check counts, and only as a claim.
    claim = "none"
    own = [c for c in comments if (c.get("user") or {}).get("id") == author["id"]]
    for comment in own:
        evidence, _ = gate.check_comment(comment, terms, repo, str(comment.get("html_url", "")))
        if evidence is not None:
            claim = "valid"
            break
        if claim == "none" and " ".join(str(comment.get("body", "")).split()) == gate.statement(
                terms.legal, repo):
            claim = "invalid_only"  # the right words, but edited, forged or from a bad source

    founder_ids = set(founders)
    asked = [
        c for c in comments
        if (c.get("user") or {}).get("id") in founder_ids
        and "contributor license agreement" in str(c.get("body", "")).lower()
    ]
    request_at = max((c.get("created_at") for c in asked if _parse_time(c.get("created_at"))),
                     default=None)
    replied = False
    if request_at:
        asked_time = _parse_time(request_at)
        for comment in own:
            when = _parse_time(comment.get("created_at"))
            is_statement = " ".join(str(comment.get("body", "")).split()) == gate.statement(
                terms.legal, repo)
            if when and asked_time and when > asked_time and not is_statement:
                replied = True
                break

    unresolved = False
    try:
        unresolved = any(p.account_id is None for p in gate.attributions(pr, resolve))
    except gate.IdentityLookup:
        lookup_failed = True
    return ClaAssessment(True, terms.legal, pr_covered, author_covered, state, claim, request_at,
                         replied, unresolved, lookup_failed)
