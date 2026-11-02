"""Static checks on the Sage review workflow: credential isolation and permission boundaries.

These read the YAML as text, so a regression (a head checkout, a wider permission, a review event)
fails CI before the workflow can ever run.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = (ROOT / ".github/workflows/sage-review.yml").read_text()
CODE = re.sub(r"(?m)^\s*#.*$", "", WORKFLOW)  # comments may mention what is forbidden


def test_triggers_are_the_approved_ones():
    assert "pull_request_target:" in CODE and "workflow_dispatch:" in CODE
    assert "types: [opened, synchronize, reopened]" in CODE and "branches: [main]" in CODE


def test_nothing_from_the_pull_request_is_checked_out_or_executed():
    checkouts = re.findall(r"actions/checkout@\S+.*\n(?:\s+with:\n(?:\s+.*\n)*)?", CODE)
    assert len(checkouts) == 1
    assert "github.event.repository.default_branch" in checkouts[0] and "persist-credentials: false" in checkouts[0]
    for forbidden in ("head.sha", "head.ref", "refs/pull", "github.head_ref",
                      "pull_request.merge_commit_sha", "github.event.pull_request.title",
                      "github.event.pull_request.body"):
        assert forbidden not in CODE
    # The only head reference allowed is the same-repo comparison used to withhold the credential.
    assert CODE.count("head.repo.full_name") == 1
    assert len(re.findall(r"\bhead\b", CODE)) == 1  # no other reference to the pull request's head


def test_permissions_are_least_privilege_and_scoped_to_the_job():
    assert re.search(r"(?m)^permissions: \{\}$", CODE)
    job = CODE.split("jobs:")[1]
    block = job.split("permissions:")[1].split("steps:")[0]
    granted = dict(re.findall(r"(\w[\w-]*): (read|write)", block))
    assert granted == {"contents": "read", "pull-requests": "write", "issues": "write", "checks": "read"}
    for never in ("administration", "statuses", "deployments", "actions", "packages", "id-token", "security-events"):
        assert never not in block


def test_the_litellm_credential_is_in_exactly_one_step_and_withheld_from_forks():
    assert CODE.count("secrets.LITELLM_API_KEY") == 1 and len(re.findall(r"secrets\.", CODE)) == 1
    line = next(l for l in CODE.splitlines() if "secrets.LITELLM_API_KEY" in l)
    assert "workflow_dispatch" in line and "head.repo.full_name == github.repository" in line and "|| ''" in line
    env_key = CODE.index("LITELLM_API_KEY:")
    assert CODE.rfind("- name:", 0, env_key) > CODE.rfind("Install Sage", 0, env_key)
    install = CODE.split("Install Sage")[1].split("- name:")[0]
    assert "secrets" not in install and "LITELLM" not in install


def test_untrusted_values_never_reach_a_shell_script_by_interpolation():
    script = CODE.split("run: |")[1]
    assert "${{" not in script
    assert "PR_NUMBER: ${{ github.event.pull_request.number || inputs.pr_number }}" in CODE


def test_workflow_submits_no_review_events_and_is_not_a_gate():
    for forbidden in ("gh pr review", "gh pr merge", "--approve", "--request-changes", "APPROVE", "REQUEST_CHANGES",
                      "actions/github-script", "deploy", "automerge", "merge"):
        assert forbidden not in CODE
    assert re.search(r"(?m)^concurrency:", CODE) and "cancel-in-progress: true" in CODE
    assert "timeout-minutes: 10" in CODE


def test_actions_are_pinned_to_full_commit_shas():
    uses = re.findall(r"uses: (\S+)", CODE)
    assert uses and all(re.fullmatch(r".+@[0-9a-f]{40}", u) for u in uses)


def test_the_workflow_is_not_a_required_check_and_ci_workflows_are_unchanged():
    # Branch protection lives in repository settings, not in files; this guards against someone
    # wiring Sage into the CI workflow's required jobs.
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    assert not re.search(r"sage[-_ ]review|sage_review", ci.lower())
