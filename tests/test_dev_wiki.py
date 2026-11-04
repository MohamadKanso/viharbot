"""Development Wiki: GitHub is faked at the transport, so nothing here touches the network."""

from __future__ import annotations

import base64
import json
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from viharbot import dev_wiki, web_app
from viharbot.dev_wiki import (
    GitHubReader, WikiConfig, WikiError, WikiService, categorize_doc, classify_references,
    is_epic, render_markdown,
)

REPO = "acme/widgets"
WEB = f"https://github.com/{REPO}"
TOKEN = "ghp_secret_token_value"


def issue(number, title, state="open", body="", labels=(), created="2026-10-01T00:00:00Z",
          closed=None, pr=False):
    row = {"number": number, "title": title, "state": state, "body": body,
           "labels": [{"name": n} for n in labels], "created_at": created, "closed_at": closed,
           "html_url": f"{WEB}/{'pull' if pr else 'issues'}/{number}", "assignees": []}
    if pr:
        row["pull_request"] = {}
    return row


def pull(number, title, merged=None, body="", state="closed"):
    return {"number": number, "title": title, "body": body, "state": state, "merged_at": merged,
            "created_at": "2026-10-02T00:00:00Z", "closed_at": merged, "draft": False,
            "html_url": f"{WEB}/pull/{number}", "head": {"ref": "feature/x"}, "user": {"login": "a"},
            "merge_commit_sha": "abc"}


EPIC_BODY = """## Objective
Build the thing.

## Scope
- [ ] #11 first child
- [x] #12 second child
- [ ] #404 does not exist

**Depends on:** #20
Blocked by #21. Related: #30. See #31 for context. `#99 in code` is ignored.

## Acceptance criteria
- [x] Works
- [ ] Documented in docs/guide.md and ADR-001
"""


def world(**over):
    issues = [
        issue(10, "[Phase 7][Epic] Platform", body=EPIC_BODY, labels=["epic"], created="2026-09-01T00:00:00Z"),
        issue(11, "child one"),
        issue(12, "child two", state="closed", closed="2026-10-05T00:00:00Z"),
        issue(20, "dependency", state="closed", closed="2026-10-04T00:00:00Z"),
        issue(21, "the blocker"),
        issue(30, "related thing"),
        issue(40, "Epic: Sage review", body="No links here."),
        issue(50, "pull masquerading", pr=True),
        issue(60, "Not an epic, but it mentions epic in prose"),
    ]
    pulls = [pull(7, "Do child", merged="2026-10-06T00:00:00Z", body="Closes #11"),
             pull(8, "Open work", state="open", body="Refs #10")]
    commits = [{"sha": "f" * 40, "commit": {"message": "Merge pull request #7 from x/y",
                                           "author": {"date": "2026-10-06T01:00:00Z"}}}]
    tree = {"tree": [
        {"path": "docs/guide.md", "type": "blob", "sha": "s1", "size": 50},
        {"path": "docs/adr/ADR-001-thing.md", "type": "blob", "sha": "s2", "size": 50},
        {"path": "docs/litellm-integration.md", "type": "blob", "sha": "s3", "size": 50},
        {"path": "README.md", "type": "blob", "sha": "s4", "size": 50},
        {"path": "src/x.md", "type": "blob", "sha": "s5", "size": 5},
    ]}
    milestones = [{"title": "M1", "state": "open", "due_on": "2026-12-01T00:00:00Z",
                   "open_issues": 2, "closed_issues": 1, "html_url": f"{WEB}/milestone/1"}]
    data = {"issues": issues, "pulls": pulls, "commits": commits, "tree": tree,
            "milestones": milestones, "releases": [], "blobs": {}}
    data.update(over)
    return data


class FakeGitHub:
    """Serves the world over the transport interface; counts calls; pages at ``page_size``."""

    def __init__(self, data=None, page_size=100, fail=None):
        self.data = data or world()
        self.page_size = page_size
        self.fail = fail  # (status, headers) to return for every call
        self.calls: list[str] = []
        self.headers_seen: list[dict] = []

    def __call__(self, url, headers):
        self.calls.append(url)
        self.headers_seen.append(headers)
        if self.fail:
            status, extra = self.fail
            return status, {k.lower(): v for k, v in extra.items()}, b"{}"
        parsed = urlparse(url)
        path, query = parsed.path.removeprefix(f"/repos/{REPO}"), parse_qs(parsed.query)
        lists = {"/issues": "issues", "/pulls": "pulls", "/milestones": "milestones",
                 "/releases": "releases"}
        base = {"x-ratelimit-remaining": "4990", "x-ratelimit-reset": "1999999999"}
        if path in lists:
            rows = self.data[lists[path]]
            page = int(query.get("page", ["1"])[0])
            chunk = rows[(page - 1) * self.page_size: page * self.page_size]
            headers = dict(base)
            if page * self.page_size < len(rows):
                headers["link"] = (f'<https://api.github.com/repos/{REPO}{path}?page={page + 1}>; '
                                   'rel="next"')
            return 200, headers, json.dumps(chunk).encode()
        if path == "/commits":
            return 200, base, json.dumps(self.data["commits"]).encode()
        if path.startswith("/git/trees/"):
            return 200, base, json.dumps(self.data["tree"]).encode()
        if path.startswith("/git/blobs/"):
            text = self.data["blobs"].get(path.rsplit("/", 1)[-1], "# Hello\n")
            blob = {"content": base64.b64encode(text.encode()).decode(), "encoding": "base64"}
            return 200, base, json.dumps(blob).encode()
        if path.endswith("/sub_issues"):
            return 404, base, b"{}"
        return 404, base, b"{}"


def make_service(fake=None, clock=None, **env):
    config = WikiConfig({"DEV_WIKI_GITHUB_TOKEN": TOKEN, "DEV_WIKI_REPO": REPO,
                         "DEV_WIKI_CACHE_SECONDS": "60", **env})
    kwargs = {"clock": clock} if clock else {}
    return WikiService(config, fake or FakeGitHub(), **kwargs)


@pytest.fixture
def client():
    web_app.app.dependency_overrides = {}
    return TestClient(web_app.app)


@pytest.fixture(autouse=True)
def reset_service():
    yield
    dev_wiki.set_service(None)


# ---- discovery and counts ----

def test_epics_are_discovered_by_label_or_title_not_listed():
    service = make_service()
    body = service.epics()
    numbers = [e["number"] for e in body["epics"]]
    assert numbers == [10, 40]  # label, and "Epic:" title; prose mention and PRs are not epics
    assert body["counts"]["epics"] == 2
    assert body["counts"]["issues_fetched"] == 8  # the pull request is not an issue
    assert body["counts"]["pull_requests_fetched"] == 2
    assert is_epic({"title": "x", "labels": [{"name": "Epic"}]})
    assert not is_epic({"title": "Not an epic, mentions epic", "labels": []})


def test_children_are_scoped_and_progress_is_derived_with_its_method():
    epic = make_service().epic(10)[0]["epic"]
    assert [c["number"] for c in epic["children"]] == [11, 12]
    assert epic["progress"]["done"] == 1 and epic["progress"]["total"] == 2
    assert epic["progress"]["percent"] == 50 and "closed" in epic["progress"]["method"].lower()
    assert epic["unresolved_references"] == [404]  # malformed/unknown reference: excluded, shown
    assert [d["number"] for d in epic["dependencies"]] == [20, 21]
    assert [b["number"] for b in epic["blockers"]] == [21]
    assert [r["number"] for r in epic["related"]] == [30]  # #31 does not exist; #30 is only related
    assert all(c["number"] != 99 for c in epic["children"])  # code spans are ignored
    assert epic["status"] == "blocked"
    assert any("#21" in e["claim"] and e["url"].startswith(WEB) for e in epic["status_evidence"])


def test_no_children_means_unknown_not_zero():
    epic = make_service().epic(40)[0]["epic"]
    assert epic["progress"]["percent"] is None and epic["progress"]["state"] == "unknown"
    assert epic["status"] == "planned"
    assert epic["next_actions"][0]["text"].startswith("Link scoped child issues")


def test_pull_requests_and_commits_attach_by_explicit_reference():
    epic = make_service().epic(10)[0]["epic"]
    assert sorted(p["number"] for p in epic["pull_requests"]) == [7, 8]
    assert [c["short"] for c in epic["commits"]] == ["fffffff"]  # via "Merge pull request #7"
    maturity = {m["level"]: m["state"] for m in epic["maturity"]}
    assert maturity["proposed"] == "evidenced" and maturity["implemented"] == "evidenced"
    for level in ("tested", "integrated", "deployed", "operationally validated"):
        assert maturity[level] == "not_evidenced"  # a merged PR never implies these


def test_closed_epic_is_completed_with_evidence_link():
    data = world()
    data["issues"][0] = issue(10, "[Epic] Done", state="closed", closed="2026-10-09T00:00:00Z",
                              labels=["epic"], body="- [x] #12")
    epic = make_service(FakeGitHub(data)).epic(10)[0]["epic"]
    assert epic["status"] == "completed" and epic["status_evidence"][0]["url"] == f"{WEB}/issues/10"


def test_dashboard_shows_blockers_prs_milestones_and_a_method():
    dash = make_service().epics()["dashboard"]
    assert dash["epic_total"] == 2 and dash["by_status"]["blocked"] == 1
    assert dash["overall_progress"]["total"] == 2 and "method" in dash["overall_progress"]
    assert dash["epics_with_unknown_progress"] == [40]
    assert [p["number"] for p in dash["recent_merged_prs"]] == [7]
    assert dash["open_blockers"][0]["number"] == 21
    assert dash["upcoming_milestones"][0]["title"] == "M1"


def test_empty_repository_is_an_empty_but_valid_wiki():
    empty = world(issues=[], pulls=[], commits=[], milestones=[], tree={"tree": []})
    body = make_service(FakeGitHub(empty)).epics()
    assert body["sync"]["state"] == "ok" and body["epics"] == []
    assert body["dashboard"]["overall_progress"]["percent"] is None


# ---- pagination, rate limits, stale cache, failures ----

def test_pagination_follows_next_links_and_counts_every_item():
    data = world()
    data["issues"] += [issue(100 + i, f"filler {i}") for i in range(250)]
    fake = FakeGitHub(data, page_size=100)
    body = make_service(fake).epics()
    assert body["counts"]["issues_fetched"] == 8 + 250
    assert body["counts"]["truncated"] is False
    assert sum("/issues?" in c for c in fake.calls) == 3


def test_pagination_never_follows_a_link_to_another_host():
    reader = GitHubReader(WikiConfig({"DEV_WIKI_GITHUB_TOKEN": TOKEN}), None)
    assert dev_wiki._next_link('<https://evil.example/x?page=2>; rel="next"', reader.config.api_url) is None


def test_no_token_is_unavailable_and_makes_no_github_call():
    fake = FakeGitHub()
    service = WikiService(WikiConfig({}), fake)
    body = service.epics()
    assert body["sync"]["state"] == "unavailable" and body["sync"]["error"] == "not_configured"
    assert body["epics"] == [] and fake.calls == []


@pytest.mark.parametrize("status,headers,code", [
    (404, {}, "not_found"), (401, {}, "auth_failed"),
    (403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1"}, "rate_limited"),
    (500, {}, "github_error"),
])
def test_github_failures_are_safe_codes_and_never_leak_the_token(status, headers, code):
    fake = FakeGitHub(fail=(status, headers))
    body = make_service(fake).epics()
    assert body["sync"]["state"] == "unavailable" and body["sync"]["error"] == code
    assert TOKEN not in json.dumps(body)  # private repo without access: 404 -> not_found, no data


def test_cache_serves_without_refetching_then_goes_stale_on_failure():
    now = [1000.0]
    fake = FakeGitHub()
    service = make_service(fake, clock=lambda: now[0])
    first = service.epics()
    calls = len(fake.calls)
    service.epics()
    assert len(fake.calls) == calls  # within the cache window: no GitHub traffic
    now[0] += 61
    fake.fail = (500, {})
    stale = service.epics()
    assert stale["sync"]["stale"] is True and stale["sync"]["error"] == "github_error"
    assert stale["sync"]["synced_at"] == first["sync"]["synced_at"]  # old timestamp is kept honest
    assert [e["number"] for e in stale["epics"]] == [10, 40]  # data still shown
    before = len(fake.calls)
    service.epics()
    assert len(fake.calls) == before  # backing off: not hammering a failing GitHub
    now[0] += 31
    fake.fail = None
    assert service.epics()["sync"]["stale"] is False  # recovers


def test_rate_limit_backoff_honours_retry_after():
    now = [1000.0]
    fake = FakeGitHub()
    service = make_service(fake, clock=lambda: now[0])
    service.epics()
    now[0] += 61
    fake.fail = (429, {"retry-after": "120"})
    assert service.epics()["sync"]["retry_after_seconds"] == 120
    before = len(fake.calls)
    now[0] += 60
    service.epics()
    assert len(fake.calls) == before


def test_requests_are_get_only_with_a_bearer_token_in_the_header_not_the_url():
    fake = FakeGitHub()
    make_service(fake).epics()
    assert all(TOKEN not in url for url in fake.calls)
    assert all(h["Authorization"] == f"Bearer {TOKEN}" for h in fake.headers_seen)


def test_token_is_refused_over_plain_http_to_a_public_host():
    assert not WikiConfig({"DEV_WIKI_GITHUB_TOKEN": "t", "DEV_WIKI_API_URL": "http://example.com"}).configured
    assert WikiConfig({"DEV_WIKI_GITHUB_TOKEN": "t", "DEV_WIKI_API_URL": "http://127.0.0.1:9"}).configured
    assert WikiConfig({"DEV_WIKI_GITHUB_TOKEN": "t"}).configured


# ---- timeline ----

def test_timeline_events_and_filters():
    service = make_service()
    events = service.timeline()["events"]
    kinds = {e["kind"] for e in events}
    assert {"epic_created", "issue_opened", "issue_closed", "pr_merged"} <= kinds
    assert [e["date"] for e in events] == sorted((e["date"] for e in events), reverse=True)
    only_epic = service.timeline(epic=10)["events"]
    assert only_epic and all(10 in e["epics"] for e in only_epic)
    assert all(e["status"] == "merged" for e in service.timeline(status="merged")["events"])
    assert service.timeline(agent="Sage")["events"] == [] or all(
        "Sage" in e["agents"] for e in service.timeline(agent="Sage")["events"])
    assert service.timeline(phase="7")["filters"]["phases"] == ["7"]


# ---- documentation ----

def test_docs_are_grouped_into_the_required_categories():
    service = make_service()
    cats = {c["name"]: [d["path"] for d in c["documents"]] for c in service.docs()["categories"]}
    assert cats["Architecture Decision Records"] == ["docs/adr/ADR-001-thing.md"]
    assert cats["LiteLLM Integration"] == ["docs/litellm-integration.md"]
    assert "src/x.md" not in sum(cats.values(), [])  # only docs/ and root files
    assert categorize_doc("docs/governance/founding-charter-v2.md") == "Founding Charter and Constitution"
    assert categorize_doc("docs/observability.md") == "Testing and Observability"
    assert categorize_doc("docs/deployment-strategy.md") == "Infrastructure and Deployment"
    assert categorize_doc("docs/skills-registry.md") == "Agent Skills and Capabilities"
    assert categorize_doc("docs/controlled-delegation.md") == "Orchestration and Delegation"


def test_document_has_revision_info_and_epic_links_to_docs():
    service = make_service()
    doc = service.document("docs/guide.md")[0]
    assert doc["revision"]["blob_sha"] == "s1" and doc["revision"]["commit_sha"] == "f" * 40
    assert doc["revision"]["source_url"] == f"{WEB}/blob/{'f' * 40}/docs/guide.md"
    assert [d["path"] for d in service.epic(10)[0]["epic"]["docs"]] == [
        "docs/adr/ADR-001-thing.md", "docs/guide.md"]


def test_only_listed_documents_are_ever_fetched():
    fake = FakeGitHub()
    service = make_service(fake)
    for path in ("../../etc/passwd.md", "docs/missing.md", "src/x.md"):
        assert service.document(path)[0] is None
    assert not any("/git/blobs/" in c for c in fake.calls)


def test_markdown_is_data_not_html_and_unsafe_links_are_dropped():
    tree = {"docs/a.md", "docs/b.md"}
    source = ("# T <script>alert(1)</script>\n\n<img src=x onerror=alert(1)>\n\n"
              "[bad](javascript:alert(1)) [data](data:text/html,x) [http](http://x.example) "
              "[ok](https://example.com/p) [doc](b.md) [up](../README.md) `code` **b** *i*\n\n"
              "![alt](https://example.com/i.png)\n\n```\n<b>raw</b>\n```\n\n"
              "- [x] done\n- item\n\n| a | b |\n|---|---|\n| 1 | 2 |\n")
    blocks = render_markdown(source, "docs/a.md", tree, WEB, "main")
    flat = json.dumps(blocks)
    links = [n for n in _walk(blocks) if n.get("t") == "link"]
    hrefs = [n.get("href") for n in links]
    assert "javascript:alert(1)" not in flat and "data:text/html" not in flat and "http://x.example" not in flat
    assert "https://example.com/p" in hrefs and f"{WEB}/blob/main/README.md" in hrefs
    assert any(n.get("wiki") == "docs/b.md" for n in links)
    assert any(b["type"] == "heading" and "<script>" in b["inlines"][0]["v"] for b in blocks)  # literal text
    assert any(b["type"] == "code" and b["text"] == "<b>raw</b>" for b in blocks)
    assert {b["type"] for b in blocks} >= {"heading", "paragraph", "code", "list", "table"}


def _walk(blocks):
    for b in blocks:
        for key in ("inlines",):
            for n in b.get(key, []):
                yield n
        for item in b.get("items", []):
            yield from item["inlines"]
        for row in [b.get("head", [])] + b.get("rows", []):
            for cell in row:
                yield from cell


def test_hostile_issue_text_is_returned_as_plain_json_strings():
    data = world()
    data["issues"][1] = issue(11, '<img src=x onerror=alert(1)>', body="x")
    data["issues"][0]["body"] = EPIC_BODY + "\n[x](javascript:alert(1))"
    epic = make_service(FakeGitHub(data)).epic(10)[0]["epic"]
    assert epic["children"][0]["title"] == '<img src=x onerror=alert(1)>'  # data; the page uses textContent
    assert all(v.startswith(WEB) for v in (c["url"] for c in epic["children"]))


def test_agent_is_the_first_one_the_title_names():
    data = world()
    data["issues"].append(issue(70, "Epic: Kepler — coding agent with Sage review", labels=["epic"]))
    epics = {e["number"]: e for e in make_service(FakeGitHub(data)).epics()["epics"]}
    assert epics[70]["agent"] == "Kepler"


def test_classifier_is_conservative():
    refs = classify_references("See #5 and #6.\n- [ ] #7\nChild issues: #8\nBlocked by #9\n", 1)
    assert refs["related"] == [5, 6] and refs["child"] == [7, 8] and refs["blocker"] == [9]


# ---- HTTP surface ----

def test_routes_require_the_existing_web_authentication(client, monkeypatch):
    monkeypatch.setenv("WEB_AUTH_ENABLED", "true")
    monkeypatch.setenv("WEB_USERNAME", "u")
    monkeypatch.setenv("WEB_PASSWORD", "p")
    dev_wiki.set_service(make_service())
    for path in ("/api/development/epics", "/api/development/epics/10",
                 "/api/development/timeline", "/api/development/docs",
                 "/api/development/docs/docs/guide.md"):
        assert client.get(path).status_code == 401
        assert client.get(path, auth=("u", "p")).status_code == 200


def test_routes_are_read_only_and_never_return_the_token(client):
    dev_wiki.set_service(make_service())
    for path in ("/api/development/epics", "/api/development/epics/10", "/api/development/timeline",
                 "/api/development/docs", "/api/development/docs/docs/guide.md"):
        response = client.get(path)
        assert response.status_code == 200 and TOKEN not in response.text
        assert response.headers["cache-control"] == "no-store"
        assert client.post(path).status_code == 405 and client.delete(path).status_code == 405


def test_routes_without_credentials_are_a_graceful_unavailable_state(client):
    dev_wiki.set_service(WikiService(WikiConfig({}), FakeGitHub()))
    body = client.get("/api/development/epics").json()
    assert body["sync"]["state"] == "unavailable" and body["epics"] == []
    assert client.get("/api/development/epics/10").status_code == 503
    assert client.get("/api/development/timeline").json()["events"] == []


def test_unknown_epic_and_document_are_404(client):
    dev_wiki.set_service(make_service())
    assert client.get("/api/development/epics/9999").status_code == 404
    assert client.get("/api/development/docs/docs/nope.md").status_code == 404
    assert client.get("/api/development/epics/not-a-number").status_code == 422


def test_page_links_the_wiki_and_chat_routes_are_untouched(client):
    page = client.get("/").text
    assert 'id="wiki-link"' in page and 'href="#wiki"' in page and 'id="wiki-page"' in page
    assert client.get("/health").json() == {"status": "ok", "agent": "web"}


def test_the_wiki_makes_no_model_calls():
    import inspect
    source = inspect.getsource(dev_wiki)
    for banned in ("import litellm", "litellm_client", "import openai", "strands", "completion("):
        assert banned not in source


# ---- frontend (Node fake DOM; innerHTML throws there, so any HTML-string rendering fails) ----

import shutil
import subprocess
from pathlib import Path

NODE = shutil.which("node")
ROOT = Path(__file__).resolve().parent.parent


def run_ui(hashes, api):
    done = subprocess.run(
        [NODE, str(ROOT / "tests" / "js" / "run_wiki.mjs"),
         str(ROOT / "src" / "viharbot" / "static" / "index.html")],
        input=json.dumps({"api": api, "hashes": hashes}), capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def api_fixture(service):
    return {"/api/development/epics": service.epics(), "/api/development/epics/10": service.epic(10)[0],
            "/api/development/timeline": service.timeline(), "/api/development/docs": service.docs(),
            "/api/development/docs/docs/guide.md": service.document("docs/guide.md")[0]}


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_ui_navigates_and_renders_every_view():
    api = api_fixture(make_service())
    result = run_ui(["#wiki", "#wiki/epic/10", "#wiki/docs", "#wiki/docs/docs/guide.md",
                     "#wiki/timeline?epic=10", "#home"], api)["out"]
    dash, epic, docs, doc, timeline, home = result
    assert not dash["wikiHidden"] and dash["chatHidden"] and dash["current"] == "page"
    assert "Last synchronized with GitHub" in dash["sync"] and "2 of 2 epics shown" in dash["body"]
    assert "details" in dash["tags"] and "progressbar" not in dash["body"]
    assert "Blocked by open #21" in epic["body"] and "Unknown / unverified" in epic["body"]
    assert "Architecture Decision Records" in docs["body"] and "revision fffffff" in doc["body"]
    assert "pr merged" in timeline["body"]
    assert home["wikiHidden"] and not home["chatHidden"] and home["current"] is None


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_ui_shows_unavailable_without_inventing_data():
    unavailable = WikiService(WikiConfig({}), FakeGitHub())
    api = {"/api/development/epics": unavailable.epics()}
    page = run_ui(["#wiki"], api)["out"]
    assert "Unavailable" in page[0]["sync"] and "DEV_WIKI_GITHUB_TOKEN" in page[0]["sync"]
    assert "details" not in page[0]["tags"]


def test_activity_endpoint_is_the_timeline_and_epics_response_is_typed(client):
    dev_wiki.set_service(make_service())
    activity = client.get("/api/development/activity?epic=10").json()
    assert activity["events"] and all(10 in e["epics"] for e in activity["events"])
    assert client.get("/api/development/epics").json()["sync"]["state"] == "ok"


# ---- redirects must never carry the token to another host ----

def test_default_transport_does_not_follow_redirects_or_forward_the_token():
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    seen = []

    class Target(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.headers.get("Authorization"))
            self.send_response(200); self.end_headers(); self.wfile.write(b"[]")

        def log_message(self, *a):
            pass

    target = HTTPServer(("127.0.0.1", 0), Target)

    class Redirector(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{target.server_port}/stolen")
            self.end_headers()

        def log_message(self, *a):
            pass

    front = HTTPServer(("127.0.0.1", 0), Redirector)
    for server in (target, front):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        config = WikiConfig({"DEV_WIKI_GITHUB_TOKEN": "synthetic-token", "DEV_WIKI_REPO": REPO,
                             "DEV_WIKI_API_URL": f"http://127.0.0.1:{front.server_port}"})
        with pytest.raises(WikiError) as caught:
            GitHubReader(config).get_json("/repos/x/y")
        assert caught.value.code == "redirect_refused"
        assert seen == []  # the second server was never contacted, so it never saw a credential
    finally:
        for server in (target, front):
            server.shutdown()
