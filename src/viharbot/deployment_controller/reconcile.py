"""Reconciliation, the TTL sweep and timeout recovery (ADR-007), as a mixin of the controller.

Part 2 of the controller split. ``reconcile`` is both the startup pass and the TTL sweep. A claim
only ever becomes bound with independent app-id evidence: the app id recorded from the create
response, or provider-backed operation evidence (``find_operation``) recorded durably first.
"""

from __future__ import annotations

import secrets
import time
from collections import Counter

from .markers import (
    BOUND_EXPIRED, BOUND_KEY, BOUND_LIVE, CLAIM_KEY, CLAIM_ONLY, Claim, bound_entry, claim_entry,
    classify, parse_claim,
)
from .policy import DeploymentRequest, build_spec
from .provider import ProviderApp, ProviderError

LIVE = ("running", "created_unbound", "reserved")  # a record that stands for its app right now
# A create whose outcome was unknown for one of these reasons can still be resolved later with
# provider-backed evidence. Replacement conflicts and duplicate claims never can.
EXPIRY_UNKNOWN = "expiry_destroy_unknown"  # a destroy that raised, outcome not yet known
RECOVERABLE = (
    None, "bind_pending", "reconcile_unavailable", "no_candidate", "no_operation_evidence",
)


class ReconcileMixin:
    """Expects the DeploymentController's store, provider, policy, clock and helper methods."""

    def _record_evidence_and_bind(self, appr, dep, app: ProviderApp, claim: Claim, **ctx) -> dict:
        """Durably record the independent app-id evidence, and only then bind (ADR-007)."""
        dep.update(app_id=app.app_id, status="created_unbound", reason=None)
        appr["app_id"] = app.app_id
        self._save(approvals={appr["approval_id"]: appr}, deployments={dep["deployment_id"]: dep})
        return self._bind(appr, dep, app, claim, **ctx)


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
        lookup_failed = False
        if len(found) == 1:
            try:
                if self.provider.find_operation(claim.claim_id) == found[0].app_id:
                    return self._record_evidence_and_bind(appr, dep, found[0], claim, **ctx)
            except ProviderError:
                lookup_failed = True  # the reservation stays: no second create, retry on reconcile
        reason = (
            "reconcile_unavailable" if apps is None or lookup_failed
            else "duplicate_claims" if len(found) > 1
            else "no_candidate" if not found else "no_operation_evidence"
        )
        self._alert("create_outcome_unknown:" + reason, request_id=ctx["request_id"], dep=dep)
        return self._finish(appr, dep, "failed_uncertain", reason=reason, **ctx)


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
            self._finish_unknown_expiries(known, request_id)
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

    def _unresolved_for_app(self, app_id: str | None) -> list[dict]:
        """Every record that leaves the app's outcome unresolved: a failed_uncertain record that
        names the app, or a still-reserved replacement that targets it (live or superseded
        records of the same app do not matter: the safeguard is a property of the app)."""
        if not app_id:
            return []
        found = [d for d in self._deps_for(app_id) if d["status"] in ("failed_uncertain", "reserved")]
        for appr in self.store.all("approvals"):
            if appr["state"] != "reserved":
                continue
            target = (appr["request"].get("replaces") or {}).get("app_id")
            dep = self.store.get("deployments", appr["deployment_id"])
            if app_id in (target, appr.get("app_id")) and dep and dep["status"] in (
                "failed_uncertain", "reserved"
            ) and dep not in found:
                found.append(dep)
        return found

    def _find_dep(self, app_id: str) -> dict | None:
        """The authoritative record for an app: the one record that is not history."""
        return next((d for d in self._deps_for(app_id) if d["status"] in LIVE), None)

    def _superseded(self, current: dict) -> dict:
        """One app id has one authoritative record: the earlier generations, as history."""
        changed = {}
        for old in self._deps_for(current.get("app_id"), current["deployment_id"]):
            if old["status"] in LIVE:
                old.update(status="superseded", reason="superseded_by:" + current["deployment_id"])
                changed[old["deployment_id"]] = old
        return changed

    def _sweep_destroy(self, app: ProviderApp, request_id: str) -> str:
        if self._unresolved_for_app(app.app_id):  # a person resolves it first, even past expiry
            self._alert("expired_destroy_skipped:unresolved_outcome", request_id=request_id,
                        app_id=app.app_id)
            return "alert"
        try:  # the listing may be stale: revalidate ownership and expiry right before deleting
            current = self.provider.get_app(app.app_id)
            if current is None or classify(
                self._key, current, self.team_id, self._clock()
            ) != BOUND_EXPIRED:
                self._alert("expired_destroy_skipped:changed", request_id=request_id,
                            app_id=app.app_id)
                return "alert"
        except ProviderError:
            self._alert("expired_destroy_failed", request_id=request_id, app_id=app.app_id)
            return "alert"
        try:
            self.provider.destroy_app(app.app_id)
        except ProviderError:
            return self._destroy_outcome_unknown(app, request_id)
        self._finalize_expired(app.app_id, request_id)
        return "destroyed"

    def _destroy_outcome_unknown(self, app: ProviderApp, request_id: str) -> str:
        """A destroy that raised may still have landed: read the app again, never guess."""
        try:
            gone = self.provider.get_app(app.app_id) is None
        except ProviderError:
            gone = None
        if gone:  # the response was lost but the app is gone
            self._finalize_expired(app.app_id, request_id)
            return "destroyed"
        if gone is None:  # cannot tell: durable uncertain note, finalized only on later evidence
            changed = {}
            for dep in self._deps_for(app.app_id):
                if dep["status"] in LIVE:
                    dep["reason"] = EXPIRY_UNKNOWN
                    changed[dep["deployment_id"]] = dep
            if changed:
                self._save(deployments=changed)
            self._alert("expired_destroy_outcome_unknown", request_id=request_id,
                        app_id=app.app_id)
            return "uncertain"
        self._alert("expired_destroy_failed", request_id=request_id, app_id=app.app_id)
        return "alert"  # still exists: records untouched, retried on the next pass

    def _finalize_expired(self, app_id: str, request_id: str) -> None:
        """The app is confirmed gone: finish every record of it as expired, once, audited."""
        changed = {}
        for dep in self._deps_for(app_id):  # every record whose app just disappeared
            if dep["status"] in LIVE or dep["status"] == "stopped":
                dep.update(status="expired", reason=None)
                changed[dep["deployment_id"]] = dep
        if changed:
            self._save(deployments=changed)
        self.audit.emit("expire_destroy", "allow", request_id=request_id, app_id=app_id,
                        provider_status="destroyed")

    def _finish_unknown_expiries(self, listed: set[str], request_id: str) -> None:
        """A destroy whose outcome was unknown and whose app has left the listing: confirm the
        absence with a direct read before finalizing; any doubt leaves the record alerting."""
        pending = {d["app_id"] for d in self.store.all("deployments")
                   if d["status"] in LIVE and d.get("reason") == EXPIRY_UNKNOWN and d.get("app_id")}
        for app_id in pending - listed:
            try:
                gone = self.provider.get_app(app_id) is None
            except ProviderError:
                gone = False
            if gone:
                self._finalize_expired(app_id, request_id)
            else:
                self._alert("expired_destroy_outcome_unknown", request_id=request_id,
                            app_id=app_id)

    def _adopt(self, app: ProviderApp, request_id: str) -> str:
        claim = parse_claim(self._key, app.env)
        approvals, deployments, handled, rejected = {}, {}, False, False
        for appr in self.store.all("approvals"):  # crash after bind, before `executed` was saved
            if appr["state"] != "reserved" or appr["claim_id"] != claim.claim_id:
                continue
            owner = self.store.get("deployments", appr["deployment_id"])
            if owner and owner["status"] == "failed_uncertain":
                continue  # a detected conflict or unknown outcome is never promoted to executed
            handled = True
            if owner and owner["kind"] == "replace" and not self._replacement_ok(appr, app, claim):
                owner.update(status="failed_uncertain", reason="unexpected_writer")
                self._alert("replace_failed_uncertain:unexpected_writer",
                            request_id=request_id, dep=owner)
                deployments[owner["deployment_id"]] = owner
                rejected = True
                continue
            appr.update(state="executed", app_id=app.app_id)
            approvals[appr["approval_id"]] = appr
            if owner:
                owner.update(status="running", reason=None, app_id=app.app_id,
                             expires_at=claim.expires_at)
                deployments[owner["deployment_id"]] = owner
                deployments.update(self._superseded(owner))
        dep = self._find_dep(app.app_id)
        if dep is None and not handled and not any(
            d["status"] == "failed_uncertain" for d in self._deps_for(app.app_id)
        ):
            dep_id = "dep_" + secrets.token_hex(8)
            deployments[dep_id] = {
                "deployment_id": dep_id, "request_id": "adopted-" + app.app_id,
                "approval_id": None, "status": "running", "requester": None, "name": app.name,
                "image_digest": (app.spec.get("image") or {}).get("digest", ""),
                "size": app.spec.get("size", ""), "region": app.spec.get("region", ""),
                "ttl_minutes": max(0, (claim.expires_at - claim.created_at) // 60),
                "request_hash": claim.request_hash, "app_id": app.app_id,
                "expires_at": claim.expires_at, "reason": None, "kind": "adopted",
            }
        if approvals or deployments:
            self._save(approvals=approvals, deployments=deployments)  # one atomic commit
        # The outcome is what really happened: never "adopted" over a record left uncertain.
        uncertain = any(d["status"] == "failed_uncertain" for d in self._deps_for(app.app_id))
        outcome = "recovery_rejected" if rejected else "left_uncertain" if uncertain else "adopted"
        if outcome == "left_uncertain":
            self._alert("adopt_left_uncertain", request_id=request_id, app_id=app.app_id)
        self.audit.emit("adopt", "allow" if outcome == "adopted" else "deny", reason=outcome,
                        request_id=request_id, app_id=app.app_id)
        return outcome

    def _replacement_ok(self, appr: dict, app: ProviderApp, claim: Claim) -> bool:
        """A replacement landed exactly as approved: version + 1 and exactly the written spec."""
        req = DeploymentRequest.model_validate(appr["request"])
        if req.replaces is None or app.version != req.replaces.expected_version + 1:
            return False
        markers = {
            CLAIM_KEY: claim_entry(self._key, claim),
            BOUND_KEY: bound_entry(self._key, claim, app.app_id),
        }
        return app.spec == build_spec(req, markers)

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
