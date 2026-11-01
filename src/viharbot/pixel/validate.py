"""Deterministic checks between model calls. The model never validates its own output."""

from __future__ import annotations

import re

from .models import DeckManifest, Issue, SlideBatch, SlidePayload, SlideStub
from .registry import UnknownLayout, framework_keys, get_layout_spec

DEFAULT_WORD_LIMIT = 15
MONEY_TOLERANCE = 0.005

# Capability and compliance claims the agent must never make. Phrases like "not compliant"
# do not match because the negation sits between the verb and the adjective.
FORBIDDEN = re.compile(
    r"\b(?:is|are|was|were|be|being)\s+(?:fully\s+|already\s+)?(?:compliant|certified|authori[sz]ed)\b"
    r"|\bin\s+compliance\s+with\b|\bachieves?\s+compliance\b",
    re.IGNORECASE,
)
_MONEY = re.compile(r"^\s*\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*([KMB])?\s*$", re.IGNORECASE)
_SCALE = {"": 1.0, "K": 1e3, "M": 1e6, "B": 1e9}


def _issue(slide_id: str, message: str, severity: str = "error") -> Issue:
    return Issue(slide_id=slide_id, severity=severity, message=message)


def _money(text: str) -> float | None:
    match = _MONEY.match(text)
    if not match:
        return None
    return float(match.group(1).replace(",", "")) * _SCALE[(match.group(2) or "").upper()]


def _check_totals(payload: SlidePayload) -> list[Issue]:
    """Roadmap header total must equal the sum of the horizon CAPEX and OPEX figures."""
    if payload.layout_id != "LAYOUT_20_MULTI_YEAR_ROADMAP":
        return []
    keys = [f"HORIZON_{i}_{kind}" for i in (1, 2, 3) for kind in ("CAPEX", "OPEX")]
    parts = [_money(payload.tokens.get(k, "")) for k in keys]
    total = _money(payload.tokens.get("TOTAL_INVESTMENT", ""))
    if total is None or any(p is None for p in parts):
        return [_issue(payload.slide_id, "totals not checked: a figure is missing or not numeric", "warning")]
    expected = sum(p for p in parts if p is not None)
    if abs(expected - total) > max(1.0, expected * MONEY_TOLERANCE):
        return [
            _issue(
                payload.slide_id,
                f"TOTAL_INVESTMENT {total:,.0f} does not equal the sum of horizon figures {expected:,.0f}",
                "warning",
            )
        ]
    return []


def validate_payload(payload: SlidePayload, stub: SlideStub | None = None) -> list[Issue]:
    sid = payload.slide_id
    try:
        spec = get_layout_spec(payload.layout_id)
    except UnknownLayout:
        return [_issue(sid, f"unknown layout {payload.layout_id}")]
    issues: list[Issue] = []
    if stub is not None and stub.layout_id != payload.layout_id:
        issues.append(_issue(sid, f"layout {payload.layout_id} differs from planned {stub.layout_id}"))

    declared = set(spec.tokens)
    given = set(payload.tokens)
    if given - declared:
        issues.append(_issue(sid, f"undeclared tokens: {sorted(given - declared)}"))
    if declared - given:
        issues.append(_issue(sid, f"missing tokens: {sorted(declared - given)}"))

    badges = set(spec.badge_tokens)
    overrides = set(payload.status_overrides)
    if overrides - badges:
        issues.append(_issue(sid, f"undeclared badge overrides: {sorted(overrides - badges)}"))
    if badges - overrides:
        issues.append(_issue(sid, f"missing badge overrides: {sorted(badges - overrides)}"))

    for name, text in payload.tokens.items():
        if not text.strip():
            issues.append(_issue(sid, f"{name} is empty; use '[to be supplied]'"))
        if "{{" in text or "}}" in text:
            issues.append(_issue(sid, f"{name} contains a token delimiter"))
        limit = spec.word_limits.get(name, DEFAULT_WORD_LIMIT)
        if len(text.split()) > limit:
            issues.append(_issue(sid, f"{name} exceeds {limit} words"))
        if FORBIDDEN.search(text):
            issues.append(_issue(sid, f"{name} makes a compliance or certification claim"))

    if payload.claim_status == "implemented" and not payload.evidence_refs:
        issues.append(_issue(sid, "claim_status implemented requires evidence_refs"))
    return issues + _check_totals(payload)


def validate_batch(batch: SlideBatch, expected: list[SlideStub] | None = None) -> list[Issue]:
    issues: list[Issue] = []
    ids = [p.slide_id for p in batch.slides]
    if len(set(ids)) != len(ids):
        issues.append(_issue("batch", "duplicate slide_id in batch"))
    stubs = {s.slide_id: s for s in expected or []}
    if expected is not None:
        if set(ids) - set(stubs):
            issues.append(_issue("batch", f"unplanned slides: {sorted(set(ids) - set(stubs))}"))
        if set(stubs) - set(ids):
            issues.append(_issue("batch", f"missing slides: {sorted(set(stubs) - set(ids))}"))
    for payload in batch.slides:
        issues += validate_payload(payload, stubs.get(payload.slide_id))
        stub = stubs.get(payload.slide_id)
        if stub is not None and not set(payload.evidence_refs) <= set(stub.evidence_refs):
            issues.append(_issue(payload.slide_id, "evidence_refs not in the planned stub"))
    return issues


def validate_manifest(manifest: DeckManifest, allowed_evidence: set[str]) -> list[Issue]:
    issues: list[Issue] = []
    known_frameworks = set(framework_keys())
    seen: set[str] = set()
    for stub in manifest.stubs():
        sid = stub.slide_id
        if sid in seen:
            issues.append(_issue(sid, "duplicate slide_id"))
        seen.add(sid)
        try:
            get_layout_spec(stub.layout_id)
        except UnknownLayout:
            issues.append(_issue(sid, f"unknown layout {stub.layout_id}"))
        for key in stub.frameworks:
            if key not in known_frameworks:
                issues.append(_issue(sid, f"unknown framework key {key}"))
        for ref in stub.evidence_refs:
            if ref not in allowed_evidence:
                issues.append(_issue(sid, f"evidence_ref {ref} was not supplied"))
        if not stub.evidence_refs:
            issues.append(_issue(sid, "no evidence_refs; its claims must be proposed", "warning"))
    if not seen:
        issues.append(_issue("manifest", "manifest has no slides"))
    return issues


def errors(issues: list[Issue]) -> list[Issue]:
    return [i for i in issues if i.severity == "error"]
