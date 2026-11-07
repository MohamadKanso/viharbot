# Kepler: first-contributor steward on AWS AgentCore (design)

Status: **AWS foundation provisioned; the agent is not built or deployed.** Tracking issue [#198](https://github.com/vchokshi/viharbot/issues/198) is the source of truth. It supersedes the autonomous coding mission in #125 and #127 to #131 (see [Kepler](kepler.md) for that history, which is preserved). Merging or reverting this PR does not change AWS: the IAM objects and foundation resources listed in [aws-lab-change-record.md](aws-lab-change-record.md) already exist in the lab account, and AWS teardown is a separate action (see Rollback). There is **no runtime, no schedule, no alert-writer, no secrets, and no application code** for the steward yet, so everything described below as behavior of the agent is proposed.

**Permanent constraint:** ViharBot does not deploy or operate unsupervised coding agents. Kepler never writes source code, pushes commits or branches, approves or merges pull requests, deploys, changes repository security settings, or grants itself permissions.

## What Kepler does

It watches for first-time contributors and their open pull requests, works out the onboarding steps still outstanding (CLA acceptance and its record, third-party rights disclosure, commit identity, branch freshness, CI and review state), tracks replies and new commits, drafts respectful follow-ups, and alerts the founder. First case: PR #179. Initial mode is **read-only monitoring plus internal alerts**. Contributor-facing text is drafted only and needs founder approval to post.

Status facts come from the GitHub API and the trusted-base `CLA-ACCEPTANCES.md`, by deterministic code, never from a model and never from an unverified comment. PR bodies, comments, commits and repository files are untrusted data and are never treated as instructions.

**No model in the first build.** Drafts are fixed templates filled only with the contributor's GitHub login, the legal-terms version and the exact acceptance statement from the CLA gate. A model may be added later for wording, through the LiteLLM gateway with `gpt-5.4-mini`, but nothing needs it today, which also means the "no Claude, directly or indirectly" rule cannot be broken by this code.

### Assessment core (built, offline; not deployed)

`src/viharbot/steward/` is a pure, read-only core with no AWS and no network of its own. It reuses the repository's own CLA gate (`.github/scripts/cla_gate.py`) by loading and calling it, so Kepler cannot disagree with the merge check about who is covered, and every verdict records a fingerprint of the gate code that produced it. The gate must be present in the runtime image; if it cannot be loaded the steward reports "cannot conclude" and never guesses.

For one pull request it reports six requirements, each `done`, `outstanding`, `needs_human` or `info`: CLA acceptance recorded, third-party rights disclosure, commit identities linked to accounts, branch up to date, automated checks, and human review. It never marks the CLA done from a comment: a valid, unedited acceptance comment from the author is reported as "claimed, awaiting the founder to verify and record it". A reply from the contributor, or CI waiting for the founder to approve a fork's workflows, is `needs_human`, because only the founder can judge them.

Each outstanding or needs-human requirement gets an observation ID built from the repository, PR number, requirement, status and, only where it depends on it, the head commit. The same facts always give the same ID (so a re-poll or a duplicate event never re-alerts), a new push changes the CI, branch and identity IDs but not the CLA one, and a changed status gives a new ID. The drafter never repeats a CLA request that is still fresh (72 hours), sends a reminder listing only what is still outstanding, and produces nothing when the contributor has already replied.

Nothing a contributor writes reaches an assessment, an alert or a draft; only GitHub account logins, which GitHub restricts to letters, digits and hyphens, can surface. GitHub is read through GET requests only.

### Orchestration (built, offline; not deployed)

`poller.run_poll` runs one poll: authenticate, read the CLA record from the base branch pinned to one commit, sweep open first-time PRs (bounded), assess each, and email the founder about what is new. Its rules, all covered by tests:

- **Authentication.** `github_auth` is the only module that sends a non-GET request, and only the token exchange for the reader's own installation. It **refuses a token whose permissions are not exactly the reader's read-only set**, or that reaches more than the one repository, and never uses it. Keys and tokens never appear in an error, a log or a record.
- **Deduplication.** An observation is claimed with a conditional write (`pending`), the email is published, then it is marked `sent`. The runtime may not delete rows, so a claim is never released: a stale `pending` claim can be retaken after 30 minutes, at most `KEPLER_MAX_ALERT_ATTEMPTS - 1` times (default 2 attempts, so one retry). The honest guarantee is **at-least-once with a bounded duplicate count**: if polls keep dying between publishing and marking, each death can cost one duplicate until the budget is spent. Then the observation becomes `exhausted`, is reported once (`alert_exhausted` audit event, `AlertsAbandoned` metric, run `partial`) and is never sent again. A restart, a repeated poll or a duplicate GitHub event sends nothing.
- **Fail closed.** If the state table or the audit log cannot be written before an email, the run stops and sends nothing, because without them there is no dedup and no record. The `alert_intent` event is audited before publishing. If the audit store fails after the email has gone, the email cannot be unsent: the intent has no outcome event, and `uncertain_intents` lists it for reconciliation. An auth, base-read or listing failure also fails the run. One bad pull request does not stop the others: it is audited and the run is `partial`.
- **Bounded.** No GitHub read, claim or email starts after the deadline (default 240 s; reads stop 15 s earlier to leave time to record the end). The worst overrun is one GitHub read already in flight, about 45 s with the configured attempts and rate-limit wait. A run reads at most 25 pull requests, and a metrics outage never fails a poll.
- **Visible.** `PollCompleted` is the heartbeat metric (0 on a failed run), `PollFailed` is 1 unless every step succeeded, and `PRsAssessed` and `AlertsSent` are counts. Logs are JSON with `agent`, `request_id`, `event`, `destination`, `model_alias` (always null: no model), `duration_ms` and `error_code`.
- **Audited.** Every run writes hash-chained events (`poll_started`, `alert_sent` with the gate fingerprint, `pr_failed`, `deadline_reached`, `poll_completed`) to the audit table. The writer refuses unknown fields and any value that is not short and plain, so a contributor's text, a token or a stack trace cannot be recorded by accident. The chain is tamper-evidence, not tamper-proofing (see the audit caveat above).
- **Email.** Fixed wording only: the PR link, the author's login (validated), what is new, where each requirement stands, recommended actions, and a draft clearly marked NOT posted. A single poll sends at most one email per PR.
- **Dry run.** `python -m viharbot.steward [--pr N]` reads and assesses with the reader app and prints what would be emailed, writing nothing (no state, audit row, email or GitHub comment). It is the local proof that the GitHub side works. A live run belongs to the deployed runtime.

AWS is imported in exactly one module (`aws.py`); everything else takes injected clients, which is how the tests run offline against fakes that implement DynamoDB's real conditional-write behaviour.

**Not yet built:** the AgentCore runtime adapter (`/ping` and `/invocations`), the arm64 image, and the IAM change that lets the runtime publish to the alerts topic (`sns:Publish`, a new boundary version and a runtime policy change). Until that IAM change is applied, the runtime role cannot send the email.

## Components

| Component | Role | Notes |
| --- | --- | --- |
| AgentCore Runtime `kepler` | The monitor. Read-only GitHub access. | Container on `linux/arm64`, port 8080, `GET /ping` and `POST /invocations` (see [AgentCore notes](../deploy/agentcore/README.md)). Region `us-west-2`. |
| EventBridge Scheduler rule | Bounded polling trigger, every 6 hours, plus a manual check-now. | AgentCore Runtime is invoked, not scheduled, so something must call it. No webhook receiver in the MVP (it would need a public endpoint). Polling frequency only changes how quickly the founder hears about something; contributor PRs move in hours and days. |
| DynamoDB `viharbot-kepler-state` (exists) | Mutable event state and deduplication keys. | AgentCore sessions have no durable local storage, which is the concrete need that justifies a store. On-demand, TTL on `expires_at`, point-in-time recovery, deletion protection. The runtime may put, get, query and update; it may not delete. |
| DynamoDB `viharbot-kepler-audit` (exists) | Audit records. | A separate table with no TTL, point-in-time recovery and deletion protection. The runtime role may put, get and query, and is explicitly denied update, delete, table changes and TTL changes. **IAM cannot make a table append-only:** `PutItem` can overwrite an item whose key is known, so a compromised runtime could replace a row it can name. Mitigations are the hash-chained audit records (written and verified by code, not yet built), point-in-time recovery, and a planned export to storage with object lock if stronger tamper-evidence is needed. The application's `attribute_not_exists` condition is a code-level guard, not an access control. |
| Comment writer (**Phase 2, later; not built, not registered**) | Posts founder-approved comments on contributor pull requests. | Replaces the earlier "alert writer posting to #198" idea: status goes by email, and the only reason for a GitHub write credential is approved contributor messages. A separate GitHub App, secret, role and small function, gated by a per-draft approval bound to a hash of the exact text; it can only create a comment. GitHub cannot enforce that limit, so the code and the gate must. See [kepler-github-apps.md](kepler-github-apps.md). The internal names (`alert-writer` role, Lambda, secret, log group) stay for now to avoid changing live IAM; they can be renamed when this phase starts. |
| Secrets Manager `viharbot/kepler/*` | Now: `github-app-reader`, then `litellm-api-key`. Phase 2: `github-app-alert-writer`. | Never in source, images or logs. Set by the founder, not by tooling. The GitHub Apps, their exact permissions, registration steps and verification are in [kepler-github-apps.md](kepler-github-apps.md). |
| CloudWatch | Structured JSON logs, a custom `ViharBot/Kepler` namespace, alarms. | Alarms notify by SNS email, so a dead monitor is noticed without depending on the monitor to say so. |
| AWS Budget | Spend alarm filtered by the `project` tag. | Needs the tag activated for cost allocation (see Billing). |
| ECR `viharbot/kepler` (exists) | Image repository. | Immutable tags, scan on push. |
| SNS `viharbot-kepler-alerts`, alarm `viharbot-kepler-poll-failures`, budget `viharbot-kepler-monthly` (exist) | The alert channel to the founder, and out-of-band failure notices. | The founder's email subscription is confirmed. Kepler will publish here only when something needs attention (the runtime role does not yet have `sns:Publish`; that administrator change comes with the runtime build). The alarm watches a metric nothing publishes yet. A heartbeat alarm (no successful poll in 7 hours, one missed 6-hour run plus margin) is deferred until the runtime exists, because it would alarm immediately now. The USD 50 budget covers the whole account until the tags are activated. |
| IAM `viharbot-kepler-runtime` (exists) | The runtime's execution role. | Carries the boundary. Not yet assumed by anything. |
| Log group `/aws/lambda/viharbot-kepler-alert-writer` (exists) | Alert writer's logs. | 30-day retention. AgentCore's own log group name depends on the runtime ID, so its retention is set when the runtime is created. |

**Alerts and status.** Alerts go to the founder by email through SNS, only when something needs attention, keyed by an observation ID recorded in the state table so a duplicate GitHub event or a re-poll cannot produce a duplicate message. They are not posted to GitHub: the repository is public, and a status note would need a GitHub write credential. Current status per contributor PR is read on demand from the state table (script planned, not built). Contributor-facing comments are drafted only; in the read-only phase the founder posts them by hand. Posting approved comments is a later, separately authorized phase.

## Inference

**Accepted exception (ADR-011), applied to IAM on 2026-11-06 (change record row 12):** Kepler may use Bedrock through its IAM role, limited to one non-Anthropic model, `meta.llama3-1-8b-instruct-v1:0`, with Anthropic models and every other model denied. The policy files in `deploy/agentcore/kepler/iam/` carry that allowlist, and boundary version 3 and the runtime role policy are live with it (simulation 41 of 41). The code-side allowlist that fails closed is **not built**, no steward code calls a model, and Bedrock model access and price in the account are **unverified**, so no model call has been made. The paragraph below describes the LiteLLM path and the pre-change IAM state; where it says the AWS roles deny model invocation, that was true until row 12.

Inference goes only through the external LiteLLM gateway (`LITELLM_BASE_URL`, its own `LITELLM_API_KEY`, its own alias). The approved alias is `gpt-5.4-mini`, the one Cipher, Charlie and Pixel already use. No Anthropic Claude model, directly or through any provider, until organizational and licensing approval is confirmed. Before row 12 the runtime, deployer and boundary policies explicitly denied `bedrock:InvokeModel` and `bedrock:InvokeModelWithResponseStream` (Converse is authorized under those actions), so Kepler's AWS roles could not call a Bedrock-hosted model; now they can call only the one allowlisted non-Anthropic model. That does **not** control which model the external LiteLLM gateway uses for a key. The rest is **planned and not built**: an alias allowlist in the agent's code that fails closed, and gateway-side restrictions on the Kepler key. Deploying the agent is conditional on verifying both, including that the gateway routes `gpt-5.4-mini` and no Claude-backed alias for that key. LiteLLM itself is not modified.

## Tags and billing

Every taggable resource gets the same five tags, so the whole agent shows up as one line in the billing profile:

| Key | Value |
| --- | --- |
| `project` | `viharbot` |
| `agent` | `kepler` |
| `environment` | `lab` |
| `owner` | the founder |
| `managed-by` | `human-supervised` |

The deployer policy only allows creating an AgentCore runtime that carries `project=viharbot` and `agent=kepler`, so an untagged runtime cannot be created by that role. It can tag, update or delete a runtime only if the runtime **already** carries those tags, so it cannot adopt an unrelated runtime by tagging it. Whether AgentCore authorizes tags supplied at creation without a separate `TagResource` permission is **not yet verified**; the first runtime creation will show it, and the policy will be adjusted by the administrator if needed. The lab account is a member of an AWS Organization, so **activating `project` and `agent` as cost allocation tags must be done by the management account**. Until then they won't appear in Cost Explorer or Budgets filters, and tags only show costs from the day they are activated. This is a founder or Cyera billing task that I cannot do from here.

## Roles and how they are created

1. **You, as administrator, once:** create the permissions-boundary policy `viharbot-kepler-boundary` and the role `viharbot-kepler-deployer` (trust and permissions in `deploy/agentcore/kepler/iam/`). The deployer cannot create or change the boundary, which is why this step is yours.
2. **The deployer role, from then on:** creates everything else. It can create or manage only four explicitly named workload roles (`viharbot-kepler-runtime`, `-scheduler`, `-alert-writer`, and `viharbot-kepler-test-*` for authorization tests), and only with the boundary attached. It can pass only the first three, only to AgentCore, Scheduler and Lambda. It is explicitly denied every change to its own role, including its trust policy, tags, permissions and deletion, and it cannot remove the boundary, create users or keys, or touch other accounts' resources. It cannot rewrite a workload role's trust policy, and it has no resource-policy powers (no `PutResourcePolicy` on secrets, `SetRepositoryPolicy` on ECR, or `AddPermission` on Lambda), so it cannot expose a secret, image or function to another principal. It can create the audit table but is denied altering, restoring or deleting it, so only the administrator can. The first version used a `viharbot-kepler-*` wildcard that also matched the deployer; the external Sage review found that, and the founder's IAM review found the rest. All are fixed (see change record).
3. **`viharbot-kepler-runtime`:** the AgentCore execution role. Pull its own image, write logs, put metrics in its own namespace, read two secrets, use the state and audit tables as described above, and invoke the Phase 2 comment-writer function (that permission already exists but there is no function yet). It does **not yet** have `sns:Publish` on the alerts topic, which it will need to send email; that administrator change, with a new boundary version, comes with the runtime build. Denied: deleting anything in DynamoDB, altering the audit table, reading the alert-writer secret, invoking any foundation model other than the one allowlisted non-Anthropic model (every Anthropic model is denied).

**Boundary.** `viharbot-kepler-boundary` (version 3) is a ceiling on every role the deployer creates, and it is **resource-scoped**: Kepler secrets (`viharbot/kepler/*`), tables (`viharbot-kepler-*`), the one ECR repository, Kepler functions, Kepler and AgentCore log groups, its own metrics namespace, and AgentCore runtimes in this account. Version 1 allowed the same actions on `*`, which a simulation showed would let a bounded role with a broad policy attached read any secret or table in the account. A simulation of an all-allow identity policy capped by version 2 now reaches only Kepler's resources, and IAM, Organizations, role assumption and model invocation beyond the one allowlisted non-Anthropic model are explicitly denied. Resource ARNs use a wildcard region segment and are scoped by account and name prefix, so agents deployed in other regions fall inside the same boundary.

**Region guard (deferred, by decision).** There is no policy that denies actions outside `us-west-2`. The founder expects agents to run in different regions for reliability and redundancy, so a hard region lock would fight that. The deployer policy's resource ARNs name `us-west-2` today and will be extended per region; the boundary is already region-agnostic. Revisit a region allowlist once the set of regions is known.

The policies passed IAM Access Analyzer validation with no findings (syntax and action names only; that does not prove sufficiency). Validation caught three mistakes in my first draft, and the Sage review plus live probes found three further authorization gaps, all recorded and fixed. Authorization tests are in `deploy/agentcore/kepler/iam/tests/`: `deployer-negative-test.sh` (27 live checks as the deployer role) and `policy-simulation.sh` (36 read-only simulator checks of the boundary, deployer and runtime role). One thing the simulator cannot show: for CloudWatch Logs it evaluates the log group (`NAME:*`), never a log-stream ARN, so whether the runtime's log writes work is proven only by its first real run.

## Network boundary

Two options, and a decision is needed. AgentCore supports `PUBLIC` and `VPC` network modes.
- **Start `PUBLIC`, restrict in code:** no NAT cost. The runtime's HTTP client only ever connects to `api.github.com` and the LiteLLM host, enforced by an allowlist in the code. This does **not** restrict egress at the network layer, so a bug or a compromised dependency could reach elsewhere.
- **`VPC`, egress on 443 only:** a security group allowing only outbound 443, private subnets and a NAT gateway (a recurring charge). A security group cannot filter by domain; domain filtering needs DNS Firewall or Network Firewall, at more cost.

I recommend `PUBLIC` plus the code allowlist for the lab, with the limitation recorded, and `VPC` when the agent gets any write capability.

## Verified, and not yet verified

Verified on 2026-11-05: the AgentCore control-plane API answers in `us-west-2` and no runtime exists; the CLI supports tags and `PUBLIC` or `VPC` network mode on `create-agent-runtime`; the resources in the change record exist with the expected tags and settings (read back after creation); the deployer role's authorization tests pass (23 of 23); and the live runtime role's effective permissions, and the boundary's cap on an all-allow policy, behave as described (by IAM policy simulation, not by a running workload).

Not verified: the exact AgentCore log-group naming and any further permissions AgentCore requires of an execution role (check the current AWS execution-role guide before the first deploy, since a validated policy can still be insufficient); whether tags supplied at runtime creation need a separate `TagResource` permission; AgentCore pricing and quotas for this account; that the gateway routes `gpt-5.4-mini` and nothing Claude-backed for a Kepler key; that a GitHub App with the planned permissions can be installed on the repository; and anything about a running agent, since none exists.

## Rollback

**Source:** revert this PR. That does not touch AWS. **AWS** (separate, human-approved): everything was created by the deployer role or the administrator with the common tags, so teardown is, in order: the schedule, runtime and endpoint (when they exist), the Lambda, the alarm, the SNS topic and subscription, the budget, the secrets (with their recovery window), the log group, the ECR repository (after deleting images), the runtime role, then the two DynamoDB tables last. Both tables have deletion protection, which must be disabled by the administrator first, and the audit table holds the audit trail, so export it before deleting. Finally the administrator removes `viharbot-kepler-deployer` and `viharbot-kepler-boundary` using the rollback steps in the IAM README. Nothing deploys on merge.

## Decisions

Made by the founder on 2026-11-05 (in the supervising session):

| Decision | Value |
| --- | --- |
| Network mode | `PUBLIC` now, with the code allowlist; move to `VPC` when Kepler gets any write capability |
| Monthly budget | USD 50, filtered by the `project=viharbot` tag |
| Polling interval | every 6 hours (4 runs a day), plus a manual check-now. Changed on 2026-11-05 from 15 minutes: contributor PRs move in hours and days |
| Alert channel | Email through SNS, on change only; no status comments on a public GitHub issue |
| GitHub write access | None in the first phase. A separate, approval-gated writer later, only after the founder authorizes it in writing |

Also confirmed by the founder: budget alerts at 50%, 80% and 100% of actual spend and at 100% of forecast; CloudWatch log retention 30 days; deduplication keys expire after 30 days by TTL; audit rows never expire (the audit table has no TTL and the runtime role cannot delete or update them; see the audit caveat under Components). Budget and alarm notifications go to the founder's work email address. The address is deliberately not recorded in this public repository; it was supplied through an environment variable when the budget and alarm topic were created by `deploy/agentcore/kepler/provision-foundation.sh`.

Budget caveat (accepted by the founder): a budget filtered by tag only works once the management account activates the cost allocation tags (see Tags and billing). Until then, the budget cannot isolate Kepler's cost and will cover the whole account.

The governance wording for this mission will be proposed as a separate human-reviewed PR. The change record is [aws-lab-change-record.md](aws-lab-change-record.md). The one-time administrator steps are in [`deploy/agentcore/kepler/iam/README.md`](../deploy/agentcore/kepler/iam/README.md).
