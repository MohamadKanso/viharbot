"""One poll: find first-time contributors' pull requests, assess them, alert on what is new.

The rules that matter:

* Read-only against GitHub. The only write-like thing it does is publish an email to the founder.
* One bad pull request does not stop the others: its failure is audited and counted, and the run
  is reported `partial`. A failure of the audit log or the state store aborts the run and sends
  nothing, because without them there is no deduplication and no record.
* An alert is claimed in the state table before it is published and marked sent afterwards, so a
  re-poll or a duplicate event does not re-alert. A crash in between allows at most
  `max_alert_attempts - 1` retries; after that the alert is reported once as exhausted and never
  sent again. An `alert_intent` is audited before the email leaves: an audit outage before it sends
  nothing, one after it cannot unsend the email and is found through `uncertain_intents`.
* No read, claim or email starts after the deadline (reads stop `FINALIZE_RESERVE_SECONDS` early).
  The worst overrun is the one GitHub read already in flight, bounded by the client's attempts and
  wait. A run reads a bounded number of pull requests and reports through metrics from its final
  status: `PollCompleted` is the heartbeat, `PollFailed` is 1 unless every step succeeded.
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
from .state import EXHAUSTED, NEW, RETRY, StateUnavailable

logger = logging.getLogger("kepler")
FINALIZE_RESERVE_SECONDS = 15  # held back from the work budget so a run can still record its end


class DeadlineExceeded(RuntimeError):
    """The run's time budget is spent. Raised before a call starts, never inside one."""


class BudgetedClient:
    """Wraps a GitHub client so no read starts after the work deadline.

    This is what makes the deadline mean something: a poll cannot sail past it through a slow pull
    request, only finish the single read already in flight (which the client's own timeout and a
    small retry count bound). It is deliberately not a `GitHubError`, so the code that tolerates a
    failed CI read does not swallow it.
    """

    def __init__(self, inner: Any, deadline: datetime.datetime,
                 clock: Callable[[], datetime.datetime]) -> None:
        self._inner, self._deadline, self._clock = inner, deadline, clock

    def _check(self) -> None:
        if self._clock() >= self._deadline:
            raise DeadlineExceeded("the time budget is spent")

    def get(self, path: str):
        self._check()
        return self._inner.get(path)

    def paginate(self, path: str, max_pages: int):
        self._check()
        return self._inner.paginate(path, max_pages)


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
    if isinstance(exc, DeadlineExceeded):
        return "deadline"
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
    # Two attempts and a short rate-limit wait bound one in-flight read to roughly 45 seconds.
    make_client = make_client or (lambda token: gate.GitHub(token=token, attempts=2, max_wait=10.0))
    started = clock()
    deadline = started + datetime.timedelta(seconds=config.deadline_seconds)
    reserve = min(FINALIZE_RESERVE_SECONDS, config.deadline_seconds // 4)
    work_deadline = deadline - datetime.timedelta(seconds=reserve)  # no new work starts after this
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
            record("poll_completed", "ok" if status == "ok" else "fail", status=status,
                   pr_count=result.prs_assessed, alert_count=result.alerts_sent,
                   failure_count=result.failures, error_code=code,
                   duration_ms=result.duration_ms, dry_run=dry_run)
        except (AuditUnavailable, ValueError):
            if result.status == "ok":
                result.status = "partial"
                result.error_code = "audit"
        # Metrics come last, from the FINAL status, so the heartbeat and the failure alarm always
        # agree with what the caller is told (the completion audit can still change the status).
        try:
            metrics.put("PollCompleted", 1 if result.status != "failed" else 0)
            metrics.put("PollFailed", 0 if result.status == "ok" else 1)
            metrics.put("PRsAssessed", result.prs_assessed)
            metrics.put("AlertsSent", result.alerts_sent)
        except Exception:  # a metrics problem must never turn a good poll into a failure
            pass
        return result

    try:
        record("poll_started", dry_run=dry_run)
        token = token_source.token()
        gh = BudgetedClient(make_client(token), work_deadline, clock)
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
            if clock() >= work_deadline:
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
            except DeadlineExceeded:  # the budget ran out mid-read: stop, do not count it as a PR bug
                result.failures += 1
                record("deadline_reached", "fail", pr=number, error_code="deadline")
                break
            except Exception as exc:
                result.failures += 1
                record("pr_failed", "fail", pr=number, error_code=_error_code(exc))
                continue
            result.prs_assessed += 1
            if clock() >= deadline:  # past the hard deadline: take no claim and send nothing
                result.failures += 1
                record("deadline_reached", "fail", pr=number, error_code="deadline")
                break

            new = []
            for observation in assessment.observations:
                if dry_run:
                    seen = state.peek(observation.observation_id) if state else "unseen"
                    if seen == "unseen":
                        new.append(observation)
                    continue
                outcome = state.claim(observation.observation_id)
                if outcome in (NEW, RETRY):
                    new.append(observation)
                elif outcome == EXHAUSTED:
                    # The retry budget for this alert is spent. It is NOT sent again; it is reported
                    # once, loudly, so a human can look instead of it being dropped silently.
                    result.failures += 1
                    record("alert_exhausted", "fail", pr=number,
                           observation_id=observation.observation_id, error_code="exhausted")
                    try:
                        metrics.put("AlertsAbandoned", 1)
                    except Exception:
                        pass
            result.new_observations += len(new)
            if not new:
                continue
            try:
                subject, body = format_alert(assessment, new, decision)
                if dry_run:
                    result.emails.append((subject, body))
                    continue
                if clock() >= deadline:  # the last look before an email leaves; claims stay pending
                    raise DeadlineExceeded("the time budget is spent")
                # The intent is durable BEFORE the email leaves. If the audit store is down this
                # raises and nothing is sent. If it fails after the send, the intent without an
                # outcome is the proof that an alert may have gone out (see `uncertain_intents`).
                record("alert_intent", pr=number, observation_count=len(new), destination="sns")
                publisher.publish(subject, body)
            except (StateUnavailable, AuditUnavailable):
                raise
            except DeadlineExceeded:
                result.failures += 1
                record("deadline_reached", "fail", pr=number, error_code="deadline")
                break
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
