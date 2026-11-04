# Contribution Agreement Exceptions

This is the **audit log of exemptions from the `CLA acceptance` check**. The check reads it only from the protected `main` branch. Only a founder-approved merge changes it, and every use of an exemption is printed in the check's report with its reason, authorizer and scope. Git history of this file is the audit trail. See [CLA acceptance](docs/governance/cla-acceptance.md).

## What is exempt without a row

Only the founder's own work, identified by the `Founder` row of [CONTRIBUTORS.md](CONTRIBUTORS.md) (numeric ID). It never excuses another person's commits, co-authors or committers in the same pull request, which are checked individually.

## Exceptions

| Exception ID | GitHub numeric ID | GitHub username | Kind | Scope | Reason | Authorizer | Approved on (UTC) | Expires (UTC) | Reference |
| --- | ---: | --- | --- | --- | --- | --- | --- | --- | --- |
| EX-001 | 49699333 | dependabot[bot] | automation | commits | Dependency update bot configured in .github/dependabot.yml; opens version-bump pull requests the founder reviews | vchokshi | 2026-11-04 | none | https://github.com/vchokshi/viharbot/pull/181 |
| EX-002 | 19864447 | web-flow | automation | commits | GitHub's own committer identity on commits made through the web interface; it adds no authorship | vchokshi | 2026-11-04 | none | https://github.com/vchokshi/viharbot/pull/181 |
| EX-003 | 0 | - | attribution | email:noreply@anthropic.com | AI coding tool attribution trailer, not a person who can accept; the founder stays accountable under agreement section 5 | vchokshi | 2026-11-04 | none | https://github.com/vchokshi/viharbot/pull/181 |

## Rules

- `automation`: a bot or platform identity; scope is `commits`; expiry may be `none`.
- `attribution`: one exact `Co-authored-by` trailer address that is a tool's attribution, not a person (numeric ID `0`, username `-`, scope `email:<address>`). It excuses only an otherwise-unresolvable **co-author trailer** with that address. It never excuses a commit author or committer, and it does not make the tool a contributor: whoever submits AI-assisted work remains accountable for it under the agreement.
- `approved-case`: a person admitted for **one pull request** (scope `pr:<number>`), with a real expiry date, a reference to the GitHub issue or pull request recording the decision, a reason of substance, and the founder as authorizer. It does not waive the agreement's eligibility rules; the founder records minor or corporate approval here with its own review attached in the private store.
- An exception never lets a non-founder change a protected file, and never covers a founder-authored pull request's third-party commits unless that third party has their own exception.
- The founder's approval of EX-001 and EX-002 is the merge of pull request #181; the dates above are the draft's proposal and take effect only on that merge.
