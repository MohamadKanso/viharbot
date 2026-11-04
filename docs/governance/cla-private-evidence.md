# CLA private evidence architecture

Where the detailed evidence of each acceptance is kept, who can read it, and how it is retained, backed up and recovered. **The store exists** as the private repository `vchokshi/viharbot-cla-evidence` (created 2026-11-04); what was verified, and the remaining gaps, are in the [activation record](cla-activation.md). A public Markdown file is not private storage, and nothing described here may be placed in the public repository.

## Why private, not public

Each acceptance comment is already public on GitHub, so the comment itself is not secret. The store is private because of what else belongs in it, and because public does not buy integrity:

* **It holds personal and confidential material beyond the comment:** the raw API response for the account, employer or corporate authorizations (an employer's position on who owns work), approvals for minors (a child-safety and privacy matter), withdrawal notices and any correspondence with contributors. Publishing those can harm the people involved and would expose the Owner to data-protection duties.
* **A public store would not make it tamper-proof.** Integrity comes from the append-only rules and the digests in the public record, which anyone can see without seeing the evidence.
* **The public record already gives outsiders what they need to check:** who has accepted which version, since when, and a fingerprint of the evidence. Transparency about the agreement itself is complete; the evidence behind each person is a record about that person.
* The founder's decision 10 requires it: minimal public registry, detailed evidence kept in a private system.

If the founder later wants a more open record, the safe step is to publish only the verified comment text and its digest, never the rest, and only after counsel has considered data-protection rules.

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

## Storage mechanism

A **separate private GitHub repository**, `vchokshi/viharbot-cla-evidence`, used only for this. Its local working copy is `~/viharbot-cla-evidence`, outside the project checkout.

**Recording procedure.** `verify-comment` refuses to write inside **any** git checkout, including this one, so that evidence can never land in a public repository by accident. It therefore writes to a **staging directory outside every checkout**, and you copy the verified file into the private repository yourself:

```bash
mkdir -p ~/.viharbot/cla-staging
python3 .github/scripts/cla_gate.py verify-comment ... --evidence-out ~/.viharbot/cla-staging
cp ~/.viharbot/cla-staging/EV-<account>-<comment>.json ~/viharbot-cla-evidence/evidence/
# then add, commit and push in ~/viharbot-cla-evidence, and delete the staged copy
```

Run `audit --evidence-dir ~/viharbot-cla-evidence/evidence` after the copy; it fails if the file does not match the public row.

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

## Verification, and what is still open

Done on 2026-11-04: the store is private with only the founder as collaborator; Actions are disabled; there are no webhooks, deploy keys or pending invitations; an append-only ruleset is live; an encrypted archive (AES-256, key file `~/.viharbot/cla-backup.key`, archive in `~/.viharbot/cla-backups/`) was restored to scratch space and compared byte-for-byte with the store.

Still open, and stated in the record: **no offsite or offline second copy** (the key and archive are on the same machine, so this is not yet the two-places backup this document requires); **the force-push rejection was not exercised**; a restore test of a store holding real evidence waits for the first acceptance; and the successor or escrow access arrangement is not yet written. The program cannot verify any of these.
