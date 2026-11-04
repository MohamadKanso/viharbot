"""Decide whether a pull request's contributors have accepted the contribution agreement.

Standard library only. The merge gate (`check`, `check-pr`) reads GitHub only through the
read-only workflow token; `verify-comment`, `audit` and `activation-check` are founder tools.

Rules that govern everything here:

* The record is data. Acceptance, exceptions and the founder's identity are read only from the
  protected base branch, never from a pull request's copy, so a pull request cannot accept for
  itself or approve its own exception.
* The gate fails closed: a missing or malformed record, an unresolved identity, an unsupported
  contributor, an incomplete commit list, a rate limit or any GitHub error is a failure, never a
  pass. While the status is `draft` every non-exempt contributor fails.
* Every identifiable author is judged: the pull request author, each commit author and committer,
  and each `Co-authored-by` trailer. A founder-authored pull request does not excuse a
  third party's commits.
* Exemptions are narrow and logged: the founder's own work (CONTRIBUTORS.md `Founder` row) and the
  rows of CLA-EXCEPTIONS.md. Each use is printed with its reason, authorizer and scope.
* A pass means "every contributor is covered". It is not review and not approval to merge.
  Founder approval is a ruleset and CODEOWNERS matter; a workflow cannot enforce it.
* The gate cannot read the private evidence store, and it does not read comments. It trusts the
  founder-approved public record. `audit` is how the founder compares that record with the
  private evidence and with GitHub.

Usage:
  cla_gate.py check-pr --repo O/N --pr N [--base-dir D] --head-dir D
  cla_gate.py check --pr-file F [--base-dir D] [--head-dir D]
  cla_gate.py verify-comment --repo O/N --comment-url U --eligibility E --verified-by L
                             --evidence-out DIR [--acceptances-file F] [--registry-pr N]
  cla_gate.py audit --repo O/N --evidence-dir DIR [--acceptances-file F]
  cla_gate.py activation-check --repo O/N [--branch main] [--acceptances-file F]
  cla_gate.py hash-agreement [--file F]
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import contributor_registry as registry  # noqa: E402

CHECK_NAME = "CLA acceptance"
ACTIONS_APP_ID = 15368  # the GitHub Actions app; pins a required check to this workflow's reporter
RECORD_PATH = "CLA-ACCEPTANCES.md"
EXCEPTIONS_PATH = "CLA-EXCEPTIONS.md"
AGREEMENT_PATH = "docs/governance/contributor-license-agreement.md"
PROTECTED_PATHS = frozenset(
    {
        RECORD_PATH,
        EXCEPTIONS_PATH,
        AGREEMENT_PATH,
        "docs/governance/cla-acceptance.md",
        "docs/governance/cla-private-evidence.md",
        "docs/governance/cla-activation.md",
        ".github/scripts/cla_gate.py",
        ".github/workflows/cla-gate.yml",
        ".github/rulesets/protect-main.json",
        ".github/CODEOWNERS",
        "tests/test_cla_gate.py",
    }
)
MAX_COMMITS = 250  # GitHub's pull request commits endpoint returns at most this many
MAX_FILES = 3000  # ... and the files endpoint at most this many
API = "https://api.github.com"

ELIGIBLE = frozenset({"adult-individual", "employee-authorized", "corporate-cla", "minor-approved"})
HOLDS = {
    "hold-corporate": (
        "their contributions need a corporate CLA or equivalent employer authorization that is "
        "not on file"
    ),
    "hold-minor": "contributions from a minor need separate legal review and express approval",
}
ELIGIBILITY = ELIGIBLE | frozenset(HOLDS)

TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
VERSION = re.compile(r"^[0-9]+\.[0-9]+$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
EVIDENCE_ID = re.compile(r"^EV-[1-9][0-9]*-[1-9][0-9]*$")
EXCEPTION_ID = re.compile(r"^EX-[0-9]{3,}$")
COMMENT_URL = re.compile(
    r"^https://github\.com/(?P<repo>[A-Za-z0-9._-]+/[A-Za-z0-9._-]+)/(?:issues|pull)/[0-9]+"
    r"#issuecomment-(?P<id>[0-9]+)$"
)
REFERENCE = re.compile(r"^https://github\.com/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/(?:issues|pull)/[0-9]+\S*$")
NOREPLY_ID = re.compile(r"^([0-9]+)\+([A-Za-z0-9-]+(?:\[bot\])?)@users\.noreply\.github\.com$")
NOREPLY_LOGIN = re.compile(r"^([A-Za-z0-9-]+)@users\.noreply\.github\.com$")
TRAILER = re.compile(r"(?im)^co-authored-by:[ \t]*.*?<([^<>\s]+)>[ \t]*$")
SHA = re.compile(r"^[0-9a-f]{7,40}$")
HEADER_VERSION = re.compile(r"\*\*Legal-terms version: ([0-9]+\.[0-9]+)\*\*")
HEADER_REVISION = re.compile(r"\*\*Editorial revision: ([0-9]+)\*\*")

STATUS_FIELDS = {
    "CLA status": r"draft|in-force",
    "CLA legal-terms version": r"[0-9]+\.[0-9]+",
    "CLA editorial revision": r"[0-9]+",
    "CLA text SHA-256": r"unfrozen|[0-9a-f]{64}",
    "Legal review": r"pending|complete: \S.{0,200}",
    "Founder approval": r"pending|approved: \S.{0,200}",
    "Private evidence storage": r"not-verified|verified: \S.{0,200}",
    "Required ruleset": r"not-verified|verified: \S.{0,200}",
}
RECORD_HEADER = (
    "GitHub numeric ID",
    "GitHub username at acceptance",
    "Legal-terms version",
    "Effective from (UTC)",
    "Status",
    "Withdrawn from (UTC)",
    "Eligibility",
    "Evidence ID",
    "Evidence SHA-256",
)
EXCEPTION_HEADER = (
    "Exception ID",
    "GitHub numeric ID",
    "GitHub username",
    "Kind",
    "Scope",
    "Reason",
    "Authorizer",
    "Approved on (UTC)",
    "Expires (UTC)",
    "Reference",
)


def statement(version: str, repo: str = "vchokshi/viharbot") -> str:
    """The exact acceptance text from section 8 of the agreement."""
    return (
        f"I have read the ViharBot Contributor License Agreement, legal-terms version {version}, "
        f"and I agree to its terms for my contributions to {repo}."
    )


def sha256_text(text: str) -> str:
    """SHA-256 of text with line endings normalised, so a checkout's CRLF cannot change it."""
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def canonical_digest(value: object) -> str:
    blob = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(blob.encode("ascii")).hexdigest()


# ---- the record -------------------------------------------------------------------------------
@dataclass(frozen=True)
class Terms:
    status: str
    legal: str
    editorial: int
    sha: str
    review: str
    approval: str
    storage: str
    ruleset: str

    @property
    def in_force(self) -> bool:
        return self.status == "in-force"

    @property
    def frozen(self) -> bool:
        return self.sha != "unfrozen"

    @property
    def legal_key(self) -> tuple[int, int]:
        major, minor = self.legal.split(".")
        return int(major), int(minor)

    def gaps(self) -> list[str]:
        """Activation prerequisites that are not satisfied."""
        gaps = []
        if not self.frozen:
            gaps.append("the agreement text is not frozen (CLA text SHA-256)")
        for name, value, done in (
            ("legal review", self.review, "complete"),
            ("founder approval", self.approval, "approved"),
            ("private evidence storage", self.storage, "verified"),
            ("required ruleset", self.ruleset, "verified"),
        ):
            if not value.startswith(done):
                gaps.append(f"{name} is {value}")
        return gaps


@dataclass(frozen=True)
class Acceptance:
    account_id: int
    username: str
    version: str
    effective: str
    status: str
    withdrawn: str
    eligibility: str
    evidence_id: str
    evidence_sha: str


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _real_date(value: str) -> bool:
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        return False
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value))


def _real_timestamp(value: str) -> bool:
    if not TIMESTAMP.match(value):
        return False
    try:
        datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    return True


def _table(text: str, heading: str, header: tuple[str, ...]) -> tuple[list[tuple[int, list[str]]], list[str]]:
    lines = text.splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == heading)
    except StopIteration:
        return [], [f"There is no '{heading}' section."]
    table = []
    for number, line in enumerate(lines[start + 1 :], start=start + 2):
        if line.startswith("## "):
            break
        if line.strip().startswith("|"):
            table.append((number, line))
    if len(table) < 2 or tuple(_cells(table[0][1])) != header:
        return [], [f"The {heading[3:]} table header must be exactly: {' | '.join(header)}."]
    return [(n, _cells(ln)) for n, ln in table[2:]], []


def parse_record(text: str) -> tuple[Terms | None, list[Acceptance], list[str]]:
    """Parse the status block and the Acceptances table. Every problem is reported."""
    errors: list[str] = []
    values: dict[str, str] = {}
    for label, pattern in STATUS_FIELDS.items():
        found = re.findall(rf"(?m)^\* {re.escape(label)}: (.+?)[ \t]*$", text)
        if len(found) != 1 or not re.fullmatch(pattern, found[0]):
            errors.append(f"There must be exactly one valid line `* {label}: <value>`.")
        else:
            values[label] = found[0]
    terms = None
    if not errors:
        terms = Terms(
            values["CLA status"], values["CLA legal-terms version"], int(values["CLA editorial revision"]),
            values["CLA text SHA-256"], values["Legal review"], values["Founder approval"],
            values["Private evidence storage"], values["Required ruleset"],
        )
        if terms.in_force and terms.gaps():
            errors.append("The status is `in-force` but: " + "; ".join(terms.gaps()) + ".")
    rows_raw, table_errors = _table(text, "## Acceptances", RECORD_HEADER)
    errors += table_errors
    rows: list[Acceptance] = []
    seen: set[tuple[int, str]] = set()
    for number, cells in rows_raw:
        if len(cells) != len(RECORD_HEADER):
            errors.append(f"Line {number}: expected {len(RECORD_HEADER)} columns, found {len(cells)}.")
            continue
        raw_id, link, version, effective, status, withdrawn, eligibility, ev_id, ev_sha = cells
        match = registry.ROW_LINK.match(link)
        problems = []
        if not registry.ACCOUNT_ID.match(raw_id):
            problems.append("the numeric ID must be a positive whole number with no leading zeros")
        if not match or match.group(2) != match.group(1) or not registry.USERNAME.match(match.group(1)):
            problems.append("the username must look like [@name](https://github.com/name)")
        if not VERSION.match(version):
            problems.append("the legal-terms version must look like 1.0")
        elif terms and tuple(map(int, version.split("."))) > terms.legal_key:
            problems.append("the legal-terms version is newer than the agreement")
        if not _real_timestamp(effective):
            problems.append("'Effective from' must be a UTC timestamp like 2026-11-05T10:00:00Z")
        if status not in {"active", "withdrawn"}:
            problems.append("Status must be `active` or `withdrawn`")
        if status == "withdrawn" and not _real_timestamp(withdrawn):
            problems.append("a withdrawn row needs a 'Withdrawn from' UTC timestamp")
        if status == "active" and withdrawn != "-":
            problems.append("an active row must have `-` as 'Withdrawn from'")
        if eligibility not in ELIGIBILITY:
            problems.append("Eligibility must be one of " + ", ".join(sorted(ELIGIBILITY)))
        if not EVIDENCE_ID.match(ev_id):
            problems.append("the evidence ID must look like EV-<user id>-<comment id>")
        if not HEX64.match(ev_sha):
            problems.append("the evidence SHA-256 must be 64 lowercase hex characters")
        if problems:
            errors.append(f"Line {number}: " + "; ".join(problems) + ".")
            continue
        key = (int(raw_id), version)
        if key in seen:
            errors.append(f"Line {number}: account ID {raw_id} already has a row for version {version}.")
        seen.add(key)
        rows.append(Acceptance(int(raw_id), match.group(1), version, effective, status, withdrawn,
                               eligibility, ev_id, ev_sha))
    return terms, rows, errors


def record_change_errors(base: list[Acceptance], head: list[Acceptance]) -> list[str]:
    """Append-only: rows are never deleted or rewritten; only withdrawal and eligibility may change."""
    errors = []
    after = {(r.account_id, r.version): r for r in head}
    for row in base:
        new = after.get((row.account_id, row.version))
        if new is None:
            errors.append(
                f"The acceptance of account {row.account_id} for version {row.version} was removed. "
                "Rows are never deleted: deleting one would erase evidence of a licence grant. "
                "Record a withdrawal by changing its status instead."
            )
            continue
        for name in ("username", "effective", "evidence_id", "evidence_sha"):
            if getattr(new, name) != getattr(row, name):
                errors.append(f"Account {row.account_id}, version {row.version}: `{name}` must not be edited.")
        if row.status == "withdrawn" and (new.status != "withdrawn" or new.withdrawn != row.withdrawn):
            errors.append(f"Account {row.account_id}: a recorded withdrawal cannot be reversed or changed.")
    return errors


def agreement_header(text: str) -> tuple[str | None, int | None]:
    version, revision = HEADER_VERSION.search(text), HEADER_REVISION.search(text)
    return (version.group(1) if version else None, int(revision.group(1)) if revision else None)


def revision_errors(base: Terms, head: Terms, head_agreement: str | None, agreement_changed: bool) -> list[str]:
    """Keep the agreement text, its version numbers and the frozen hash consistent."""
    if head_agreement is None:
        return [f"{AGREEMENT_PATH} must not be deleted."]
    errors = []
    version, revision = agreement_header(head_agreement)
    if (version, revision) != (head.legal, head.editorial):
        errors.append(
            f"The agreement says legal-terms version {version}, revision {revision}; the record says "
            f"{head.legal}, revision {head.editorial}. They must match."
        )
    if head.frozen and sha256_text(head_agreement) != head.sha:
        errors.append("The agreement text does not match the frozen `CLA text SHA-256` in the record.")
    if agreement_changed and base.frozen:
        newer = head.legal_key > base.legal_key or (head.legal_key == base.legal_key and head.editorial > base.editorial)
        if not newer:
            errors.append(
                "The agreement changed after it was frozen without a new version. A material change "
                "needs a higher legal-terms version (renewed acceptance); an editorial-only change "
                "needs a higher editorial revision."
            )
    if head.legal_key < base.legal_key:
        errors.append("The legal-terms version must not go down.")
    return errors


# ---- exceptions -------------------------------------------------------------------------------
@dataclass(frozen=True)
class Exemption:
    exception_id: str
    account_id: int
    username: str
    kind: str
    scope: str
    reason: str
    authorizer: str
    approved: str
    expires: str
    reference: str

    def active(self, today: datetime.date) -> bool:
        return self.expires == "none" or datetime.date.fromisoformat(self.expires) >= today

    def covers(self, pr_number: int | None) -> bool:
        return self.scope == "commits" or (pr_number is not None and self.scope == f"pr:{pr_number}")

    def describe(self) -> str:
        return (f"{self.exception_id} ({self.kind}, scope {self.scope}, expires {self.expires}, "
                f"authorized by {registry.safe(self.authorizer)}, reason: {registry.safe(self.reason, 100)}, "
                f"reference {registry.safe(self.reference, 100)})")


def parse_exceptions(text: str) -> tuple[list[Exemption], list[str]]:
    rows_raw, errors = _table(text, "## Exceptions", EXCEPTION_HEADER)
    rules: list[Exemption] = []
    seen_ids: set[str] = set()
    for number, cells in rows_raw:
        if len(cells) != len(EXCEPTION_HEADER):
            errors.append(f"Line {number}: expected {len(EXCEPTION_HEADER)} columns, found {len(cells)}.")
            continue
        ex_id, raw_id, user, kind, scope, reason, authorizer, approved, expires, ref = cells
        problems = []
        if not EXCEPTION_ID.match(ex_id) or ex_id in seen_ids:
            problems.append("the exception ID must be a unique EX-<number>")
        if not registry.ACCOUNT_ID.match(raw_id):
            problems.append("the numeric ID must be a positive whole number")
        if not registry.USERNAME.match(user.removesuffix("[bot]")):
            problems.append("the username is not a valid GitHub login")
        if kind not in {"automation", "approved-case"}:
            problems.append("Kind must be `automation` or `approved-case`")
        if kind == "automation" and scope != "commits":
            problems.append("an automation exception has scope `commits`")
        if kind == "approved-case" and not re.fullmatch(r"pr:[1-9][0-9]*", scope):
            problems.append("an approved case is limited to one pull request: scope `pr:<number>`")
        if len(reason) < 10:
            problems.append("a reason of at least 10 characters is required")
        if not registry.USERNAME.match(authorizer):
            problems.append("the authorizer must be a GitHub username")
        if not _real_date(approved):
            problems.append("'Approved on' must be a real date, YYYY-MM-DD")
        if not (_real_date(expires) or (expires == "none" and kind == "automation")):
            problems.append("an approved case needs a real expiry date (only automation may use `none`)")
        if kind == "approved-case" and not REFERENCE.match(ref):
            problems.append("an approved case needs a GitHub issue or pull request URL as its reference")
        if problems:
            errors.append(f"Line {number}: " + "; ".join(problems) + ".")
            continue
        seen_ids.add(ex_id)
        rules.append(Exemption(ex_id, int(raw_id), user, kind, scope, reason, authorizer, approved, expires, ref))
    return rules, errors


# ---- who contributed --------------------------------------------------------------------------
@dataclass(frozen=True)
class Person:
    account_id: int | None
    login: str | None
    kind: str = "User"
    source: str = "commit author"
    sha: str | None = None

    def label(self) -> str:
        if self.login and self.account_id is not None:
            return f"`{registry.safe(self.login)}` (ID {self.account_id}, {self.source})"
        return f"an unlinked {self.source} in commit `{registry.safe(self.sha or '?', 12)}`"


class IdentityLookup(Exception):
    """GitHub could not resolve a login, so the identity is neither confirmed nor refuted."""


Resolver = Callable[[str], "int | None"]


def _person(item: dict | None, source: str, sha: str, email: str | None = None) -> Person:
    if isinstance(item, dict) and isinstance(item.get("id"), int) and isinstance(item.get("login"), str):
        kind = item.get("type") or ("Bot" if item["login"].endswith("[bot]") else "User")
        return Person(item["id"], item["login"], kind, source, sha)
    match = NOREPLY_ID.match(email or "")
    if match:
        login = match.group(2)
        return Person(int(match.group(1)), login, "Bot" if login.endswith("[bot]") else "User", source, sha)
    return Person(None, None, "User", source, sha)


def attributions(pr: dict, resolve: Resolver) -> list[Person]:
    """Every identifiable contributor: PR author, commit authors, committers, co-author trailers."""
    author = pr["author"]
    people = [Person(author["id"], author["login"], author.get("type", "User"), "pull request author")]
    for commit in pr["commits"]:
        sha = str(commit.get("sha", ""))[:12]
        people.append(_person(commit.get("author"), "commit author", sha, commit.get("author_email")))
        if commit.get("committer"):
            people.append(_person(commit["committer"], "committer", sha))
        for email in TRAILER.findall(str(commit.get("message", ""))):
            by_id = NOREPLY_ID.match(email)
            by_login = NOREPLY_LOGIN.match(email)
            if by_id:
                login = by_id.group(2)
                people.append(Person(int(by_id.group(1)), login, "Bot" if login.endswith("[bot]") else "User",
                                     "co-author", sha))
            elif by_login:
                account_id = resolve(by_login.group(1))
                people.append(Person(account_id, by_login.group(1) if account_id else None, "User",
                                     "co-author", sha))
            else:
                people.append(Person(None, None, "User", "co-author", sha))
    unique: dict[object, Person] = {}
    for index, person in enumerate(people):
        key = person.account_id if person.account_id is not None else (index, person.sha, person.source)
        unique.setdefault(key, person)
    return list(unique.values())


# ---- the decision -----------------------------------------------------------------------------
@dataclass
class Files:
    """Texts the decision reads. `base_*` are from the protected base branch; `head` holds the pull
    request's copy of only those protected files it changes (None means deleted)."""

    base_record: str | None = None
    base_exceptions: str | None = None
    base_registry: str | None = None
    base_agreement: str | None = None
    head: dict[str, str | None] = field(default_factory=dict)


@dataclass
class Result:
    errors: list[str] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)
    covered: list[str] = field(default_factory=list)
    exemptions: list[str] = field(default_factory=list)
    terms: Terms | None = None

    @property
    def ok(self) -> bool:
        return not self.errors


def founders_from(registry_text: str | None) -> tuple[dict[int, str], list[str]]:
    if registry_text is None:
        return {}, [f"{registry.REGISTRY_PATH} is missing on the base branch, so the founder is unknown."]
    entries, errors = registry.parse(registry_text)
    if errors:
        return {}, [f"{registry.REGISTRY_PATH} on the base branch is invalid: {errors[0]}"]
    return {e.account_id: e.username for e in entries if e.is_founder}, []


def evaluate(files: Files, pr: dict, resolve: Resolver, today: datetime.date | None = None) -> Result:
    """Judge a pull request. Pure: no network, no clock unless `today` is omitted."""
    today = today or datetime.datetime.now(datetime.timezone.utc).date()
    result = Result()
    result.errors += [f"Unsupported pull request: {u}" for u in pr.get("unsupported", [])]
    founders, problems = founders_from(files.base_registry)
    result.errors += problems
    if files.base_record is None or files.base_exceptions is None or files.base_agreement is None:
        result.errors.append(
            f"{RECORD_PATH}, {EXCEPTIONS_PATH} and {AGREEMENT_PATH} must all exist on the base "
            "branch; the check fails closed."
        )
        return result
    terms, rows, errors = parse_record(files.base_record)
    result.terms = terms
    rules, rule_errors = parse_exceptions(files.base_exceptions)
    result.errors += [f"{RECORD_PATH} on the base branch is invalid: {e}" for e in errors]
    result.errors += [f"{EXCEPTIONS_PATH} on the base branch is invalid: {e}" for e in rule_errors]
    if terms is None or errors or rule_errors or problems:
        return result

    if terms.in_force:
        base_version, base_revision = agreement_header(files.base_agreement)
        if sha256_text(files.base_agreement) != terms.sha or (base_version, base_revision) != (
            terms.legal, terms.editorial):
            result.errors.append("The agreement on the base branch does not match the frozen text in the record.")
            return result

    author_id = pr["author"]["id"]
    changed = {p for p in pr["files"]}
    touched = sorted(changed & PROTECTED_PATHS)
    if touched and author_id not in founders:
        result.errors.append(
            f"This pull request changes protected files ({', '.join(touched)}). Only the founder may "
            "change the agreement, its record, its exceptions or the enforcement."
        )
    result.errors += check_protected_changes(files, terms, rows, changed)

    exemption_by_id: dict[int, list[Exemption]] = {}
    for rule in rules:
        exemption_by_id.setdefault(rule.account_id, []).append(rule)
    founder_names = {name.lower() for name in founders.values()}
    for person in attributions(pr, resolve):
        judge_person(person, pr, terms, rows, exemption_by_id, founders, founder_names, today, result)
    return result


def check_protected_changes(files: Files, terms: Terms, rows: list[Acceptance], changed: set[str]) -> list[str]:
    """Validate the pull request's own copies of the record and agreement, as data."""
    errors: list[str] = []
    head_record = files.head.get(RECORD_PATH, files.base_record) if RECORD_PATH in changed else files.base_record
    head_agreement = (
        files.head.get(AGREEMENT_PATH, files.base_agreement) if AGREEMENT_PATH in changed else files.base_agreement
    )
    if RECORD_PATH in changed or AGREEMENT_PATH in changed:
        if head_record is None:
            return [f"{RECORD_PATH} must not be deleted."]
        head_terms, head_rows, head_errors = parse_record(head_record)
        errors += [f"The proposed {RECORD_PATH} is invalid: {e}" for e in head_errors]
        if head_terms is not None:
            errors += revision_errors(terms, head_terms, head_agreement, AGREEMENT_PATH in changed)
            if not head_errors:
                errors += record_change_errors(rows, head_rows)
    if EXCEPTIONS_PATH in changed:
        head_exceptions = files.head.get(EXCEPTIONS_PATH)
        if head_exceptions is None:
            errors.append(f"{EXCEPTIONS_PATH} must not be deleted.")
        else:
            errors += [f"The proposed {EXCEPTIONS_PATH} is invalid: {e}" for e in parse_exceptions(head_exceptions)[1]]
    return errors


def judge_person(person: Person, pr: dict, terms: Terms, rows: list[Acceptance],
                 exemptions: dict[int, list[Exemption]], founders: dict[int, str],
                 founder_names: set[str], today: datetime.date, result: Result) -> None:
    label = person.label()
    if person.account_id is None:
        result.errors.append(
            f"{label} cannot be matched to a GitHub account, so its acceptance cannot be checked. "
            "Link the commit email to your GitHub account, or remove the trailer."
        )
        return
    if person.account_id in founders:
        result.exemptions.append(f"{label}: founder-owned work (founder row in {registry.REGISTRY_PATH})")
        return
    for rule in exemptions.get(person.account_id, []):
        if not rule.active(today) or not rule.covers(pr.get("number")):
            continue
        if rule.authorizer.lower() not in founder_names:
            result.errors.append(f"Exception {rule.exception_id} was not authorized by the founder.")
            continue
        result.exemptions.append(f"{label}: {rule.describe()}")
        return
    if person.kind != "User" or person.login is None or person.login.endswith("[bot]"):
        result.errors.append(
            f"{label} is not an individual user account (type {registry.safe(person.kind)}). Only an "
            f"individual can accept the agreement; automation needs a founder-approved exception."
        )
        return
    if not terms.in_force:
        result.errors.append(
            f"{label} has not accepted a contribution agreement because none is in force "
            f"(status: {terms.status}). External code is not merged until the founder activates one."
        )
        return
    mine = [r for r in rows if r.account_id == person.account_id]
    if any(r.status == "withdrawn" for r in mine):
        result.errors.append(
            f"{label} has withdrawn from future participation. New contributions are blocked; only "
            "a founder-approved exception for this one pull request can admit it."
        )
        return
    current = next((r for r in mine if r.version == terms.legal), None)
    if current is None:
        older = ", ".join(sorted({r.version for r in mine}))
        result.errors.append(
            f"{label} has no recorded acceptance of legal-terms version {terms.legal}"
            + (f" (they accepted {older}; a material change needs renewed acceptance)." if older else ".")
            + " Post the acceptance statement from section 8 of the agreement as a comment, then ask "
            "the founder to record it. A comment is a request, not a record."
        )
        return
    if current.eligibility in HOLDS:
        result.errors.append(f"{label}: {HOLDS[current.eligibility]} (eligibility `{current.eligibility}`).")
        return
    note = f"{label}: accepted version {current.version}, eligibility `{current.eligibility}`"
    if current.username.lower() != (person.login or "").lower():
        note += f" (username changed since acceptance: was `{registry.safe(current.username)}`)"
    result.covered.append(note)


def render(result: Result) -> str:
    lines = ["### CLA acceptance check", "", f"* Result: **{'PASS' if result.ok else 'FAIL'}**"]
    if result.terms:
        t = result.terms
        lines.append(f"* Agreement on the protected base branch: `{t.status}`, legal-terms version "
                     f"`{t.legal}`, editorial revision `{t.editorial}`")
    lines += ["", "Passing means every contributor is covered. It is not a code review and not approval to merge."]
    for title, items in (("Covered", result.covered), ("Exemptions applied (audit log)", result.exemptions),
                         ("Notes", result.notices)):
        if items:
            lines += ["", f"**{title}**", *[f"* {i}" for i in items]]
    if result.errors:
        lines += ["", "**Fix these**", *[f"* {e}" for e in result.errors]]
    return "\n".join(lines) + "\n"


# ---- GitHub client ----------------------------------------------------------------------------
class GitHubError(Exception):
    pass


class NotFound(GitHubError):
    pass


class GitHub:
    """Read-only client with pagination, retry on transient errors and bounded rate-limit waits."""

    def __init__(self, token: str | None = None, opener=urllib.request.urlopen, sleep=time.sleep,
                 clock=time.time, attempts: int = 4, max_wait: float = 60.0) -> None:
        self.token, self.opener, self.sleep, self.clock = token, opener, sleep, clock
        self.attempts, self.max_wait = attempts, max_wait

    def _once(self, url: str):
        request = urllib.request.Request(  # noqa: S310 - https URL built from validated parts
            url, headers={"Accept": "application/vnd.github+json", "User-Agent": "viharbot-cla-gate"})
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        with self.opener(request, timeout=15) as response:  # noqa: S310  # nosec B310
            return json.load(response), response.headers

    def _wait(self, error: urllib.error.HTTPError, attempt: int) -> float | None:
        headers = error.headers or {}
        if error.code in (500, 502, 503, 504):
            return float(2**attempt)
        if error.code in (403, 429):
            retry = headers.get("Retry-After")
            if retry and str(retry).isdigit():
                return float(retry)
            reset = headers.get("X-RateLimit-Reset")
            if headers.get("X-RateLimit-Remaining") == "0" and reset and str(reset).isdigit():
                return max(0.0, int(reset) - self.clock()) + 1
        return None

    def get(self, path: str):
        url = path if path.startswith(API + "/") else API + path
        for attempt in range(self.attempts):
            try:
                return self._once(url)
            except urllib.error.HTTPError as error:
                if error.code == 404:
                    raise NotFound(path) from None
                wait = self._wait(error, attempt)
                if wait is None:
                    raise GitHubError(f"GitHub answered HTTP {error.code}") from None
                if wait > self.max_wait:
                    raise GitHubError("GitHub rate limit reached; re-run the check later") from None
            except (urllib.error.URLError, TimeoutError, OSError, ValueError):
                wait = float(2**attempt)
            if attempt + 1 < self.attempts:
                self.sleep(wait)
        raise GitHubError(f"GitHub could not be reached after {self.attempts} attempts")

    def paginate(self, path: str, max_pages: int) -> list:
        url = f"{path}{'&' if '?' in path else '?'}per_page=100"
        items: list = []
        for _ in range(max_pages):
            data, headers = self.get(url)
            items += data
            nxt = re.search(r'<([^>]+)>;\s*rel="next"', headers.get("Link", "") or "")
            if not nxt:
                return items
            url = nxt.group(1)
            if not url.startswith(API + "/"):
                raise GitHubError("GitHub returned a pagination link outside the API")
        raise GitHubError("The result has more pages than the check will read")


def _user(item: dict | None) -> dict | None:
    if isinstance(item, dict) and isinstance(item.get("id"), int) and item.get("login"):
        return {"id": item["id"], "login": item["login"], "type": item.get("type", "User")}
    return None


def fetch_pull_request(gh: GitHub, repo: str, number: int) -> dict:
    """The pull request's attribution facts. Anything it cannot read in full is `unsupported`."""
    meta = gh.get(f"/repos/{repo}/pulls/{number}")[0]
    author = _user(meta.get("user"))
    if author is None:
        raise GitHubError("The pull request has no readable author")
    pr = {"number": number, "author": author, "commits": [], "files": [], "unsupported": []}
    if meta["commits"] > MAX_COMMITS:
        pr["unsupported"].append(f"{meta['commits']} commits exceed the {MAX_COMMITS} GitHub lists; split it.")
    else:
        raw = gh.paginate(f"/repos/{repo}/pulls/{number}/commits", 3)
        if len(raw) != meta["commits"]:
            pr["unsupported"].append(f"GitHub listed {len(raw)} of {meta['commits']} commits.")
        for item in raw:
            commit = item.get("commit", {})
            pr["commits"].append({
                "sha": item.get("sha", ""), "message": commit.get("message", ""),
                "author_email": (commit.get("author") or {}).get("email"),
                "author": _user(item.get("author")), "committer": _user(item.get("committer")),
            })
    if meta["changed_files"] > MAX_FILES:
        pr["unsupported"].append(f"{meta['changed_files']} changed files exceed the {MAX_FILES} GitHub lists.")
    else:
        raw_files = gh.paginate(f"/repos/{repo}/pulls/{number}/files", 30)
        if len(raw_files) != meta["changed_files"]:
            pr["unsupported"].append(f"GitHub listed {len(raw_files)} of {meta['changed_files']} files.")
        for item in raw_files:
            pr["files"] += [item["filename"], *([item["previous_filename"]] if item.get("previous_filename") else [])]
    return pr


def github_resolver(gh: GitHub) -> Resolver:
    def resolve(login: str) -> int | None:
        try:
            data = gh.get(f"/users/{urllib.parse.quote(login, safe='')}")[0]
        except NotFound:
            return None
        except GitHubError as error:
            raise IdentityLookup(str(error)) from None
        return data["id"] if isinstance(data.get("id"), int) else None

    return resolve


# ---- gate commands ----------------------------------------------------------------------------
def _read(path: str | Path) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def load_files(base_dir: str, head_dir: str | None, changed: set[str]) -> Files:
    base = Path(base_dir)
    files = Files(_read(base / RECORD_PATH), _read(base / EXCEPTIONS_PATH),
                  _read(base / registry.REGISTRY_PATH), _read(base / AGREEMENT_PATH))
    if head_dir:
        for path in (RECORD_PATH, EXCEPTIONS_PATH, AGREEMENT_PATH):
            if path in changed:
                files.head[path] = _read(Path(head_dir) / path)
    return files


def finish(result: Result) -> int:
    report = render(result)
    sys.stdout.write(report)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(report)
    return 0 if result.ok else 1


def run_check(args: argparse.Namespace, resolve: Resolver | None = None) -> int:
    try:
        pr = json.loads(Path(args.pr_file).read_text(encoding="utf-8"))
        files = load_files(args.base_dir, args.head_dir, set(pr["files"]))
        return finish(evaluate(files, pr, resolve or (lambda login: None)))
    except (OSError, ValueError, KeyError, TypeError) as error:
        return finish(Result(errors=[f"The pull request data could not be read ({registry.safe(type(error).__name__)})."]))


def run_check_pr(args: argparse.Namespace, gh: GitHub | None = None) -> int:
    gh = gh or GitHub(os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"))
    try:
        pr = fetch_pull_request(gh, args.repo, args.pr)
        files = load_files(args.base_dir, args.head_dir, set(pr["files"]))
        return finish(evaluate(files, pr, github_resolver(gh)))
    except (GitHubError, IdentityLookup, KeyError, TypeError, ValueError) as error:
        return finish(Result(errors=[
            f"The check could not complete and fails closed ({registry.safe(error, 120)}). Re-run it."]))


# ---- founder tools ----------------------------------------------------------------------------
def inside_a_git_checkout(path: Path) -> bool:
    return any((p / ".git").exists() for p in [path.resolve(), *path.resolve().parents])


def check_comment(comment: dict, terms: Terms, repo: str, url: str) -> tuple[dict | None, list[str]]:
    """Judge an acceptance comment. Returns the evidence core to store privately, or the reasons."""
    user = comment.get("user") or {}
    problems = []
    if not terms.in_force:
        problems.append(f"No agreement is in force (status: {terms.status}); there is nothing to record yet.")
    if user.get("type") != "User" or not isinstance(user.get("id"), int) or not user.get("login"):
        problems.append("The comment was not written by an ordinary GitHub user account.")
    body = str(comment.get("body", ""))
    if " ".join(body.split()) != statement(terms.legal, repo):
        problems.append(f"The comment is not exactly the acceptance statement for version {terms.legal}.")
    if comment.get("updated_at") != comment.get("created_at"):
        problems.append("The comment was edited after it was posted. Ask for a fresh one.")
    if not _real_timestamp(str(comment.get("created_at", ""))):
        problems.append("The comment has no usable creation time.")
    if comment.get("html_url") != url:
        problems.append("The comment's own URL does not match the URL given.")
    if not str(comment.get("issue_url", "")).startswith(f"{API}/repos/{repo}/issues/"):
        problems.append("The comment does not belong to this repository.")
    if problems:
        return None, problems
    return {
        "schema_version": 1,
        "evidence_id": f"EV-{user['id']}-{comment['id']}",
        "repository": repo,
        "legal_terms_version": terms.legal,
        "frozen_text_sha256": terms.sha,
        "comment": {
            "id": comment["id"], "url": url, "created_at": comment["created_at"],
            "updated_at": comment["updated_at"], "body": body, "body_sha256": sha256_text(body),
        },
        "account": {"id": user["id"], "login_at_acceptance": user["login"], "type": user["type"]},
    }, []


def run_verify_comment(args: argparse.Namespace, gh: GitHub | None = None, now: str | None = None) -> int:
    text = _read(args.acceptances_file)
    terms = parse_record(text)[0] if text is not None else None
    match = COMMENT_URL.match(args.comment_url)
    if terms is None or not match or match.group("repo").lower() != args.repo.lower():
        sys.stderr.write("Need a readable record and a comment URL in the given repository.\n")
        return 2
    out = Path(args.evidence_out)
    if inside_a_git_checkout(out):
        sys.stderr.write("Refusing to write evidence inside a git checkout; use the private evidence store.\n")
        return 2
    gh = gh or GitHub(os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"))
    try:
        comment = gh.get(f"/repos/{args.repo}/issues/comments/{int(match.group('id'))}")[0]
    except GitHubError as error:
        sys.stderr.write(f"Could not read the comment from GitHub ({registry.safe(error)}).\n")
        return 2
    core, problems = check_comment(comment, terms, args.repo, args.comment_url)
    if core is None:
        sys.stdout.write("Not recordable:\n" + "".join(f"* {registry.safe(p, 200)}\n" for p in problems))
        return 1
    stamp = now or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    core["verification"] = {
        "verified_at": stamp, "verified_by": args.verified_by, "result": "valid",
        "checks": ["user account", "exact statement", "unedited at verification", "own URL", "repository"],
        "eligibility": args.eligibility, "registry_pr": args.registry_pr,
        "raw_comment_sha256": canonical_digest(comment),
    }
    digest = canonical_digest(core)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{core['evidence_id']}.json").write_text(
        json.dumps({"evidence": core, "evidence_sha256": digest, "raw_comment": comment}, indent=2, sort_keys=True),
        encoding="utf-8")
    acct = core["account"]
    sys.stdout.write(
        "Valid acceptance. Evidence written to the private store. Add this row to the Acceptances table\n"
        "through a pull request you review (it contains only what the check needs):\n\n"
        f"| {acct['id']} | [@{acct['login_at_acceptance']}](https://github.com/{acct['login_at_acceptance']}) "
        f"| {terms.legal} | {core['comment']['created_at']} | active | - | {args.eligibility} "
        f"| {core['evidence_id']} | {digest} |\n")
    return 0


def audit(rows: list[Acceptance], evidence_dir: Path, repo: str, gh: GitHub) -> tuple[list[str], list[str]]:
    """Compare each public row with its private evidence and with the live comment."""
    failures: list[str] = []
    warnings: list[str] = []
    for row in rows:
        name = f"account {row.account_id} ({row.evidence_id})"
        path = evidence_dir / f"{row.evidence_id}.json"
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
            core = stored["evidence"]
        except (OSError, ValueError, KeyError):
            failures.append(f"{name}: private evidence is missing or unreadable.")
            continue
        if canonical_digest(core) != row.evidence_sha or stored.get("evidence_sha256") != row.evidence_sha:
            failures.append(f"{name}: private evidence does not match the public digest.")
            continue
        if (core.get("account", {}).get("id") != row.account_id or core.get("legal_terms_version") != row.version
                or core.get("comment", {}).get("created_at") != row.effective
                or core.get("verification", {}).get("result") != "valid"
                or core.get("verification", {}).get("eligibility") != row.eligibility
                or core.get("repository", "").lower() != repo.lower()):
            failures.append(f"{name}: private evidence disagrees with the public row.")
            continue
        try:
            live = gh.get(f"/repos/{repo}/issues/comments/{core['comment']['id']}")[0]
        except NotFound:
            warnings.append(f"{name}: the acceptance comment was deleted after verification. The grant "
                            "stands on the private snapshot; the deletion changes nothing recorded.")
            continue
        except GitHubError as error:
            failures.append(f"{name}: could not read the live comment ({registry.safe(error)}).")
            continue
        if sha256_text(str(live.get("body", ""))) != core["comment"]["body_sha256"]:
            warnings.append(f"{name}: the live comment was edited after verification. The grant stands on "
                            "the private snapshot of what was verified.")
        elif live.get("user", {}).get("id") != row.account_id:
            failures.append(f"{name}: the live comment's author is not the account on record.")
    return failures, warnings


def run_audit(args: argparse.Namespace, gh: GitHub | None = None) -> int:
    text = _read(args.acceptances_file)
    terms, rows, errors = parse_record(text) if text is not None else (None, [], ["record missing"])
    if errors or terms is None:
        sys.stdout.write("The record is invalid:\n" + "".join(f"* {registry.safe(e, 200)}\n" for e in errors))
        return 1
    failures, warnings = audit(rows, Path(args.evidence_dir), args.repo,
                               gh or GitHub(os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")))
    sys.stdout.write(f"Audited {len(rows)} acceptance row(s).\n")
    sys.stdout.write("".join(f"WARN: {w}\n" for w in warnings) + "".join(f"FAIL: {f}\n" for f in failures))
    return 1 if failures else 0


def activation_report(repo: str, branch: str, record_text: str | None, gh: GitHub) -> tuple[list[tuple[str, str]], bool]:
    """(status, line) pairs. `ok` is only the checks a program can make; the rest are MANUAL."""
    lines: list[tuple[str, str]] = []
    terms, _, errors = parse_record(record_text) if record_text else (None, [], ["record missing"])
    lines.append(("PASS" if terms and not errors else "FAIL", "the record parses"))
    if terms:
        lines.append(("PASS" if not terms.gaps() else "BLOCKED", "record prerequisites: "
                      + ("all recorded" if not terms.gaps() else "; ".join(terms.gaps()))))
    try:
        rules = gh.get(f"/repos/{repo}/rules/branches/{urllib.parse.quote(branch, safe='')}")[0]
    except GitHubError as error:
        lines.append(("FAIL", f"could not read the live rules for `{branch}` ({registry.safe(error)})"))
        return lines, False
    kinds = {r.get("type") for r in rules}
    checks = [c for r in rules if r.get("type") == "required_status_checks"
              for c in r.get("parameters", {}).get("required_status_checks", [])]
    mine = [c for c in checks if c.get("context") == CHECK_NAME]
    lines.append(("PASS" if mine else "FAIL", f"`{CHECK_NAME}` is a required status check on `{branch}` (live)"))
    lines.append(("PASS" if any(c.get("integration_id") == ACTIONS_APP_ID for c in mine) else "FAIL",
                  f"`{CHECK_NAME}` is pinned to the GitHub Actions app, so no other app can report it"))
    pulls = [r.get("parameters", {}) for r in rules if r.get("type") == "pull_request"]
    lines.append(("PASS" if pulls and any(p.get("required_approving_review_count", 0) >= 1 for p in pulls)
                  else "FAIL", "a pull request with at least one approval is required"))
    lines.append(("PASS" if any(p.get("require_code_owner_review") for p in pulls) else "FAIL",
                  "code-owner review is required (makes CODEOWNERS binding)"))
    lines.append(("PASS" if {"deletion", "non_fast_forward"} <= kinds else "FAIL",
                  "deleting and force-pushing the branch are blocked"))
    return lines, all(status == "PASS" for status, _ in lines)


MANUAL_ITEMS = (
    "legal review of the agreement is complete and recorded",
    "the private evidence store exists, access is verified, and a restore test passed",
    "a real pull request from an unaccepted account failed `CLA acceptance` and could not merge",
    "the founder's admin bypass is understood and will not be used for external code",
)


def run_activation_check(args: argparse.Namespace, gh: GitHub | None = None) -> int:
    gh = gh or GitHub(os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"))
    lines, ok = activation_report(args.repo, args.branch, _read(args.acceptances_file), gh)
    for status, text in lines:
        sys.stdout.write(f"{status:8} {text}\n")
    for item in MANUAL_ITEMS:
        sys.stdout.write(f"MANUAL   {item}\n")
    sys.stdout.write("\nThis reads live GitHub settings. It cannot verify the MANUAL items; activation "
                     "is blocked until the founder has confirmed each one.\n")
    return 0 if ok else 1


def run_hash_agreement(args: argparse.Namespace) -> int:
    text = _read(args.file)
    if text is None:
        sys.stderr.write("File not found.\n")
        return 2
    sys.stdout.write(sha256_text(text) + "\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    pr = sub.add_parser("check-pr")
    pr.add_argument("--repo", required=True)
    pr.add_argument("--pr", type=int, required=True)
    pr.add_argument("--base-dir", default=".")
    pr.add_argument("--head-dir")
    chk = sub.add_parser("check")
    chk.add_argument("--pr-file", required=True)
    chk.add_argument("--base-dir", default=".")
    chk.add_argument("--head-dir")
    ver = sub.add_parser("verify-comment")
    ver.add_argument("--repo", required=True)
    ver.add_argument("--comment-url", required=True)
    ver.add_argument("--eligibility", required=True, choices=sorted(ELIGIBILITY))
    ver.add_argument("--verified-by", required=True)
    ver.add_argument("--evidence-out", required=True)
    ver.add_argument("--registry-pr", type=int)
    ver.add_argument("--acceptances-file", default=RECORD_PATH)
    aud = sub.add_parser("audit")
    aud.add_argument("--repo", required=True)
    aud.add_argument("--evidence-dir", required=True)
    aud.add_argument("--acceptances-file", default=RECORD_PATH)
    act = sub.add_parser("activation-check")
    act.add_argument("--repo", required=True)
    act.add_argument("--branch", default="main")
    act.add_argument("--acceptances-file", default=RECORD_PATH)
    hsh = sub.add_parser("hash-agreement")
    hsh.add_argument("--file", default=AGREEMENT_PATH)
    args = parser.parse_args(argv)
    handlers = {"check-pr": run_check_pr, "check": run_check, "verify-comment": run_verify_comment,
                "audit": run_audit, "activation-check": run_activation_check, "hash-agreement": run_hash_agreement}
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
