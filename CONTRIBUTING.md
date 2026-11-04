# Contributing to ViharBot

Thank you for your interest in ViharBot. This document says how to propose changes, what happens to them, and what is not yet decided. It describes policy. Where something is not yet in place, it says so.

## Ownership and licensing

ViharBot is personally owned by its founder and is intended to remain proprietary software. It may become a commercial product. The repository is publicly visible so that people can read it, discuss it and propose changes.

Public visibility is not a licence. No open-source licence has been granted, and the repository has no `LICENSE` file on purpose. Nothing here permits copying, modifying, redistributing or using the code commercially, beyond viewing it and forking it on GitHub to prepare a contribution. No one may add a licence file or a licence statement without the founder's explicit authorization.

A copyright notice does not by itself establish who owns a contribution. Rights in contributed code are settled by the contribution agreement described below, not by a header in a file.

This document has not had legal review. It is a statement of intent and process, not a contract or a legal opinion.

## Who decides

The founder holds final authority over architecture, over repository access and over merges. These are separate: approving a design is not approving a merge, and a passing CI run is not approval. The founder may ask for revisions, reject a change or defer a proposal. Nobody merges their own pull request.

## Two contribution paths

### Path A: external contributors

You do not need onboarding or special access.

1. Open or find an issue that describes the problem and the scope you intend. For anything large, ask first.
2. Fork the repository and create a focused branch in your fork.
3. Make and test the change (see [Testing](#testing-and-validation)).
4. Open a pull request against `vchokshi/viharbot:main`. Link the issue and describe what you ran, what it showed and what could go wrong.
5. Respond to review comments.

Your pull request can be reviewed, discussed and merged without completing the onboarding below, subject to code review, the [contribution-rights requirement](#contribution-rights) and the founder's approval. Submitting a pull request does not guarantee a merge, and it does not give you write access to the repository.

### Path B: trusted contributors

Trusted contributors can be given additional repository access. Approved trusted-contributor status is recorded in [CONTRIBUTORS.md](CONTRIBUTORS.md). All three of these are needed:

1. A security acknowledgment.
2. Successful technical qualification (see [Onboarding](#onboarding)).
3. Authorization from the founder.

Completing onboarding does not grant access by itself. The founder decides, and access can be limited to a defined scope of work. A proposed registration row does not confer trusted status; only a founder-approved merge into the current registry establishes registry eligibility. GitHub collaborator/team permissions must be granted separately. Contribution-rights terms for trusted contributors are set when access is authorized; they are not defined in this document yet.

Trusted contributors branch from current `main` in this repository, push to a feature branch and open a pull request. They do not push directly to `main`.

Both paths follow the same rule: **never push directly to `main`.**

The `protect-main` ruleset is **active** on `main`. It is defined in [`.github/rulesets/protect-main.json`](.github/rulesets/protect-main.json). GitHub enforces it: it blocks deletion and force pushes, requires a pull request with one approval and resolved conversations, and requires the `CI result`, `Contributor registry`, `CLA acceptance` and `Dependency review` checks to pass (the ruleset file lists `CLA acceptance`; it is enforced only once the founder applies it, see [CLA acceptance](docs/governance/cla-acceptance.md)). A repository admin may merge their own pull request once those checks pass (bypass mode `pull_request`), but cannot push to `main` directly. The Sage review is advisory and is deliberately **not** a required check.

### Branches, commits and pull requests

* Use a short prefix that matches the repository's existing branches: `feature/`, `fix/`, `docs/` or `refactor/`, followed by a description, for example `docs/issue-30-contributor-governance` or `feature/event-tracing`.
* Keep each pull request to one purpose. Do not mix in unrelated refactors.
* Write commit messages that say what changed and why.
* In the pull request, state the problem, the approach, compatibility impact, any new dependency, and the tests you ran with their results. Say which checks you skipped. Never say a check passed if you did not run it.

## Contribution rights

External contributions will not be merged until the founder has approved a contribution agreement and the contributor has accepted it.

The intended mechanism is a Contributor License Agreement (CLA) that supports possible future commercial distribution and relicensing. **No CLA is in force yet.** A [draft](docs/governance/contributor-license-agreement.md) exists for the founder's review; it has not had legal review and binds no one. Until the founder approves one, external pull requests can be opened, reviewed and discussed, but external code will not be merged. This restriction is temporary and stays in force until the agreement is approved and published. A Developer Certificate of Origin (DCO) sign-off is not required and is not being introduced.

Once an agreement is in force, you accept it by posting the statement it specifies as a comment on your pull request. The founder records your acceptance in [CLA-ACCEPTANCES.md](CLA-ACCEPTANCES.md), and the `CLA acceptance` check recognises you only after that record is merged. While no agreement is in force, the check fails for external authors, which is how this rule is enforced. How it works and what the founder must still do are in [CLA acceptance](docs/governance/cla-acceptance.md).

When the CLA is drafted it is expected to cover these subjects, so you can see what you would be asked to confirm:

* That you own what you contribute, or have the authority to contribute it.
* The rights you grant the project owner.
* Commercial use and distribution.
* Modification and sublicensing.
* Future relicensing.
* Relevant patent rights.
* Provenance of any third-party code and dependencies.
* Intellectual property owned by your employer.

This list says what the agreement is meant to address. It is not the agreement and does not bind anyone.

Whatever the agreement says, only contribute work you are entitled to submit. Identify third-party code and dependencies, their licences and any compatibility concern, and record this in the pull request.

## Onboarding

Onboarding is how someone qualifies for trusted access. It has two parts and is run privately. Ask the founder if you are interested. It is not needed to submit a pull request.

**Part 1: general engineering qualification.** This checks Git proficiency, branching and pull-request workflow, code comprehension, debugging and root-cause analysis, testing and validation, code-review judgment, and security fundamentals. The manual assessment must be done without AI assistance.

**Part 2: ViharBot architectural orientation.** This is an introduction, not an exam. It covers the project's mission and governance; the multi-agent architecture and how the agents collaborate; agent identities and responsibilities; inference routing through the external LiteLLM gateway; security boundaries and handling of sensitive data; architectural change control; and the founder's decision authority. Not every topic is separately graded.

### First Git proficiency assessment: contributor registration

The first assessment is a real pull request proposing the candidate's addition to [CONTRIBUTORS.md](CONTRIBUTORS.md). The candidate must fork the public ViharBot repository, clone their fork, configure `origin` and `upstream`, create an `onboarding/<github-username>` branch, add only their own row with GitHub username and numeric account ID, inspect and commit the change, push it, and open a PR against `vchokshi/viharbot:main`. They must respond to instructor review feedback by updating the same PR and explain the difference between requesting approval and receiving authorization.

The candidate must not use AI during this manual assessment. The instructor separately evaluates Git proficiency, including troubleshooting and recovery in a safe training environment. The founder verifies security acknowledgment, instructor qualification and contribution rights, then decides whether to approve the registry PR. **The candidate's proposed row must never be treated as authorization before a founder-approved merge.** External Path A contributors remain free to submit ordinary PRs without being registered.

### Offboarding trusted contributors

Offboarding requires a founder-approved PR removing the person's row from the current [CONTRIBUTORS.md](CONTRIBUTORS.md), plus **separate** revocation of GitHub collaborator/team access and relevant credentials. Verify both steps and retain evidence privately. Removing a row does not erase Git history or revoke GitHub permissions on its own. What is automated, what is not, the required GitHub settings and the full offboarding steps are in [contributor registry automation](docs/governance/contributor-registry-automation.md). Do not commit personal access records or assessment results to the public repository.

The assessment tasks, answers and grading criteria are private and are not in this repository. Do not ask for them in public issues or pull requests, and do not share them if you have seen them.

## AI-assisted engineering

AI may accelerate engineering work. It must not stand in for engineering competence.

* **During a manual assessment, AI assistance is prohibited.** You must show your own reasoning and work.
* **Otherwise, authorized AI assistance is permitted.** You stay accountable for everything you submit. You must understand it, be able to explain it, review it and test it. Do not submit output you have not read.
* Check AI-assisted output for security problems, copied code and licence or attribution obligations. Do not assume it is free of intellectual-property obligations.
* Do not send credentials, secrets, personal or sensitive data, confidential onboarding material or non-public project information to an AI service that has not been authorized for it.

## Architecture changes

A material change needs a written proposal and the founder's approval before you implement it. This includes changes to:

* LiteLLM routing architecture. All model inference goes through the external LiteLLM project, and ViharBot does not deploy or modify it.
* Agent identities and responsibilities.
* Agent communication and trust boundaries.
* Security architecture.
* The [Founding Charter](docs/governance/founding-charter-v2.md) or the [Agent Constitution](docs/governance/agent-constitution-v1.md).
* Other foundational system design.

Open an issue with the proposal. It should explain:

1. The problem.
2. The proposed solution.
3. Alternatives considered.
4. Tradeoffs.
5. Security and operational impact.
6. How it fits the Founding Charter and Agent Constitution.

Approval to explore an idea is not approval to merge or deploy it. Read the two governance documents before proposing a foundational change. They describe direction; they do not mean every feature in them exists. The Constitution has its own amendment process (Article XII), which a proposal to change it must follow.

## Testing and validation

Run these before asking for review:

```bash
python -m pip install -e ".[test]" ruff==0.17.3
python -m pytest -q
ruff check src tests scripts .github/scripts
```

* The UI tests run the page's script in Node, so Node must be installed.
* A local `.env` can turn feature flags on and make about 15 tests fail. Move it aside when running tests.
* The default suite mocks every model call. Do not run the opt-in live checks (`RUN_LITELLM_INTEGRATION`, `RUN_LITELLM_INFERENCE`, `scripts/fact_check_eval/run.py`) unless the founder has told you to. They make billable calls.
* Never put a real key in code, tests, logs or a pull request.

Continuous integration runs the Python and frontend tests, Ruff, a container build and several security scans on pull requests. See [continuous integration](docs/continuous-integration.md) for what each one does. Passing is necessary and not sufficient: a person still reviews the change.

## Code review

* Reviewers look at correctness, tests, security, compatibility with existing contracts and fit with the architecture.
* Reply to every review comment, either with a change or with a reason.
* Do not bypass or work around CI, review or approval steps.
* Keep to the existing style of the code around your change.

## Reporting a security vulnerability

Do not post a vulnerability, a credential, sensitive data or attack steps in a public issue or pull request.

GitHub private vulnerability reporting is **not enabled** for this repository at the time of writing, and no private security address is published here. Until one is, contact the founder privately, for example through the contact details on the owner's GitHub profile, and say only that you have a security concern. Do not include details until the founder has replied with a private channel. Never include confidential assessment material in a report.

## Not yet decided

These are open and are not resolved by this document: the contribution agreement's final wording and legal review (a draft exists); the repository's licence terms and any notice; contribution terms for trusted contributors; and a private channel for security reports.
