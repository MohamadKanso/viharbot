"""The pull request comment for a Sage review. Pure text: no network, no model.

Model output is untrusted. Every string is neutralized before it is placed in the comment so it
cannot @-mention people, forge the hidden markers that identify and date the comment, or inject
HTML. The comment is capped below GitHub's 65,536-character limit and says so when it trims.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from .sage_code_review import ReviewFinding, SageReviewResult

MARKER = "<!-- sage-code-review:v1 -->"
SHA_MARKER = "<!-- sage-reviewed-sha:{sha} -->"
STATUS_MARKER = "<!-- sage-status:{status} -->"
SHA_RE = re.compile(r"<!-- sage-reviewed-sha:([0-9a-f]{40}) -->")
STATUS_RE = re.compile(r"<!-- sage-status:(complete|incomplete|failed|skipped|stale) -->")
MAX_COMMENT_CHARS = 60_000
TITLE = "Sage AI Code Review — Advisory Recommendation"

_MENTION = re.compile(r"@(?=[A-Za-z0-9])")


def neutralize(text: str) -> str:
    """Make untrusted text inert in GitHub markdown."""
    text = text.replace("<", "&lt;").replace(">", "&gt;")
    text = _MENTION.sub("@​", text)
    return text.replace("\r", "").strip()


def _lines(items: list[str]) -> str:
    return "\n".join(f"- {neutralize(item)}" for item in items) if items else "_None._"


def _where(finding: ReviewFinding) -> str:
    if not finding.file_path:
        return ""
    if finding.line_start is None:
        return f" `{neutralize(finding.file_path)}`"
    end = f"-{finding.line_end}" if finding.line_end and finding.line_end != finding.line_start else ""
    return f" `{neutralize(finding.file_path)}:{finding.line_start}{end}`"


def _finding(finding: ReviewFinding) -> str:
    return (
        f"- **{finding.severity.upper()}** · {finding.category} · {finding.basis.replace('_', ' ')}"
        f"{_where(finding)}\n"
        f"  {neutralize(finding.description)}\n"
        f"  - Evidence: {neutralize(finding.evidence)}\n"
        f"  - Suggested remediation: {neutralize(finding.remediation)}"
    )


def _ci(result: SageReviewResult) -> str:
    if not result.ci_results:
        return "No CI results were available to this review."
    return "\n".join(
        f"- {neutralize(c.name)}: {c.conclusion or c.status}" for c in result.ci_results[:30]
    )


def format_comment(result: SageReviewResult, *, timestamp: datetime | None = None) -> str:
    """The full comment body for a finished (complete, incomplete or failed) review."""
    when = (timestamp or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M UTC")
    review = result.review
    recommended = [f for f in review.findings if f.severity in ("critical", "high")]
    other = [f for f in review.findings if f.severity not in ("critical", "high")]

    parts = [
        MARKER,
        SHA_MARKER.format(sha=result.head_sha),
        STATUS_MARKER.format(status=result.completeness),
        f"## {TITLE}",
        "",
        f"**Recommendation: {review.recommendation}**",
        "",
        "_This is an advisory opinion from an AI reviewer. It is not a GitHub review, not a required "
        "check, and it cannot merge, block or approve anything. The repository founder decides._",
        "",
        f"Reviewed commit `{result.head_sha[:12]}` · {when} · "
        f"review completeness: **{result.completeness}**",
    ]
    if result.completeness != "complete":
        parts += ["", "> **Incomplete review.** No approval is offered.", ">",
                  *(f"> - {neutralize(r)}" for r in result.incomplete_reasons)]
    if result.requires_independent_human_assessment:
        parts += ["", "> **Independent human assessment required.** This pull request changes Sage, its "
                  "review policy, its authorization or the workflow that runs it, so Sage cannot "
                  "assess it on its own authority."]
    parts += [
        "", "### Executive summary", neutralize(review.executive_summary),
        "", "### Recommended changes", "_Advice only; nothing here is enforced._",
        "\n".join(_finding(f) for f in recommended) if recommended else "_None._",
        "", "### Nonblocking observations",
        "\n".join(_finding(f) for f in other) if other else "_None._",
        "", "### Architectural implications", neutralize(review.architectural_assessment) or "_Not assessed._",
        "", "### Security considerations", neutralize(review.security_assessment) or "_Not assessed._",
        "", "### Testing observations", neutralize(review.test_assessment) or "_Not assessed._",
        "", "### Governance considerations", neutralize(review.governance_assessment) or "_Not assessed._",
        "", "### Evidence references",
        "Findings cite files and lines that were checked against the supplied diff. CI results below "
        "come from GitHub, not from the model.", _ci(result),
        "", "### Uncertainties", _lines(review.assumptions_and_uncertainties),
        "", "### Suggested next steps", _lines(review.recommended_next_steps),
    ]
    if result.adjustments:
        parts += ["", "### Adjustments made after the model answered", _lines(result.adjustments)]
    footer = f"Model alias: `{neutralize(result.model_alias or 'none')}` · request `{result.request_id[:36]}`"
    parts += ["", "---", footer]
    body = "\n".join(parts)
    if len(body) > MAX_COMMENT_CHARS:
        body = body[: MAX_COMMENT_CHARS - 200].rstrip() + "\n\n_The review was trimmed to fit GitHub's comment size limit._"
    return body


def format_skipped(head_sha: str, reason: str, *, timestamp: datetime | None = None) -> str:
    """A comment for a pull request that was deliberately not reviewed."""
    when = (timestamp or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M UTC")
    return "\n".join([
        MARKER, SHA_MARKER.format(sha=head_sha), STATUS_MARKER.format(status="skipped"),
        f"## {TITLE}", "", "**Recommendation: none. Sage did not review this pull request.**", "",
        neutralize(reason), "", f"Commit `{head_sha[:12]}` · {when}", "",
        "_Advisory only. The repository founder decides._",
    ])


def mark_stale(body: str, new_sha: str) -> str:
    """Prefix an existing review with a stale banner. Idempotent."""
    old = SHA_RE.search(body)
    body = STATUS_RE.sub(STATUS_MARKER.format(status="stale"), body, count=1)
    if "**This recommendation is stale.**" in body:
        return body
    reviewed = old.group(1)[:12] if old else "an earlier commit"
    banner = (
        f"> **This recommendation is stale.** It applies to commit `{reviewed}`. The pull request is "
        f"now at `{new_sha[:12]}`, and a new review is in progress.\n\n"
    )
    index = body.find("## ")
    return body[:index] + banner + body[index:] if index != -1 else banner + body
