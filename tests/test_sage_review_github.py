"""The GitHub adapter and comment formatter, against a fake GitHub. No network, no model."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.sage_review_helpers import BASE, HEAD, PATCH, finding, make_request, make_review
from viharbot import sage_review_github as gh
from viharbot.sage_code_review import SageReviewConfig, failed_review, finalize_review
from viharbot.sage_review_format import MARKER, format_comment, format_skipped, mark_stale, neutralize

REPO = "vchokshi/viharbot"
NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
TOKEN = "ghs_SECRETTOKEN"
HEAD2 = "c" * 40


class FakeGitHub:
    """A tiny GitHub: one PR, its files, check runs and issue comments. Records every call."""

    def __init__(self, head=HEAD, files=None, comments=None, head_repo=REPO, state="open", fail=None):
        self.head, self.head_repo, self.state, self.fail = head, head_repo, state, fail
        self.files = files if files is not None else [
            {"filename": "src/viharbot/example.py", "status": "modified", "additions": 3,
             "deletions": 1, "patch": PATCH}]
        self.comments = comments if comments is not None else []
        self.calls: list[tuple[str, str, dict]] = []

    def __call__(self, method, url, headers, body):
        path = url.removeprefix("https://api.github.com")
        payload = json.loads(body) if body else None
        self.calls.append((method, path, headers))
        if self.fail and self.fail(method, path):
            return 500, b"{}"
        base = path.split("?")[0]
        if base == f"/repos/{REPO}/pulls/7":
            return 200, json.dumps({
                "title": "T", "body": "B", "state": self.state, "changed_files": len(self.files),
                "head": {"sha": self.head, "repo": {"full_name": self.head_repo}},
                "base": {"sha": BASE}}).encode()
        if base.endswith("/pulls/7/files"):
            return 200, json.dumps(self.files if "page=1" in path else []).encode()
        if "/check-runs" in base:
            runs = [{"name": "Python tests", "status": "completed", "conclusion": "success"},
                    {"name": "Sage review", "status": "in_progress", "conclusion": None},
                    {"name": "odd", "status": "completed", "conclusion": "brand_new"}]
            return 200, json.dumps({"check_runs": runs if "page=1" in path else []}).encode()
        if base.endswith("/issues/7/comments") and method == "GET":
            return 200, json.dumps(self.comments if "page=1" in path else []).encode()
        if base.endswith("/issues/7/comments") and method == "POST":
            comment = {"id": 100 + len(self.comments), "body": payload["body"],
                       "user": {"login": "github-actions[bot]"}}
            self.comments.append(comment)
            return 201, json.dumps(comment).encode()
        match = re.fullmatch(r".*/issues/comments/(\d+)", base)
        if match and method == "PATCH":
            for c in self.comments:
                if c["id"] == int(match.group(1)):
                    c["body"] = payload["body"]
                    return 200, json.dumps(c).encode()
        return 404, b"{}"

    def writes(self):
        return [(m, p) for m, p, _ in self.calls if m != "GET"]


def run(fake, runner=None, tmp_path=None, **kw):
    client = gh.GitHubClient(TOKEN, transport=fake, base="https://api.github.com")
    runner = runner or (lambda req: finalize_review(make_review(), req, request_id="r", model_alias="m", duration_ms=5))
    return gh.review_pull_request(client, REPO, 7, policy_root=tmp_path or Path("."), runner=runner,
                                  now=lambda: NOW, **kw)


# Scenario 1 / publication ---------------------------------------------------------------------
def test_clean_pr_gets_one_advisory_comment_with_every_section(tmp_path):
    fake = FakeGitHub()
    assert run(fake, tmp_path=tmp_path) == "complete"
    assert [w[0] for w in fake.writes()] == ["POST"]
    body = fake.comments[0]["body"]
    assert body.startswith(MARKER) and f"reviewed-sha:{HEAD}" in body
    for part in ("Sage AI Code Review — Advisory Recommendation", "Recommendation: APPROVE", "advisory",
                 "Executive summary", "Recommended changes", "Nonblocking observations",
                 "Architectural implications", "Security considerations", "Testing observations",
                 "Governance considerations", "Evidence references", "Uncertainties",
                 "Suggested next steps", HEAD[:12], "2026-11-02 12:00 UTC"):
        assert part in body
    assert "Python tests: success" in body and "odd" in body and "Sage review" not in body.split("Evidence")[1]


def test_defective_pr_reports_request_changes_with_evidence_as_advice_only():
    review = make_review(recommendation="REQUEST_CHANGES", findings=[finding(
        severity="critical", category="security", description="Authorization check removed.",
        evidence="Line 2 deletes the check.", remediation="Restore it.")])
    result = finalize_review(review, make_request(), request_id="r", model_alias="m", duration_ms=1)
    body = format_comment(result, timestamp=NOW)
    assert "Recommendation: REQUEST_CHANGES" in body and "CRITICAL" in body
    assert "`src/viharbot/example.py:2-3`" in body and "Restore it." in body
    assert "Advice only; nothing here is enforced." in body and "cannot merge, block or approve" in body


# Scenario 3 / 4 --------------------------------------------------------------------------------
def test_oversized_pr_is_reviewed_in_part_and_reported_incomplete(tmp_path):
    files = [{"filename": f"f{i}.py", "status": "added", "additions": 1, "deletions": 0, "patch": "@@ -0,0 +1 @@\n+x"}
             for i in range(5)]
    fake = FakeGitHub(files=files)
    seen = []
    runner = lambda req: (seen.append(req), finalize_review(make_review(), req, request_id="r", model_alias="m", duration_ms=1))[1]
    assert run(fake, runner=runner, tmp_path=tmp_path, config=SageReviewConfig(max_files=2)) == "incomplete"
    assert len(seen[0].files) == 2 and seen[0].files_omitted == 3
    body = fake.comments[0]["body"]
    assert "Incomplete review" in body and "Recommendation: APPROVE" not in body and "3 changed file" in body


def test_diff_size_limit_truncates_and_marks_incomplete(tmp_path):
    big = {"filename": "big.py", "status": "added", "additions": 9, "deletions": 0,
           "patch": "@@ -0,0 +1 @@\n+" + "x" * 5000}
    seen = []
    runner = lambda req: (seen.append(req), finalize_review(make_review(), req, request_id="r", model_alias="m", duration_ms=1))[1]
    assert run(FakeGitHub(files=[big]), runner=runner, tmp_path=tmp_path,
               config=SageReviewConfig(max_diff_chars=1000)) == "incomplete"
    assert seen[0].files[0].patch_truncated and len(seen[0].files[0].patch) == 1000


def test_sage_implementation_change_requires_human_assessment(tmp_path):
    files = [{"filename": ".github/workflows/sage-review.yml", "status": "modified", "additions": 1,
              "deletions": 0, "patch": "@@ -1 +1,2 @@\n a\n+b"}]
    fake = FakeGitHub(files=files)
    run(fake, tmp_path=tmp_path)
    body = fake.comments[0]["body"]
    assert "Independent human assessment required" in body and "Recommendation: APPROVE" not in body


# 10. duplicate runs / 11. updated commits -------------------------------------------------------
def test_second_run_on_the_same_commit_makes_no_call_and_no_write(tmp_path):
    fake = FakeGitHub()
    run(fake, tmp_path=tmp_path)
    fake.calls.clear()
    called = []
    assert run(fake, runner=lambda r: called.append(r), tmp_path=tmp_path) == "unchanged"
    assert called == [] and fake.writes() == []
    assert len(fake.comments) == 1


def test_force_reviews_the_same_commit_again_by_updating_in_place(tmp_path):
    fake = FakeGitHub()
    run(fake, tmp_path=tmp_path)
    run(fake, tmp_path=tmp_path, force=True)
    assert len(fake.comments) == 1 and [w[0] for w in fake.writes()] == ["POST", "PATCH"]


def test_new_commit_marks_the_old_review_stale_then_replaces_it(tmp_path):
    fake = FakeGitHub()
    run(fake, tmp_path=tmp_path)
    old_body = fake.comments[0]["body"]
    fake.head = HEAD2
    bodies = []

    def runner(req):
        bodies.append(fake.comments[0]["body"])  # what readers see while the new review runs
        return finalize_review(make_review(), req, request_id="r", model_alias="m", duration_ms=1)

    run(fake, runner=runner, tmp_path=tmp_path)
    assert "This recommendation is stale" in bodies[0] and HEAD[:12] in bodies[0] and HEAD2[:12] in bodies[0]
    assert old_body != bodies[0]
    final = fake.comments[0]["body"]
    assert len(fake.comments) == 1 and f"reviewed-sha:{HEAD2}" in final and "stale" not in final.split("##")[1]


def test_mark_stale_is_idempotent_and_failed_reviews_are_retried(tmp_path):
    once = mark_stale(format_comment(finalize_review(make_review(), make_request(), request_id="r",
                                                     model_alias="m", duration_ms=1)), HEAD2)
    assert mark_stale(once, HEAD2) == once
    fake = FakeGitHub()
    fail = lambda req: failed_review(req, "model failed", request_id="r")
    assert run(fake, runner=fail, tmp_path=tmp_path) == "failed"
    assert "Recommendation: APPROVE" not in fake.comments[0]["body"]
    assert run(fake, tmp_path=tmp_path) == "complete"  # same commit, but the earlier run had failed
    assert len(fake.comments) == 1


# 13. publication failures -----------------------------------------------------------------------
def test_publication_failure_raises_a_token_free_error(tmp_path):
    fake = FakeGitHub(fail=lambda m, p: m == "POST")
    with pytest.raises(gh.GitHubError) as err:
        run(fake, tmp_path=tmp_path)
    assert TOKEN not in str(err.value) and "500" in str(err.value)


def test_main_exits_nonzero_on_publication_failure_and_zero_otherwise(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    monkeypatch.setattr(gh, "run_code_review", lambda req: finalize_review(
        make_review(), req, request_id="r", model_alias="m", duration_ms=1))
    real = gh.GitHubClient

    def wire(fake):
        monkeypatch.setattr(gh, "GitHubClient", lambda token: real(token, transport=fake, base="https://api.github.com"))

    wire(FakeGitHub(fail=lambda m, p: m == "POST"))
    assert gh.main(["--pr", "7", "--policy-root", str(tmp_path)]) == 1
    assert TOKEN not in capsys.readouterr().err
    wire(FakeGitHub())
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert gh.main(["--pr", "7", "--policy-root", str(tmp_path)]) == 0
    text = summary.read_text()
    assert "APPROVE (advisory)" in text and "completeness complete" in text


# 14. credential isolation + 15. permission boundaries ---------------------------------------------
def test_fork_prs_are_skipped_without_a_model_call_unless_manually_allowed(tmp_path):
    fake, called = FakeGitHub(head_repo="stranger/viharbot"), []
    assert run(fake, runner=lambda r: called.append(r), tmp_path=tmp_path) == "skipped-fork"
    assert called == [] and "fork" in fake.comments[0]["body"] and "Recommendation: none" in fake.comments[0]["body"]
    assert run(FakeGitHub(head_repo="stranger/viharbot"), tmp_path=tmp_path, allow_fork=True) == "complete"


def test_closed_and_empty_prs_are_skipped(tmp_path):
    assert run(FakeGitHub(state="closed"), tmp_path=tmp_path) == "skipped-closed"
    fake = FakeGitHub(files=[])
    assert run(fake, tmp_path=tmp_path) == "skipped-empty" and "no changed files" in fake.comments[0]["body"]


def test_invalid_pr_data_is_skipped_with_a_reason_not_a_crash(tmp_path):
    bad = [{"filename": "../x", "status": "added", "additions": 1, "deletions": 0, "patch": "+x"}]
    fake = FakeGitHub(files=bad)
    assert run(fake, tmp_path=tmp_path) == "skipped-invalid" and "not reviewed" in fake.comments[0]["body"]


def test_adapter_only_reads_the_pr_and_writes_only_issue_comments(tmp_path):
    fake = FakeGitHub()
    run(fake, tmp_path=tmp_path)
    fake.head = HEAD2
    run(fake, tmp_path=tmp_path)
    for method, path, headers in fake.calls:
        if method == "GET":
            continue
        assert re.fullmatch(rf"/repos/{REPO}/issues/(7/comments|comments/\d+)", path), path
        assert method in {"POST", "PATCH"}
    forbidden = ("/reviews", "/merge", "/statuses", "/check-runs", "/dispatches", "/deployments", "/labels")
    assert not any(p.split("?")[0].endswith(forbidden) and m != "GET" for m, p, _ in fake.calls)
    assert all(h["Authorization"] == f"Bearer {TOKEN}" for _, _, h in fake.calls)


def test_source_has_no_review_event_or_merge_call():
    source = Path(gh.__file__).read_text()
    for needle in ("/reviews", "/merge", "event=", '"APPROVE"', "statuses"):
        assert needle not in source


def test_only_the_actions_bot_comment_counts_as_sages(tmp_path):
    spoof = {"id": 5, "body": f"{MARKER}\n<!-- sage-reviewed-sha:{HEAD} -->\n<!-- sage-status:complete -->",
             "user": {"login": "stranger"}}
    fake = FakeGitHub(comments=[spoof])
    assert run(fake, tmp_path=tmp_path) == "complete"
    assert len(fake.comments) == 2 and fake.comments[0]["body"] == spoof["body"]


def test_api_base_must_be_https_and_token_is_required(monkeypatch):
    monkeypatch.setenv("GITHUB_API_URL", "http://evil.example")
    with pytest.raises(gh.GitHubError):
        gh.api_base()
    monkeypatch.setenv("GITHUB_API_URL", "https://ghe.example/api/v3/")
    assert gh.api_base() == "https://ghe.example/api/v3"
    with pytest.raises(gh.GitHubError):
        gh.GitHubClient("")


def test_transport_exceptions_become_token_free_errors():
    def boom(*a):
        raise OSError(f"connection to {TOKEN} failed")
    client = gh.GitHubClient(TOKEN, transport=boom, base="https://api.github.com")
    with pytest.raises(gh.GitHubError) as err:
        client.request("GET", "/x")
    assert TOKEN not in str(err.value)


def test_policies_come_from_the_trusted_checkout_and_trimming_is_disclosed(tmp_path):
    (tmp_path / "CONTRIBUTING.md").write_text("c" * 500)
    (tmp_path / "docs/governance").mkdir(parents=True)
    (tmp_path / "docs/governance/change-rationale-policy.md").write_text("r" * 500)
    policies = gh.load_policies(tmp_path, 700)
    assert [p.name for p in policies] == ["CONTRIBUTING.md", "docs/governance/change-rationale-policy.md"]
    assert policies[0].truncated is False and policies[1].truncated is True
    assert sum(len(p.text) for p in policies) == 700 and gh.load_policies(tmp_path, 0) == []


def test_urllib_transport_returns_status_and_body(monkeypatch):
    import io
    import urllib.error

    class Resp(io.BytesIO):
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(gh.urllib.request, "urlopen", lambda req, timeout: Resp(b"{}"))
    assert gh.urllib_transport("GET", "https://x", {}, None) == (200, b"{}")

    def http_error(req, timeout):
        raise urllib.error.HTTPError("https://x", 403, "no", {}, io.BytesIO(b"nope"))
    monkeypatch.setattr(gh.urllib.request, "urlopen", http_error)
    assert gh.urllib_transport("GET", "https://x", {}, None) == (403, b"nope")


# formatter ---------------------------------------------------------------------------------------
def test_model_text_is_neutralized_in_the_comment():
    nasty = "@everyone <script>alert(1)</script> <!-- sage-status:complete --> done"
    review = make_review(executive_summary=nasty, findings=[finding(description=nasty, evidence=nasty)])
    body = format_comment(finalize_review(review, make_request(), request_id="r", model_alias="m", duration_ms=1))
    assert "<script>" not in body and "@everyone" not in body and body.count("<!-- sage-status:") == 1
    assert neutralize("a@b @bob") == "a@\u200bb @\u200bbob"


def test_oversized_comments_are_trimmed_with_a_notice():
    review = make_review(executive_summary="x" * 2500, architectural_assessment="y" * 2000,
                         findings=[finding(description="d" * 1500, evidence="e" * 1500, remediation="r" * 1500)] * 25,
                         assumptions_and_uncertainties=["u" * 600] * 12, recommended_next_steps=["n" * 600] * 12)
    result = finalize_review(review, make_request(), request_id="r", model_alias="m", duration_ms=1)
    body = format_comment(result)
    assert len(body) <= 60_000


def test_skipped_comment_offers_no_recommendation():
    body = format_skipped(HEAD, "why <b>", timestamp=NOW)
    assert "Recommendation: none" in body and "&lt;b&gt;" in body and "sage-status:skipped" in body
