"""Judge one first-time contributor's pull request. Pure: no network, no clock, no model.

Every requirement is decided from structured facts and the CLA gate's own functions. No wording
here depends on anything a contributor wrote, and no contributor text appears in the result: the
only contributor-controlled strings that can surface are GitHub account logins, which GitHub itself
restricts to letters, digits and hyphens.

Statuses are `done`, `outstanding` (something is missing), `needs_human` (only the founder can
resolve or confirm it) and `info`. Nothing is ever marked `done` from an unverified claim.
"""

from __future__ import annotations

import datetime
import hashlib

from .cla import ClaAssessment, _parse_time, assess_cla, gate_fingerprint, load_gate
from .model import DONE, INFO, NEEDS_HUMAN, OUTSTANDING, Observation, PullAssessment, Requirement
from .snapshot import FIRST_TIMER_ASSOCIATIONS, PullSnapshot, summarize_ci

REMINDER_AFTER_HOURS = 72


def _hours_since(then: str | None, now: datetime.datetime) -> float | None:
    parsed = _parse_time(then)
    return None if parsed is None else (now - parsed).total_seconds() / 3600


def _observation_id(repo: str, number: int, key: str, status: str, scope: str) -> str:
    raw = f"{repo}#{number}|{key}|{status}|{scope}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def assess(
    snapshot: PullSnapshot, files, resolve, now: datetime.datetime,
    reminder_after_hours: int = REMINDER_AFTER_HOURS,
) -> tuple[PullAssessment, ClaAssessment]:
    gate = load_gate()
    cla = assess_cla(gate, files, snapshot.pr, list(snapshot.comments), resolve, now.date(),
                     snapshot.repo)
    requirements: list[Requirement] = []
    actions: list[str] = []
    scopes: dict[str, str] = {}
    asked_hours = _hours_since(cla.maintainer_request_at, now)

    # 1. CLA acceptance, recorded by the founder.
    if not cla.record_readable:
        requirements.append(Requirement(
            "cla_recorded", "CLA acceptance recorded", NEEDS_HUMAN,
            "The CLA record on the protected branch could not be read or validated, so nothing "
            "can be concluded. Check the record."))
        actions.append("Check CLA-ACCEPTANCES.md, CLA-EXCEPTIONS.md and CONTRIBUTORS.md on main.")
    elif cla.author_covered:
        requirements.append(Requirement(
            "cla_recorded", "CLA acceptance recorded", DONE,
            "The author is covered by the founder-approved record (or an exemption) for the "
            "current legal-terms version."))
    elif cla.claim == "valid":
        requirements.append(Requirement(
            "cla_recorded", "CLA acceptance recorded", NEEDS_HUMAN,
            "The author posted the exact acceptance statement, unedited. It is only a request: "
            "it counts once the founder verifies it and the record is merged."))
        actions.append("Verify the author's acceptance comment and record it in "
                       "CLA-ACCEPTANCES.md through a founder pull request.")
    else:
        detail = {
            "withdrawn": "The author has withdrawn from future participation; only a "
                         "founder-approved exception admits this pull request.",
            "hold": "The author's eligibility is on hold pending a founder decision.",
            "older_version": "The author accepted an earlier legal-terms version; a material "
                             "change needs renewed acceptance.",
        }.get(cla.author_state, "No acceptance is recorded for the current legal-terms version.")
        if cla.claim == "invalid_only":
            detail += " A comment with the right words exists but is edited or otherwise invalid."
        requirements.append(Requirement("cla_recorded", "CLA acceptance recorded", OUTSTANDING,
                                        detail))
        scopes["cla_recorded"] = f"{cla.legal_version}|{cla.author_state}|{cla.claim}"
        if cla.maintainer_request_at is None:
            actions.append("Ask the author for the CLA acceptance (a draft is attached).")
        elif asked_hours is not None and asked_hours < reminder_after_hours:
            actions.append(
                f"The CLA was already requested about {round(asked_hours)} hours ago. Do not "
                f"repeat it yet; consider a gentle reminder after {reminder_after_hours} hours.")
        else:
            actions.append("The CLA request is over "
                           f"{reminder_after_hours} hours old with no acceptance. A gentle "
                           "reminder is reasonable (a draft is attached).")

    # 2. Third-party rights disclosure. Only a human can judge it.
    if cla.author_covered:
        requirements.append(Requirement(
            "third_party_disclosure", "Third-party rights disclosure", DONE,
            "Eligibility was settled when the acceptance was recorded."))
    elif cla.maintainer_request_at is None:
        requirements.append(Requirement(
            "third_party_disclosure", "Third-party rights disclosure", OUTSTANDING,
            "The author has not yet been asked about employer or third-party rights."))
    elif cla.author_replied_after_request:
        requirements.append(Requirement(
            "third_party_disclosure", "Third-party rights disclosure", NEEDS_HUMAN,
            "The author replied after the request. Read the reply for any disclosure; Kepler "
            "cannot judge it."))
        actions.append("Read the author's reply for any employer, third-party code or age "
                       "disclosure before going further.")
        scopes["third_party_disclosure"] = "replied"
    else:
        requirements.append(Requirement(
            "third_party_disclosure", "Third-party rights disclosure", OUTSTANDING,
            "No answer to the disclosure question has been seen."))
        scopes["third_party_disclosure"] = "asked"

    # 3. Commit identity.
    if cla.lookup_failed:
        requirements.append(Requirement(
            "commit_identity", "Commit identities linked to GitHub accounts", NEEDS_HUMAN,
            "GitHub could not resolve an identity, so this is unconfirmed. It will be re-checked."))
    elif cla.identity_unresolved:
        requirements.append(Requirement(
            "commit_identity", "Commit identities linked to GitHub accounts", OUTSTANDING,
            "At least one commit author, committer or co-author is not linked to a GitHub "
            "account, so its acceptance cannot be checked."))
        scopes["commit_identity"] = snapshot.head_sha
        actions.append("Ask the author to use a commit email linked to their GitHub account.")
    else:
        requirements.append(Requirement(
            "commit_identity", "Commit identities linked to GitHub accounts", DONE,
            "Every contributor identity is linked to a GitHub account."))

    # 4. Branch freshness (informational: whether an update is required is the founder's call).
    state = snapshot.mergeable_state
    if state == "behind":
        requirements.append(Requirement(
            "branch_current", "Branch up to date with main", OUTSTANDING,
            "The branch is behind main. Whether an update is required is the founder's decision."))
        scopes["branch_current"] = snapshot.head_sha
    elif state == "dirty":
        requirements.append(Requirement(
            "branch_current", "Branch up to date with main", OUTSTANDING,
            "The branch has merge conflicts with main."))
        scopes["branch_current"] = snapshot.head_sha
        actions.append("The branch conflicts with main; the author needs to resolve it.")
    elif state in ("clean", "unstable", "has_hooks", "blocked", "behind_check"):
        requirements.append(Requirement("branch_current", "Branch up to date with main", DONE,
                                        "No conflicts, and not reported as behind."))
    else:
        requirements.append(Requirement("branch_current", "Branch up to date with main", INFO,
                                        "GitHub has not computed the merge state yet."))

    # 5. CI.
    if snapshot.ci_error:
        requirements.append(Requirement("ci", "Automated checks", NEEDS_HUMAN,
                                        "CI state could not be read. It will be retried."))
    else:
        # The CLA gate fails by design until the founder records an acceptance. Counting it as a
        # CI failure would report the same blocker twice, so it is judged under "cla_recorded".
        ci_runs = tuple(r for r in snapshot.ci_runs if r.get("name") != gate.CHECK_NAME)
        ci_state, _ = summarize_ci(ci_runs)
        ci_text = {
            "awaiting_approval": (NEEDS_HUMAN, "Some workflows are waiting for the maintainer to "
                                  "approve running a fork's code. Review the diff first."),
            "failing": (OUTSTANDING, "At least one automated check failed."),
            "pending": (INFO, "Automated checks are still running."),
            "passing": (DONE, "All automated checks passed."),
            "none": (INFO, "No automated checks have run yet."),
        }[ci_state]
        requirements.append(Requirement("ci", "Automated checks", ci_text[0], ci_text[1]))
        if ci_state in ("awaiting_approval", "failing"):
            scopes["ci"] = f"{snapshot.head_sha}|{ci_state}"
        if ci_state == "awaiting_approval":
            actions.append("Review the diff, then approve the fork's workflows if it is safe.")

    # 6. Human review.
    founder_ids = set(gate_founders(files))
    review_states = {r["state"] for r in snapshot.reviews if r.get("id") in founder_ids}
    if "APPROVED" in review_states:
        requirements.append(Requirement("human_review", "Human review", DONE,
                                        "The founder has approved the pull request."))
    elif "CHANGES_REQUESTED" in review_states:
        requirements.append(Requirement("human_review", "Human review", OUTSTANDING,
                                        "The founder has requested changes."))
        scopes["human_review"] = "changes_requested"
    else:
        requirements.append(Requirement("human_review", "Human review", OUTSTANDING,
                                        "The founder has not yet reviewed the pull request."))
        scopes["human_review"] = "none"
        actions.append("Review the pull request (Sage did not review a fork automatically).")

    blockers = tuple(f"{r.title}: {r.detail}" for r in requirements
                     if r.status in (OUTSTANDING, NEEDS_HUMAN)
                     and r.key in ("cla_recorded", "commit_identity", "ci", "human_review",
                                   "third_party_disclosure"))
    observations = tuple(
        Observation(
            _observation_id(snapshot.repo, snapshot.number, r.key, r.status, scopes.get(r.key, "")),
            r.key, r.status, f"PR #{snapshot.number}: {r.title} is {r.status.replace('_', ' ')}.")
        for r in requirements if r.status in (OUTSTANDING, NEEDS_HUMAN)
    )
    first_time = (snapshot.author_association in FIRST_TIMER_ASSOCIATIONS
                  and snapshot.author.get("type") == "User")
    assessment = PullAssessment(
        repo=snapshot.repo, number=snapshot.number, author_login=snapshot.author["login"],
        author_id=snapshot.author["id"], head_sha=snapshot.head_sha, first_time=first_time,
        is_fork=snapshot.is_fork, requirements=tuple(requirements), blockers=blockers,
        recommended_actions=tuple(actions), observations=observations,
        gate_sha256=gate_fingerprint(),
    )
    return assessment, cla


def gate_founders(files) -> dict[int, str]:
    """Founder account ids from the protected base branch's registry."""
    founders, _ = load_gate().founders_from(files.base_registry)
    return founders
