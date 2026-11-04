# CLA acceptance

How contributors accept the [Contributor License Agreement](contributor-license-agreement.md), how that acceptance is recorded and audited, and how the `CLA acceptance` check gates merges. Read "What is enforced" first. **The record says `in-force`, which takes effect when the pull request that sets it is merged to `main`. The agreement has not been reviewed by a lawyer; the founder waived legal review.** What was verified before activation, and what was not, is in the [activation record](cla-activation.md); where evidence is kept is in [private evidence architecture](cla-private-evidence.md).

## What is enforced

| Thing | Enforced by | State |
|---|---|---|
| No merge of external code without a recorded acceptance | The `CLA acceptance` check, required by the live `protect-main` ruleset (applied 2026-11-04) | The ruleset is live. **The workflow has never run on GitHub Actions**: it is not on `main` until the merge |
| Only the founder changes the agreement, record, exceptions or enforcement | The check fails a non-founder who touches a protected file; `CODEOWNERS` plus required code-owner review | Check: yes. Code-owner review: required by one live ruleset, and set in `protect-main.json` |
| Acceptances are never deleted or rewritten | The check rejects any change that removes a row or edits an immutable column, even from the founder | Yes, in the check |
| The agreement's legal sufficiency | Counsel | **Not done.** Nothing here is legal advice |
| Founder approval of a merge | The ruleset's required approval | Existing |

A passing check means every contributor is covered. It is not a code review and not approval to merge.

## The three records

* [`CLA-ACCEPTANCES.md`](../../CLA-ACCEPTANCES.md), **public and minimal**: the agreement's status, legal-terms version, editorial revision, frozen text hash, prerequisite lines, and one row per acceptance: numeric ID, username at acceptance, version, effective time, `active`/`withdrawn`, withdrawal time, eligibility, an opaque evidence ID and its SHA-256. Nothing else.
* [`CLA-EXCEPTIONS.md`](../../CLA-EXCEPTIONS.md), **public audit log of exemptions**: automation, and one-pull-request approved cases, each with reason, authorizer, scope, expiry and reference.
* **The private evidence store**, which holds the evidence itself (see [private evidence architecture](cla-private-evidence.md)). A public Markdown file is not private storage and is never used as evidence storage.

The check reads all of these only from the protected `main` branch, never from a pull request's copy.

## How a contributor accepts

1. Read the agreement and note the **legal-terms version** in force.
2. Post the exact statement from section 8 of the agreement as a comment on your pull request, from your own account. It names the version and the repository.
3. Disclose anything that affects eligibility: employer rights, third-party code, or being under 18 (see sections 5 and 6). Do not contribute until the founder has confirmed any needed authorization.
4. Wait. The comment is a **request**. You are covered when the founder-approved record that names you is merged into `main`.

## How the founder records an acceptance

1. Run the verifier. It reads the comment from GitHub and writes the evidence to the **private store** (it refuses to write inside a git checkout):

   ```bash
   GITHUB_TOKEN=... python3 .github/scripts/cla_gate.py verify-comment \
     --repo vchokshi/viharbot --comment-url "https://github.com/vchokshi/viharbot/pull/N#issuecomment-ID" \
     --eligibility adult-individual --verified-by vchokshi --evidence-out /path/to/private/store/evidence
   ```

   It refuses unless: an agreement is `in-force`; the author is an ordinary user account; the text is exactly the statement for the current version; the comment is **unedited since it was posted**; its own URL and repository match.
2. Confirm eligibility yourself (adult, employer authorization, corporate agreement) and store that authorization in the private store. Choose `--eligibility` accordingly; there is no default.
3. Open a pull request adding the printed row. It contains only the columns the check needs. Review and merge it. Nobody merges their own record.
4. Run `audit` (below) and re-run the contributor's check.

## What verification does and does not guarantee

* **At verification** the program checks the points listed above and stores the comment text, its SHA-256 and the raw API response in the private evidence file, fingerprinted by the digest in the public row.
* **After verification, the check does not look at the comment.** It reads only the public record. So if the contributor **edits or deletes the comment later, the recorded acceptance and the check's result do not change**, and the agreement's section 8 says the private snapshot of what was verified governs. This is a design choice, not a stronger guarantee: GitHub comments are editable by their author and the evidence of what they said at verification is the stored snapshot.
* **`audit` makes drift visible.** For every row it checks the private evidence exists and matches the public digest and the row's fields, then fetches the live comment: a deleted comment or an edited body is a **warning** (the grant stands on the snapshot); missing or mismatched private evidence is a **failure**. It writes nothing. Run it on a schedule you choose and before activation.
* The program cannot prove the person is who the account says, that they are adult, or that an employer agreed. Those are the contributor's confirmations and the founder's checks.

## Withdrawal

A contributor may withdraw from **future** participation by notice (agreement sections 10 and 12). The founder then:

1. Records the notice in the private store (a new file; evidence is never edited).
2. Opens a pull request that changes that row's `Status` to `withdrawn` and sets `Withdrawn from (UTC)` to the time the notice was recorded. The check allows exactly this change, and refuses to remove the row, edit its other columns, or reverse a withdrawal.
3. From merge, the check **blocks every new pull request** by that account. A pull request already open can be admitted only by a founder-approved exception for that one pull request.

Merged commits and the licences for contributions submitted before the withdrawal are unaffected. Deleting a row is never a withdrawal, and the check rejects it.

## How the check decides

On each pull request to `main`, for **every identifiable contributor**, it looks at the pull request author, each commit's author and committer, and each `Co-authored-by` trailer. A contributor passes if they are:

* the founder (the `Founder` row of `CONTRIBUTORS.md` on `main`), for the founder's own work only;
* covered by a **row in `CLA-EXCEPTIONS.md`** that is unexpired, in scope for this pull request, and authorized by the founder; each use is printed in the report as an audit log;
* an individual user account with an `active` row for the **current legal-terms version** and an eligible `Eligibility`.

Everything else fails, including: no acceptance, an older version after a material change, a withdrawn account, `hold-corporate` or `hold-minor`, a bot without an exception, an author or co-author that cannot be resolved to a numeric GitHub ID, a draft status, an invalid record, or an invalid founder registry.

**A founder-authored pull request does not excuse a third party's commits, committers or co-authors.** Co-author trailers resolve only when they use a GitHub noreply address (`<id>+<login>@users.noreply.github.com` directly; `<login>@users.noreply.github.com` through a lookup); any other email is unresolvable and fails.

Identity is by numeric ID. A renamed account keeps its acceptance, and the report notes the change.

**Commit identity.** Commit with an email linked to your GitHub account; GitHub's noreply address (`<id>+<login>@users.noreply.github.com`) works. A commit whose author GitHub cannot link to an account is unresolved and fails the check, for everyone including the founder. The one narrow exception is a logged `attribution` row in `CLA-EXCEPTIONS.md` for an exact `Co-authored-by` trailer address that is a tool's attribution rather than a person (currently `noreply@anthropic.com`); it never excuses a commit author or committer.

**Waived legal review.** The record may say `Legal review: waived: <reason>` only as an explicit founder decision. Every check report then states that the agreement has not been reviewed by counsel.

## API handling

The check reads the pull request's commits and files with the read-only token. It paginates, retries transient errors (5xx, network) with backoff, waits at most 60 seconds for a rate limit and otherwise fails with "re-run", and **fails closed on any GitHub error**. It also fails a pull request it cannot read in full: over 250 commits or 3000 files (GitHub's listing limits), or a list shorter than the count GitHub reports. Re-running fixes transient failures; splitting fixes large pull requests.

## Versions: material and editorial

The record carries a **legal-terms version** (`1.0`) and an **editorial revision** (`1`). Acceptances name the legal-terms version only.

* A **material** change gets a higher legal-terms version. Existing rows no longer match, so every contributor must accept again.
* An **editorial** change gets a higher editorial revision. Rows still match.
* The check enforces the mechanics: the agreement's header must match the record, the record's `CLA text SHA-256` must match the text once frozen, and a changed agreement must raise the version or the revision, never silently. **Classifying a change as editorial is the founder's judgement, and counsel's if in doubt**; the check cannot tell whether a wording change alters a right. Agreement section 9 treats an "editorial" change that does alter a right as material.

## Protected files

Only the founder may change: the record, the exceptions, the agreement, the three CLA governance documents, the gate script and workflow, `protect-main.json`, `CODEOWNERS` and the CLA tests. The check fails any other author who touches them, and `CODEOWNERS` plus the ruleset's code-owner review make the founder's review binding. An exemption from the agreement is not permission to edit enforcement.

## Why the workflow is safe

`pull_request_target` runs the workflow, script, record, exceptions and founder identity from the base branch, so a pull request cannot weaken the check that judges it or accept for itself. The only checkout is the base commit. The pull request's commits and files are read through the API as data, and only the few protected files it changes are fetched as plain text to be parsed. Permissions are read-only, no secret is used, event data goes through `env:`, untrusted text is neutralised before printing, there is no path filter, and the job has no `if:`, `needs` or `continue-on-error`, so it cannot be skipped into a pass. `tests/test_cla_gate.py` asserts these properties.

## Known limitations

* **The workflow has not run on GitHub yet.** The logic has run offline against fixtures and against real pull requests #179, #181 and #184 through the live API, but the workflow file itself first runs on the first pull request after the merge. The ruleset was verified live with `activation-check`; the end-to-end block of an unaccepted pull request still needs its first real test (see the [activation record](cla-activation.md)).
* The check trusts the founder-approved public record and cannot read the private store; `audit` is how the two are compared, and only when the founder runs it.
* Authorship evidence is what GitHub reports: commit authors, committers and noreply trailers. A contributor can be omitted by copying code without attribution; the contributor's section 5 confirmations and review are the safeguard.
* Material-versus-editorial classification is a human judgement (above).
* Acceptance by GitHub comment is a design choice; counsel must confirm it is sufficient.
* The founder's admin bypass in the ruleset can merge past a required check. Do not use it for external code.
