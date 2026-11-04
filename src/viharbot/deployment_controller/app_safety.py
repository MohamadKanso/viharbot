"""The ONE app-safety guard (ADR-007). Every path that acts on an app, or on a record that maps to
one, must call ``_guard`` before any provider mutation; no other code decides these rules.

The guard loads every record that shares the app id or whose reservation targets it, and refuses
when any of them leaves the app's outcome unresolved (``failed_uncertain``, a reservation, an
unknown expiry destroy) or when the record the action goes through has a status that the action
table below does not allow (human-resolved or terminal states are never revived by automation).
"""

from __future__ import annotations

from dataclasses import dataclass

LIVE = ("running", "created_unbound", "reserved")  # a record that stands for its app right now
EXPIRY_UNKNOWN = "expiry_destroy_unknown"  # a destroy that raised, outcome not yet known
# A create whose outcome was unknown for one of these reasons can still be resolved later with
# provider-backed evidence. Replacement conflicts and duplicate claims never can.
RECOVERABLE = (
    None, "bind_pending", "reconcile_unavailable", "no_candidate", "no_operation_evidence",
)
UNRESOLVED, NOT_AUTHORITATIVE = "app_has_unresolved_outcome", "record_not_authoritative"
HOLDS = ("failed_uncertain", "reserved")  # another record: an unresolved outcome or reservation

# action -> statuses of the record the action goes through (None: the app alone, no such record).
# failed, expired and the rest are absent on purpose: a person resolved them, automation must not
# revive them.
ACTIONS = {
    "stop": ("running", "created_unbound", "stopped", "superseded"),
    "delete": ("running", "created_unbound", "stopped", "superseded"),
    "sweep_destroy": None,
    "replace": None,
    "bind": ("created_unbound",),
    "adopt": ("reserved", "created_unbound", "running"),
    "recover_create": ("reserved", "created_unbound", "failed_uncertain"),
    "recover_replace": ("reserved",),
}


@dataclass(frozen=True)
class Decision:
    action: str
    app_id: str | None
    reason: str | None = None  # None: allowed; otherwise the stable refusal code

    @property
    def allowed(self) -> bool:
        return self.reason is None

    def __str__(self) -> str:  # the shared outcome vocabulary of results and audit
        return "allowed" if self.allowed else "refused:" + self.reason


class AppSafetyMixin:
    """Expects the controller's store and ``guard_log``."""

    def _records_of(self, app_id: str | None, exclude: str | None = None) -> list[dict]:
        """Every record of the app: those naming its id, and those whose reservation targets it."""
        if not app_id:
            return []
        found = {d["deployment_id"]: d for d in self.store.all("deployments")
                 if d.get("app_id") == app_id}
        for appr in self.store.all("approvals"):
            if appr["state"] == "reserved" and app_id in (
                (appr["request"].get("replaces") or {}).get("app_id"), appr.get("app_id")
            ) and (dep := self.store.get("deployments", appr["deployment_id"])):
                found[dep["deployment_id"]] = dep
        found.pop(exclude, None)
        return list(found.values())

    def _guard(self, action: str, app_id: str | None, dep: dict | None = None) -> Decision:
        """Allowed, or a refusal with a stable reason. ``dep`` is the record the action goes
        through (never counted as its own blocker). An unknown action raises: no silent allow."""
        statuses = ACTIONS[action]
        others = self._records_of(app_id, dep["deployment_id"] if dep else None)
        reason = None
        if dep is not None and statuses is not None and (
            dep["status"] not in statuses
            or (dep["status"] == "failed_uncertain" and dep.get("reason") not in RECOVERABLE)
        ):
            reason = NOT_AUTHORITATIVE
        elif any(d["status"] in HOLDS for d in others) or (
            action != "sweep_destroy"  # the sweep is what retries an unknown expiry destroy
            and any(d.get("reason") == EXPIRY_UNKNOWN for d in others + ([dep] if dep else []))
        ):
            reason = UNRESOLVED
        decision = Decision(action, app_id, reason)
        self.guard_log.append(decision)
        return decision
