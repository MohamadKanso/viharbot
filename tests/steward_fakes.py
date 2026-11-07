"""Fakes for the steward orchestration tests: DynamoDB with real conditional-write behaviour, SNS,
CloudWatch, Secrets Manager, an installation-token opener. Nothing here touches a network."""

from __future__ import annotations

import datetime
import json
from collections.abc import Callable


class CondFail(Exception):
    """What botocore raises for a failed condition: an exception carrying `.response`."""

    def __init__(self) -> None:
        super().__init__("conditional check failed")
        self.response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class Clock:
    def __init__(self, now: datetime.datetime, step: float = 0.0) -> None:
        self.now, self.step = now, step

    def __call__(self) -> datetime.datetime:
        current = self.now
        self.now = current + datetime.timedelta(seconds=self.step)
        return current

    def advance(self, **kwargs) -> None:
        self.now += datetime.timedelta(**kwargs)


class FakeDynamo:
    """The subset of the DynamoDB client the steward uses, with real condition semantics."""

    def __init__(self, page: int = 100) -> None:
        self.tables: dict[str, dict[tuple[str, str], dict]] = {}
        self.writes = 0
        self.fail: set[str] = set()  # operations that raise, e.g. {"put"}, or {"all"}
        self.fail_tables: set[str] = set()  # tables whose writes raise
        self.race_hook: Callable[[dict, tuple], None] | None = None
        self.races_left = 0
        self.page = page
        self.fail_audit_events: set[str] = set()  # audit events whose write raises (an outage mid-run)

    def _table(self, name: str) -> dict:
        return self.tables.setdefault(name, {})

    def _check(self, op: str, table: str) -> None:
        if op in self.fail or "all" in self.fail or (op in ("put", "update") and table in self.fail_tables):
            raise RuntimeError("simulated outage")

    def put_item(self, TableName, Item, ConditionExpression=None):
        self._check("put", TableName)
        if "body" in Item and json.loads(Item["body"]["S"]).get("event") in self.fail_audit_events:
            raise RuntimeError("simulated audit outage for this event")
        key = (Item["pk"]["S"], Item["sk"]["S"])
        table = self._table(TableName)
        if self.races_left > 0 and self.race_hook:
            self.races_left -= 1
            self.race_hook(table, key)
            raise CondFail()
        if ConditionExpression and "attribute_not_exists" in ConditionExpression and key in table:
            raise CondFail()
        table[key] = dict(Item)
        self.writes += 1

    def get_item(self, TableName, Key, ConsistentRead=False):
        self._check("get", TableName)
        item = self._table(TableName).get((Key["pk"]["S"], Key["sk"]["S"]))
        return {"Item": item} if item else {}

    def update_item(self, TableName, Key, UpdateExpression, ConditionExpression=None,
                    ExpressionAttributeNames=None, ExpressionAttributeValues=None):
        self._check("update", TableName)
        values = ExpressionAttributeValues or {}
        item = self._table(TableName).get((Key["pk"]["S"], Key["sk"]["S"]))
        if ConditionExpression and "attribute_exists(pk)" in ConditionExpression and item is None:
            raise CondFail()
        if ConditionExpression and "claimed_at = :old" in ConditionExpression:
            if (item is None or item["claimed_at"]["S"] != values[":old"]["S"]
                    or item["status"]["S"] != values[":pending"]["S"]):
                raise CondFail()
        if ConditionExpression and "attempts < :max" in ConditionExpression:
            if item is None or int(item["attempts"]["N"]) >= int(values[":max"]["N"]):
                raise CondFail()
        if ConditionExpression and "attempts >= :max" in ConditionExpression:
            if (item is None or int(item["attempts"]["N"]) < int(values[":max"]["N"])
                    or item["status"]["S"] != values[":pending"]["S"]):
                raise CondFail()
        if UpdateExpression.startswith("SET #s = :sent"):
            item["status"], item["sent_at"] = {"S": "sent"}, values[":now"]
        elif ":exhausted" in values:
            item["status"], item["exhausted_at"] = {"S": "exhausted"}, values[":now"]
        else:
            item["claimed_at"] = values[":now"]
            item["attempts"] = {"N": str(int(item["attempts"]["N"]) + 1)}
        self.writes += 1

    def query(self, TableName, KeyConditionExpression, ExpressionAttributeValues,
              ScanIndexForward=True, Limit=None, ConsistentRead=False, ExclusiveStartKey=None):
        self._check("query", TableName)
        pk = ExpressionAttributeValues[":pk"]["S"]
        rows = sorted(((k, v) for k, v in self._table(TableName).items() if k[0] == pk),
                      key=lambda kv: kv[0][1], reverse=not ScanIndexForward)
        if ExclusiveStartKey:
            after = ExclusiveStartKey["sk"]["S"]
            rows = [r for r in rows if (r[0][1] > after if ScanIndexForward else r[0][1] < after)]
        size = min(Limit or self.page, self.page)
        page = rows[:size]
        result = {"Items": [v for _, v in page]}
        if len(rows) > size:
            result["LastEvaluatedKey"] = {"pk": {"S": pk}, "sk": {"S": page[-1][0][1]}}
        return result


class FakeSns:
    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.fail = False

    def publish(self, TopicArn, Subject, Message):
        if self.fail:
            raise RuntimeError("simulated outage")
        self.messages.append({"TopicArn": TopicArn, "Subject": Subject, "Message": Message})
        return {"MessageId": f"m-{len(self.messages)}"}


class FakeCloudWatch:
    def __init__(self) -> None:
        self.points: list[tuple[str, float]] = []
        self.fail = False

    def put_metric_data(self, Namespace, MetricData):
        if self.fail:
            raise RuntimeError("simulated outage")
        assert Namespace == "ViharBot/Kepler"
        self.points += [(d["MetricName"], d["Value"]) for d in MetricData]

    def last(self, name: str) -> float | None:
        values = [v for n, v in self.points if n == name]
        return values[-1] if values else None


class FakeSecrets:
    def __init__(self, secret: dict | str | Exception) -> None:
        self.secret = secret
        self.requested: list[str] = []

    def get_secret_value(self, SecretId):
        self.requested.append(SecretId)
        if isinstance(self.secret, Exception):
            raise self.secret
        return {"SecretString": self.secret if isinstance(self.secret, str) else json.dumps(self.secret)}


class FakeTokens:
    def __init__(self, token: str = "ghs_FAKEINSTALLATIONTOKEN", error: Exception | None = None):
        self._token, self._error, self.calls = token, error, 0

    def token(self) -> str:
        self.calls += 1
        if self._error:
            raise self._error
        return self._token
