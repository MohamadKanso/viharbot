"""Decide whether a pull request's authors have accepted the contribution agreement.

Standard library only, offline except for the founder's `verify-comment` helper.

Rules that govern everything here:

* The record is data. Acceptance is read only from the protected base branch, never from a
  pull request's copy of CLA-ACCEPTANCES.md, so a pull request cannot accept for itself.
* The check fails closed. A missing or malformed record, an unlinked commit author, or a commit
  list that does not match the pull request's commit count is a failure, never a pass.
* While the record says `draft` no agreement is in force, so every author who is not exempt
  fails. That is CONTRIBUTING's "external code will not be merged until a CLA is approved".
* Exempt: the founder (the `Founder` row of CONTRIBUTORS.md on the base branch) and the
  dependency bot. Trusted contributors are not exempt; their terms are not defined yet.
* A pass means "every author is covered". It is not code review and not approval to merge.
  Founder approval is a ruleset and CODEOWNERS matter; Actions cannot enforce it.

Usage:
  cla_gate.py check --acceptances-file F --registry-file F --author-id N --author-login L
                    --commits-file F --commit-count N [--changed-files a,b]
  cla_gate.py verify-comment --repo OWNER/NAME --comment-url URL [--acceptances-file F]
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import contributor_registry as registry  # noqa: E402

ACCEPTANCES_PATH = "CLA-ACCEPTANCES.md"
CLA_PATH = "docs/governance/contributor-license-agreement.md"
PROTECTED_PATHS = frozenset({ACCEPTANCES_PATH, CLA_PATH})
EXEMPT_BOTS = frozenset({"dependabot[bot]"})
HEADER = ("GitHub username", "GitHub numeric account ID", "CLA version", "Accepted on (UTC)", "Evidence")

STATUS = re.compile(r"(?m)^\* CLA status: (draft|in-force)[ \t]*$")
VERSION = re.compile(r"(?m)^\* CLA version: ([0-9A-Za-z][0-9A-Za-z.\-]{0,31})[ \t]*$")
VERSION_VALUE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.\-]{0,31}$")
EVIDENCE = re.compile(
    r"^https://github\.com/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/(?:issues|pull)/[0-9]+#issuecomment-[0-9]+$"
)
COMMENT_URL = re.compile(
    r"^https://github\.com/(?P<repo>[A-Za-z0-9._-]+/[A-Za-z0-9._-]+)/(?:issues|pull)/[0-9]+"
    r"#issuecomment-(?P<id>[0-9]+)$"
)
SHA = re.compile(r"^[0-9a-f]{7,40}$")


def statement(version: str, repo: str = "vchokshi/viharbot") -> str:
    """The exact acceptance text from section 8 of the agreement."""
    return (
        f"I have read the ViharBot Contributor License Agreement, version {version}, "
        f"and I agree to its terms for my contributions to {repo}."
    )


@dataclass(frozen=True)
class Acceptance:
    username: str
    account_id: int
    version: str
    accepted: str
    evidence: str


@dataclass(frozen=True)
class Terms:
    status: str  # draft | in-force
    version: str

    @property
    def in_force(self) -> bool:
        return self.status == "in-force"


@dataclass(frozen=True)
class Person:
    account_id: int | None
    login: str | None
    sha: str | None = None


@dataclass
class Result:
    errors: list[str] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)
    covered: list[str] = field(default_factory=list)
    exempt: list[str] = field(default_factory=list)
    terms: Terms | None = None

    @property
    def ok(self) -> bool:
        return not self.errors


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _real_date(value: str) -> bool:
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        return False
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value))


def parse(text: str) -> tuple[Terms | None, list[Acceptance], list[str]]:
    """Parse the status lines and the Acceptances table. Any problem is reported."""
    errors: list[str] = []
    statuses, versions = STATUS.findall(text), VERSION.findall(text)
    if len(statuses) != 1:
        errors.append("There must be exactly one line `* CLA status: draft` or `* CLA status: in-force`.")
    if len(versions) != 1:
        errors.append("There must be exactly one line `* CLA version: <version>`.")
    terms = Terms(statuses[0], versions[0]) if len(statuses) == 1 and len(versions) == 1 else None

    lines = text.splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == "## Acceptances")
    except StopIteration:
        return terms, [], [*errors, "There is no '## Acceptances' section."]
    table: list[tuple[int, str]] = []
    for number, line in enumerate(lines[start + 1 :], start=start + 2):
        if line.startswith("## "):
            break
        if line.strip().startswith("|"):
            table.append((number, line))
    if len(table) < 2 or tuple(_cells(table[0][1])) != HEADER:
        return terms, [], [*errors, f"The Acceptances table header must be exactly: {' | '.join(HEADER)}."]

    rows: list[Acceptance] = []
    seen: set[tuple[int, str]] = set()
    for number, line in table[2:]:
        cells = _cells(line)
        if len(cells) != 5:
            errors.append(f"Line {number}: expected 5 columns, found {len(cells)}.")
            continue
        link, raw_id, version, accepted, evidence = cells
        match = registry.ROW_LINK.match(link)
        problems = []
        if not match or match.group(2) != match.group(1) or not registry.USERNAME.match(match.group(1)):
            problems.append("the username must look like [@name](https://github.com/name)")
        if not registry.ACCOUNT_ID.match(raw_id):
            problems.append("the account ID must be a positive whole number with no leading zeros")
        if not VERSION_VALUE.match(version) or version.endswith("-draft"):
            problems.append("the version must be an approved version, not a draft")
        if not _real_date(accepted):
            problems.append("'Accepted on' must be a real date, YYYY-MM-DD")
        if not EVIDENCE.match(evidence):
            problems.append("the evidence must be the URL of the acceptance comment")
        if problems:
            errors.append(f"Line {number}: " + "; ".join(problems) + ".")
            continue
        key = (int(raw_id), version)
        if key in seen:
            errors.append(f"Line {number}: account ID {raw_id} already has an acceptance of version {version}.")
        seen.add(key)
        rows.append(Acceptance(match.group(1), int(raw_id), version, accepted, evidence))
    return terms, rows, errors


def founder_ids(registry_text: str | None) -> tuple[set[int], list[str]]:
    if registry_text is None:
        return set(), [f"{registry.REGISTRY_PATH} is missing on the base branch, so the founder is unknown."]
    entries, errors = registry.parse(registry_text)
    if errors:
        return set(), [f"{registry.REGISTRY_PATH} on the base branch is invalid: {errors[0]}"]
    return {e.account_id for e in entries if e.is_founder}, []


def label(person: Person) -> str:
    if person.login:
        return f"`{registry.safe(person.login)}` (ID {person.account_id})"
    return f"commit `{registry.safe(person.sha or '?', 12)}` (no linked GitHub account)"


def evaluate(
    acceptances_text: str | None,
    registry_text: str | None,
    author: Person,
    commit_authors: list[Person],
    commit_count: int,
    changed_files: set[str],
) -> Result:
    """Judge a pull request. `acceptances_text` and `registry_text` come from the base branch."""
    result = Result()
    founders, problems = founder_ids(registry_text)
    result.errors += problems
    if acceptances_text is None:
        result.errors.append(f"{ACCEPTANCES_PATH} is missing on the base branch; the check fails closed.")
        return result
    terms, rows, errors = parse(acceptances_text)
    result.terms = terms
    if errors or terms is None:
        result.errors += [f"{ACCEPTANCES_PATH} on the base branch is invalid: {e}" for e in errors]
        return result
    if result.errors:
        return result

    if len(commit_authors) != commit_count:
        result.errors.append(
            f"GitHub reports {commit_count} commits but {len(commit_authors)} were listed, so some "
            "authors cannot be checked. Split the pull request or ask the founder to review it."
        )
    touched = sorted(changed_files & PROTECTED_PATHS)
    if touched and author.account_id not in founders:
        result.errors.append(
            f"This pull request changes {', '.join(touched)}. Only the founder may change the agreement "
            "or its acceptance record; an author cannot accept for themselves."
        )

    subjects: dict[int | str, Person] = {}
    for person in [author, *commit_authors]:
        subjects.setdefault(person.account_id if person.account_id is not None else f"sha:{person.sha}", person)
    for person in subjects.values():
        if person.account_id is None:
            result.errors.append(
                f"{label(person)} cannot be matched to an account. Set the commit email to one linked "
                "to your GitHub account, or ask the founder to review it."
            )
        elif person.account_id in founders:
            result.exempt.append(f"{label(person)}: founder")
        elif person.login in EXEMPT_BOTS:
            result.exempt.append(f"{label(person)}: dependency bot")
        elif not terms.in_force:
            result.errors.append(
                f"{label(person)} has not accepted a contribution agreement because none is in force "
                f"(status: {terms.status}). External code is not merged until the founder approves one."
            )
        elif any(r.account_id == person.account_id and r.version == terms.version for r in rows):
            result.covered.append(f"{label(person)}: accepted version {terms.version}")
        else:
            result.errors.append(
                f"{label(person)} has no recorded acceptance of version {terms.version}. Post the "
                f"acceptance statement from section 8 of the agreement as a comment, then ask the "
                "founder to record it. A comment is a request, not a record."
            )
    return result


def render(result: Result) -> str:
    status = "PASS" if result.ok else "FAIL"
    lines = ["### CLA acceptance check", "", f"* Result: **{status}**"]
    if result.terms:
        lines.append(f"* Agreement on the protected base branch: `{result.terms.status}`, version `{result.terms.version}`")
    lines += [
        "",
        "Passing means every author is covered. It is not a code review and not approval to merge.",
    ]
    for title, items in (("Covered", result.covered), ("Exempt", result.exempt), ("Notes", result.notices)):
        if items:
            lines += ["", f"**{title}**", *[f"* {i}" for i in items]]
    if result.errors:
        lines += ["", "**Fix these**", *[f"* {e}" for e in result.errors]]
    return "\n".join(lines) + "\n"


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except FileNotFoundError:
        return None


def read_commits(path: str) -> list[Person]:
    """JSON lines of {"sha", "id", "login"} as written by the workflow. Raises ValueError."""
    people = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            item = json.loads(line)
            ident, login, sha = item.get("id"), item.get("login"), str(item.get("sha") or "")
            if ident is not None and not isinstance(ident, int):
                raise ValueError("a commit author ID is not a number")
            people.append(Person(ident, login if isinstance(login, str) else None, sha if SHA.match(sha) else None))
    return people


def run_check(args: argparse.Namespace) -> int:
    try:
        commits = read_commits(args.commits_file)
    except (OSError, ValueError) as error:
        result = Result(errors=[f"The commit list could not be read ({registry.safe(type(error).__name__)})."])
    else:
        result = evaluate(
            _read(args.acceptances_file),
            _read(args.registry_file),
            Person(args.author_id, args.author_login),
            commits,
            args.commit_count,
            {f for f in (args.changed_files or "").split(",") if f},
        )
    report = render(result)
    sys.stdout.write(report)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(report)
    return 0 if result.ok else 1


def fetch_comment(repo: str, comment_id: int, token: str | None = None) -> dict:
    request = urllib.request.Request(  # noqa: S310 - fixed https URL, validated components
        f"https://api.github.com/repos/{repo}/issues/comments/{int(comment_id)}",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "viharbot-cla-gate"},
    )
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310  # nosec B310
        return json.load(response)


def check_comment(comment: dict, terms: Terms, repo: str, url: str) -> tuple[Acceptance | None, list[str]]:
    """Judge an acceptance comment. Returns the row to record, or the reasons it is not valid."""
    user = comment.get("user") or {}
    problems: list[str] = []
    if not terms.in_force:
        problems.append(f"No agreement is in force (status: {terms.status}); there is nothing to record yet.")
    if user.get("type") != "User" or not isinstance(user.get("id"), int) or not user.get("login"):
        problems.append("The comment was not written by an ordinary GitHub user account.")
    if " ".join(str(comment.get("body", "")).split()) != statement(terms.version, repo):
        problems.append(
            "The comment is not exactly the acceptance statement for version "
            f"{terms.version}. Ask the contributor to post it unchanged."
        )
    if comment.get("updated_at") != comment.get("created_at"):
        problems.append("The comment was edited after it was posted. Ask for a fresh one.")
    created = str(comment.get("created_at", ""))[:10]
    if not _real_date(created):
        problems.append("The comment has no usable creation date.")
    if problems:
        return None, problems
    return Acceptance(user["login"], user["id"], terms.version, created, url), []


def run_verify_comment(args: argparse.Namespace, fetch=fetch_comment) -> int:
    text = _read(args.acceptances_file)
    terms = parse(text)[0] if text is not None else None
    match = COMMENT_URL.match(args.comment_url)
    if terms is None or not match or match.group("repo").lower() != args.repo.lower():
        sys.stderr.write("Need a readable record and a comment URL in the given repository.\n")
        return 2
    try:
        comment = fetch(args.repo, int(match.group("id")), os.environ.get("GITHUB_TOKEN"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as error:
        sys.stderr.write(f"Could not read the comment from GitHub ({registry.safe(type(error).__name__)}).\n")
        return 2
    row, problems = check_comment(comment, terms, args.repo, args.comment_url)
    if row is None:
        sys.stdout.write("Not recordable:\n" + "".join(f"* {registry.safe(p, 200)}\n" for p in problems))
        return 1
    sys.stdout.write(
        "Valid acceptance. Add this row to the Acceptances table through a pull request you review:\n\n"
        f"| [@{row.username}](https://github.com/{row.username}) | {row.account_id} | {row.version} "
        f"| {row.accepted} | {row.evidence} |\n"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check")
    check.add_argument("--acceptances-file", required=True)
    check.add_argument("--registry-file", required=True)
    check.add_argument("--author-id", type=int, required=True)
    check.add_argument("--author-login", required=True)
    check.add_argument("--commits-file", required=True)
    check.add_argument("--commit-count", type=int, required=True)
    check.add_argument("--changed-files", default="")
    verify = sub.add_parser("verify-comment")
    verify.add_argument("--repo", required=True)
    verify.add_argument("--comment-url", required=True)
    verify.add_argument("--acceptances-file", default=ACCEPTANCES_PATH)
    args = parser.parse_args(argv)
    return run_check(args) if args.command == "check" else run_verify_comment(args)


if __name__ == "__main__":
    sys.exit(main())
