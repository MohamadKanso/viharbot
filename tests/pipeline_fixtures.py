"""Mocked agent responses shared by the pipeline tests."""

from __future__ import annotations

import copy

RID = "req-1"
FINDINGS = {
    "summary": "s",
    "findings": [{"claim": "c", "evidence": "e", "confidence": "high"}],
    "sources": ["src"],
    "uncertainties": ["u"],
}
ASSESSMENT = {
    "overall_risk": "low",
    "implications": ["i"],
    "recommendations": ["r"],
    "uncertainties": ["u"],
}
RESPONSES = {
    "research": {
        "request_id": RID, "agent": "research", "requested_model_alias": "m-r",
        "findings": FINDINGS,
    },
    "fact-check": {
        "request_id": RID, "agent": "fact-check", "requested_model_alias": "m-f",
        "result": {
            "summary": "s",
            "claims": [{"claim": "c", "status": "mixed_evidence", "rationale": "r", "follow_up": "f"}],
            "limitations": ["l"],
        },
    },
    "source-quality": {
        "request_id": RID, "agent": "source-quality", "requested_model_alias": "m-s",
        "result": {
            "summary": "s",
            "sources": [{"source": "x", "apparent_quality": "unknown", "rationale": "r",
                         "checks_to_perform": ["c"]}],
            "limitations": ["l"],
        },
    },
    "security": {
        "request_id": RID, "agent": "security", "requested_model_alias": "m-sec",
        "assessment": ASSESSMENT,
    },
    "privacy-review": {
        "request_id": RID, "agent": "privacy-review", "requested_model_alias": "m-p",
        "result": {"overall_risk": "low", "sensitive_data_categories": [], "concerns": [],
                   "recommendations": [], "processing_note": "n"},
    },
    "synthesis": {
        "request_id": RID, "agent": "synthesis", "requested_model_alias": "m-y",
        "result": {"answer": "a", "key_points": ["k"], "confidence": "high", "caveats": []},
    },
    "sage": {
        "request_id": RID, "agent": "sage", "requested_model_alias": "m-sg",
        "result": {
            "executive_summary": "Monitoring staff changes trust.",
            "stakeholders": [{"name": "Employees", "interest": "Fair treatment",
                              "potential_impact": "Monitored", "impact_direction": "mixed"}],
            "ethical_considerations": [{"statement": "Proportionality matters.",
                                        "claim_type": "normative_judgment", "evidence_refs": [],
                                        "framework": None}],
            "human_and_organizational_impacts": [],
            "risks_and_tradeoffs": [],
            "recommended_mitigations": [],
            "assumptions_and_uncertainties": ["No staff survey was supplied."],
            "recommendations_for_charlie": ["Weigh monitoring against proportionality."],
        },
    },
}
URLS = {
    "fact_check": "u-f",
    "source_quality": "u-s",
    "privacy_review": "u-p",
    "synthesis": "u-y",
}
ORDER = ["research", "fact-check", "source-quality", "security", "privacy-review", "synthesis"]


def response(destination: str) -> dict:
    return copy.deepcopy(RESPONSES[destination])


class FakeSend:
    """Injected transport that records calls and can corrupt one agent's reply."""

    def __init__(self, overrides: dict | None = None) -> None:
        self.overrides = overrides or {}
        self.calls: list[dict] = []

    def __call__(self, url, payload, request_id, destination):
        self.calls.append({"url": url, "payload": payload, "destination": destination})
        raw = {**response(destination), "request_id": request_id}
        reply = self.overrides.get(destination)
        return reply(raw) if callable(reply) else raw

    @property
    def order(self) -> list[str]:
        return [call["destination"] for call in self.calls]

    def payload(self, destination: str) -> dict:
        return next(c["payload"] for c in self.calls if c["destination"] == destination)
