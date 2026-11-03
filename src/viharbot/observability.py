"""Small JSON logging setup shared by the sample agents."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from uuid import uuid4

REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def get_request_id(value: str | None) -> str:
    if value and REQUEST_ID_PATTERN.fullmatch(value):
        return value
    return str(uuid4())


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        fields = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "agent": getattr(record, "agent", record.name),
            "event": record.getMessage(),
            "request_id": getattr(record, "request_id", None),
            "destination_agent": getattr(record, "destination_agent", None),
            "model_alias": getattr(
                record,
                "model_alias",
                getattr(record, "requested_model_alias", None),
            ),
            "duration_ms": getattr(
                record,
                "duration_ms",
                getattr(
                    record,
                    "inference_latency_ms",
                    getattr(record, "communication_latency_ms", None),
                ),
            ),
        }
        for key in (
            "identity_id",
            "caller",
            "decision",
            "reason",
            "target",
            "capability",
            "personality_mode",
            "planner_identity",
            "recommended_workflow",
            "approved_workflow",
            "planning_duration_ms",
            "execution_duration_ms",
            "delegation_status",
            "iteration",
            "decision_number",
            "requested_action",
            "action_status",
            "service_name",
            "remaining_budget",
            "completion_status",
            "completion_reason",
            "elapsed_ms",
            "remaining_ms",
            "stale_excluded",
            "deck_id",
            "batch",
            "slides",
            "model_calls",
            "input_tokens",
            "output_tokens",
        ):
            if hasattr(record, key):
                fields[key] = getattr(record, key)
        if hasattr(record, "error"):
            fields["error"] = record.error
        return json.dumps(fields, separators=(",", ":"))


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
