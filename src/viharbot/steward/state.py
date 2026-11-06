"""Deduplication state: has this observation already been reported?

Backed by the DynamoDB state table through an injected low-level client. The runtime role may put,
get, query and update but not delete, so a claim is never released. Instead an observation moves
through these states:

  pending    claimed by a poll that has not yet confirmed the alert went out
  sent       the alert was published
  exhausted  the claim went stale `max_attempts` times; it is reported once and never retaken

`claim` is a conditional put, so two polls racing for the same observation cannot both win. A poll
that dies after publishing but before marking `sent` leaves the claim `pending`; after a retry
window the next poll may retake it, counting each attempt, until `max_attempts` is reached. The
honest guarantee is at-least-once with a duplicate count bounded by `max_attempts - 1` per
observation, only in that crash window, not exactly-once, which no scheme can promise. If the table cannot be written, no alert is sent: without dedup a database outage would
become an email flood.
"""

from __future__ import annotations

import datetime
from collections.abc import Callable
from typing import Any

NEW, RETRY, SEEN, EXHAUSTED = "new", "retry", "seen", "exhausted"
_CONDITION_FAILED = "ConditionalCheckFailedException"


class StateUnavailable(RuntimeError):
    """The state store failed. The message is fixed wording and carries no data."""


def _failed_condition(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    return isinstance(response, dict) and response.get("Error", {}).get("Code") == _CONDITION_FAILED


def _iso(moment: datetime.datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class ObservationState:
    def __init__(self, client: Any, table: str, clock: Callable[[], datetime.datetime],
                 ttl_days: int = 30, retry_minutes: int = 30, max_attempts: int = 2) -> None:
        if not 1 <= max_attempts <= 5:
            raise ValueError("max_attempts must be between 1 and 5")
        self._db, self._table, self._clock = client, table, clock
        self._ttl_days, self._retry_minutes = ttl_days, retry_minutes
        self._max_attempts = max_attempts  # claims in total: the first, plus max_attempts - 1 retries

    @staticmethod
    def _key(observation_id: str) -> dict:
        return {"pk": {"S": f"obs#{observation_id}"}, "sk": {"S": "v1"}}

    def _get(self, observation_id: str) -> dict | None:
        try:
            return self._db.get_item(TableName=self._table, Key=self._key(observation_id),
                                     ConsistentRead=True).get("Item")
        except Exception:
            raise StateUnavailable("the state store could not be read") from None

    def peek(self, observation_id: str) -> str:
        """Read-only: `unseen`, `pending` or `sent`. Used by dry runs, which write nothing."""
        item = self._get(observation_id)
        return "unseen" if item is None else item.get("status", {}).get("S", "unseen")

    def claim(self, observation_id: str) -> str:
        """`new` (first time), `retry` (a stale pending claim, taken over, at most max_attempts - 1
        times), `exhausted` (the retry budget is spent: returned exactly once, for a human to look
        at) or `seen`."""
        now = self._clock()
        expires = int((now + datetime.timedelta(days=self._ttl_days)).timestamp())
        item = {**self._key(observation_id), "status": {"S": "pending"},
                "claimed_at": {"S": _iso(now)}, "attempts": {"N": "1"},
                "expires_at": {"N": str(expires)}}
        try:
            self._db.put_item(TableName=self._table, Item=item,
                              ConditionExpression="attribute_not_exists(pk)")
            return NEW
        except Exception as exc:
            if not _failed_condition(exc):
                raise StateUnavailable("the state store could not be written") from None
        existing = self._get(observation_id)
        if existing is None or existing.get("status", {}).get("S") != "pending":
            return SEEN
        claimed = existing.get("claimed_at", {}).get("S", "")
        try:
            claimed_at = datetime.datetime.strptime(claimed, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=datetime.timezone.utc)
        except ValueError:
            return SEEN  # a damaged record is left for a human, never silently re-sent
        if (now - claimed_at).total_seconds() < self._retry_minutes * 60:
            return SEEN  # another poll is probably still working on it
        try:
            attempts = int(existing.get("attempts", {}).get("N", ""))
        except ValueError:
            return SEEN  # a damaged record is left for a human, never silently re-sent
        names = {"#s": "status"}
        values = {":now": {"S": _iso(now)}, ":old": {"S": claimed}, ":pending": {"S": "pending"},
                  ":max": {"N": str(self._max_attempts)}}
        try:
            if attempts >= self._max_attempts:
                # The retry budget is spent. Mark it, once and atomically, so the poll can report an
                # alert it gave up on; from then on the observation is simply `seen`.
                self._db.update_item(
                    TableName=self._table, Key=self._key(observation_id),
                    UpdateExpression="SET #s = :exhausted, exhausted_at = :now",
                    ConditionExpression="claimed_at = :old AND #s = :pending AND attempts >= :max",
                    ExpressionAttributeNames=names,
                    ExpressionAttributeValues={**values, ":exhausted": {"S": "exhausted"}})
                return EXHAUSTED
            self._db.update_item(
                TableName=self._table, Key=self._key(observation_id),
                UpdateExpression="SET claimed_at = :now, attempts = attempts + :one",
                ConditionExpression="claimed_at = :old AND #s = :pending AND attempts < :max",
                ExpressionAttributeNames=names,
                ExpressionAttributeValues={**values, ":one": {"N": "1"}})
            return RETRY
        except Exception as exc:
            if _failed_condition(exc):
                return SEEN  # someone else took it over (or exhausted it) first
            raise StateUnavailable("the state store could not be written") from None

    def mark_sent(self, observation_id: str) -> None:
        try:
            self._db.update_item(
                TableName=self._table, Key=self._key(observation_id),
                UpdateExpression="SET #s = :sent, sent_at = :now",
                ConditionExpression="attribute_exists(pk)", ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":sent": {"S": "sent"}, ":now": {"S": _iso(self._clock())}})
        except Exception:
            raise StateUnavailable("the state store could not record the alert as sent") from None
