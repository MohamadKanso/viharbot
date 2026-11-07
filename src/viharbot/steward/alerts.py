"""The alert email to the founder, sent through SNS. Fixed wording only.

An email names the PR, the author's GitHub login (validated: GitHub logins are letters, digits and
hyphens), what is new, where each requirement stands, what to do, and, when the drafter has one, a
draft message marked clearly as NOT posted. No contributor-written text is ever included, and
nothing is posted anywhere: the founder decides what to do with a draft.
"""

from __future__ import annotations

import re
from typing import Any

from .drafts import DraftDecision
from .model import Observation, PullAssessment

_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
MAX_SUBJECT = 100
MAX_BODY = 20_000  # far under SNS's 256 KB limit
_NO_DRAFT = {
    "already_requested_waiting": "the CLA was already requested recently; waiting before a reminder",
    "nothing_to_ask": "nothing is left to ask the contributor for",
    "contributor_replied_human_should_read": "the contributor replied; read their reply first",
    "record_unreadable": "the CLA record could not be read",
}


class AlertUnavailable(RuntimeError):
    """The alert could not be published. Fixed wording, no data."""


def _login(login: str) -> str:
    return login if _LOGIN.fullmatch(login or "") else "(unusual login)"


def format_alert(assessment: PullAssessment, new: list[Observation],
                 decision: DraftDecision | None) -> tuple[str, str]:
    n, repo = assessment.number, assessment.repo
    subject = f"Kepler: PR #{n} needs attention ({len(new)} new)"[:MAX_SUBJECT]
    who = _login(assessment.author_login)
    lines = [
        f"PR #{n} by @{who} ({'first-time contributor' if assessment.first_time else 'contributor'}"
        f"{', from a fork' if assessment.is_fork else ''})",
        f"https://github.com/{repo}/pull/{n}",
        "",
        "New since the last alert:",
        *[f"  - {o.summary}" for o in new],
        "",
        "Where it stands:",
        *[f"  [{r.status.replace('_', ' ')}] {r.title}: {r.detail}" for r in assessment.requirements],
        "",
        "Recommended actions:",
        *([f"  - {a}" for a in assessment.recommended_actions] or ["  - none"]),
        "",
    ]
    if decision and decision.body:
        lines += ["DRAFT message (NOT posted; you decide whether to post it):",
                  "-----", decision.body, "-----"]
    elif decision:
        lines += [f"No draft: {_NO_DRAFT.get(decision.reason, decision.reason)}."]
    lines += ["", "Sent by Kepler, the read-only steward. Nothing has been posted to GitHub."]
    return subject, "\n".join(lines)[:MAX_BODY]


class SnsPublisher:
    def __init__(self, client: Any, topic_arn: str) -> None:
        self._sns, self._topic = client, topic_arn

    def publish(self, subject: str, body: str) -> str:
        if "\n" in subject or "\r" in subject or len(subject) > MAX_SUBJECT:
            raise ValueError("subject must be one short line")
        try:
            return self._sns.publish(TopicArn=self._topic, Subject=subject, Message=body)["MessageId"]
        except Exception:
            raise AlertUnavailable("the alert could not be published") from None
