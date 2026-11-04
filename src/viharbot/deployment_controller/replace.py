"""Replacement updates (ADR-007), as a mixin of the controller.

Part 2 of the controller split. A replacement is reserved durably like a create, preflighted
against the approved target, then written once. With a conditional provider the approved version is
the condition. Without one the update is unconditional under the store lock and is ALWAYS verified
by a read-back: a foreign version, a missing claim or a spec that is not exactly what was written
is ``failed_uncertain`` plus an alert (detection, not prevention; ADR-007).
"""

from __future__ import annotations

from .markers import BOUND_KEY, CLAIM_KEY, bound_entry, claim_entry, parse_claim, verified_bound
from .policy import DeploymentRequest, PolicyDenied, build_spec
from .provider import ProviderError, ProviderTimeout, VersionConflict


class ReplaceMixin:
    """Expects the DeploymentController's provider, policy, clock and helper methods."""

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

    def _replace(self, appr, dep, req, claim, request_id, caller, started) -> dict:
        target, ctx = req.replaces, dict(request_id=request_id, caller=caller, started=started)
        dep["app_id"] = target.app_id
        markers = {
            CLAIM_KEY: claim_entry(self._key, claim),
            BOUND_KEY: bound_entry(self._key, claim, target.app_id),
        }
        spec = build_spec(req, markers)
        conditional = self.provider.conditional_update
        if self._clock() >= claim.expires_at:  # immediately before the write
            return self._uncertain(appr, dep, "claim_expired", "bind_refused:claim_expired", **ctx)
        read_back = False  # True when `result` came from a read, not from the update response
        try:
            result = self.provider.update_app(
                target.app_id, spec, target.expected_version if conditional else None)
        except VersionConflict:
            return self._finish(appr, dep, "failed", reason="version_conflict", **ctx)
        except ProviderTimeout:
            read_back, result = True, self._read(target.app_id)
        except ProviderError:
            return self._finish(appr, dep, "failed", reason="provider_error_on_update", **ctx)
        else:
            if not conditional:  # the response alone is not evidence: nothing prevented a writer
                read_back, result = True, self._read(target.app_id)
        if result is not None and (
            result.app_id != target.app_id or not result.team_id or result.team_id != self.team_id
        ):
            self._alert("replace_failed_uncertain:foreign_team", request_id=request_id, dep=dep)
            return self._finish(appr, dep, "failed_uncertain", reason="foreign_team", **ctx)
        landed = result is not None and (c := parse_claim(self._key, result.env)) is not None \
            and c.claim_id == claim.claim_id
        explained = result is not None and (not read_back or result.spec == spec)
        if not landed or not explained or result.version != target.expected_version + 1:
            reason = "unexpected_writer" if landed else "update_outcome_unknown"
            self._alert("replace_failed_uncertain:" + reason, request_id=request_id, dep=dep)
            return self._finish(appr, dep, "failed_uncertain", reason=reason, **ctx)
        self._supersede(dep)
        return self._finish(appr, dep, "running", approval_state="executed", **ctx)

    def _read(self, app_id: str):
        """The app as it is now, or None if it cannot be read (an unverifiable outcome)."""
        try:
            return self.provider.get_app(app_id)
        except ProviderError:
            return None
