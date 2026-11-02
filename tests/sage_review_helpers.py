"""Shared builders for the Sage code-review tests. Offline: no model, no network."""

from __future__ import annotations

from types import SimpleNamespace

from viharbot.sage_code_review import (
    ChangedFile,
    CIResult,
    ReviewFinding,
    SageCodeReview,
    SageCodeReviewRequest,
)

BASE = "a" * 40
HEAD = "b" * 40
PATCH = "@@ -1,3 +1,5 @@\n context\n-old\n+new one\n+new two\n context2\n+tail\n"
# New-side lines shown by PATCH: 1 (context), 2, 3 (added), 4 (context), 5 (added).


def changed(path="src/viharbot/example.py", patch=PATCH, **kw) -> ChangedFile:
    return ChangedFile(path=path, status="modified", additions=3, deletions=1, patch=patch, **kw)


def make_request(files=None, **kw) -> SageCodeReviewRequest:
    base = dict(
        repository="vchokshi/viharbot", pr_number=7, base_sha=BASE, head_sha=HEAD,
        title="Add a small helper", description="Adds a helper and a test.",
        files=files if files is not None else [changed()],
        ci_results=[CIResult(name="Python tests", status="completed", conclusion="success")],
    )
    return SageCodeReviewRequest(**{**base, **kw})


def finding(**kw) -> ReviewFinding:
    base = dict(severity="low", category="maintainability", basis="verified_observation",
                description="A small issue.", file_path="src/viharbot/example.py", line_start=2,
                line_end=3, evidence="Lines 2-3 add two values.", remediation="Rename them.")
    return ReviewFinding(**{**base, **kw})


def make_review(**kw) -> SageCodeReview:
    base = dict(
        recommendation="APPROVE", executive_summary="Looks fine.", findings=[],
        architectural_assessment="No change to boundaries.", security_assessment="Nothing found.",
        test_assessment="A test was added.", governance_assessment="Consistent.",
        assumptions_and_uncertainties=[], recommended_next_steps=["Merge when ready."],
        review_completeness="complete",
    )
    return SageCodeReview(**{**base, **kw})


class FakeAgent:
    """Stands in for a Strands agent: records the prompt and returns a scripted outcome."""

    def __init__(self, result=None, error=None, usage=None):
        self.result, self.error, self.usage, self.calls = result, error, usage, []

    def __call__(self, prompt, structured_output_model=None):
        self.calls.append((prompt, structured_output_model))
        if self.error:
            raise self.error
        metrics = SimpleNamespace(accumulated_usage=self.usage) if self.usage is not None else None
        return SimpleNamespace(structured_output=self.result, metrics=metrics)


def factory(agent, builds=None):
    def build(**kwargs):
        if builds is not None:
            builds.append(kwargs)
        return agent
    return build
