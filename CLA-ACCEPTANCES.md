# Contribution Agreement Acceptances

This is the **public record the `CLA acceptance` check reads**: the agreement's status, its frozen legal-terms version, and one row per accepted account. It contains only what the automated check needs. **It is not the evidence.** Evidence of each acceptance is kept in a private store ([private evidence architecture](docs/governance/cla-private-evidence.md)); a public Markdown file is not private storage and must never hold comment text, emails or personal data. The check reads this file only from the protected `main` branch, never from a pull request's copy. See [CLA acceptance](docs/governance/cla-acceptance.md).

## Status

* CLA status: draft
* CLA legal-terms version: 1.0
* CLA editorial revision: 1
* CLA text SHA-256: unfrozen
* Legal review: pending
* Founder approval: pending
* Private evidence storage: not-verified
* Required ruleset: not-verified

`in-force` is accepted only when the text is frozen and every prerequisite line above is recorded as complete; otherwise the file is invalid and the check fails closed. Nothing here is activated yet. While the status is `draft`, the check fails every contributor who is not exempt.

## Acceptances

| GitHub numeric ID | GitHub username at acceptance | Legal-terms version | Effective from (UTC) | Status | Withdrawn from (UTC) | Eligibility | Evidence ID | Evidence SHA-256 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |

## Rules

- The numeric GitHub ID is the key; the username is what the account was called at acceptance and may differ later.
- **Rows are never deleted or rewritten.** Only `Status` (to `withdrawn`, with a UTC timestamp, never back) and `Eligibility` may change. The check rejects any change that removes a row or edits another column, even from the founder, because deleting a row would erase evidence of a licence grant.
- A row covers the exact legal-terms version it names. A material change (new legal-terms version) needs renewed acceptance; an editorial revision does not.
- `Eligibility`: `adult-individual`, `employee-authorized`, `corporate-cla`, `minor-approved` are covered. `hold-corporate` and `hold-minor` block the contributor until the founder resolves them. `employee-authorized`, `corporate-cla` and `minor-approved` must be backed by authorization held in the private store.
- `Evidence ID` and `Evidence SHA-256` point at, and fingerprint, the private evidence record. The digest lets the founder detect a missing or altered private record; it reveals nothing.
- Withdrawal blocks new contributions only. Merged commits and their grants are unaffected.
