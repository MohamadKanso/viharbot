"""What GitHub says about a pull request, gathered read-only and reduced to structured facts.

Only GET requests are made, through the CLA gate's own read-only client. The pull request,
comments and commits are untrusted: this module keeps the fields the assessment needs (ids, logins,
timestamps, states) and the raw comment objects only because the gate's exact-match check needs
them. Nothing here interprets free text.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass

from .cla import load_gate

FIRST_TIMER_ASSOCIATIONS = frozenset({"FIRST_TIME_CONTRIBUTOR", "FIRST_TIMER", "NONE"})
MAX_OPEN_PULLS = 25  # a bounded sweep: a run never reads an unbounded number of pull requests
MAX_COMMENT_PAGES = 5
MAX_REVIEW_PAGES = 3
CLA_PATHS = {
    "base_record": "CLA-ACCEPTANCES.md",
    "base_exceptions": "CLA-EXCEPTIONS.md",
    "base_registry": "CONTRIBUTORS.md",
    "base_agreement": "docs/governance/contributor-license-agreement.md",
}
_SAFE_REF = re.compile(r"^[A-Za-z0-9._/-]{1,100}$")


@dataclass(frozen=True)
class PullSnapshot:
    repo: str
    number: int
    author: dict  # {"id", "login", "type"}
    author_association: str
    state: str
    draft: bool
    head_sha: str
    is_fork: bool
    mergeable_state: str
    created_at: str
    updated_at: str
    pr: dict  # the CLA gate's normalized pull request (author, commits, files, unsupported)
    comments: tuple  # raw issue comments, needed by the gate's exact-match check
    reviews: tuple  # {"login", "id", "state"} only
    ci_runs: tuple  # {"name", "status", "conclusion"} only
    ci_error: bool = False  # CI could not be read, so its state is unknown


def summarize_ci(runs: tuple) -> tuple[str, int]:
    """Reduce workflow and check runs to one state and a count. Fixed vocabulary only."""
    if not runs:
        return "none", 0
    conclusions = {r.get("conclusion") for r in runs}
    statuses = {r.get("status") for r in runs}
    if "action_required" in conclusions or "waiting" in statuses:
        return "awaiting_approval", len(runs)
    if conclusions & {"failure", "timed_out", "cancelled", "startup_failure"}:
        return "failing", len(runs)
    if statuses - {"completed"}:
        return "pending", len(runs)
    if conclusions <= {"success", "skipped", "neutral"}:
        return "passing", len(runs)
    return "pending", len(runs)


def _user(item: dict | None) -> dict | None:
    if isinstance(item, dict) and isinstance(item.get("id"), int) and item.get("login"):
        return {"id": item["id"], "login": item["login"], "type": item.get("type", "User")}
    return None


def _decode(item: dict) -> str | None:
    if item.get("encoding") != "base64" or not isinstance(item.get("content"), str):
        return None
    try:
        return base64.b64decode(item["content"]).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None


def fetch_cla_files(gh, repo: str, branch: str = "main"):
    """The CLA texts from the protected base branch, pinned to one commit so they are consistent.

    A file that is missing becomes None, which the gate treats as fail-closed. Anything the
    contributor's branch contains is never read here.
    """
    gate = load_gate()
    if not _SAFE_REF.match(branch):
        raise ValueError("unsafe branch name")
    sha = gh.get(f"/repos/{repo}/commits/{branch}")[0].get("sha")
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise gate.GitHubError("could not pin the base branch")
    texts = {}
    for attribute, path in CLA_PATHS.items():
        try:
            data = gh.get(f"/repos/{repo}/contents/{path}?ref={sha}")[0]
        except gate.NotFound:
            texts[attribute] = None
            continue
        texts[attribute] = _decode(data) if isinstance(data, dict) else None
    return gate.Files(**texts), sha


def fetch_snapshot(gh, repo: str, number: int) -> PullSnapshot:
    """Gather one pull request. Raises the gate's GitHubError if the essentials cannot be read."""
    gate = load_gate()
    meta = gh.get(f"/repos/{repo}/pulls/{number}")[0]
    author = _user(meta.get("user"))
    if author is None:
        raise gate.GitHubError("the pull request has no readable author")
    pr = gate.fetch_pull_request(gh, repo, number)
    comments = tuple(gh.paginate(f"/repos/{repo}/issues/{number}/comments", MAX_COMMENT_PAGES))
    reviews = tuple(
        {"login": (r.get("user") or {}).get("login"), "id": (r.get("user") or {}).get("id"),
         "state": r.get("state")}
        for r in gh.paginate(f"/repos/{repo}/pulls/{number}/reviews", MAX_REVIEW_PAGES)
    )
    head = meta.get("head") or {}
    head_sha = str(head.get("sha", ""))
    head_repo = (head.get("repo") or {}).get("full_name")
    runs: tuple = ()
    ci_error = not re.fullmatch(r"[0-9a-f]{40}", head_sha)  # never build a URL from odd input
    try:
        if ci_error:
            raise gate.GitHubError("unusable head sha")
        workflow = gh.get(f"/repos/{repo}/actions/runs?head_sha={head_sha}&per_page=50")[0]
        checks = gh.get(f"/repos/{repo}/commits/{head_sha}/check-runs?per_page=50")[0]
        runs = tuple(
            {"name": r.get("name"), "status": r.get("status"), "conclusion": r.get("conclusion")}
            for r in [*workflow.get("workflow_runs", []), *checks.get("check_runs", [])]
        )
    except gate.GitHubError:
        ci_error = True
    mergeable_state = str(meta.get("mergeable_state") or "unknown")
    if mergeable_state == "unknown":
        # GitHub computes this lazily on the first read of a pull request. By now (after the other
        # reads) it has usually finished, so look once more rather than report "not computed".
        try:
            again = gh.get(f"/repos/{repo}/pulls/{number}")[0]
            mergeable_state = str(again.get("mergeable_state") or "unknown")
        except gate.GitHubError:
            pass
    return PullSnapshot(
        repo=repo, number=number, author=author,
        author_association=str(meta.get("author_association", "NONE")),
        state=str(meta.get("state", "")), draft=bool(meta.get("draft")), head_sha=head_sha,
        is_fork=bool(head_repo and head_repo.lower() != repo.lower()),
        mergeable_state=mergeable_state,
        created_at=str(meta.get("created_at", "")), updated_at=str(meta.get("updated_at", "")),
        pr=pr, comments=comments, reviews=reviews, ci_runs=runs, ci_error=ci_error,
    )


def list_first_time_pulls(gh, repo: str, founder_ids: set[int]) -> list[int]:
    """Open pull request numbers from people who look like first-time contributors.

    Bots and the founder are excluded. The sweep is bounded to MAX_OPEN_PULLS.
    """
    items = gh.paginate(f"/repos/{repo}/pulls?state=open", 1)
    numbers = []
    for item in items:
        user = item.get("user") or {}
        if user.get("type") != "User" or user.get("id") in founder_ids:
            continue
        if str(item.get("author_association", "NONE")) in FIRST_TIMER_ASSOCIATIONS:
            numbers.append(int(item["number"]))
    return numbers[:MAX_OPEN_PULLS]
