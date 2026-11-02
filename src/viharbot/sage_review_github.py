"""The GitHub adapter for Sage's `code_review`: fetch a pull request as data, review it, publish one
advisory comment.

Run from the trusted base branch by `.github/workflows/sage-review.yml`:

    python -m viharbot.sage_review_github --repo OWNER/NAME --pr 12 [--allow-fork] [--force]

The pull request is only ever read through the GitHub API. Its code is never checked out, imported
or executed. This adapter has no call that submits a review, sets a status, merges, closes or
changes any setting; it can only read the pull request, read check runs and write one issue comment.

Configuration: GITHUB_TOKEN (least-privilege, from the workflow), GITHUB_REPOSITORY,
GITHUB_API_URL (https only), and the model variables in `sage_code_review`. The LiteLLM key is read
by the model client alone and is never passed through this module.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pydantic import ValidationError

from .observability import configure_logging
from .sage_code_review import (
    HARD_MAX_FILES,
    MAX_PATCH_CHARS,
    ChangedFile,
    CIResult,
    PolicyExcerpt,
    SageCodeReviewRequest,
    SageReviewConfig,
    SageReviewResult,
    run_code_review,
)
from .sage_review_format import (
    CI_PENDING_MARKER,
    MARKER,
    SHA_RE,
    STATUS_RE,
    format_comment,
    format_skipped,
    mark_stale,
)

logger = logging.getLogger("sage-review")

DEFAULT_API = "https://api.github.com"
# Read from the checked-out base branch, never from the pull request.
POLICY_FILES: tuple[str, ...] = (
    "CONTRIBUTING.md",
    "docs/governance/change-rationale-policy.md",
    "docs/governance/agent-constitution-v1.md",
    "docs/governance/agent-architecture-v1.1.md",
    "docs/governance/founding-charter-v2.md",
)
# Our own workflow's check must not count as CI evidence for the review it is producing.
SELF_CHECK_NAMES = {"sage-review", "Sage review"}
CONCLUSIONS = {"success", "failure", "neutral", "cancelled", "skipped", "timed_out",
               "action_required", "stale", "startup_failure"}
BOT_LOGINS = {"github-actions[bot]"}
COMMENT_LIMIT = 65_000

# (method, url, headers, body) -> (status, response bytes). Injected in tests; no network there.
Transport = Callable[[str, str, dict[str, str], bytes | None], tuple[int, bytes]]


class GitHubError(RuntimeError):
    """A GitHub call failed. The message never includes the token or response bodies."""


class HeadMoved(RuntimeError):
    """The pull request received a new commit while it was being reviewed."""


def urllib_transport(method: str, url: str, headers: dict[str, str], body: bytes | None) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)  # noqa: S310  # nosec B310 - https only; see api_base()
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310  # nosec B310 - https only; see api_base()
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def api_base() -> str:
    """The API root. Only https is accepted, so the token is never sent in the clear."""
    value = os.getenv("GITHUB_API_URL", DEFAULT_API).rstrip("/")
    if urlparse(value).scheme != "https":
        raise GitHubError("GITHUB_API_URL must be https")
    return value


class GitHubClient:
    def __init__(self, token: str, transport: Transport = urllib_transport, base: str | None = None) -> None:
        if not token:
            raise GitHubError("no GitHub token")
        self._token = token
        self._transport = transport
        self._base = base or api_base()

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "viharbot-sage-review",
        }
        body = None
        if payload is not None:
            body = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        try:
            status, data = self._transport(method, f"{self._base}{path}", headers, body)
        except Exception as exc:
            raise GitHubError(f"GitHub request failed: {type(exc).__name__}") from None
        if status >= 400:
            raise GitHubError(f"GitHub returned {status} for {method} {path.split('?')[0]}")
        return json.loads(data) if data else None

    def pages(self, path: str, limit: int) -> list[Any]:
        """Up to `limit` items from a paginated list endpoint."""
        items: list[Any] = []
        sep = "&" if "?" in path else "?"
        page = 1
        while len(items) < limit:
            batch = self.request("GET", f"{path}{sep}per_page=100&page={page}")
            if not isinstance(batch, list):
                batch = (batch or {}).get("check_runs", [])
            items.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return items


# ---- building the request -----------------------------------------------------------------------


def load_policies(root: Path, budget: int) -> list[PolicyExcerpt]:
    """Governance text from the trusted checkout, trimmed to the budget; trimming is disclosed."""
    excerpts: list[PolicyExcerpt] = []
    remaining = budget
    for name in POLICY_FILES:
        path = root / name
        if remaining <= 0 or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        take = text[:remaining]
        excerpts.append(PolicyExcerpt(name=name, text=take, truncated=len(take) < len(text)))
        remaining -= len(take)
    return excerpts


def build_request(
    client: GitHubClient, repo: str, number: int, config: SageReviewConfig, policy_root: Path
) -> tuple[SageCodeReviewRequest | None, dict[str, Any]]:
    """Fetch the pull request as data and fit it to the configured limits.

    Returns the request (None when the pull request has no reviewable files) and the raw PR record.
    Files beyond the limits are counted in `files_omitted`, which makes the review incomplete.
    """
    pr = client.request("GET", f"/repos/{repo}/pulls/{number}")
    raw_files = client.pages(f"/repos/{repo}/pulls/{number}/files", HARD_MAX_FILES * 3)
    files: list[ChangedFile] = []
    used = 0
    omitted = 0
    for raw in raw_files:
        if len(files) >= config.max_files:
            omitted += 1
            continue
        patch = raw.get("patch")
        truncated = False
        if isinstance(patch, str):
            # Bounded by the remaining total budget and by the schema's per-file ceiling.
            room = max(min(config.max_diff_chars - used, MAX_PATCH_CHARS), 0)
            if len(patch) > room:
                patch, truncated = patch[:room], True
            used += len(patch)
        else:
            patch = None
        files.append(
            ChangedFile(
                path=raw["filename"], status=raw.get("status", "changed"),
                additions=int(raw.get("additions", 0)), deletions=int(raw.get("deletions", 0)),
                patch=patch, patch_truncated=truncated,
            )
        )
    if not files:
        return None, pr
    # The file list is fetched after the PR record and can be newer; refuse a mixed snapshot.
    if client.request("GET", f"/repos/{repo}/pulls/{number}")["head"]["sha"] != pr["head"]["sha"]:
        raise HeadMoved
    total = int(pr.get("changed_files") or len(raw_files))
    omitted = max(omitted, total - len(files))
    head = pr["head"]["sha"]
    runs = client.pages(f"/repos/{repo}/commits/{head}/check-runs", 100)
    ci = [
        CIResult(name=str(r["name"])[:200], status=r.get("status", "queued"), conclusion=r.get("conclusion") if r.get("conclusion") in CONCLUSIONS else None)
        for r in runs
        if r.get("name") not in SELF_CHECK_NAMES and r.get("status") in {"queued", "in_progress", "completed"}
    ][:60]
    request = SageCodeReviewRequest(
        repository=repo, pr_number=number, base_sha=pr["base"]["sha"], head_sha=head,
        title=(pr.get("title") or "")[:500], description=(pr.get("body") or "")[:20_000],
        files=files, files_omitted=omitted, ci_results=ci, config=config,
        policies=load_policies(policy_root, config.max_policy_chars),
    )
    return request, pr


# ---- publishing ---------------------------------------------------------------------------------


def find_comment(client: GitHubClient, repo: str, number: int) -> dict[str, Any] | None:
    """Sage's own comment. Only one written by the Actions bot counts, so a person who pastes the
    marker into a comment cannot get Sage to overwrite theirs or to skip a review."""
    for comment in client.pages(f"/repos/{repo}/issues/{number}/comments", 1000):
        user = comment.get("user") or {}
        if MARKER in (comment.get("body") or "") and user.get("login") in BOT_LOGINS:
            return comment
    return None


def publish(client: GitHubClient, repo: str, number: int, body: str, existing: dict[str, Any] | None) -> None:
    body = body[:COMMENT_LIMIT]
    if existing:
        client.request("PATCH", f"/repos/{repo}/issues/comments/{existing['id']}", {"body": body})
    else:
        client.request("POST", f"/repos/{repo}/issues/{number}/comments", {"body": body})


def already_reviewed(existing: dict[str, Any] | None, head_sha: str) -> bool:
    """A finished review (complete or incomplete) for exactly this commit makes a rerun redundant.
    A failed or skipped one does not, so a rerun can recover."""
    if not existing:
        return False
    body = existing.get("body") or ""
    sha, status = SHA_RE.search(body), STATUS_RE.search(body)
    if CI_PENDING_MARKER in body:  # approval was withheld for unfinished CI; a rerun may refresh it
        return False
    return bool(sha and status and sha.group(1) == head_sha and status.group(1) in {"complete", "incomplete"})


def write_summary(text: str) -> None:
    path = os.getenv("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text + "\n")


def outcome_line(result: SageReviewResult) -> str:
    """The workflow's record of the outcome. Metadata only: no review content."""
    return (
        f"Sage review of PR #{result.pr_number} at `{result.head_sha[:12]}`: "
        f"{result.review.recommendation} (advisory), completeness {result.completeness}, "
        f"model `{result.model_alias or 'none'}`, {result.duration_ms:.0f} ms"
    )


def review_pull_request(
    client: GitHubClient,
    repo: str,
    number: int,
    *,
    policy_root: Path,
    config: SageReviewConfig | None = None,
    allow_fork: bool = False,
    force: bool = False,
    runner: Callable[[SageCodeReviewRequest], SageReviewResult] | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> str:
    """Run one review end to end and return a short status word. Raises GitHubError only when
    the comment could not be published."""
    config = config or SageReviewConfig()
    existing = find_comment(client, repo, number)
    pr = client.request("GET", f"/repos/{repo}/pulls/{number}")
    head = pr["head"]["sha"]

    head_repo = ((pr.get("head") or {}).get("repo") or {}).get("full_name")
    if pr.get("state") != "open":
        return "skipped-closed"
    if head_repo != repo and not allow_fork:
        publish(client, repo, number, format_skipped(
            head, "This pull request comes from a fork, so it was not reviewed automatically. "
            "Automatic reviews run only for branches in this repository, because the review needs "
            "a credential that must not be exposed to forked changes. The founder can run the "
            "workflow manually if a review is wanted.", timestamp=now()), existing)
        return "skipped-fork"
    if not force and already_reviewed(existing, head):
        return "unchanged"
    previous = SHA_RE.search((existing or {}).get("body") or "")
    if existing and previous and previous.group(1) != head:
        # The previous recommendation applies to an older commit.
        publish(client, repo, number, mark_stale(existing.get("body") or "", head), existing)

    try:
        request, _ = build_request(client, repo, number, config, policy_root)
    except HeadMoved:
        return "superseded"
    except ValidationError:
        publish(client, repo, number, format_skipped(
            head, "This pull request's data could not be turned into a valid review request "
            "(for example an unsupported file path), so it was not reviewed.", timestamp=now()), existing)
        return "skipped-invalid"
    if request is None:
        publish(client, repo, number, format_skipped(
            head, "This pull request has no changed files to review.", timestamp=now()), existing)
        return "skipped-empty"
    if request.head_sha != head:
        return "superseded"
    result = (runner or run_code_review)(request)
    # A push during the model call makes this result stale before it is shown. Publish nothing; the
    # new commit's own run reviews it, and the old comment already carries the stale banner.
    if client.request("GET", f"/repos/{repo}/pulls/{number}")["head"]["sha"] != request.head_sha:
        return "superseded"
    # Re-find: the stale-marking update above changed nothing about the comment's id.
    publish(client, repo, number, format_comment(result, timestamp=now()), existing or find_comment(client, repo, number))
    write_summary(outcome_line(result))
    logger.info(
        "sage review published",
        extra={"agent": "sage", "request_id": result.request_id, "model_alias": result.model_alias,
               "duration_ms": result.duration_ms, "outcome": result.review.recommendation},
    )
    return result.completeness


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    parser = argparse.ArgumentParser(description="Publish Sage's advisory review of a pull request.")
    parser.add_argument("--repo", default=os.getenv("GITHUB_REPOSITORY", ""))
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--allow-fork", action="store_true", help="only for a manual, founder-run review")
    parser.add_argument("--force", action="store_true", help="review again even if this commit was reviewed")
    parser.add_argument("--policy-root", default=".")
    args = parser.parse_args(argv)
    try:
        client = GitHubClient(os.getenv("GITHUB_TOKEN", ""))
        status = review_pull_request(
            client, args.repo, args.pr, policy_root=Path(args.policy_root),
            allow_fork=args.allow_fork, force=args.force,
        )
    except GitHubError as exc:
        # The comment could not be written. Fail loudly; this never gates a merge.
        logger.error("sage review not published", extra={"agent": "sage", "error": str(exc)})
        print(f"Sage review was not published: {exc}", file=sys.stderr)
        return 1
    print(f"sage review: {status}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
