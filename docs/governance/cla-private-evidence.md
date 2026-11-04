# CLA private evidence architecture

Where the detailed evidence of each acceptance is kept, who can read it, and how it is retained, backed up and recovered. **This is a specification. The store does not exist yet and is not verified**; the record says `Private evidence storage: not-verified` until the founder has created it and run the checks below. A public Markdown file is not private storage, and nothing described here may be placed in this repository.

## What is public and what is private

| Public (`CLA-ACCEPTANCES.md`, needed by the check) | Private (evidence store) |
|---|---|
| Numeric GitHub ID, username at acceptance | Everything in the public row, plus: |
| Legal-terms version, effective time | The full comment text and its SHA-256 |
| `active` or `withdrawn`, withdrawal time | The raw GitHub API response and its digest |
| Eligibility class | The verification record: who verified, when, which checks, the result |
| Evidence ID and the evidence SHA-256 | Employer or corporate authorization, minor approvals, any contributor correspondence |
| | Withdrawal notices, founder approval records, the registry pull request number |

The public SHA-256 fingerprints the private evidence record without revealing it, so the founder can detect a missing or altered record.

## Storage mechanism (proposed)

A **separate private GitHub repository** (suggested name `vchokshi/viharbot-cla-evidence`), used only for this:

* Collaborators: the founder only. No teams, no deploy keys, no GitHub Actions, no secrets, no forks, no outside collaborators, no apps. Two-factor or passkey on the founder's account.
* Branch protection on `main`: no force-push, no deletion, signed commits if the founder uses signing.
* Layout: `evidence/EV-<account>-<comment>.json` written once by `verify-comment` (never edited); `approvals/EV-....approval.json` and `withdrawals/EV-....withdrawal.json` added as new files; `corporate/` and `minors/` for authorizations. **Files are append-only by procedure: a correction is a new file that references the old one.**
* Why a repository: history, access control and recoverability without new infrastructure, consistent with the project's "simple isolated storage". The founder may choose another store (an encrypted drive or a managed vault) if it meets every requirement below; the check does not depend on the choice.

## Access permissions

* Read and write: the founder.
* A named **successor or escrow holder** (a person or a company on transfer under agreement section 13) receives access only through a documented, founder-approved handover. Record the handover in the store.
* Nobody else, including contributors. A contributor may ask what is held about them; answer from the store without giving access.
* No credential for this store is placed in any workflow, secret or log. The gate cannot read it by design.

## Evidence record

Each `EV-<account id>-<comment id>.json` is written by `verify-comment` and contains, as one JSON object with an `evidence_sha256` over its canonical form:

`evidence_id`, `repository`, `legal_terms_version`, `frozen_text_sha256`; `comment` (id, url, created and updated times, body, body SHA-256); `account` (numeric ID, login at acceptance, type); `verification` (verified at, verified by, result, checks run, eligibility class, registry pull request, SHA-256 of the raw API response); and alongside it the raw API response.

The registry row's `Evidence SHA-256` is that digest. `audit` recomputes it and compares the row's account, version, effective time and eligibility with the record, so a swapped or edited file fails.

## Retention

Keep each evidence file for as long as the licences it supports can matter, which is effectively **the life of the Project and any Successor's use of the contributions**, and for a period after that which **counsel must set**. Withdrawal does not shorten retention. Deleting evidence on request is a legal question handled case by case by the founder with counsel, because deleting evidence of a grant can harm the owner and the contributor. **[COUNSEL: retention period and data-protection requests.]**

## Backup and recovery

* **Backup:** after every new evidence file, and at least monthly, create an encrypted archive of the whole store (for example with `age` or GPG using a key kept separately from the archive) and keep **two copies in two places**, one offline. Record the archive's SHA-256.
* **Restore test:** at least quarterly and **before activation**, restore the latest archive to a scratch location and run `audit --evidence-dir <restored copy>`. It must report no failures. Record the date and result.
* **Recovery:** if the store is lost or damaged, restore the newest verified archive, run `audit`, and for any row whose evidence is missing or mismatched, mark the contributor's pull requests as blocked (set `Eligibility` to a `hold-` value) until the founder resolves it, and tell counsel. The public row's digest and the live comment, if still intact, are evidence of what was recorded, but they are not a substitute for the store.
* **Key loss:** keep the archive key and a recovery path with the successor or escrow arrangement; an archive that cannot be decrypted is not a backup.

## Verification the founder must complete before activation

1. The store exists, with only the founder as a collaborator (check Settings > Collaborators, no Actions, no apps).
2. A test evidence record was written to it by `verify-comment` and `audit` passes on it.
3. A backup was made and a restore test passed.
4. Access for a successor or escrow holder is documented.

Only then set `Private evidence storage: verified: <date and reference>` in the record. The program cannot verify any of this.
