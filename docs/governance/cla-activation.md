# CLA activation checklist

The ordered steps that take the contribution agreement from draft to `in-force`, what is done, what is blocked and why, and how to back out. **Nothing is activated.** The record says `draft`, and the `in-force` status is rejected by the check unless every prerequisite line in it is recorded as complete. Do not set it early. This checklist is the founder's; no tool completes legal review or private storage for you.

## Live repository settings (read-only, 2026-11-04)

Read with `gh api repos/vchokshi/viharbot/rules/branches/main`. Re-run `cla_gate.py activation-check` for the current state.

* Two active rulesets apply to `main`: **"Ruleset"** (id 26066523) and **`protect-main`** (id 26490874). Together they block deletion and force-push, require a pull request with one approval, and **"Ruleset" requires code-owner review** and a set of checks pinned to the Actions app.
* **`CLA acceptance` is not a required check on GitHub.** `protect-main` requires `CI result`, `Contributor registry` and `Dependency review` only. The checked-in `protect-main.json` now lists `CLA acceptance` (pinned to the Actions app, id 15368), but **a file in the repository enables nothing.** It must be applied in Settings > Rules and then verified live.

## Sequence

| # | Step | Status | Who / how |
|---|---|---|---|
| 1 | Complete the final legal text | **Drafted, not final.** Open items are marked `[COUNSEL]` in the agreement | Founder and counsel |
| 2 | Documented legal review and founder approval | **Blocked: not done** | Counsel's written review recorded as `Legal review: complete: <reference>`; founder as `Founder approval: approved: <reference>` |
| 3 | Freeze and identify the initial binding version | Blocked by 1-2 | Run `cla_gate.py hash-agreement`; record `CLA text SHA-256`, the legal-terms version and editorial revision. Any later text change must follow the version rules |
| 4 | Configure private evidence storage and verify access controls | **Blocked: not created** | [Private evidence architecture](cla-private-evidence.md); then `Private evidence storage: verified: <ref>` |
| 5 | Publish the agreement and acceptance instructions | Written (this PR) | Merge of this PR publishes them; they say the agreement is not yet in force |
| 6 | Test the acceptance workflow against the frozen agreement | Offline tests pass. **Live test blocked** until step 3 and the workflow is on `main` | Use a test account: post the statement, run `verify-comment` and `audit` into the real store, merge a record row, confirm the check passes |
| 7 | Enable and verify the required ruleset and check | **Blocked: see "Circularity" below** | Settings > Rules: add `CLA acceptance`, source GitHub Actions; run `activation-check`; record `Required ruleset: verified: <ref>` |
| 8 | Verify it fails closed for an unaccepted external contribution | **Blocked** until the workflow exists on `main` | Open a pull request from an unaccepted account; confirm `CLA acceptance` fails and the merge button is blocked |
| 9 | Set the status to `in-force` | **Not done** | One founder-approved change setting `CLA status: in-force` once every prerequisite line is complete (the check rejects it otherwise) |
| 10 | Merge pull request #181 | **Not done; do not merge before steps 1-9 unless you choose the two-phase path below** | Founder |

## Circularity, and a decision for the founder

Steps 6-8 need the `CLA acceptance` workflow to be running on pull requests. A `pull_request_target` workflow runs from the **base branch**, so it does not exist until #181 (or something containing it) is merged. Requiring the check first would leave every pull request, including #181, waiting for a check that cannot report. The assignment's sequence therefore cannot be completed in the stated order.

**Recommended (needs founder approval): two phases.**

1. **Phase 1, merge #181 with `CLA status: draft`.** That is safe and already the policy: in draft the check fails every non-exempt contributor. Nothing is activated; the workflow, record and docs go live.
2. **Verify live** (steps 6-8) using the now-running workflow and test accounts, add the required check, and run `activation-check`.
3. **Phase 2: a small activation pull request** records the legal review, approval, frozen hash, storage and ruleset lines and sets `in-force` (step 9).

**As specified (#181 flips to `in-force` itself):** possible only if steps 6-8 are completed some other way first, for example in a private test repository carrying the same workflow, which proves the code but **not** this repository's live protections. The founder's admin bypass could also merge #181 past a required check that cannot yet report; that is exactly the bypass the docs say not to rely on. This conflicts with "verify before activating" and needs your explicit choice.

## Readiness commands

```bash
python3 .github/scripts/cla_gate.py hash-agreement                       # the value for CLA text SHA-256
python3 .github/scripts/cla_gate.py activation-check --repo vchokshi/viharbot
python3 .github/scripts/cla_gate.py audit --repo vchokshi/viharbot --evidence-dir /path/to/restored/store
```

`activation-check` reads the live rules (and needs no secret for a public repository). It checks that `CLA acceptance` is a required check pinned to the Actions app, that a pull request with approval and code-owner review is required, that deletion and force-push are blocked, and that the record's prerequisites are recorded. It lists legal review, private storage, a real negative test and the bypass decision as **MANUAL**, and exits non-zero until the automated ones pass. **A passing run is not proof that protection is live for every pull request; the negative test in step 8 is.**

## Migration

* Existing open external pull requests (for example #179) are unaffected until the check is required. Once it runs, they fail while the status is `draft`. After activation, each contributor posts the statement and the founder records it (see [CLA acceptance](cla-acceptance.md)); re-run the check.
* A pull request opened before a contributor withdraws is admitted only by a one-pull-request exception.
* The initial exceptions (`dependabot[bot]`, `web-flow`) are proposed in this PR; the merge is the founder's approval of them.

## Rollback

Choose by what went wrong.

* **The check wrongly blocks legitimate work** (outage, bug, mistaken record): remove `CLA acceptance` from the required checks in Settings > Rules (this unblocks immediately and is reversible), fix the cause, re-add. For one stuck pull request, add a one-pull-request exception instead. Do not edit the workflow from a pull request; only the founder changes it.
* **The check wrongly lets something through** (a bug found after activation): set `CLA status: draft` in a founder pull request. Draft fails every non-exempt contributor, which is the safe direction, and then fix and test before reactivating. Review what merged in the window using the check's logs.
* **A wrongly recorded acceptance:** do not delete the row. Set it to `withdrawn` (or `hold-corporate`/`hold-minor` for eligibility problems) and add a new correcting evidence file in the private store.
* **The whole change should be undone:** revert #181. That removes the workflow, record, exceptions and docs; the ruleset change in GitHub must be reverted by hand, and the history of any recorded acceptances and the private store are kept.
* **If the private store is lost:** see its recovery section; hold affected contributors until restored.
