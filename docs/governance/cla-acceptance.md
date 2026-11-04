# CLA acceptance

This describes how contributors accept the [Contributor License Agreement](contributor-license-agreement.md), how the `CLA acceptance` check gates merges on it, and what the founder must still do by hand. It implements the acceptance workflow and merge gate asked for in issue #56. **No agreement is in force yet**: the agreement is a draft, and the record says so. Read "What is enforced" first.

## What is enforced

| Thing | Enforced by | State |
|---|---|---|
| External code is not merged without an accepted agreement | The `CLA acceptance` check, **once it is a required check** | Written. Not required until the founder applies the ruleset (below) |
| Only the founder changes the agreement or the record | `CODEOWNERS`, plus the check failing a non-founder who edits them | Check enforces it; CODEOWNERS review is binding only if the ruleset requires code-owner review, which `protect-main.json` does not today |
| The agreement's legal sufficiency | Counsel | **Not done.** Nothing here is legal advice |
| Founder approval of a merge | The ruleset's required approval | Existing |

A passing check means every author is covered. It is not a code review and not approval to merge.

## The record

[`CLA-ACCEPTANCES.md`](../../CLA-ACCEPTANCES.md) holds two things: the agreement's **status** (`draft` or `in-force`) and **version**, and a table of acceptances keyed by the contributor's numeric GitHub account ID (usernames can change), the version accepted, the date and a link to the acceptance comment.

The check reads this file only from the protected base branch. A pull request that adds its own row accepts nothing, and the check also fails any non-founder pull request that edits the record or the agreement.

## How a contributor accepts

1. Read the agreement and note the version in force.
2. Post the exact statement from section 8 of the agreement as a comment on your pull request, from your own account.
3. Wait. The comment is a request. The founder records it; you are covered when that record is merged into `main`.

## How the founder records an acceptance

1. Check that the comment is the unedited statement for the version in force, from the person who opened the pull request. The helper does this and prints the row:

   ```bash
   GITHUB_TOKEN=... python3 .github/scripts/cla_gate.py verify-comment \
     --repo vchokshi/viharbot \
     --comment-url "https://github.com/vchokshi/viharbot/pull/N#issuecomment-ID"
   ```

   It refuses if no agreement is in force, if the comment was edited, if the text differs, or if the author is not an ordinary user account. It writes nothing; the token is optional and only raises the rate limit.
2. Open a pull request adding the printed row to the Acceptances table. Review and merge it like any change. Nobody merges their own record.
3. Re-run the contributor's `CLA acceptance` check. It now passes for that account and version.

## How the check decides

On every pull request to `main`, the check takes the pull request author **and every commit author** (a pull request can carry commits from someone else), and requires each to be one of:

* the founder (the `Founder` row of `CONTRIBUTORS.md` on `main`);
* the dependency bot (`dependabot[bot]`);
* an account with a row in the record for the **exact version in force**.

It fails closed. It fails when the record or registry is missing or malformed, when a commit has no linked GitHub account, when the listed commits do not match the pull request's commit count (GitHub returns at most 250), and while the status is `draft`. **Trusted contributors are not exempt**; their terms are not defined yet (CONTRIBUTING).

## Why the workflow is designed this way

It follows the same model as [contributor registry automation](contributor-registry-automation.md): `pull_request_target`, so the workflow, the script and the record come from the base branch and a pull request cannot weaken the check that judges it. The only checkout is the base commit. The pull request's commit authors and changed files are read through the API as data. Permissions are read-only, no secret is used, event data goes through `env:`, untrusted text is neutralised before printing, and the check has no path filter so it can be required without stalling unrelated pull requests. There are no automated tests for it yet, by design of this first draft (the repository guidance is to add tests only when asked); adding them is listed under known limitations.

## Settings the founder must apply (manual, not applied by this change)

1. **Approve an agreement.** Obtain legal review, resolve every **[DECISION: ...]** in the agreement, set a version without `-draft`, and in one founder-approved pull request set the record to `* CLA status: in-force` and `* CLA version: <that version>`. Update CONTRIBUTING's "No CLA exists yet" text in the same pull request.
2. **Require the check.** `.github/rulesets/protect-main.json` lists `CLA acceptance` as a required status check, but that file documents the ruleset; GitHub enforces only what is applied in Settings > Rules. Apply it after the check has run once so GitHub can offer it. Until then the check reports but does not block.
3. **Make the founder's own bypass deliberate.** The bypass actor in `protect-main.json` lets an admin merge once checks pass; a required `CLA acceptance` check still blocks an unaccepted external pull request, but an admin can bypass it. Do not use that for external code.
4. **Consider code-owner review.** `require_code_owner_review` is `false` in the ruleset, so `CODEOWNERS` entries for the record and agreement advise but do not bind. Turn it on if you want them binding.
5. **Negative test.** After applying it, confirm from an account with no acceptance that its pull request cannot merge.

## Known limitations

* **Untested.** Nothing here has run on GitHub, and the script and workflow have no automated tests yet; they were only syntax-checked. Tests should be added before the check is made required. The first pull request after merge is the real test, and before then the check does not exist on `main`.
* The check does not read pull request comments. It trusts the founder-approved record, so an acceptance is only as good as the founder's check of the comment and the helper's tests of it.
* Acceptance by posting a comment is a design choice, not a legal finding. Counsel must confirm it is enough (agreement's open questions).
* Co-authors named only in a commit message trailer are not detected; only the commit's author is. Employer rights and third-party code are confirmed by the contributor, not verified.
* A new version needs new acceptances; the gate matches the exact version.
* Changing the agreement does not alter commits already merged under an earlier version.
