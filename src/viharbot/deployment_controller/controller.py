"""The controller core: approval state machine, reserve-then-execute, reconciliation (ADR-007).

Approval states are ``pending -> reserved(claim_id) -> executed | expired``. A reservation is
durably recorded before any provider call and is never returned to ``pending``. A provider timeout
is an unknown outcome and never produces a second create. Everything here runs under one lock, so
replacements are serialized, including across controller instances and processes that share the
store file: ``RecordStore.locked()`` takes a file lock and reloads before every read-modify-write,
so only one instance can reserve (consume) a given approval.
"""

from __future__ import annotations

import copy
import hmac
import secrets
import time
from collections import Counter
from typing import Callable

from pydantic import ValidationError

from .audit import AuditLog
from .markers import (
    BOUND_EXPIRED, BOUND_KEY, BOUND_LIVE, CLAIM_KEY, CLAIM_ONLY, Claim, bound_entry, claim_entry,
    classify, parse_claim, verified_bound,
)
from .policy import (
    DeploymentRequest, Policy, PolicyDenied, build_spec, request_hash,
)
from .provider import (
    Provider, ProviderApp, ProviderError, ProviderTimeout, VersionConflict,
)
from .store import RecordStore, StoreError

HOUR, DAY = 3600, 86400
LIVE = ("running", "created_unbound", "reserved")  # a record that stands for its app right now
# A create whose outcome was unknown for one of these reasons can still be resolved later with
# provider-backed evidence. Replacement conflicts and duplicate claims never can.
RECOVERABLE = (
    None, "bind_pending", "reconcile_unavailable", "no_candidate", "no_operation_evidence",
)


class DeploymentController:
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
        self._key, self._clock, self._instance = marker_key, clock, instance_id

    # --- helpers --------------------------------------------------------------------------------

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
        decision: str = "allow", provider_status: str | None = None,
    ) -> dict:
        dep.update(status=status, reason=reason)
        if approval_state:
            appr["state"] = approval_state
        appr["app_id"] = dep.get("app_id")
        self._save(approvals={appr["approval_id"]: appr}, deployments={dep["deployment_id"]: dep})
        self.audit.emit(
            "execute", decision, reason=reason or status, request_id=request_id, caller=caller,
            provider_status=provider_status or status, approver=appr.get("approver"),
            duration_ms=round((time.perf_counter() - started) * 1000, 2), **self._fields(dep),
        )
        return dep

    # --- request and approval -------------------------------------------------------------------

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

    def approve(self, approval_id: str, *, approver: str, reviewed_hash: str, request_id: str) -> dict:
        """An approver accepts the exact request whose hash they reviewed. Does not execute."""
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
            if not hmac.compare_digest(str(reviewed_hash), appr["request_hash"]):
                raise refuse("request_hash_mismatch", 409, dep)
            if appr["approver"]:
                return appr  # same approver, same hash, still pending: a retry after a 429 or 502
            appr.update(approver=approver, approved_at=int(self._clock()))
            self._save(approvals={approval_id: appr})
            self.audit.emit("approve", "allow", request_id=request_id, caller=approver,
                            approver=approver, **self._fields(dep))
            return appr

    # --- execution ------------------------------------------------------------------------------

    def _recent(self, now: float, window: int, kind: str | None = None) -> int:
        actions = self.store.data["actions"]
        return sum(1 for a in actions if a["ts"] > now - window and kind in (None, a["kind"]))

    def execute(self, approval_id: str, *, caller: str, request_id: str) -> dict:
        """Reserve durably, then call the provider. Returns the deployment record."""
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
            claim = Claim(
                secrets.token_hex(16), self._instance, self.team_id, int(now),
                int(now) + req.ttl_minutes * 60, appr["request_hash"],
            )
            appr.update(state="reserved", claim_id=claim.claim_id)
            dep.update(status="reserved", expires_at=claim.expires_at)
            action = {"ts": now, "kind": dep["kind"]}
            self._save(approvals={approval_id: appr}, deployments={dep["deployment_id"]: dep},
                       actions=[action])  # durable before any provider call
            args = (appr, dep, req, claim, request_id, caller, started)
            return self._replace(*args) if req.replaces else self._create(*args)

    def _preflight(self, appr: dict, req: DeploymentRequest) -> None:
        """Recheck the replacement target. Only a preflight: see ADR-007 on the race."""
        target = req.replaces
        current = self.provider.get_app(target.app_id)
        if current is None:
            reason = "target_missing"
        elif verified_bound(self._key, current, self.team_id) is None:
            reason = "target_not_owned"
        elif current.name != req.name:
            reason = "target_name_mismatch"
        elif current.version != target.expected_version:
            reason = "version_changed"
        else:
            return
        self._expire(appr, "preflight_" + reason)
        raise PolicyDenied("replacement_preflight_failed", 409)

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
        appr["app_id"] = app.app_id  # durable before the bind: independent app-id evidence
        self._save(approvals={appr["approval_id"]: appr}, deployments={dep["deployment_id"]: dep})
        return self._bind(appr, dep, app, claim, **ctx)

    def _record_evidence_and_bind(self, appr, dep, app: ProviderApp, claim: Claim, **ctx) -> dict:
        """Durably record the independent app-id evidence, and only then bind (ADR-007)."""
        dep.update(app_id=app.app_id, status="created_unbound", reason=None)
        appr["app_id"] = app.app_id
        self._save(approvals={appr["approval_id"]: appr}, deployments={dep["deployment_id"]: dep})
        return self._bind(appr, dep, app, claim, **ctx)

    def _bind(self, appr, dep, app: ProviderApp, claim: Claim, **ctx) -> dict:
        spec = copy.deepcopy(app.spec)
        spec["env"][BOUND_KEY] = bound_entry(self._key, claim, app.app_id)
        try:
            self.provider.update_app(app.app_id, spec, None)
        except ProviderError:
            self._alert("bind_pending", request_id=ctx["request_id"], dep=dep)
            return self._finish(appr, dep, "created_unbound", reason="bind_pending", **ctx)
        return self._finish(appr, dep, "running", approval_state="executed", **ctx)

    def _resolve_unknown_create(self, appr, dep, claim: Claim, **ctx) -> dict:
        """Timeout: look for the claim. Bind only with provider-backed app-id evidence."""
        try:
            apps = self.provider.list_apps()
        except ProviderError:
            apps = None
        found = [
            a for a in apps or []
            if (c := parse_claim(self._key, a.env)) and c.claim_id == claim.claim_id
        ]
        if len(found) == 1 and self.provider.find_operation(claim.claim_id) == found[0].app_id:
            return self._record_evidence_and_bind(appr, dep, found[0], claim, **ctx)
        reason = (
            "reconcile_unavailable" if apps is None else "duplicate_claims" if len(found) > 1
            else "no_candidate" if not found else "no_operation_evidence"
        )
        self._alert("create_outcome_unknown:" + reason, request_id=ctx["request_id"], dep=dep)
        return self._finish(appr, dep, "failed_uncertain", reason=reason, **ctx)

    def _replace(self, appr, dep, req, claim, request_id, caller, started) -> dict:
        target, ctx = req.replaces, dict(request_id=request_id, caller=caller, started=started)
        dep["app_id"] = target.app_id
        markers = {
            CLAIM_KEY: claim_entry(self._key, claim),
            BOUND_KEY: bound_entry(self._key, claim, target.app_id),
        }
        expected = target.expected_version if self.provider.conditional_update else None
        try:
            result = self.provider.update_app(target.app_id, build_spec(req, markers), expected)
        except VersionConflict:
            return self._finish(appr, dep, "failed", reason="version_conflict", **ctx)
        except ProviderTimeout:
            try:
                result = self.provider.get_app(target.app_id)
            except ProviderError:
                result = None
        except ProviderError:
            return self._finish(appr, dep, "failed", reason="provider_error_on_update", **ctx)
        landed = result is not None and (c := parse_claim(self._key, result.env)) is not None \
            and c.claim_id == claim.claim_id
        if not landed or result.version != target.expected_version + 1:
            reason = "unexpected_writer" if landed else "update_outcome_unknown"
            self._alert("replace_failed_uncertain:" + reason, request_id=request_id, dep=dep)
            return self._finish(appr, dep, "failed_uncertain", reason=reason, **ctx)
        self._supersede(dep)
        return self._finish(appr, dep, "running", approval_state="executed", **ctx)

    # --- read, stop, delete ---------------------------------------------------------------------

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
        """The provider app for a deployment, only if ownership is verified for its id and team."""
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
            return text[-self.policy.max_log_bytes:]

    def destroy(self, deployment_id: str, *, caller: str, request_id: str, keep_record: bool) -> dict:
        """Stop (keep the record) or delete. Only an app with verified ownership is destroyed."""
        action = "stop" if keep_record else "delete"
        with self.store.locked():
            dep = self.get(deployment_id)
            if dep["status"] == "failed_uncertain":
                # An unresolved conflict must not be overwritten by a stop: a person resolves it.
                raise self._refuse(action, "record_not_authoritative", 409,
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
                            provider_status="destroyed", **self._fields(dep))
            return dep

    # --- reconciliation and TTL sweep -----------------------------------------------------------

    def reconcile(self, *, request_id: str = "reconcile") -> list[dict]:
        """Startup reconciliation and the TTL sweep (the same pass). Returns what it did.

        verified bound and expired: destroy. verified bound, live: adopt. A claim-only app gets its
        bind completed only if the durable reservation recorded this exact app id (and the claim is
        not duplicated). Everything else, and anything with no verified marker, is left alone and
        alerted. Nothing is deleted on a name prefix alone.
        """
        with self.store.locked():
            now = self._clock()
            apps = [a for a in self.provider.list_apps()
                    if a.name.startswith(self.policy.name_prefix)]
            seen = Counter(
                c.claim_id for a in apps if (c := parse_claim(self._key, a.env)) is not None
            )
            results = []
            for app in apps:
                kind = classify(self._key, app, self.team_id, now)
                outcome = {"app_id": app.app_id, "classification": kind, "action": "alert"}
                if kind == BOUND_EXPIRED:
                    outcome["action"] = self._sweep_destroy(app, request_id)
                elif kind == BOUND_LIVE:
                    outcome["action"] = self._adopt(app, request_id)
                elif kind == CLAIM_ONLY and self._complete_bind(app, seen, now, request_id):
                    outcome["action"] = "bound"
                else:
                    # A claim-only app is never adopted or deleted, expired or not: alert a person.
                    expired = kind == CLAIM_ONLY and now >= parse_claim(self._key, app.env).expires_at
                    self._alert(f"left_alone:{kind}{':expired' if expired else ''}",
                                request_id=request_id, app_id=app.app_id)
                results.append(outcome)
            known = {a.app_id for a in apps}
            for appr in self.store.all("approvals"):
                if appr["state"] != "reserved":
                    continue
                dep = self.store.get("deployments", appr["deployment_id"])
                if dep and dep["status"] == "failed_uncertain":
                    # Never resolved by the controller: it keeps alerting until a person acts.
                    self._alert("failed_uncertain_unresolved:" + str(dep.get("reason")),
                                request_id=request_id, dep=dep)
                elif appr.get("app_id") not in known:
                    self._alert("reservation_unresolved", request_id=request_id, dep=dep)
            return results

    def _deps_for(self, app_id: str | None, exclude: str | None = None) -> list[dict]:
        return [d for d in self.store.all("deployments")
                if app_id and d.get("app_id") == app_id and d["deployment_id"] != exclude]

    def _find_dep(self, app_id: str) -> dict | None:
        """The authoritative record for an app: the one record that is not history."""
        return next((d for d in self._deps_for(app_id) if d["status"] in LIVE), None)

    def _supersede(self, current: dict) -> None:
        """One app id has one authoritative record. Earlier generations become history."""
        changed = {}
        for old in self._deps_for(current.get("app_id"), current["deployment_id"]):
            if old["status"] in LIVE:
                old.update(status="superseded", reason="superseded_by:" + current["deployment_id"])
                changed[old["deployment_id"]] = old
        if changed:
            self._save(deployments=changed)

    def _sweep_destroy(self, app: ProviderApp, request_id: str) -> str:
        try:
            self.provider.destroy_app(app.app_id)
        except ProviderError:
            self._alert("expired_destroy_failed", request_id=request_id, app_id=app.app_id)
            return "alert"
        changed = {}
        for dep in self._deps_for(app.app_id):  # every record whose app just disappeared
            if dep["status"] in LIVE or dep["status"] == "stopped":
                dep["status"] = "expired"
                changed[dep["deployment_id"]] = dep
        if changed:
            self._save(deployments=changed)
        self.audit.emit("expire_destroy", "allow", request_id=request_id, app_id=app.app_id,
                        provider_status="destroyed")
        return "destroyed"

    def _adopt(self, app: ProviderApp, request_id: str) -> str:
        claim = parse_claim(self._key, app.env)
        dep = self._find_dep(app.app_id)
        if dep is None and not any(d["status"] == "failed_uncertain"
                                   for d in self._deps_for(app.app_id)):
            dep_id = "dep_" + secrets.token_hex(8)
            dep = {
                "deployment_id": dep_id, "request_id": "adopted-" + app.app_id,
                "approval_id": None, "status": "running", "requester": None, "name": app.name,
                "image_digest": (app.spec.get("image") or {}).get("digest", ""),
                "size": app.spec.get("size", ""), "region": app.spec.get("region", ""),
                "ttl_minutes": max(0, (claim.expires_at - claim.created_at) // 60),
                "request_hash": claim.request_hash, "app_id": app.app_id,
                "expires_at": claim.expires_at, "reason": None, "kind": "adopted",
            }
            self._save(deployments={dep_id: dep})
        approvals, deployments = {}, {}
        for appr in self.store.all("approvals"):  # crash after bind, before `executed` was saved
            if appr["state"] != "reserved" or appr["claim_id"] != claim.claim_id:
                continue
            owner = self.store.get("deployments", appr["deployment_id"])
            if owner and owner["status"] == "failed_uncertain":
                continue  # a detected conflict or unknown outcome is never promoted to executed
            if owner and owner["kind"] == "replace" and not self._lineage_ok(appr, app):
                owner.update(status="failed_uncertain", reason="unexpected_writer")
                self._alert("replace_failed_uncertain:unexpected_writer",
                            request_id=request_id, dep=owner)
                deployments[owner["deployment_id"]] = owner
                continue
            appr.update(state="executed", app_id=app.app_id)
            approvals[appr["approval_id"]] = appr
            if owner:
                owner.update(status="running", reason=None, app_id=app.app_id,
                             expires_at=claim.expires_at)
                deployments[owner["deployment_id"]] = owner
        if approvals or deployments:
            self._save(approvals=approvals, deployments=deployments)
            for owner in deployments.values():
                if owner["status"] == "running":
                    self._supersede(owner)
        self.audit.emit("adopt", "allow", request_id=request_id, app_id=app.app_id)
        return "adopted"

    @staticmethod
    def _lineage_ok(appr: dict, app: ProviderApp) -> bool:
        """A replacement landed exactly approved version + 1 (no foreign writer in between)."""
        target = DeploymentRequest.model_validate(appr["request"]).replaces
        return target is not None and app.version == target.expected_version + 1

    def _complete_bind(self, app: ProviderApp, seen: Counter, now: float, request_id: str) -> bool:
        """Bind a claim-only app of an intact reservation, only with independent app-id evidence.

        Evidence is the app id recorded from the create response, or else provider-backed operation
        evidence, which is recorded durably before the bind. An expired claim is never bound.
        """
        claim = parse_claim(self._key, app.env)
        if seen[claim.claim_id] != 1 or now >= claim.expires_at:
            return False  # duplicate claim matches or an expired claim: leave untouched, alert
        for appr in self.store.all("approvals"):
            if not (
                appr["state"] == "reserved" and appr["claim_id"] == claim.claim_id
                and appr["request_hash"] == claim.request_hash
            ):
                continue
            dep = self.store.get("deployments", appr["deployment_id"])
            if dep is None or dep["kind"] != "create" or dep.get("reason") not in RECOVERABLE:
                return False
            ctx = dict(request_id=request_id, caller=None, started=time.perf_counter())
            if appr.get("app_id") == app.app_id:
                evidence = True  # recorded from the create response
            elif appr.get("app_id"):
                return False  # the reservation names another app: this is a copy
            else:
                try:
                    evidence = self.provider.find_operation(claim.claim_id) == app.app_id
                except ProviderError:
                    evidence = False  # lookup unavailable: unresolved, try again next pass
            if not evidence:
                return False
            self._record_evidence_and_bind(appr, dep, app, claim, **ctx)
            return self.store.get("deployments", dep["deployment_id"])["status"] == "running"
        return False
