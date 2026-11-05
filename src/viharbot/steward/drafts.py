"""Draft a respectful follow-up for the founder to review. Nothing is posted by this module.

The wording is fixed templates filled only with values the code controls: the contributor's GitHub
login, the legal-terms version and the exact acceptance statement from the CLA gate. No model is
used and nothing a contributor wrote is copied in, so a hostile pull request or comment cannot
steer a draft.

It never repeats a request that is still fresh: if the founder already asked for the CLA, no
draft is produced until the reminder window has passed, and a reminder lists only what is still
outstanding. If the contributor has already replied, there is nothing to ask: a human reads it.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from .assess import REMINDER_AFTER_HOURS, _hours_since
from .cla import ClaAssessment, load_gate
from .model import OUTSTANDING, PullAssessment


@dataclass(frozen=True)
class DraftDecision:
    reason: str  # why there is, or is not, a draft
    kind: str | None = None  # welcome or reminder
    body: str | None = None
    items: tuple[str, ...] = ()


def _outstanding(assessment: PullAssessment, key: str) -> bool:
    return assessment.requirement(key).status == OUTSTANDING


def draft_followup(
    assessment: PullAssessment, cla: ClaAssessment, now: datetime.datetime, repo: str,
    reminder_after_hours: int = REMINDER_AFTER_HOURS,
) -> DraftDecision:
    if not cla.record_readable:
        return DraftDecision("record_unreadable")
    items: list[str] = []
    if _outstanding(assessment, "cla_recorded") and cla.claim != "valid":
        items.append("cla")
    if _outstanding(assessment, "third_party_disclosure"):
        items.append("disclosure")
    if _outstanding(assessment, "commit_identity"):
        items.append("identity")
    if assessment.requirement("branch_current").status == OUTSTANDING and (
            "conflict" in assessment.requirement("branch_current").detail):
        items.append("conflicts")
    if not items:
        return DraftDecision("nothing_to_ask")
    if cla.author_replied_after_request:
        return DraftDecision("contributor_replied_human_should_read")

    asked_hours = _hours_since(cla.maintainer_request_at, now)
    if cla.maintainer_request_at is None:
        kind = "welcome"
    elif asked_hours is not None and asked_hours < reminder_after_hours:
        return DraftDecision("already_requested_waiting")
    else:
        kind = "reminder"

    statement = load_gate().statement(cla.legal_version or "", repo)
    lines = [f"Hi @{assessment.author_login}, "
             + ("thanks for the contribution." if kind == "welcome"
                else "a gentle reminder about the open items on this pull request.")]
    if kind == "welcome":
        lines.append("Before this can be considered for merge, a few things are needed:")
    numbered: list[str] = []
    if "cla" in items:
        numbered.append(
            "Read the Contributor License Agreement (legal-terms version "
            f"{cla.legal_version}; note it has not been reviewed by a lawyer), then post this exact "
            "text as a new comment from your own account, and don't edit it afterwards:\n"
            f"   ```text\n   {statement}\n   ```")
    if "disclosure" in items:
        numbered.append(
            "Tell us here if any of these apply: your employer or another party may have rights "
            "in this work, the pull request includes code from somewhere else, or you are under "
            "18. If none apply, say so.")
    if "identity" in items:
        numbered.append(
            "Make sure your commits use an email address linked to your GitHub account (your "
            "`@users.noreply.github.com` address works), otherwise the automated check cannot "
            "identify you.")
    if "conflicts" in items:
        numbered.append("Update the branch from `main` and resolve the merge conflicts.")
    lines += [f"{index}. {text}" for index, text in enumerate(numbered, start=1)]
    lines.append(
        "Posting the acceptance comment is a request: you are covered only once the maintainer "
        "has verified it and the record is merged. This doesn't guarantee a merge.")
    return DraftDecision("draft_ready", kind, "\n\n".join(lines), tuple(items))
