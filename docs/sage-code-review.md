# Sage code review (Phase 7.2A)

> **Status: implemented, advisory only.** Sage can review pull requests to `main` and post one recommendation as a comment. The recommendation cannot merge, block, approve or deploy anything. The repository founder keeps every merge decision. Nothing here has run against a real model or a real pull request yet; see "Not done".

## What it is

`code_review` is a second capability of Sage's existing identity, beside `philosophical_analysis`. It reuses Sage's identity prompt, `build_agent()` and the external LiteLLM gateway. It is a distinct workflow, not a chat delegation: it is absent from Charlie's planner vocabulary and `HARD_DEPENDENCIES`, so the planner cannot ask for it, and Charlie does not declare it. The execution binding is `sage` / `code_review` / `sage-code-review`. That service name is an in-process runner invoked by the GitHub adapter, not a deployed container (the bindings require one service per capability, and `sage` is taken). `IN_PROCESS_CAPABILITIES` keeps it off the team page.

```
GitHub event -> workflow (base branch code) -> sage_review_github (PR fetched as data)
  -> SageCodeReviewRequest -> run_code_review (one LiteLLM call) -> SageCodeReview
  -> finalize_review (evidence guard + decision rules) -> format_comment -> one PR comment
```

Files: `sage_code_review.py` (models, guard, rules, prompt, model call), `sage_review_github.py` (adapter, stdlib `urllib` only), `sage_review_format.py` (comment), `.github/workflows/sage-review.yml`.

## Recommendations are advice

`APPROVE`, `REQUEST_CHANGES` and `COMMENT` appear only as text in the comment. The adapter has no call that submits a GitHub review, sets a status, merges, closes or labels; a test asserts every write is an issue-comment POST or PATCH. The workflow is not a required check and must not be made one in this phase. No branch protection is changed.

## What the guard enforces after the model answers

The model proposes; `finalize_review` decides.

- **Fabricated references.** A finding that cites a file not in the pull request is dropped. A line number the supplied diff does not show (new-side added or context lines) is cleared. Each change is disclosed in the comment.
- **Never approve when unsure.** APPROVE becomes COMMENT when the review is incomplete (files omitted, a diff missing or truncated, or the model says it could not assess everything), when CI has a failure, when the PR changes Sage itself, or when the PR text addresses the reviewer. A failed, timed-out or malformed model call produces a `failed` advisory with `COMMENT`, never an approval. APPROVE with a critical or high finding becomes REQUEST_CHANGES.
- **Self-review.** Changes to `sage*.py`, Sage's identity, prompt and skills files, `bindings.toml`, `execution_bindings.py`, `identity.py`, `sage-review.yml`, this document and `docs/governance/*` mark the review "independent human assessment required" and cap it at COMMENT or REQUEST_CHANGES. The list is `SELF_REVIEW_PATTERNS`.
- **CI claims.** CI results come from GitHub check runs, not from the model. With none supplied, the comment says test outcomes are unverified.
- **Prompt injection.** The title, description, diffs, CI names and policy text go into one escaped JSON data line, and the system prompt calls them untrusted. As a backstop, a small set of phrases aimed at the reviewer (for example "ignore previous instructions", "you must approve") blocks APPROVE and is disclosed. This is a heuristic: it can miss paraphrases and can flag text that merely quotes such a phrase, and it only ever makes the outcome more cautious.
- **Comment safety.** Model text is escaped so it cannot @-mention people, inject HTML or forge the hidden markers.

## Pull request flow

Triggers: `pull_request_target` (opened, synchronize, reopened, base `main`) and a manual `workflow_dispatch` with a PR number and an optional `force`.

- **One comment per PR**, found by a hidden marker and only if the Actions bot wrote it, so someone pasting the marker cannot hijack or suppress the review. It is updated in place and shows the reviewed commit SHA and a timestamp.
- **Idempotent.** A finished review (complete or incomplete) for the same head SHA makes a rerun a no-op with no model call. A failed or skipped one is retried. `force` overrides.
- **Fresh.** When a new commit arrives, the old comment is marked stale ("applies to commit X, new review in progress") before the new review runs, then replaced. A per-PR concurrency group cancels a superseded run.
- **Forks.** Automatic runs skip fork PRs with a short comment and never give the job the LiteLLM key. A founder-run `workflow_dispatch` may review a fork PR; the diff is still only data.
- **Failure.** If the model fails, a `failed` advisory is posted. If the comment itself cannot be posted, the job exits non-zero. Neither affects mergeability. The job summary records the recommendation, completeness, model alias and duration, never review content.

## Configuration

| Setting | Where | Default |
|---|---|---|
| `LITELLM_API_KEY` | repository **secret** | none; the run reports a failed review without it |
| `LITELLM_BASE_URL` | repository variable | `https://litellm.viharchokshi.com` |
| `SAGE_REVIEW_MODEL` | repository variable | `gpt-6.1-sol`; falls back to `SAGE_MODEL` in code. Unverified against `/v1/models` for this use (see #74) |
| `SAGE_REVIEW_TIMEOUT_SECONDS` | env | 120, clamped to 5 to 300 |
| `SageReviewConfig` | request | 40 files, 120,000 diff characters, 24,000 policy characters; ceilings 100 files, 400,000 and 60,000 |

Limits: one model call per review with no retries; output bounded by schema length limits; input bounded by the character limits above (there is no token counter, so the characters are the bound). Over-limit PRs are reviewed in part, reported incomplete and never approved. Token counts are logged when the client reports them; the client reports no cost, so none is logged. Logs carry agent, request ID, model alias, duration, outcome and error type, never prompts, PR text, review text or secrets.

Policy text (`CONTRIBUTING.md` and the `docs/governance` documents, trimmed to the budget with the trim disclosed) is read from the base-branch checkout, never from the pull request.

## Security and permissions

- Pull request content is untrusted. It is read through the API. Nothing from it is checked out, installed, imported or executed.
- **Why `pull_request_target`.** With `pull_request`, GitHub runs the workflow file from the PR's own merge commit, so a same-repository PR could edit this workflow and print the key. `pull_request_target` runs the base branch's workflow and code. It is safe only because the single checkout is pinned to the default branch and no PR ref is used; `tests/test_sage_review_workflow.py` fails if a head or merge ref, an interpolated PR value in a shell script, or a second secret appears. Review any change to that file with the same care.
- **Job permissions** (top-level `permissions: {}`): `contents: read`, `pull-requests: write`, `issues: write` (to post the comment), `checks: read` (to read CI results). Nothing else: no administration, statuses, deployments, actions or contents write.
- The LiteLLM key exists in one step only, and is empty for fork PRs. The API base must be https.
- Sage cannot change its own authorization: the files that define it are in `CODEOWNERS` (`/.github/`, `/docs/governance/`) and in the self-review list. `CODEOWNERS` only requests review; whether it is required depends on the ruleset, which this change does not touch.

## Sample report

An illustrative comment, produced by the real formatter from a scripted review of an invented pull request. It is not output from a live model.

> <!-- sage-code-review:v1 -->
> <!-- sage-reviewed-sha:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb -->
> <!-- sage-status:complete -->
> ## Sage AI Code Review — Advisory Recommendation
> 
> **Recommendation: REQUEST_CHANGES**
> 
> _This is an advisory opinion from an AI reviewer. It is not a GitHub review, not a required check, and it cannot merge, block or approve anything. The repository founder decides._
> 
> Reviewed commit `bbbbbbbbbbbb` · 2026-11-02 12:00 UTC · review completeness: **complete**
> 
> ### Executive summary
> The change lets any request that sets a `trusted` flag skip the per-agent capability check. That weakens the authorization boundary the Agent Constitution relies on, and no test covers the new branch.
> 
> ### Recommended changes
> _Advice only; nothing here is enforced._
> - **HIGH** · security · verified observation `src/viharbot/orchestration_policy.py:12-13`
>   The capability check is now bypassed when `request.trusted` is true. Nothing in the diff shows who can set that field.
>   - Evidence: Lines 12-13 add `and not request.trusted` to the condition that raises Forbidden.
>   - Suggested remediation: Remove the bypass, or derive trust from configuration the request cannot set, and keep the check unconditional.
> 
> ### Nonblocking observations
> - **MEDIUM** · testing · reasoned interpretation
>   No test file changed, so the new branch and the denial path appear unexercised.
>   - Evidence: The supplied file list contains only src/viharbot/orchestration_policy.py.
>   - Suggested remediation: Add a test showing an untrusted request is still denied.
> 
> ### Architectural implications
> The change moves an authorization decision into a request field, which cuts across the explicit capability authorization the architecture describes.
> 
> ### Security considerations
> A caller-controlled flag disables a security control. Treat as a possible authorization bypass until shown otherwise.
> 
> ### Testing observations
> No tests were added. CI reported success for the existing suite only; that says nothing about the new branch.
> 
> ### Governance considerations
> Agent Constitution section 2.4 expects consequential actions to need explicit approval; this removes a check rather than adding one.
> 
> ### Evidence references
> Findings cite files and lines that were checked against the supplied diff. CI results below come from GitHub, not from the model.
> - Python tests: success
> 
> ### Uncertainties
> - I could not see where `request.trusted` is set.
> 
> ### Suggested next steps
> - Restore the unconditional check.
> - Add a regression test for the denial path.
> 
> ---
> Model alias: `SAGE_REVIEW_MODEL alias` · request `00000000-sample`


## Not done

- No real model call has been made and no real pull request reviewed. Tests use mocked inference only. Review quality, false-positive rates and prompt-injection resistance of a real model are unmeasured.
- Setting the `LITELLM_API_KEY` secret and checking the alias against the gateway are manual steps.
- The injection heuristic is a backstop, not a defense on its own.
- The review sees the diff and base-branch policy excerpts, not the rest of the repository, so it cannot judge code it was not shown.
- Inline line comments, a required check and a GitHub review event are intentionally out of scope.
