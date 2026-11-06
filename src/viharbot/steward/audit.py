"""A hash-chained audit trail in the audit table.

Each event stores the SHA-256 of its predecessor, so removing, reordering or editing a stored event
is detectable by `verify_chain`. This is tamper-evidence, not tamper-proofing: DynamoDB `PutItem`
can overwrite a key that is known, so a compromised runtime could replace a row it can name. The
chain makes that visible to a later check, point-in-time recovery and a planned object-locked export
are the stronger mitigations, and the runtime role is denied update and delete on this table.

Only allowlisted fields are written, and every value must be short and plain: a contributor's text,
a token or a stack trace cannot be recorded by accident.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import re
from collections.abc import Callable
from typing import Any

GENESIS = "0" * 64
CHAIN = "chain"
MAX_RETRIES = 3
FIELDS = (
    "request_id", "poll_id", "repo", "pr", "observation_id", "observation_count", "alert_count",
    "pr_count", "failure_count", "error_code", "duration_ms", "gate_sha256", "model_alias",
    "destination", "status", "dry_run",
)
_PLAIN = re.compile(r"^[A-Za-z0-9_.:/#@ -]{0,120}$")


class AuditUnavailable(RuntimeError):
    """The audit table could not be written. Fixed wording, no data."""


class AuditIntegrityError(RuntimeError):
    """The stored chain has been altered."""


def _digest(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _clean(name: str, value: Any) -> Any:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, str) and _PLAIN.fullmatch(value):
        return value
    raise ValueError(f"audit field {name} must be a short plain value")


def build_body(previous: dict | None, seq: int, timestamp: str, event: str, decision: str,
               fields: dict) -> str:
    unknown = set(fields) - set(FIELDS)
    if unknown:
        raise ValueError(f"unknown audit fields: {sorted(unknown)}")
    body = {
        "seq": seq, "timestamp": timestamp, "agent": "kepler", "event": _clean("event", event),
        "decision": _clean("decision", decision),
        "prev_sha256": GENESIS if previous is None else previous["sha256"],
        **{name: _clean(name, fields.get(name)) for name in FIELDS},
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


def verify_chain(items: list[dict]) -> None:
    """`items` are `{"seq", "body", "sha256"}` in order. Raises if anything was altered."""
    previous = GENESIS
    for index, item in enumerate(items):
        body = json.loads(item["body"])
        if (item["sha256"] != _digest(item["body"]) or body["seq"] != index
                or body["prev_sha256"] != previous):
            raise AuditIntegrityError(f"audit chain broken at event {index}")
        previous = item["sha256"]


def uncertain_intents(items: list[dict]) -> list[dict]:
    """Alert intents with no recorded outcome.

    The poller records `alert_intent` before it publishes an email and `alert_sent` or
    `alert_failed` after. An email cannot be unsent, so if the audit store fails between the two, the
    intent is the durable proof that an alert was about to go out. This lists every intent that has
    no later outcome for the same poll and pull request, so an operator can reconcile it by hand.
    """
    intents: list[tuple[tuple, dict]] = []
    outcomes: set[tuple] = set()
    for item in items:
        body = json.loads(item["body"])
        key = (body.get("poll_id"), body.get("pr"))
        if body["event"] == "alert_intent":
            intents.append((key, body))
        elif body["event"] in ("alert_sent", "alert_failed"):
            outcomes.add(key)
    return [{"pr": body["pr"], "poll_id": body["poll_id"], "seq": body["seq"],
             "timestamp": body["timestamp"]} for key, body in intents if key not in outcomes]


class AuditLog:
    def __init__(self, client: Any, table: str, clock: Callable[[], datetime.datetime]) -> None:
        self._db, self._table, self._clock = client, table, clock

    def _latest(self) -> dict | None:
        try:
            result = self._db.query(
                TableName=self._table, KeyConditionExpression="pk = :pk",
                ExpressionAttributeValues={":pk": {"S": CHAIN}}, ScanIndexForward=False, Limit=1,
                ConsistentRead=True)
        except Exception:
            raise AuditUnavailable("the audit table could not be read") from None
        items = result.get("Items", [])
        if not items:
            return None
        body = items[0]["body"]["S"]
        return {"seq": json.loads(body)["seq"], "sha256": items[0]["sha256"]["S"]}

    def append(self, event: str, decision: str = "ok", **fields: Any) -> str:
        """Add one event to the chain. Returns its SHA-256. Retries a lost race, then gives up."""
        for _ in range(MAX_RETRIES):
            previous = self._latest()
            seq = 0 if previous is None else previous["seq"] + 1
            body = build_body(previous, seq, self._clock().strftime("%Y-%m-%dT%H:%M:%SZ"),
                              event, decision, fields)
            digest = _digest(body)
            try:
                self._db.put_item(
                    TableName=self._table,
                    Item={"pk": {"S": CHAIN}, "sk": {"S": f"{seq:012d}"}, "body": {"S": body},
                          "sha256": {"S": digest}},
                    ConditionExpression="attribute_not_exists(pk) AND attribute_not_exists(sk)")
                return digest
            except Exception as exc:
                response = getattr(exc, "response", None)
                code = response.get("Error", {}).get("Code") if isinstance(response, dict) else None
                if code != "ConditionalCheckFailedException":
                    raise AuditUnavailable("the audit table could not be written") from None
        raise AuditUnavailable("the audit chain is being written by someone else")

    def read_all(self) -> list[dict]:
        """For verification only. Reads the whole chain in order."""
        items: list[dict] = []
        start = None
        while True:
            args = {"TableName": self._table, "KeyConditionExpression": "pk = :pk",
                    "ExpressionAttributeValues": {":pk": {"S": CHAIN}}, "ConsistentRead": True}
            if start:
                args["ExclusiveStartKey"] = start
            try:
                page = self._db.query(**args)
            except Exception:
                raise AuditUnavailable("the audit table could not be read") from None
            items += [{"seq": int(i["sk"]["S"]), "body": i["body"]["S"], "sha256": i["sha256"]["S"]}
                      for i in page.get("Items", [])]
            start = page.get("LastEvaluatedKey")
            if not start:
                return items
