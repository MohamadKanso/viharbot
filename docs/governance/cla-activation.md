# CLA activation record

What was done to activate the contribution agreement, what was verified and how, what was not, and how to back out. Written 2026-11-04. The record file, not this document, is what the check reads; this explains it.

## Decisions taken by the founder

* **Legal review is waived.** The founder approved the agreement alone. No lawyer has reviewed it. The record says `Legal review: waived`, the agreement says "Not reviewed by counsel", and every check report repeats it. "Known legal risks" at the end of the agreement lists what a lawyer would likely examine.
* The agreement text was frozen at legal-terms version 1.0, editorial revision 1, SHA-256 `8e49e4e6632529764659cec94877c7fbb7631114e4a448c8c5473b49ebac9396` (`cla_gate.py hash-agreement`). The check rejects the record if the text no longer matches.
* The status in the record is `in-force`, set in this pull request. **It takes effect when the pull request is merged**, because the check reads the record from `main`.

## Prerequisites and how each was verified

| Prerequisite | Result | How it was verified, and the limits |
|---|---|---|
| Final text, frozen | Done | Hash recorded; any later edit must raise the version or revision (the check enforces it) |
| Legal review | **Waived** | Founder's decision; not a verification of the text |
| Founder approval | Done | The founder's instruction on 2026-11-04 |
| Private evidence store | **Done, with listed gaps** | Private repository `vchokshi/viharbot-cla-evidence`, read through the API: private, one collaborator (the founder), Actions disabled, no webhooks, no deploy keys, no pending invitations, issues and wiki off. An append-only ruleset (id 26567016: no deletion, no force-push) is live on its `main`; **its rejection of a force-push was not tested** (the test was blocked), so it is confirmed by reading the rules only. Forking cannot be switched off for a personal private repository; only people with access can fork it, and only the founder has access. An encrypted archive was created and restored into scratch space, and the restored tree is byte-identical (`diff -r`). **Gaps:** the archive and its key (`~/.viharbot/`) are on one machine, so there is no offsite or offline second copy yet, and a restore test of a store holding real evidence cannot happen until the first acceptance exists |
| Required ruleset | Done (live) | `protect-main` (ruleset 26490874) was updated through the API to require `CLA acceptance` pinned to the GitHub Actions app, and code-owner review. `cla_gate.py activation-check --repo vchokshi/viharbot` then read the **live** rules and passed every automated line. A second, older ruleset ("Ruleset", id 26066523) already applies and is unchanged |
| Negative test on real data | Done (offline) | The gate was run against the real pull request #179 using the live GitHub API and the post-merge files. It failed #179 as intended and resolved the author to account 91088196 |
| Negative test through GitHub's merge protection | **Not done** | The workflow runs from the base branch and does not exist on `main` until this pull request is merged. A throwaway sandbox repository for a real Actions run was not created (blocked). **The workflow has never run on GitHub Actions.** The first real run is the first pull request after merge |

## What this means for pull requests right now

* **Every open and new pull request now waits for `CLA acceptance`, including this one.** The check cannot report on a pull request until the workflow is on `main`. Merging this pull request therefore needs the founder's admin bypass ("merge without waiting for requirements", ruleset bypass mode `pull_request`). That is the intended use of the bypass here and should not be used for external code.
* **Commit identity.** The check resolves every commit author, committer and co-author to a numeric GitHub ID and fails closed on any it cannot. Commits made on this machine carry the machine-default email `vihar.chokshi@vihar.chokshi-L9KP6LY7M7`, which GitHub does not link to an account, so their author is unresolved. This affects #181 and #184 (checked against the real API). Fix it for future work with `git config --global user.email 8943966+vchokshi@users.noreply.github.com`. Existing commits keep the old email unless their authorship is rewritten, which was not done. After the merge, #184 will fail the check until its commits are fixed or the founder merges with the bypass.
* **AI attribution.** `Co-Authored-By: ... <noreply@anthropic.com>` trailers are excused by logged exception EX-003, which matches that exact address in co-author trailers only.

## Readiness commands

```bash
python3 .github/scripts/cla_gate.py hash-agreement
python3 .github/scripts/cla_gate.py activation-check --repo vchokshi/viharbot
python3 .github/scripts/cla_gate.py audit --repo vchokshi/viharbot --evidence-dir <path>/viharbot-cla-evidence/evidence
```

`activation-check` reads the live rules. A passing run is not proof that protection holds for every pull request; only a pull request from an unaccepted account being blocked is.

## After the merge: verify live (do this first)

1. Open any pull request and confirm a check named `CLA acceptance` appears and reports. If it is missing or errors, **use the rollback below immediately**: a required check that cannot report blocks every pull request.
2. From an account that has not accepted, open a pull request and confirm it fails and cannot merge.
3. Ask a contributor (for example the author of #179) to post the acceptance statement, then record it ([CLA acceptance](cla-acceptance.md)), and confirm the check passes for them.

## Rollback

* **The check blocks legitimate work or cannot report:** remove `CLA acceptance` from the required checks. The pre-change ruleset is saved at `~/.viharbot/ruleset-rollback/protect-main.before.json`, with a ready payload `protect-main.rollback-payload.json`:

  ```bash
  gh api -X PUT repos/vchokshi/viharbot/rulesets/26490874 --input ~/.viharbot/ruleset-rollback/protect-main.rollback-payload.json
  ```

  This also restores the earlier code-owner setting. For one stuck pull request, add a one-pull-request exception instead.
* **The check wrongly lets something through:** set `CLA status: draft` in a founder pull request. Draft fails every non-exempt contributor. Fix, test, then reactivate. Review what merged in the window.
* **A wrongly recorded acceptance:** never delete the row. Set it to `withdrawn` (or `hold-corporate` or `hold-minor`) and add a correcting file to the private store.
* **Undo the whole change:** revert this pull request and restore the ruleset as above. The private store and any recorded acceptances are kept.
* **The private store is lost:** restore the newest archive (`openssl enc -d -aes-256-cbc -pbkdf2 -pass file:<key> -in <archive> | tar -xz`), run `audit`, and hold affected contributors until it passes.

## Still open

* Counsel has not reviewed the agreement (waived, not resolved).
* An offsite or offline second backup copy, and key custody beyond this machine.
* The force-push protection on the evidence store has not been exercised.
* The workflow's first real GitHub Actions run, and the live negative test.
