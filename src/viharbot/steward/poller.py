"""One poll: find first-time contributors' pull requests, assess them, alert on what is new.

The rules that matter:

* Read-only against GitHub. The only write-like thing it does is publish an email to the founder.
* One bad pull request does not stop the others: its failure is audited and counted, and the run
  is reported `partial`. A failure of the audit log or the state store aborts the run and sends
  nothing, because without them there is no deduplication and no record.
* An alert is claimed in the state table before it is published and marked sent afterwards, so a
  re-poll or a duplicate event does not re-alert; a crash in between causes at most one retry.
* A run never outlives its deadline, reads a bounded number of pull requests, and reports through
  metrics: `PollCompleted` is the heartbeat, `PollFailed` is 1 unless every step succeeded.
* A dry run reads and assesses but writes nothing: no claim, no audit row, no email.
* Nothing returned or logged contains contributor-written text, a token or a key.
"""

from __future__ import annotations

import datetime
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .alerts import AlertUnavailable, format_alert
from .assess import assess
from .audit import AuditUnavailable
from .cla import ClaGateUnavailable, load_gate
from .config import StewardConfig
from .drafts import draft_followup
from .github_auth import AuthError
from .snapshot import fetch_cla_files, fetch_snapshot, list_first_time_pulls
from .state import NEW, RETRY, StateUnavailable

logger = logging.getLogger("kepler")


@dataclass
class PollResult:
    status: str  # ok, partial or failed
    prs_assessed: int = 0
    alerts_sent: int = 0
    failures: int = 0
    new_observations: int = 0
    error_code: str | None = None
    duration_ms: int = 0
    dry_run: bool = False
    pull_numbers: list[int] = field(default_factory=list)
    emails: list[tuple[str, str]] = field(default_factory=list)  # filled in dry runs only

    def public(self) -> dict:
        """What may be returned to an invoker: counts and codes, never email bodies."""
        return {
            "status": self.status, "prs_assessed": self.prs_assessed,
            "alerts_sent": self.alerts_sent, "failures": self.failures,
            "new_observations": self.new_observations, "error_code": self.error_code,
            "duration_ms": self.duration_ms, "dry_run": self.dry_run,
            "pull_numbers": self.pull_numbers,
        }


def _error_code(exc: Exception) -> str:
    gate = load_gate()
    if isinstance(exc, gate.GitHubError):
        return "github"
    for kind, code in ((ClaGateUnavailable, "gate"), (AuthError, "auth"), (StateUnavailable, "state"),
                       (AuditUnavailable, "audit"), (AlertUnavailable, "alert")):
        if isinstance(exc, kind):
            return code
    return "internal"


def run_poll(
    config: StewardConfig,
    *,
    token_source: Any,
    make_client: Callable[[str], Any] | None,
    state: Any,
    audit: Any,
    publisher: Any,
    metrics: Any,
    clock: Callable[[], datetime.datetime],
    request_id: str = "manual",
    dry_run: bool = False,
    only_pr: int | None = None,
) -> PollResult:
    gate = load_gate()
    make_client = make_client or (lambda token: gate.GitHub(token=token))
    started = clock()
    deadline = started + datetime.timedelta(seconds=config.deadline_seconds)
    request_id = re.sub(r"[^A-Za-z0-9_.-]", "", request_id)[:64] or "manual"
    poll_id = f"poll-{started:%Y%m%dT%H%M%SZ}"
    result = PollResult(status="ok", dry_run=dry_run)

    def record(event: str, decision: str = "ok", **fields: Any) -> None:
        line = {"agent": "kepler", "request_id": request_id, "event": event, "decision": decision,
                "destination": fields.get("destination"), "model_alias": None,
                "duration_ms": fields.get("duration_ms"), "error_code": fields.get("error_code")}
        (logger.warning if decision == "fail" else logger.info)(json.dumps(line, sort_keys=True))
        if not dry_run:
            audit.append(event, decision, request_id=request_id, poll_id=poll_id,
                         repo=config.repo, **fields)

    def finish(status: str, code: str | None = None) -> PollResult:
        result.status, result.error_code = status, code
        result.duration_ms = int((clock() - started).total_seconds() * 1000)
        try:
            metrics.put("PollCompleted", 1 if status != "failed" else 0)
            metrics.put("PollFailed", 0 if status == "ok" else 1)
            metrics.put("PRsAssessed", result.prs_assessed)
            metrics.put("AlertsSent", result.alerts_sent)
        except Exception:  # a metrics problem must never turn a good poll into a failure
            pass
        try:
            record("poll_completed", "ok" if status == "ok" else "fail", status=status,
                   pr_count=result.prs_assessed, alert_count=result.alerts_sent,
                   failure_count=result.failures, error_code=code,
                   duration_ms=result.duration_ms, dry_run=dry_run)
        except (AuditUnavailable, ValueError):
            if result.status == "ok":
                result.status = "partial"
                result.error_code = "audit"
        return result

    try:
        record("poll_started", dry_run=dry_run)
        token = token_source.token()
        gh = make_client(token)
        files, pinned = fetch_cla_files(gh, config.repo, config.base_branch)
        founders, _ = gate.founders_from(files.base_registry)
        numbers = [only_pr] if only_pr else list_first_time_pulls(gh, config.repo, set(founders))
        numbers = numbers[: config.max_pulls]
        resolver = gate.github_resolver(gh)
    except Exception as exc:  # auth, base-branch read, listing, audit, or a bug: the run cannot start
        return finish("failed", _error_code(exc))
    result.pull_numbers = list(numbers)

    try:
        for number in numbers:
            if clock() >= deadline:
                record("deadline_reached", "fail", error_code="deadline")
                result.failures += 1
                break
            try:
                snapshot = fetch_snapshot(gh, config.repo, number)
                now = clock()
                assessment, cla = assess(snapshot, files, resolver, now, config.reminder_after_hours)
                decision = draft_followup(assessment, cla, now, config.repo,
                                          config.reminder_after_hours)
            except (StateUnavailable, AuditUnavailable):
                raise
            except Exception as exc:
                result.failures += 1
                record("pr_failed", "fail", pr=number, error_code=_error_code(exc))
                continue
            result.prs_assessed += 1

            new = []
            for observation in assessment.observations:
                if dry_run:
                    seen = state.peek(observation.observation_id) if state else "unseen"
                    if seen == "unseen":
                        new.append(observation)
                elif state.claim(observation.observation_id) in (NEW, RETRY):
                    new.append(observation)
            result.new_observations += len(new)
            if not new:
                continue
            try:
                subject, body = format_alert(assessment, new, decision)
                if dry_run:
                    result.emails.append((subject, body))
                    continue
                publisher.publish(subject, body)
            except (StateUnavailable, AuditUnavailable):
                raise
            except Exception as exc:
                result.failures += 1
                record("alert_failed", "fail", pr=number, observation_count=len(new),
                       destination="sns", error_code=_error_code(exc))
                continue  # the claims stay pending, so a later poll retries after the window
            for observation in new:
                state.mark_sent(observation.observation_id)
            result.alerts_sent += 1
            record("alert_sent", pr=number, observation_count=len(new), destination="sns",
                   gate_sha256=assessment.gate_sha256)
    except (StateUnavailable, AuditUnavailable) as exc:
        return finish("failed", _error_code(exc))

    return finish("ok" if result.failures == 0 else "partial")
