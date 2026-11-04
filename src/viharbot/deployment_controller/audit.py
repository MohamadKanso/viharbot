"""Structured audit events (ADR-007 decision 8). Only named fields are ever written: never the
token, environment values or request bodies."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

FIELDS = (
    "request_id", "caller", "reason", "deployment_id", "app_id", "image_digest", "size", "region",
    "ttl_minutes", "provider_status", "duration_ms", "error", "approver", "request_hash",
)
logger = logging.getLogger("deployment_controller")


class AuditLog:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def emit(self, action: str, decision: str, **fields: object) -> dict:
        unknown = set(fields) - set(FIELDS)
        if unknown:
            raise ValueError(f"unknown audit fields: {sorted(unknown)}")
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "decision": decision,
            **{name: fields.get(name) for name in FIELDS},
        }
        self.events.append(event)
        log = logger.warning if decision in ("deny", "alert") else logger.info
        log(
            json.dumps(event, sort_keys=True),
            extra={
                "agent": "deployment-controller", "request_id": event["request_id"],
                "decision": decision, "reason": event["reason"], "caller": event["caller"],
            },
        )
        return event
