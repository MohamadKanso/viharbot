# Contribution Agreement Acceptances

This is the **public record the `CLA acceptance` check reads**: the agreement's status, its frozen legal-terms version, and one row per accepted account. It contains only what the automated check needs. **It is not the evidence.** Evidence of each acceptance is kept in a private store ([private evidence architecture](docs/governance/cla-private-evidence.md)); a public Markdown file is not private storage and must never hold comment text, emails or personal data. The check reads this file only from the protected `main` branch, never from a pull request's copy. See [CLA acceptance](docs/governance/cla-acceptance.md).

## Status

* CLA status: in-force
* CLA legal-terms version: 1.0
* CLA editorial revision: 1
* CLA text SHA-256: 8e49e4e6632529764659cec94877c7fbb7631114e4a448c8c5473b49ebac9396
* Legal review: waived: founder decision 2026-11-04; no counsel review; risk accepted by the founder
* Founder approval: approved: vchokshi 2026-11-04
* Private evidence storage: verified: private repo, founder-only, Actions off, restore test 2026-11-04; offsite copy pending
* Required ruleset: verified: protect-main live 2026-11-04 (activation-check); up-to-date freshness policy pending founder

**The agreement has not been reviewed by a lawyer.** The founder approved it alone and waived legal review; the check prints that on every run. `in-force` is accepted only when the text is frozen (the hash above must match the agreement file exactly) and every prerequisite line is recorded as complete, waived, approved or verified; otherwise the file is invalid and the check fails closed.

## Acceptances

| GitHub numeric ID | GitHub username at acceptance | Legal-terms version | Effective from (UTC) | Status | Withdrawn from (UTC) | Eligibility | Evidence ID | Evidence SHA-256 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 8943966 | [@vchokshi](https://github.com/vchokshi) | 1.0 | 2026-11-04T22:21:40Z | active | - | adult-individual | EV-8943966-6643628800 | 67f6230c8c25391dee3908deb9bdb4fe4d84f9a80d73193d008dec6d97647129 |
| 91088196 | [@MohamadKanso](https://github.com/MohamadKanso) | 1.0 | 2026-11-05T08:39:26Z | active | - | adult-individual | EV-91088196-6653561487 | c2eeef541ae5068c343c75cbe5497b91f66a8b18190f3253e351c998886e3c88 |

## Rules

- The numeric GitHub ID is the key; the username is what the account was called at acceptance and may differ later.
- **Rows are never deleted or rewritten.** Only `Status` (to `withdrawn`, with a UTC timestamp, never back) and `Eligibility` may change. The check rejects any change that removes a row or edits another column, even from the founder, because deleting a row would erase evidence of a licence grant.
- A row covers the exact legal-terms version it names. A material change (new legal-terms version) needs renewed acceptance; an editorial revision does not.
- `Eligibility`: `adult-individual`, `employee-authorized`, `corporate-cla`, `minor-approved` are covered. `hold-corporate` and `hold-minor` block the contributor until the founder resolves them. `employee-authorized`, `corporate-cla` and `minor-approved` must be backed by authorization held in the private store.
- `Evidence ID` and `Evidence SHA-256` point at, and fingerprint, the private evidence record. The digest lets the founder detect a missing or altered private record; it reveals nothing.
- Withdrawal blocks new contributions only. Merged commits and their grants are unaffected.
