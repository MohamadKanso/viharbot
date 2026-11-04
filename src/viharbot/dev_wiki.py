"""Development Wiki: a read-only view of GitHub epics, issues, pull requests and repository docs.

GitHub stays the source of truth. This module only reads from it, on the server, with a token the
browser never sees, and turns what it finds into plain JSON for the web app's Development Wiki.
It makes no model calls and writes nothing anywhere (the one GitHub verb it has is GET).

Rules the numbers follow, so nothing is invented:

* An *epic* is an issue labelled ``epic`` or titled ``[Epic]`` / ``Epic:``. The set is discovered,
  never listed here.
* A *child* is an issue the epic names as its own: a checklist item, a "Child ..." line, a bullet
  that starts with the reference, an issue linked through GitHub sub-issues, or an issue whose body
  says ``Parent: #N`` / ``Part of #N``. Other mentions are *related* or *dependencies*.
* Completion is ``closed children / linked children``. With no linked children it is ``null``
  (unknown), never zero and never a guess. The method is returned next to every figure.
* Only the first two maturity levels can be shown from GitHub (an issue exists; a merged pull
  request is linked). Tested, integrated, deployed and operationally validated are reported as
  "not evidenced here" with a pointer to ``docs/development-status.md``. A closed issue or a merged
  pull request does not prove deployment.

Configuration (environment): ``DEV_WIKI_GITHUB_TOKEN`` (read-only; a fine-grained token with
Contents, Issues, Pull requests and Metadata read access to this one repository), ``DEV_WIKI_REPO``
(default ``vchokshi/viharbot``), ``DEV_WIKI_API_URL`` (https, or http to a loopback address),
``DEV_WIKI_CACHE_SECONDS`` (default 300), ``DEV_WIKI_BRANCH`` (default ``main``). Without a token
the wiki reports itself unavailable and makes no GitHub call.
"""

from __future__ import annotations

import base64
import binascii
import gzip
import http.client
import ipaddress
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger("dev-wiki")

DEFAULT_REPO = "vchokshi/viharbot"
DEFAULT_API = "https://api.github.com"
PAGE_SIZE = 100
MAX_PAGES = 20  # per list; a list that needs more is reported as truncated, not silently cut
MAX_DOC_BYTES = 400_000
REQUEST_TIMEOUT = 20
TRANSPORT_ATTEMPTS = 3  # GETs are idempotent; a dropped or truncated read is retried
FAILURE_RETRY_SECONDS = 30
AGENT_NAMES = ("Charlie", "Nova", "Atlas", "Pixel", "Sage", "Cipher", "Scout", "Orbit",
               "Kepler", "Archie", "Ada")
MATURITY_LEVELS = ("proposed", "implemented", "tested", "integrated", "deployed",
                   "operationally validated")
STATUS_DOC = "docs/development-status.md"

# (method-less) GET: (url, headers) -> (status, response headers with lower-case names, body).
Transport = Callable[[str, dict[str, str]], tuple[int, dict[str, str], bytes]]


# --------------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------------

class WikiConfig:
    def __init__(self, env: dict[str, str] | None = None) -> None:
        env = os.environ if env is None else env
        self.token = env.get("DEV_WIKI_GITHUB_TOKEN", "").strip()
        self.repo = env.get("DEV_WIKI_REPO", DEFAULT_REPO).strip() or DEFAULT_REPO
        self.api_url = (env.get("DEV_WIKI_API_URL", "").strip() or DEFAULT_API).rstrip("/")
        self.branch = env.get("DEV_WIKI_BRANCH", "main").strip() or "main"
        try:
            self.ttl = max(30, int(env.get("DEV_WIKI_CACHE_SECONDS", "300")))
        except ValueError:
            self.ttl = 300

    @property
    def configured(self) -> bool:
        return bool(self.token) and bool(re.fullmatch(r"[\w.-]+/[\w.-]+", self.repo)) \
            and api_url_allowed(self.api_url)

    @property
    def web_base(self) -> str:
        return f"https://github.com/{self.repo}"


def api_url_allowed(url: str) -> bool:
    """The token may go to https, or to http only at a literal loopback or private address."""
    parsed = urlparse(url)
    if parsed.scheme == "https" and parsed.hostname:
        return True
    if parsed.scheme == "http" and parsed.hostname:
        try:
            ip = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            return False
        return ip.is_loopback or ip.is_private
    return False


# --------------------------------------------------------------------------------------------
# GitHub reader
# --------------------------------------------------------------------------------------------

class WikiError(Exception):
    """A failure with a code that is safe to show. The message never holds the token or a body."""

    def __init__(self, code: str, retry_after: float | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: urllib would resend the Authorization header to the new host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _attempt(request: urllib.request.Request) -> tuple[int, dict[str, str], bytes]:
    """One GET. Reads the body of an HTTP error response too, inside the caller's retry boundary."""
    try:
        with _OPENER.open(request, timeout=REQUEST_TIMEOUT) as response:  # noqa: S310
            return response.status, *_read(response)
    except urllib.error.HTTPError as exc:  # includes unfollowed 3xx; a cut-off body raises out
        try:
            return exc.code, *_read(exc)
        finally:
            exc.close()


def _default_transport(url: str, headers: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
    headers = {**headers, "Accept-Encoding": "gzip"}  # smaller bodies are less likely to be cut
    last: Exception | None = None
    for attempt in range(TRANSPORT_ATTEMPTS):
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            return _attempt(request)
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError, EOFError,
                ValueError) as exc:
            last = exc
            if attempt + 1 < TRANSPORT_ATTEMPTS:
                time.sleep(0.5 * (attempt + 1))
    logger.warning("development wiki transport failed", extra={
        "agent": "web", "error": type(last).__name__})
    raise WikiError("network")


def _read(response) -> tuple[dict[str, str], bytes]:
    headers = {k.lower(): v for k, v in response.headers.items()}
    body = response.read()
    if headers.get("content-encoding", "").lower() == "gzip":
        body = gzip.decompress(body)
    return headers, body


class GitHubReader:
    """Read-only GitHub REST client: pagination, rate-limit bookkeeping, no other verbs."""

    def __init__(self, config: WikiConfig, transport: Transport | None = None) -> None:
        self.config = config
        self.transport = transport or _default_transport
        self.rate_remaining: int | None = None
        self.rate_reset: int | None = None
        self.requests = 0

    def _get(self, url: str, accept: str = "application/vnd.github+json"):
        headers = {"Accept": accept, "Authorization": f"Bearer {self.config.token}",
                   "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "viharbot-dev-wiki"}
        status, response_headers, body = self.transport(url, headers)
        self.requests += 1
        if "x-ratelimit-remaining" in response_headers:
            self.rate_remaining = _to_int(response_headers.get("x-ratelimit-remaining"))
            self.rate_reset = _to_int(response_headers.get("x-ratelimit-reset"))
        if status in (403, 429) and (
            self.rate_remaining == 0 or "retry-after" in response_headers or status == 429
        ):
            wait = _to_int(response_headers.get("retry-after"))
            if wait is None and self.rate_reset:
                wait = max(0, self.rate_reset - int(time.time()))
            raise WikiError("rate_limited", retry_after=float(wait if wait is not None else 60))
        if 300 <= status < 400:
            raise WikiError("redirect_refused")  # a moved API is reported, never followed
        if status in (401, 403):
            raise WikiError("auth_failed")
        if status == 404:
            raise WikiError("not_found")
        if status >= 400:
            raise WikiError("github_error")
        return response_headers, body

    def get_json(self, path: str, params: dict[str, Any] | None = None):
        query = "&".join(f"{k}={v}" for k, v in (params or {}).items())
        url = f"{self.config.api_url}{path}" + (f"?{query}" if query else "")
        headers, body = self._get(url)
        try:
            return json.loads(body)
        except ValueError:
            raise WikiError("bad_response") from None

    def paginate(self, path: str, params: dict[str, Any] | None = None) -> tuple[list, bool]:
        """Every item of a list endpoint, following ``Link: rel="next"``. Returns (items, truncated)."""
        query = "&".join(f"{k}={v}" for k, v in {"per_page": PAGE_SIZE, **(params or {})}.items())
        url: str | None = f"{self.config.api_url}{path}?{query}"
        items: list = []
        for _ in range(MAX_PAGES):
            headers, body = self._get(url)
            try:
                page = json.loads(body)
            except ValueError:
                raise WikiError("bad_response") from None
            if not isinstance(page, list):
                raise WikiError("bad_response")
            items.extend(page)
            url = _next_link(headers.get("link", ""), self.config.api_url)
            if url is None:
                return items, False
        return items, True


def _to_int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _next_link(link_header: str, api_url: str) -> str | None:
    """The ``next`` URL, only if it stays on the configured API host (the token goes with it)."""
    for part in link_header.split(","):
        match = re.match(r'\s*<([^>]+)>\s*;\s*rel="next"', part)
        if match:
            nxt = match.group(1)
            a, b = urlparse(nxt), urlparse(api_url)
            if (a.scheme, a.netloc) == (b.scheme, b.netloc):
                return nxt
    return None


# --------------------------------------------------------------------------------------------
# Reference parsing
# --------------------------------------------------------------------------------------------

REF_RE = re.compile(r"(?<![\w/&#])#(\d{1,6})\b")
CODE_FENCE_RE = re.compile(r"```.*?```", re.S)
INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
DEPENDENCY_RE = re.compile(
    r"\b(depends?\s+on|dependenc(?:y|ies)|blocked\s+by|requires?|prerequisite|after)\b", re.I)
BLOCKER_RE = re.compile(r"\bblocked\s+by\b", re.I)
CHILD_RE = re.compile(r"\bchild(?:ren)?\b|child issue index|execution hierarchy|→", re.I)
CHECKLIST_RE = re.compile(r"^\s*[-*]\s+\[[ xX]\]\s")
BULLET_REF_RE = re.compile(r"^\s*[-*]\s+(?:\*\*)?#\d")
PARENT_RE = re.compile(r"\b(?:parent|part\s+of|epic)\s*:?\s*#(\d{1,6})\b", re.I)
EPIC_TITLE_RE = re.compile(r"\[epic\]|^\s*epic\s*[:—–-]", re.I)
PHASE_RE = re.compile(r"\[Phase\s+([\d.]+)\]", re.I)
DOC_PATH_RE = re.compile(r"(?<![\w/])((?:docs/[\w./-]+|README|CONTRIBUTING)\.md)")
ADR_RE = re.compile(r"\bADR-(\d{3})\b")


def strip_code(text: str) -> str:
    return INLINE_CODE_RE.sub("", CODE_FENCE_RE.sub("", text or ""))


def classify_references(body: str, own_number: int) -> dict[str, list[int]]:
    """Sort the ``#N`` references of an epic body into child / dependency / blocker / related.

    The classification is by the line a reference sits on, so it is conservative: a mention that
    is not clearly a child is "related" and never counts toward progress.
    """
    result: dict[str, list[int]] = {"child": [], "dependency": [], "blocker": [], "related": []}
    seen: dict[int, str] = {}
    rank = {"blocker": 0, "dependency": 1, "child": 2, "related": 3}
    for line in strip_code(body).splitlines():
        line_is_item = bool(CHECKLIST_RE.match(line) or BULLET_REF_RE.match(line))
        # A line often holds several sentences ("Blocked by #21. Related: #30."); classify each
        # clause on its own so one phrase does not decide the fate of every reference on the line.
        for clause in re.split(r"(?<=[.;])\s+", line):
            numbers = [int(n) for n in REF_RE.findall(clause) if int(n) != own_number]
            if not numbers:
                continue
            if BLOCKER_RE.search(clause):
                kind = "blocker"
            elif DEPENDENCY_RE.search(clause) and not line_is_item:
                kind = "dependency"
            elif line_is_item or CHILD_RE.search(clause):
                kind = "child"
            else:
                kind = "related"
            for number in numbers:
                if number not in seen or rank[kind] < rank[seen[number]]:
                    seen[number] = kind
    for number, kind in seen.items():
        result[kind].append(number)
    return result


def parse_checklist(body: str) -> list[dict[str, Any]]:
    """Acceptance and scope checkboxes from the epic body, with the section they sit under."""
    items: list[dict[str, Any]] = []
    section = ""
    for line in CODE_FENCE_RE.sub("", body or "").splitlines():
        heading = re.match(r"^#{1,6}\s+(.*)", line)
        if heading:
            section = heading.group(1).strip()
            continue
        match = re.match(r"^\s*[-*]\s+\[([ xX])\]\s+(.*)", line)
        if match:
            items.append({"section": section, "text": match.group(2).strip()[:300],
                          "checked": match.group(1).lower() == "x"})
    return items


def is_epic(issue: dict) -> bool:
    labels = {name.lower() for name in _label_names(issue)}
    return "epic" in labels or bool(EPIC_TITLE_RE.search(issue.get("title") or ""))


# --------------------------------------------------------------------------------------------
# Snapshot
# --------------------------------------------------------------------------------------------

def _iso(value: str | None) -> str | None:
    return value if isinstance(value, str) and value else None


def _label_names(issue: dict) -> list[str]:
    names = []
    for label in issue.get("labels") or []:
        if isinstance(label, dict) and label.get("name"):
            names.append(str(label["name"]))
        elif isinstance(label, str):
            names.append(label)
    return names


class Snapshot:
    """Everything the wiki shows, computed once per sync as plain JSON-safe data."""

    def __init__(self, data: dict[str, Any], tree: dict[str, dict], head_sha: str | None) -> None:
        self.data = data
        self.tree = tree  # path -> {"sha": blob sha, "size": int}
        self.head_sha = head_sha


def _agent_of(issue: dict) -> tuple[str | None, str | None]:
    for label in _label_names(issue):
        match = re.match(r"(?i)agent\s*[:/]\s*(.+)", label)
        if match:
            return match.group(1).strip().title(), "label"
    title = issue.get("title") or ""
    # The agent the epic is about is the first one its title names ("Kepler ... with Sage review"
    # is Kepler's epic, not Sage's).
    hits = [(m.start(), name) for name in AGENT_NAMES
            if (m := re.search(rf"\b{name}\b", title))]
    return (min(hits)[1], "title") if hits else (None, None)


def build_snapshot(reader: GitHubReader, now: datetime | None = None) -> Snapshot:
    config = reader.config
    repo = config.repo
    web = config.web_base
    now = now or datetime.now(timezone.utc)

    raw_issues, issues_truncated = reader.paginate(f"/repos/{repo}/issues", {"state": "all"})
    raw_pulls, pulls_truncated = reader.paginate(
        f"/repos/{repo}/pulls", {"state": "all", "sort": "updated", "direction": "desc"})
    milestones, _ = reader.paginate(f"/repos/{repo}/milestones", {"state": "all"})
    commits_raw = reader.get_json(f"/repos/{repo}/commits", {"sha": config.branch, "per_page": 100})
    if not isinstance(commits_raw, list):
        raise WikiError("bad_response")
    try:
        releases, _ = reader.paginate(f"/repos/{repo}/releases")
    except WikiError as exc:
        if exc.code in {"rate_limited", "network"}:
            raise
        releases = []
    tree_json = reader.get_json(f"/repos/{repo}/git/trees/{config.branch}", {"recursive": 1})
    tree: dict[str, dict] = {}
    for entry in tree_json.get("tree", []) if isinstance(tree_json, dict) else []:
        path = entry.get("path", "")
        if entry.get("type") == "blob" and path.endswith(".md") and (
            path.startswith("docs/") or "/" not in path
        ):
            tree[path] = {"sha": entry.get("sha"), "size": entry.get("size", 0)}
    head_sha = commits_raw[0].get("sha") if commits_raw and isinstance(commits_raw[0], dict) else None

    def safe_url(url: Any, fallback: str) -> str:
        return url if isinstance(url, str) and url.startswith(web + "/") else fallback

    issues: dict[int, dict] = {}
    for raw in raw_issues:
        if not isinstance(raw, dict) or not isinstance(raw.get("number"), int):
            continue
        if "pull_request" in raw:
            continue
        number = raw["number"]
        issues[number] = {
            "number": number, "title": raw.get("title") or "", "state": raw.get("state"),
            "url": safe_url(raw.get("html_url"), f"{web}/issues/{number}"),
            "created_at": _iso(raw.get("created_at")), "closed_at": _iso(raw.get("closed_at")),
            "labels": _label_names(raw),
            "assignees": [a.get("login") for a in raw.get("assignees") or [] if isinstance(a, dict)
                          and a.get("login")],
            "milestone": (raw.get("milestone") or {}).get("title"),
            "_body": raw.get("body") or "",
        }

    pulls: dict[int, dict] = {}
    for raw in raw_pulls:
        if not isinstance(raw, dict) or not isinstance(raw.get("number"), int):
            continue
        number = raw["number"]
        merged = bool(raw.get("merged_at"))
        pulls[number] = {
            "number": number, "title": raw.get("title") or "",
            "state": "merged" if merged else raw.get("state"), "draft": bool(raw.get("draft")),
            "url": safe_url(raw.get("html_url"), f"{web}/pull/{number}"),
            "created_at": _iso(raw.get("created_at")), "merged_at": _iso(raw.get("merged_at")),
            "closed_at": _iso(raw.get("closed_at")), "merge_commit_sha": raw.get("merge_commit_sha"),
            "author": (raw.get("user") or {}).get("login"),
            "_text": f"{raw.get('title') or ''}\n{raw.get('body') or ''}\n"
                     f"{(raw.get('head') or {}).get('ref') or ''}",
        }

    epic_numbers = sorted(n for n, i in issues.items() if is_epic(i))
    epic_set = set(epic_numbers)

    # Child links: from the epic body, from GitHub sub-issues, and from "Parent: #N" in children.
    links: dict[int, dict[str, Any]] = {}
    sub_issue_children: dict[int, set[int]] = {}
    for number in epic_numbers:
        refs = classify_references(issues[number]["_body"], number)
        sub: set[int] = set()
        try:
            for item in reader.get_json(f"/repos/{repo}/issues/{number}/sub_issues",
                                        {"per_page": PAGE_SIZE}) or []:
                if isinstance(item, dict) and isinstance(item.get("number"), int):
                    sub.add(item["number"])
        except WikiError as exc:
            if exc.code in {"rate_limited", "network"}:
                raise  # the endpoint not existing is fine; being cut off is not
        sub_issue_children[number] = sub
        links[number] = {"refs": refs}
    parented: dict[int, set[int]] = {n: set() for n in epic_numbers}
    for number, issue in issues.items():
        if number in epic_set:
            continue
        for match in PARENT_RE.finditer(strip_code(issue["_body"])):
            parent = int(match.group(1))
            if parent in parented:
                parented[parent].add(number)

    pr_epics: dict[int, set[int]] = {}
    commit_epics: dict[str, set[int]] = {}

    epics: list[dict[str, Any]] = []
    children_union: dict[int, dict] = {}
    for number in epic_numbers:
        issue = issues[number]
        refs = links[number]["refs"]
        child_numbers: dict[int, str] = {}
        unresolved: list[int] = []
        pr_refs: list[int] = []
        for n in refs["child"]:
            if n in epic_set:
                refs["related"].append(n)  # epics are never children of epics here
            elif n in issues:
                child_numbers[n] = "body"
            elif n in pulls:
                pr_refs.append(n)
            else:
                unresolved.append(n)
        for n in sub_issue_children[number]:
            if n in issues and n not in epic_set:
                child_numbers[n] = "sub-issue"
        for n in parented[number]:
            child_numbers.setdefault(n, "parent-reference")
        children = []
        for n, how in sorted(child_numbers.items()):
            child = {k: v for k, v in issues[n].items() if not k.startswith("_")}
            child["linked_by"] = how
            children.append(child)
            children_union[n] = child
        done = sum(1 for c in children if c["state"] == "closed")
        progress = {
            "done": done, "total": len(children),
            "percent": round(100 * done / len(children)) if children else None,
            "method": ("Closed linked child issues ÷ linked child issues. Children are the issues "
                       "this epic names as its own (checklist items, 'Child' lines, sub-issues, or "
                       "'Parent: #N'); related issues and unresolved references are excluded."),
            "state": "measured" if children else "unknown",
        }
        dependencies = [n for n in refs["dependency"] + refs["blocker"] if n in issues]
        dependency_rows = [
            {**{k: v for k, v in issues[n].items() if not k.startswith("_")},
             "explicit_blocker": n in refs["blocker"]} for n in dict.fromkeys(dependencies)]
        epic_label = _label_names(issue)
        explicit_blocked = any("blocked" in label.lower() for label in epic_label)
        open_blockers = [d for d in dependency_rows if d["explicit_blocker"] and d["state"] == "open"]
        agent, agent_source = _agent_of(issue)
        phase_match = PHASE_RE.search(issue["title"])
        docs_mentioned = []
        body = issue["_body"]
        for path in dict.fromkeys(DOC_PATH_RE.findall(body)):
            if path in tree:
                docs_mentioned.append(path)
        for adr in dict.fromkeys(ADR_RE.findall(body)):
            for path in tree:
                if path.startswith(f"docs/adr/ADR-{adr}"):
                    docs_mentioned.append(path)
        epics.append({
            **{k: v for k, v in issue.items() if not k.startswith("_")},
            "agent": agent, "agent_source": agent_source,
            "phase": phase_match.group(1) if phase_match else None,
            "owners": issue["assignees"],
            "owner_note": None if issue["assignees"] or agent else "No owner or agent is documented.",
            "objective": _objective(body),
            "children": children,
            "unresolved_references": unresolved,
            "related": [{k: v for k, v in issues[n].items() if not k.startswith("_")}
                        for n in dict.fromkeys(refs["related"]) if n in issues and n != number
                        ][:30],
            "dependencies": dependency_rows,
            "blockers": open_blockers,
            "explicit_blocked_label": explicit_blocked,
            "progress": progress,
            "checklist": parse_checklist(body),
            "docs": sorted(set(docs_mentioned)),
            "_child_numbers": set(child_numbers),
            "_ref_numbers": {number} | set(child_numbers),
        })

    # Pull requests and commits attach to an epic through an explicit #N reference.
    epic_by_number = {e["number"]: e for e in epics}
    for pr_number, pr in pulls.items():
        mentioned = {int(n) for n in REF_RE.findall(strip_code(pr["_text"]))} - {pr_number}
        for epic in epics:
            if mentioned & epic["_ref_numbers"]:
                pr_epics.setdefault(pr_number, set()).add(epic["number"])
    for commit in commits_raw:
        if not isinstance(commit, dict) or not commit.get("sha"):
            continue
        message = (commit.get("commit") or {}).get("message") or ""
        found: set[int] = set()
        for n in (int(x) for x in REF_RE.findall(message)):
            if n in pulls:
                found |= pr_epics.get(n, set())
            for epic in epics:
                if n in epic["_ref_numbers"]:
                    found.add(epic["number"])
        if found:
            commit_epics[commit["sha"]] = found

    def commit_row(commit: dict) -> dict:
        info = commit.get("commit") or {}
        return {"sha": commit["sha"], "short": commit["sha"][:7],
                "message": (info.get("message") or "").splitlines()[0][:200] if info.get("message")
                else "", "date": _iso((info.get("author") or {}).get("date")),
                "url": f"{web}/commit/{commit['sha']}"}

    commit_rows = {c["sha"]: commit_row(c) for c in commits_raw
                   if isinstance(c, dict) and c.get("sha")}

    for epic in epics:
        n = epic["number"]
        epic_prs = [{k: v for k, v in pulls[p].items() if not k.startswith("_")}
                    for p, e in sorted(pr_epics.items(), reverse=True) if n in e]
        epic_commits = [commit_rows[s] for s, e in commit_epics.items() if n in e][:15]
        merged = [p for p in epic_prs if p["state"] == "merged"]
        open_prs = [p for p in epic_prs if p["state"] == "open"]
        epic["pull_requests"] = epic_prs
        epic["commits"] = epic_commits
        epic["counts"] = {
            "children": len(epic["children"]),
            "children_open": sum(1 for c in epic["children"] if c["state"] == "open"),
            "children_closed": sum(1 for c in epic["children"] if c["state"] == "closed"),
            "prs_merged": len(merged), "prs_open": len(open_prs),
        }
        epic["status"], epic["status_evidence"] = _status(epic, merged, open_prs)
        epic["maturity"] = _maturity(epic, merged)
        epic["activity"] = _activity(epic)
        epic["next_actions"] = _next_actions(epic)
        epic["milestones"] = sorted({c["milestone"] for c in epic["children"] if c.get("milestone")})

    # ---- dashboard ----
    status_counts = {s: 0 for s in ("completed", "active", "planned", "blocked")}
    for epic in epics:
        status_counts[epic["status"]] += 1
    all_children = list(children_union.values())
    overall = {
        "done": sum(1 for c in all_children if c["state"] == "closed"), "total": len(all_children),
        "percent": (round(100 * sum(1 for c in all_children if c["state"] == "closed")
                          / len(all_children)) if all_children else None),
        "method": ("Closed ÷ total over the distinct child issues linked from all epics. Epics "
                   "with no linked children contribute nothing and are listed as unknown."),
    }
    merged_recent = sorted(
        (p for p in pulls.values() if p["state"] == "merged" and p["merged_at"]),
        key=lambda p: p["merged_at"], reverse=True)[:10]
    pr_epic_numbers = lambda num: sorted(pr_epics.get(num, set()))  # noqa: E731
    recent_prs = [{**{k: v for k, v in p.items() if not k.startswith("_")},
                   "epics": pr_epic_numbers(p["number"])} for p in merged_recent]
    blockers = [{"epic": e["number"], "epic_title": e["title"], **b}
                for e in epics for b in e["blockers"]]
    blockers += [{"epic": e["number"], "epic_title": e["title"], "number": e["number"],
                  "title": "Epic carries a 'blocked' label", "url": e["url"], "state": "open",
                  "explicit_blocker": True}
                 for e in epics if e["explicit_blocked_label"] and e["state"] == "open"]
    upcoming = []
    for m in milestones:
        if not isinstance(m, dict) or m.get("state") != "open":
            continue
        due = _iso(m.get("due_on"))
        upcoming.append({"title": m.get("title"), "due_on": due,
                         "open_issues": m.get("open_issues"), "closed_issues": m.get("closed_issues"),
                         "url": safe_url(m.get("html_url"), f"{web}/milestones")})
    upcoming.sort(key=lambda m: (m["due_on"] is None, m["due_on"] or ""))

    # ---- timeline ----
    epic_attrs = {e["number"]: e for e in epics}
    events: list[dict[str, Any]] = []

    def add_event(date, kind, title, number, url, state, epic_numbers_):
        if not date:
            return
        eps = sorted(epic_numbers_)
        events.append({
            "date": date, "kind": kind, "title": title, "number": number, "url": url,
            "status": state, "epics": eps,
            "agents": sorted({epic_attrs[e]["agent"] for e in eps if epic_attrs[e]["agent"]}),
            "phases": sorted({epic_attrs[e]["phase"] for e in eps if epic_attrs[e]["phase"]}),
        })

    issue_epics: dict[int, set[int]] = {}
    for epic in epics:
        for n in epic["_ref_numbers"]:
            issue_epics.setdefault(n, set()).add(epic["number"])
    for n, issue in issues.items():
        kind_open, kind_close = ("epic_created", "epic_closed") if n in epic_set else (
            "issue_opened", "issue_closed")
        eps = issue_epics.get(n, set())
        add_event(issue["created_at"], kind_open, issue["title"], n, issue["url"], "open", eps)
        if issue["state"] == "closed":
            add_event(issue["closed_at"], kind_close, issue["title"], n, issue["url"], "closed", eps)
    for n, pr in pulls.items():
        if pr["merged_at"]:
            add_event(pr["merged_at"], "pr_merged", pr["title"], n, pr["url"], "merged",
                      pr_epics.get(n, set()))
    for m in milestones:
        if isinstance(m, dict) and m.get("closed_at"):
            add_event(_iso(m.get("closed_at")), "milestone_reached", m.get("title") or "",
                      None, safe_url(m.get("html_url"), f"{web}/milestones"), "closed", set())
    for r in releases:
        if isinstance(r, dict) and not r.get("draft") and r.get("published_at"):
            add_event(_iso(r.get("published_at")), "release", r.get("name") or r.get("tag_name") or "",
                      None, safe_url(r.get("html_url"), f"{web}/releases"), "published", set())
    events.sort(key=lambda e: e["date"], reverse=True)

    docs_index = [{"path": p, "size": m["size"]} for p, m in sorted(tree.items())]
    public_epics = [{k: v for k, v in e.items() if not k.startswith("_")} for e in epics]
    data = {
        "repo": repo, "branch": config.branch, "head_sha": head_sha,
        "synced_at": now.isoformat().replace("+00:00", "Z"),
        "counts": {
            "issues_fetched": len(issues), "pull_requests_fetched": len(pulls),
            "epics": len(epics), "milestones": len(milestones), "commits_scanned": len(commit_rows),
            "github_requests": reader.requests,
            "truncated": bool(issues_truncated or pulls_truncated),
        },
        "rate_limit": {"remaining": reader.rate_remaining, "reset": reader.rate_reset},
        "dashboard": {
            "epic_total": len(epics), "by_status": status_counts, "overall_progress": overall,
            "epics_with_unknown_progress": [e["number"] for e in epics
                                            if e["progress"]["state"] == "unknown"],
            "recent_merged_prs": recent_prs, "open_blockers": blockers,
            "upcoming_milestones": upcoming[:10],
            "deployments_note": ("Deployments are not shown: GitHub deploy records are not "
                                 "evidence that anything ran. Releases appear on the timeline."),
        },
        "epics": public_epics,
        "timeline": events[:1000],
        "docs": docs_index,
    }
    return Snapshot(data, tree, head_sha)


def _objective(body: str) -> str | None:
    """The text under an Objective / Purpose / Goal heading, or the first paragraph."""
    text = CODE_FENCE_RE.sub("", body or "")
    match = re.search(r"(?im)^#{1,6}\s*(?:objective|purpose|goal|why)[^\n]*\n+(.+?)(?:\n#{1,6}\s|\Z)",
                      text, re.S)
    chunk = match.group(1) if match else next(
        (p for p in re.split(r"\n\s*\n", text) if p.strip() and not p.lstrip().startswith("#")), "")
    chunk = chunk.strip()
    return chunk[:700] if chunk else None


def _status(epic: dict, merged: list, open_prs: list) -> tuple[str, list[dict]]:
    evidence: list[dict] = []
    if epic["state"] == "closed":
        evidence.append({"claim": "The epic issue is closed.", "url": epic["url"]})
        return "completed", evidence
    if epic["blockers"] or epic["explicit_blocked_label"]:
        for b in epic["blockers"]:
            evidence.append({"claim": f"Blocked by open #{b['number']}.", "url": b["url"]})
        if epic["explicit_blocked_label"]:
            evidence.append({"claim": "The epic carries a 'blocked' label.", "url": epic["url"]})
        return "blocked", evidence
    closed_children = [c for c in epic["children"] if c["state"] == "closed"]
    for c in closed_children[:5]:
        evidence.append({"claim": f"Child #{c['number']} is closed.", "url": c["url"]})
    for p in merged[:5]:
        evidence.append({"claim": f"PR #{p['number']} referencing this epic was merged.",
                         "url": p["url"]})
    for p in open_prs[:5]:
        evidence.append({"claim": f"PR #{p['number']} referencing this epic is open.",
                         "url": p["url"]})
    if evidence:
        return "active", evidence
    return "planned", [{"claim": "Open, with no closed child issue and no linked pull request.",
                        "url": epic["url"]}]


def _maturity(epic: dict, merged: list) -> list[dict]:
    rows = [{"level": "proposed", "state": "evidenced",
             "evidence": [{"claim": f"Epic #{epic['number']} exists.", "url": epic["url"]}]}]
    if merged:
        rows.append({"level": "implemented", "state": "evidenced",
                     "evidence": [{"claim": f"Merged PR #{p['number']}: {p['title']}",
                                   "url": p["url"]} for p in merged[:5]]})
    else:
        rows.append({"level": "implemented", "state": "not_evidenced", "evidence": []})
    for level in MATURITY_LEVELS[2:]:
        rows.append({"level": level, "state": "not_evidenced", "evidence": [],
                     "note": "Unknown: GitHub issues and pull requests do not establish this; see "
                             f"{STATUS_DOC} for audited evidence."})
    return rows


def _activity(epic: dict) -> list[dict]:
    rows = []
    for p in epic["pull_requests"]:
        date = p["merged_at"] or p["created_at"]
        rows.append({"date": date, "kind": f"pr_{p['state']}", "title": p["title"],
                     "number": p["number"], "url": p["url"]})
    for c in epic["children"]:
        if c["state"] == "closed" and c["closed_at"]:
            rows.append({"date": c["closed_at"], "kind": "issue_closed", "title": c["title"],
                         "number": c["number"], "url": c["url"]})
    for c in epic["commits"]:
        rows.append({"date": c["date"], "kind": "commit", "title": c["message"],
                     "number": None, "url": c["url"]})
    rows = [r for r in rows if r["date"]]
    rows.sort(key=lambda r: r["date"], reverse=True)
    return rows[:15]


def _next_actions(epic: dict) -> list[dict]:
    """Derived suggestions, labelled as such. They come from the data above, not from judgement."""
    actions = []
    for b in epic["blockers"]:
        actions.append({"text": f"Resolve blocker #{b['number']}: {b['title']}", "url": b["url"]})
    for p in epic["pull_requests"]:
        if p["state"] == "open":
            actions.append({"text": f"Review open PR #{p['number']}: {p['title']}", "url": p["url"]})
    for c in [c for c in epic["children"] if c["state"] == "open"][:5]:
        actions.append({"text": f"Open child #{c['number']}: {c['title']}", "url": c["url"]})
    for d in epic["dependencies"]:
        if d["state"] == "open" and not d["explicit_blocker"]:
            actions.append({"text": f"Open dependency #{d['number']}: {d['title']}", "url": d["url"]})
    if not epic["children"] and epic["state"] == "open":
        actions.append({"text": "Link scoped child issues to this epic so progress can be measured.",
                        "url": epic["url"]})
    return actions[:8]


# --------------------------------------------------------------------------------------------
# Cache and service
# --------------------------------------------------------------------------------------------

DOC_CATEGORIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Founding Charter and Constitution", ("charter", "constitution")),
    ("Architecture Decision Records", ("docs/adr/",)),
    ("LiteLLM Integration", ("litellm",)),
    ("Orchestration and Delegation", ("delegation", "orchestration", "identity-execution")),
    ("Agent Skills and Capabilities", ("skills", "capabilit", "pixel", "personality",
                                       "education-agents", "sage-")),
    ("Agent Architecture", ("agent-architecture", "agent-identity", "architecture.md",
                            "memory-architecture", "agent-communication")),
    ("Security and Governance", ("docs/governance/", "security", "threat-model", "contributing",
                                 "change-rationale")),
    ("Infrastructure and Deployment", ("deployment", "roadmap", "learning-roadmap")),
    ("Testing and Observability", ("observability", "continuous-integration", "audits/",
                                   "development-status", "sage-review-trial")),
)
DOC_CATEGORY_ORDER = tuple(name for name, _ in DOC_CATEGORIES) + ("Other documentation",)


def categorize_doc(path: str) -> str:
    lowered = path.lower()
    for name, needles in DOC_CATEGORIES:
        if any(needle in lowered for needle in needles):
            return name
    return "Other documentation"


class WikiService:
    """Holds the cached snapshot. Refreshes lazily; never serves nothing if it has served something."""

    def __init__(self, config: WikiConfig | None = None, transport: Transport | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.config = config or WikiConfig()
        self.transport = transport
        self.clock = clock
        self._lock = threading.Lock()
        self._snapshot: Snapshot | None = None
        self._fetched_at = 0.0
        self._error: str | None = None
        self._backoff_until = 0.0
        self._doc_cache: OrderedDict[str, dict] = OrderedDict()

    def _meta(self, stale: bool) -> dict[str, Any]:
        snap = self._snapshot
        return {
            "state": "stale" if stale else "ok", "stale": stale, "error": self._error if stale else None,
            "synced_at": snap.data["synced_at"] if snap else None,
            "cache_seconds": self.config.ttl, "repo": self.config.repo,
            "head_sha": snap.head_sha if snap else None,
            "rate_limit": snap.data["rate_limit"] if snap else None,
            "retry_after_seconds": max(0, int(self._backoff_until - self.clock()))
            if self._backoff_until > self.clock() else None,
        }

    def unavailable(self, reason: str) -> dict[str, Any]:
        return {"state": "unavailable", "stale": False, "error": reason, "synced_at": None,
                "cache_seconds": self.config.ttl, "repo": self.config.repo, "head_sha": None,
                "rate_limit": None, "retry_after_seconds": None}

    def snapshot(self) -> tuple[Snapshot | None, dict[str, Any]]:
        """The current snapshot (maybe stale) and its sync metadata. Never raises."""
        if not self.config.configured:
            return None, self.unavailable("not_configured")
        now = self.clock()
        fresh = self._snapshot is not None and now - self._fetched_at < self.config.ttl
        if fresh:
            return self._snapshot, self._meta(False)
        blocked = now < self._backoff_until
        if blocked or not self._lock.acquire(blocking=self._snapshot is None):
            return self._served_or_unavailable()
        try:
            started = time.perf_counter()
            reader = GitHubReader(self.config, self.transport)
            try:
                self._snapshot = build_snapshot(reader)
                self._fetched_at = self.clock()
                self._error = None
                self._backoff_until = 0.0
                logger.info("development wiki synced", extra={
                    "agent": "web", "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    "github_requests": reader.requests})
            except WikiError as exc:
                self._error = exc.code
                wait = exc.retry_after if exc.retry_after is not None else FAILURE_RETRY_SECONDS
                self._backoff_until = self.clock() + min(max(wait, 1), 3600)
                logger.warning("development wiki sync failed", extra={
                    "agent": "web", "error": exc.code,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2)})
            except Exception as exc:  # a parsing surprise must not take the page down
                self._error = "internal"
                self._backoff_until = self.clock() + FAILURE_RETRY_SECONDS
                logger.error("development wiki sync failed", extra={
                    "agent": "web", "error": type(exc).__name__})
            return self._served_or_unavailable()
        finally:
            self._lock.release()

    def _served_or_unavailable(self) -> tuple[Snapshot | None, dict[str, Any]]:
        if self._snapshot is None:
            return None, self.unavailable(self._error or "syncing")
        stale = self.clock() - self._fetched_at >= self.config.ttl
        return self._snapshot, self._meta(stale)

    # ---- views ----

    def epics(self) -> dict[str, Any]:
        snap, meta = self.snapshot()
        if snap is None:
            return {"sync": meta, "dashboard": None, "epics": []}
        d = snap.data
        summary = [{k: e[k] for k in ("number", "title", "url", "state", "status", "agent", "phase",
                                      "progress", "counts", "labels")} for e in d["epics"]]
        return {"sync": meta, "counts": d["counts"], "dashboard": d["dashboard"], "epics": summary}

    def epic(self, number: int) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        snap, meta = self.snapshot()
        if snap is None:
            return None, meta
        for epic in snap.data["epics"]:
            if epic["number"] == number:
                docs = []
                for path in epic["docs"]:
                    docs.append({"path": path, "title": doc_title(path),
                                 "category": categorize_doc(path)})
                return {"sync": meta, "epic": {**epic, "docs": docs}}, meta
        return None, meta

    def timeline(self, epic: int | None = None, agent: str | None = None,
                 phase: str | None = None, status: str | None = None) -> dict[str, Any]:
        snap, meta = self.snapshot()
        if snap is None:
            return {"sync": meta, "events": [], "filters": {}}
        events = snap.data["timeline"]
        if epic is not None:
            events = [e for e in events if epic in e["epics"]]
        if agent:
            events = [e for e in events if agent.lower() in (a.lower() for a in e["agents"])]
        if phase:
            events = [e for e in events if phase in e["phases"]]
        if status:
            events = [e for e in events if e["status"] == status]
        epics = snap.data["epics"]
        filters = {
            "epics": [{"number": e["number"], "title": e["title"]} for e in epics],
            "agents": sorted({e["agent"] for e in epics if e["agent"]}),
            "phases": sorted({e["phase"] for e in epics if e["phase"]}),
            "statuses": ["open", "closed", "merged", "published"],
        }
        return {"sync": meta, "events": events[:500], "filters": filters,
                "note": ("Events with no epic link appear only when no epic, agent or phase "
                         "filter is set.")}

    def docs(self) -> dict[str, Any]:
        snap, meta = self.snapshot()
        if snap is None:
            return {"sync": meta, "categories": []}
        groups: dict[str, list] = {name: [] for name in DOC_CATEGORY_ORDER}
        for item in snap.data["docs"]:
            groups[categorize_doc(item["path"])].append({**item, "title": doc_title(item["path"])})
        return {"sync": meta, "categories": [
            {"name": name, "documents": groups[name]} for name in DOC_CATEGORY_ORDER if groups[name]]}

    def document(self, path: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        snap, meta = self.snapshot()
        if snap is None:
            return None, meta
        entry = snap.tree.get(path)
        if entry is None:  # only paths GitHub's tree listed; nothing else is ever fetched
            return None, meta
        if entry["size"] > MAX_DOC_BYTES:
            return {"sync": meta, "path": path, "error": "too_large"}, meta
        sha = entry["sha"]
        cached = self._doc_cache.get(sha)
        if cached is None:
            reader = GitHubReader(self.config, self.transport)
            try:
                blob = reader.get_json(f"/repos/{self.config.repo}/git/blobs/{sha}")
                raw = base64.b64decode(blob.get("content", ""), validate=False).decode("utf-8")
            except WikiError as exc:
                return {"sync": {**meta, "state": "stale", "error": exc.code}, "path": path,
                        "error": exc.code}, meta
            except (ValueError, binascii.Error, AttributeError):
                return {"sync": meta, "path": path, "error": "bad_response"}, meta
            cached = {"blocks": render_markdown(raw, path, set(snap.tree), self.config.web_base,
                                                 snap.head_sha or self.config.branch)}
            self._doc_cache[sha] = cached
            while len(self._doc_cache) > 200:
                self._doc_cache.popitem(last=False)
        revision = {
            "blob_sha": sha, "commit_sha": snap.head_sha, "branch": self.config.branch,
            "source_url": f"{self.config.web_base}/blob/{snap.head_sha or self.config.branch}/{path}",
            "history_url": f"{self.config.web_base}/commits/{self.config.branch}/{path}",
        }
        return {"sync": meta, "path": path, "title": doc_title(path),
                "category": categorize_doc(path), "revision": revision,
                "blocks": cached["blocks"]}, meta


def doc_title(path: str) -> str:
    name = path.rsplit("/", 1)[-1][:-3]
    return name.replace("-", " ").replace("_", " ") if not name.isupper() else name


# --------------------------------------------------------------------------------------------
# Markdown to safe blocks (data, not HTML: the browser builds DOM text nodes from it)
# --------------------------------------------------------------------------------------------

INLINE_RE = re.compile(
    r"(?P<code>`+)(?P<codetext>.+?)(?P=code)"
    r"|!?\[(?P<label>[^\]]+)\]\((?P<href>[^)\s]+)(?:\s+\"[^\"]*\")?\)"
    r"|\*\*(?P<strong>.+?)\*\*|__(?P<strong2>.+?)__"
    r"|(?<![\w*])\*(?P<em>[^*\s][^*]*?)\*(?![\w*])|(?<![\w_])_(?P<em2>[^_\s][^_]*?)_(?![\w_])")


def _resolve_link(href: str, doc_path: str, tree: set[str], web: str, ref: str) -> dict | None:
    href = href.strip()
    parsed = urlparse(href)
    if parsed.scheme == "https" and parsed.netloc:
        return {"href": href}
    if parsed.scheme or href.startswith("//"):
        return None  # javascript:, data:, http:, mailto: and the rest are shown as plain text
    if href.startswith("#") or not href:
        return None
    base = doc_path.rsplit("/", 1)[0] if "/" in doc_path else ""
    parts = [] if href.startswith("/") else (base.split("/") if base else [])
    for piece in parsed.path.lstrip("/").split("/"):
        if piece == "..":
            if parts:
                parts.pop()
        elif piece and piece != ".":
            parts.append(piece)
    target = "/".join(parts)
    if target in tree:
        return {"wiki": target}
    return {"href": f"{web}/blob/{ref}/{target}"}


def parse_inline(text: str, doc_path: str, tree: set[str], web: str, ref: str,
                 depth: int = 0) -> list[dict]:
    out: list[dict] = []
    pos = 0
    for m in INLINE_RE.finditer(text):
        if m.start() > pos:
            out.append({"t": "text", "v": text[pos:m.start()]})
        pos = m.end()
        if m.group("code"):
            out.append({"t": "code", "v": m.group("codetext")})
        elif m.group("label") is not None:
            label = m.group("label")
            link = _resolve_link(m.group("href"), doc_path, tree, web, ref)
            if m.group(0).startswith("!") or link is None:
                out.append({"t": "text", "v": label if link is None else f"[{label}]"})
                if link is not None:
                    out.append({"t": "link", "v": "image source", **link})
            else:
                out.append({"t": "link", "v": label, **link})
        else:
            inner = m.group("strong") or m.group("strong2")
            kind = "strong" if inner else "em"
            inner = inner or m.group("em") or m.group("em2")
            out.append({"t": kind, "v": inner if depth > 2 else None,
                        "children": [] if depth > 2 else parse_inline(
                            inner, doc_path, tree, web, ref, depth + 1)})
    if pos < len(text):
        out.append({"t": "text", "v": text[pos:]})
    return out


def render_markdown(source: str, doc_path: str, tree: set[str], web: str, ref: str) -> list[dict]:
    def inl(text: str) -> list[dict]:
        return parse_inline(text, doc_path, tree, web, ref)

    blocks: list[dict] = []
    lines = source.replace("\r\n", "\n").split("\n")
    i = 0
    paragraph: list[str] = []

    def flush() -> None:
        if paragraph:
            blocks.append({"type": "paragraph", "inlines": inl(" ".join(s.strip() for s in paragraph))})
            paragraph.clear()

    # Front matter is metadata, not content.
    if lines and lines[0].strip() == "---":
        for j in range(1, min(len(lines), 60)):
            if lines[j].strip() == "---":
                i = j + 1
                break
    while i < len(lines):
        line = lines[i]
        fence = re.match(r"^\s*(```+|~~~+)\s*([\w+-]*)", line)
        if fence:
            flush()
            marker, lang = fence.group(1), fence.group(2)
            body: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith(marker[:3]):
                body.append(lines[i])
                i += 1
            blocks.append({"type": "code", "lang": lang, "text": "\n".join(body)})
            i += 1
            continue
        heading = re.match(r"^(#{1,6})\s+(.*?)\s*#*\s*$", line)
        if heading:
            flush()
            blocks.append({"type": "heading", "level": len(heading.group(1)),
                           "inlines": inl(heading.group(2))})
            i += 1
            continue
        if re.match(r"^\s*([-*_])(\s*\1){2,}\s*$", line):
            flush()
            blocks.append({"type": "rule"})
            i += 1
            continue
        if line.lstrip().startswith("|") and i + 1 < len(lines) and re.match(
            r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$", lines[i + 1]
        ):
            flush()

            def cells(row: str) -> list[str]:
                return [c.strip() for c in row.strip().strip("|").split("|")]
            head = cells(line)
            rows = []
            i += 2
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                rows.append(cells(lines[i]))
                i += 1
            blocks.append({"type": "table", "head": [inl(c) for c in head],
                           "rows": [[inl(c) for c in r] for r in rows[:300]]})
            continue
        if line.lstrip().startswith(">"):
            flush()
            quote = []
            while i < len(lines) and lines[i].lstrip().startswith(">"):
                quote.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            blocks.append({"type": "quote", "inlines": inl(" ".join(q.strip() for q in quote))})
            continue
        item = re.match(r"^(\s*)([-*+]|\d+[.)])\s+(?:\[([ xX])\]\s+)?(.*)", line)
        if item:
            flush()
            ordered = item.group(2)[0].isdigit()
            items = []
            while i < len(lines):
                m = re.match(r"^(\s*)([-*+]|\d+[.)])\s+(?:\[([ xX])\]\s+)?(.*)", lines[i])
                if m:
                    text = m.group(4)
                    i += 1
                    while i < len(lines) and lines[i].strip() and not re.match(
                        r"^\s*([-*+]|\d+[.)])\s+", lines[i]
                    ) and lines[i].startswith(" "):
                        text += " " + lines[i].strip()
                        i += 1
                    entry = {"depth": min(len(m.group(1)) // 2, 4), "inlines": inl(text)}
                    if m.group(3) is not None:
                        entry["checked"] = m.group(3).lower() == "x"
                    items.append(entry)
                elif not lines[i].strip() and i + 1 < len(lines) and re.match(
                    r"^\s*([-*+]|\d+[.)])\s+", lines[i + 1]
                ):
                    i += 1
                else:
                    break
            blocks.append({"type": "list", "ordered": ordered, "items": items})
            continue
        if not line.strip():
            flush()
        else:
            paragraph.append(line)
        i += 1
    flush()
    return blocks[:2000]


# --------------------------------------------------------------------------------------------
# Process-wide instance (replaceable in tests)
# --------------------------------------------------------------------------------------------

_service: WikiService | None = None
_service_lock = threading.Lock()


def get_service() -> WikiService:
    global _service
    with _service_lock:
        if _service is None:
            _service = WikiService()
        return _service


def set_service(service: WikiService | None) -> None:
    global _service
    with _service_lock:
        _service = service
