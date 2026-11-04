"""The controller core: approval state machine, reserve-then-execute, create, bind (ADR-007).

Approval states are ``pending -> reserved(claim_id) -> executed | expired``. A reservation is
durably recorded before any provider call and is never returned to ``pending``. A provider timeout
is an unknown outcome and never produces a second create. Everything runs under
``RecordStore.locked()`` (a file lock plus reload), so only one instance can consume an approval.

Reconciliation and timeout recovery live in ``reconcile.py`` and replacement updates in
``replace.py``, as mixins of the controller below.
"""

from __future__ import annotations

import copy
import hmac
import secrets
import time
from collections import deque
from typing import Callable

from pydantic import ValidationError

from .app_safety import LIVE, UNRESOLVED, AppSafetyMixin
from .audit import AuditLog
from .markers import (
    BOUND_KEY, CLAIM_KEY, Claim, bound_entry, claim_entry, parse_claim, verified_bound,
)
from .policy import (
    DeploymentRequest, Policy, PolicyDenied, build_spec, is_request_hash, request_hash,
)
from .provider import Provider, ProviderApp, ProviderError, ProviderTimeout, VersionConflict
from .reconcile import ReconcileMixin
from .replace import ReplaceMixin
from .store import RecordStore, StoreError

HOUR, DAY = 3600, 86400


class DeploymentController(AppSafetyMixin, ReplaceMixin, ReconcileMixin):
    def __init__(
        self, *, provider: Provider, store: RecordStore, policy: Policy, marker_key: bytes,
        audit: AuditLog | None = None, clock: Callable[[], float] = time.time,
        instance_id: str = "controller-1",
    ) -> None:
        if not marker_key:
            raise ValueError("a marker key is required")
        self.provider, self.store, self.policy = provider, store, policy
        self.audit = audit or AuditLog()
        self.alerts: list[dict] = []
        self.guard_log: deque = deque(maxlen=256)  # recent guard decisions
        self._key, self._clock, self._instance = marker_key, clock, instance_id

    # --- helpers ---
    @property
    def team_id(self) -> str:
        return self.provider.team_id

    def _fields(self, dep: dict | None) -> dict:
        if not dep:
            return {}
        return {
            "deployment_id": dep["deployment_id"], "app_id": dep.get("app_id"),
            "image_digest": dep["image_digest"], "size": dep["size"], "region": dep["region"],
            "ttl_minutes": dep["ttl_minutes"], "request_hash": dep["request_hash"],
        }

    def _refuse(
        self, action: str, reason: str, status: int, *, request_id: str, caller: str | None,
        dep: dict | None = None,
    ) -> PolicyDenied:
        self.audit.emit(
            action, "deny", reason=reason, request_id=request_id, caller=caller,
            **self._fields(dep),
        )
        return PolicyDenied(reason, status)

    def _alert(self, reason: str, *, request_id: str, dep: dict | None = None, **extra) -> None:
        self.alerts.append(
            self.audit.emit(
                "alert", "alert", reason=reason, request_id=request_id, **self._fields(dep),
                **extra,
            )
        )

    def _save(self, **changes) -> None:
        try:
            self.store.commit(**changes)
        except StoreError:
            raise PolicyDenied("store_unavailable", 503) from None

    def _finish(
        self, appr: dict, dep: dict, status: str, *, approval_state: str | None = None,
        reason: str | None = None, request_id: str, caller: str | None, started: float,
        decision: str | None = None, provider_status: str | None = None,
        extra_deployments: dict | None = None,
    ) -> dict:
        if decision is None:  # the audit decision reports the real outcome, never a hopeful one
            decision = {"running": "allow", "failed_uncertain": "alert",
                        "created_unbound": "pending"}.get(status, "deny")
        dep.update(status=status, reason=reason)
        if approval_state:
            appr["state"] = approval_state
        appr["app_id"] = dep.get("app_id")
        self._save(approvals={appr["approval_id"]: appr},
                   deployments={**(extra_deployments or {}), dep["deployment_id"]: dep})
        self.audit.emit(
            "execute", decision, reason=reason or status, request_id=request_id, caller=caller,
            provider_status=provider_status or status, approver=appr.get("approver"),
            duration_ms=round((time.perf_counter() - started) * 1000, 2), **self._fields(dep),
        )
        return dep

    # --- request and approval ---
    def submit(self, payload: dict, *, caller: str, request_id: str) -> dict:
        if caller not in self.policy.requesters:
            raise self._refuse("submit", "caller_not_requester", 403,
                               request_id=request_id, caller=caller)
        try:
            req = DeploymentRequest.model_validate(payload)
            self.policy.check(req)
        except ValidationError:
            raise self._refuse("submit", "invalid_request", 422,
                               request_id=request_id, caller=caller) from None
        except PolicyDenied as denied:
            raise self._refuse("submit", denied.reason, denied.status,
                               request_id=request_id, caller=caller) from None
        digest = request_hash(req, self.team_id)
        with self.store.locked():
            for existing in self.store.all("deployments"):
                if existing["request_id"] == req.request_id:
                    if existing["request_hash"] != digest:
                        raise self._refuse("submit", "request_id_reused", 409,
                                           request_id=request_id, caller=caller)
                    return existing
            now = int(self._clock())
            dep_id, appr_id = "dep_" + secrets.token_hex(8), "apr_" + secrets.token_hex(8)
            dep = {
                "deployment_id": dep_id, "request_id": req.request_id, "approval_id": appr_id,
                "status": "pending_approval", "requester": caller, "name": req.name,
                "image_digest": req.image.digest, "size": req.size, "region": req.region,
                "ttl_minutes": req.ttl_minutes, "request_hash": digest, "app_id": None,
                "expires_at": None, "reason": None,
                "kind": "replace" if req.replaces else "create",
            }
            appr = {
                "approval_id": appr_id, "deployment_id": dep_id, "request": req.model_dump(),
                "request_hash": digest, "requester": caller, "state": "pending",
                "created_at": now, "expires_at": now + self.policy.approval_ttl_seconds,
                "approver": None, "approved_at": None, "claim_id": None, "app_id": None,
                "reason": None,
            }
            self._save(approvals={appr_id: appr}, deployments={dep_id: dep})
            self.audit.emit("submit", "pending", request_id=request_id, caller=caller,
                            **self._fields(dep))
            return dep

    def _expire(self, appr: dict, reason: str) -> None:
        dep = self.store.get("deployments", appr["deployment_id"])
        appr.update(state="expired", reason=reason)
        deployments = {}
        if dep:
            dep.update(status="expired", reason=reason)
            deployments = {dep["deployment_id"]: dep}
        self._save(approvals={appr["approval_id"]: appr}, deployments=deployments)

    def approve(
        self, approval_id: str, *, approver: str, reviewed_hash: object, request_id: str,
    ) -> dict:
        """Accept the exact request whose hash the approver reviewed. Does not execute."""
        refuse = lambda reason, status, dep=None: self._refuse(  # noqa: E731
            "approve", reason, status, request_id=request_id, caller=approver, dep=dep)
        if approver not in self.policy.approvers:
            raise refuse("approver_not_authorized", 403)
        with self.store.locked():
            appr = self.store.get("approvals", approval_id)
            if appr is None:
                raise refuse("unknown_approval", 404)
            dep = self.store.get("deployments", appr["deployment_id"])
            if approver == appr["requester"]:
                raise refuse("self_approval_refused", 403, dep)
            if appr["state"] != "pending" or appr["approver"] not in (None, approver):
                raise refuse("approval_not_pending", 409, dep)
            if self._clock() >= appr["expires_at"]:
                self._expire(appr, "approval_expired")
                raise refuse("approval_expired", 409, dep)
            # raw from the body: refuse anything malformed before compare_digest, here rather
            # than in the API so the refusal is audited with the deployment's fields (#176)
            if not is_request_hash(reviewed_hash):
                raise refuse("invalid_request_hash", 422, dep)
            if not hmac.compare_digest(reviewed_hash.encode(), appr["request_hash"].encode()):
                raise refuse("request_hash_mismatch", 409, dep)
            if appr["approver"]:
                return appr  # same approver, same hash, still pending: a retry after a 429 or 502
            appr.update(approver=approver, approved_at=int(self._clock()))
            self._save(approvals={approval_id: appr})
            self.audit.emit("approve", "allow", request_id=request_id, caller=approver,
                            approver=approver, **self._fields(dep))
            return appr

    # --- execution ---
    def _recent(self, now: float, window: int, kind: str | None = None) -> int:
        actions = self.store.data["actions"]
        return sum(1 for a in actions if a["ts"] > now - window and kind in (None, a["kind"]))

    def execute(self, approval_id: str, *, caller: str, request_id: str) -> dict:
        """Reserve durably, then call the provider."""
        started = time.perf_counter()
        with self.store.locked():
            appr = self.store.get("approvals", approval_id)
            if appr is None:  # also what a wiped store looks like: never read as pending
                raise self._refuse("execute", "unknown_approval", 404,
                                   request_id=request_id, caller=caller)
            dep = self.store.get("deployments", appr["deployment_id"])
            refuse = lambda reason, status: self._refuse(  # noqa: E731
                "execute", reason, status, request_id=request_id, caller=caller, dep=dep)
            if appr["state"] != "pending":
                raise refuse("approval_not_pending", 409)
            now = self._clock()
            if now >= appr["expires_at"]:
                self._expire(appr, "approval_expired")
                raise refuse("approval_expired", 409)
            if not appr["approver"]:
                raise refuse("not_approved", 409)
            req = DeploymentRequest.model_validate(appr["request"])
            if request_hash(req, self.team_id) != appr["request_hash"]:
                self._expire(appr, "request_changed_after_approval")
                raise refuse("request_changed_after_approval", 409)
            try:
                self.policy.check(req)
                if req.replaces:
                    self._preflight(appr, req)
                self._check_quota(req, now)
            except PolicyDenied as denied:
                raise refuse(denied.reason, denied.status) from None
            except ProviderError:
                raise refuse("provider_unavailable", 502) from None
            # The quota read may be slow: take a fresh timestamp and recheck expiry right before
            # the durable reservation, so an approval that expired meanwhile reserves nothing.
            now = self._clock()
            if now >= appr["expires_at"]:
                self._expire(appr, "approval_expired")
                raise refuse("approval_expired", 409)
            claim = Claim(
                secrets.token_hex(16), self._instance, self.team_id, int(now),
                int(now) + req.ttl_minutes * 60, appr["request_hash"],
            )
            appr.update(state="reserved", claim_id=claim.claim_id)
            dep.update(status="reserved", expires_at=claim.expires_at)
            if req.replaces:  # the target is durable before any provider call
                dep["app_id"] = req.replaces.app_id
            action = {"ts": now, "kind": dep["kind"]}
            self._save(approvals={approval_id: appr}, deployments={dep["deployment_id"]: dep},
                       actions=[action])  # durable before any provider call
            if self._clock() >= appr["expires_at"]:  # expired during the durable write
                return self._finish(  # the reservation stays consumed (terminal), never retryable
                    appr, dep, "expired", approval_state="expired", reason="approval_expired",
                    request_id=request_id, caller=caller, started=started, decision="deny")
            args = (appr, dep, req, claim, request_id, caller, started)
            return self._replace(*args) if req.replaces else self._create(*args)

    def _check_quota(self, req: DeploymentRequest, now: float) -> None:
        if self._recent(now, DAY) >= self.policy.max_actions_per_day:
            raise PolicyDenied("daily_quota_exhausted", 429)
        if req.replaces:
            return
        if self._recent(now, HOUR, "create") >= self.policy.max_creates_per_hour:
            raise PolicyDenied("hourly_quota_exhausted", 429)
        live = [a for a in self.provider.list_apps() if a.name.startswith(self.policy.name_prefix)]
        if len(live) >= self.policy.max_concurrent:
            raise PolicyDenied("concurrent_cap_reached", 429)

    def _create(self, appr, dep, req, claim, request_id, caller, started) -> dict:
        spec = build_spec(req, {CLAIM_KEY: claim_entry(self._key, claim)})
        ctx = dict(request_id=request_id, caller=caller, started=started)
        try:
            app = self.provider.create_app(req.name, spec)
        except ProviderTimeout:
            return self._resolve_unknown_create(appr, dep, claim, **ctx)
        except ProviderError:
            return self._finish(appr, dep, "failed", reason="provider_error_on_create", **ctx)
        dep.update(app_id=app.app_id, status="created_unbound")
        appr["app_id"] = app.app_id  # durable before the bind
        self._save(approvals={appr["approval_id"]: appr}, deployments={dep["deployment_id"]: dep})
        return self._bind(appr, dep, app, claim, **ctx)

    def _uncertain(self, appr, dep, reason: str, alert: str, **ctx) -> dict:
        """A bind that cannot be shown safe: never executed, alerted, never retried."""
        self._alert(alert, request_id=ctx["request_id"], dep=dep)
        return self._finish(appr, dep, "failed_uncertain", reason=reason, **ctx)

    def _bind(self, appr, dep, app: ProviderApp, claim: Claim, **ctx) -> dict:
        """Write the bound marker onto the app's CURRENT spec, never over a stale copy.

        A conditional provider gets the just-read version as the condition, so a writer that slipped
        in is refused (never retried). Otherwise the write is verified by read-back and a foreign
        version is failed_uncertain (detection only). The current spec must equal the approved
        template (no normalization is defined). Expiry is checked AFTER the read and right before
        the write, so a slow read cannot carry a bind past expiry; a write already dispatched is
        not undone (dispatch-before-expiry, see the docs).
        """
        try:
            current = self.provider.get_app(app.app_id)
        except ProviderError:
            self._alert("bind_pending", request_id=ctx["request_id"], dep=dep)
            return self._finish(appr, dep, "created_unbound", reason="bind_pending", **ctx)
        decision = self._guard("bind", app.app_id, dep)
        if not decision.allowed:
            return self._uncertain(appr, dep, decision.reason, "bind_refused:" + decision.reason,
                                   **ctx)
        mine = current is not None and (c := parse_claim(self._key, current.env)) is not None \
            and c.claim_id == claim.claim_id
        if not mine:
            return self._uncertain(appr, dep, "unexpected_writer", "bind_failed_uncertain", **ctx)
        if current.app_id != app.app_id or not current.team_id or current.team_id != self.team_id:
            return self._uncertain(appr, dep, "foreign_team", "bind_refused:foreign_team", **ctx)
        approved = build_spec(
            DeploymentRequest.model_validate(appr["request"]),
            {CLAIM_KEY: claim_entry(self._key, claim)})
        if current.spec != approved:  # the claim proves the request, not that the spec is intact
            return self._uncertain(appr, dep, "unexpected_writer",
                                   "bind_failed_uncertain:spec_changed", **ctx)
        spec = copy.deepcopy(current.spec)
        spec["env"][BOUND_KEY] = bound_entry(self._key, claim, app.app_id)
        if self._clock() >= claim.expires_at:  # the last thing before the write: no read follows
            return self._uncertain(appr, dep, "claim_expired", "bind_refused:claim_expired", **ctx)
        conditional = self.provider.conditional_update
        try:
            result = self.provider.update_app(
                app.app_id, spec, current.version if conditional else None)
        except VersionConflict:
            return self._uncertain(appr, dep, "bind_version_conflict",
                                   "bind_failed_uncertain:version_conflict", **ctx)
        except ProviderError:
            self._alert("bind_pending", request_id=ctx["request_id"], dep=dep)
            return self._finish(appr, dep, "created_unbound", reason="bind_pending", **ctx)
        if result.app_id != app.app_id or not result.team_id or result.team_id != self.team_id:
            return self._uncertain(appr, dep, "foreign_team", "bind_failed_uncertain:foreign_team",
                                   **ctx)
        if not conditional:
            try:
                result = self.provider.get_app(app.app_id)
            except ProviderError:
                result = None
            if result is None or result.version != current.version + 1 \
                    or verified_bound(self._key, result, self.team_id) is None:
                return self._uncertain(appr, dep, "unexpected_writer",
                                       "bind_failed_uncertain:unverified", **ctx)
        return self._finish(appr, dep, "running", approval_state="executed", **ctx)


    # --- read, stop, delete ---
    def get(self, deployment_id: str) -> dict:
        dep = self.store.get("deployments", deployment_id)
        if dep is None:
            raise PolicyDenied("unknown_deployment", 404)
        return dep

    def get_approval(self, approval_id: str) -> dict:
        appr = self.store.get("approvals", approval_id)
        if appr is None:
            raise PolicyDenied("unknown_approval", 404)
        return appr

    def _owned_app(self, dep: dict, action: str, caller: str, request_id: str) -> ProviderApp | None:
        """The provider app, only if ownership is verified for its id and team."""
        refuse = lambda reason, status: self._refuse(  # noqa: E731
            action, reason, status, request_id=request_id, caller=caller, dep=dep)
        if not dep.get("app_id"):
            raise refuse("no_app", 409)
        try:
            app = self.provider.get_app(dep["app_id"])
        except ProviderError:
            raise refuse("provider_unavailable", 502) from None
        if app is not None and verified_bound(self._key, app, self.team_id) is None:
            self._alert("ownership_not_verified", request_id=request_id, dep=dep)
            raise refuse("ownership_not_verified", 409)
        return app

    def logs(self, deployment_id: str, lines: int, *, caller: str, request_id: str) -> str:
        with self.store.locked():
            dep = self.get(deployment_id)
            app = self._owned_app(dep, "logs", caller, request_id)
            if app is None:
                raise self._refuse("logs", "app_gone", 404, request_id=request_id,
                                   caller=caller, dep=dep)
            lines = max(1, min(lines, self.policy.max_log_lines))
            try:
                text = self.provider.read_logs(app.app_id, lines)
            except ProviderError:
                raise self._refuse("logs", "provider_unavailable", 502, request_id=request_id,
                                   caller=caller, dep=dep) from None
            self.audit.emit("logs", "allow", request_id=request_id, caller=caller,
                            **self._fields(dep))
            raw = text.encode("utf-8")[-self.policy.max_log_bytes:]
            return raw.decode("utf-8", errors="ignore")  # a split character is dropped

    def destroy(self, deployment_id: str, *, caller: str, request_id: str, keep_record: bool) -> dict:
        """Stop (keep the record) or delete, only for an app with verified ownership."""
        action = "stop" if keep_record else "delete"
        with self.store.locked():
            dep = self.get(deployment_id)
            decision = self._guard(action, dep.get("app_id"), dep)
            if not decision.allowed:
                if decision.reason == UNRESOLVED:
                    self._alert("destroy_refused:unresolved_outcome", request_id=request_id,
                                dep=dep)
                raise self._refuse(action, decision.reason, 409,
                                   request_id=request_id, caller=caller, dep=dep)
            app = self._owned_app(dep, action, caller, request_id)
            try:
                if app is not None:
                    self.provider.destroy_app(app.app_id)
            except ProviderError:
                raise self._refuse(action, "provider_unavailable", 502, request_id=request_id,
                                   caller=caller, dep=dep) from None
            dep["status"] = "stopped"
            changed = {deployment_id: dep}
            for other in self._deps_for(dep["app_id"], deployment_id):  # same app, same fate
                if other["status"] in LIVE:
                    other["status"] = "stopped"
                    changed[other["deployment_id"]] = other
            self._save(deployments=changed,
                       remove_deployments=() if keep_record else (deployment_id,))
            self.audit.emit(action, "allow", request_id=request_id, caller=caller,
                            provider_status="destroyed" if app is not None else "already_gone",
                            **self._fields(dep))
            return dep
