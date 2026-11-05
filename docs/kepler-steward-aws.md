# Kepler: first-contributor steward on AWS AgentCore (design)

Status: **proposed, not deployed.** Tracking issue [#198](https://github.com/vchokshi/viharbot/issues/198) is the source of truth. It supersedes the autonomous coding mission in #125 and #127 to #131 (see [Kepler](kepler.md) for that history, which is preserved). Nothing in this document has been created in AWS, and nothing here is implemented as code except the policy files under `deploy/agentcore/kepler/iam/`, which are drafts.

**Permanent constraint:** ViharBot does not deploy or operate unsupervised coding agents. Kepler never writes source code, pushes commits or branches, approves or merges pull requests, deploys, changes repository security settings, or grants itself permissions.

## What Kepler does

It watches for first-time contributors and their open pull requests, works out the onboarding steps still outstanding (CLA acceptance and its record, third-party rights disclosure, commit identity, branch freshness, CI and review state), tracks replies and new commits, drafts respectful follow-ups, and alerts the founder. First case: PR #179. Initial mode is **read-only monitoring plus internal alerts**. Contributor-facing text is drafted only and needs founder approval to post.

Status facts come from the GitHub API and the trusted-base `CLA-ACCEPTANCES.md`, by deterministic code, never from a model and never from an unverified comment. A model is used only to draft message text. PR bodies, comments, commits and repository files are untrusted data and are never treated as instructions.

## Components

| Component | Role | Notes |
| --- | --- | --- |
| AgentCore Runtime `kepler` | The monitor. Read-only GitHub access. | Container on `linux/arm64`, port 8080, `GET /ping` and `POST /invocations` (see [AgentCore notes](../deploy/agentcore/README.md)). Region `us-west-2`. |
| EventBridge Scheduler rule | Bounded polling trigger. | AgentCore Runtime is invoked, not scheduled, so something must call it. No webhook receiver in the MVP. |
| DynamoDB table `viharbot-kepler-state` | Event state, deduplication, audit. | AgentCore sessions have no durable local storage, which is the concrete need that justifies a store. On-demand billing, TTL for dedup keys only, point-in-time recovery on. Audit rows are written with `attribute_not_exists` conditions and the runtime role cannot delete. |
| Lambda `viharbot-kepler-alert-writer` | The only writer. Posts to #198. | Separate IAM role and a separate GitHub App. Accepts a fixed alert shape, updates one deduplicated tracking comment, and has no right to post anywhere else. The runtime role can invoke it but cannot read its secret. |
| Secrets Manager `viharbot/kepler/*` | `github-app-reader`, `github-app-alert-writer`, `litellm-api-key`. | Never in source, images or logs. Set by the founder, not by tooling. |
| CloudWatch | Structured JSON logs, a custom `ViharBot/Kepler` namespace, alarms. | Alarms notify by SNS email, so a dead monitor is noticed without depending on the monitor to say so. |
| AWS Budget | Spend alarm filtered by the `project` tag. | Needs the tag activated for cost allocation (see Billing). |
| ECR `viharbot/kepler` | Image repository. | Scan on push. |

Alerts go to #198 as one tracking comment updated in place, keyed by event ID, so a duplicate GitHub event or a re-poll cannot produce a duplicate message.

## Inference

Inference goes only through the external LiteLLM gateway (`LITELLM_BASE_URL`, its own `LITELLM_API_KEY`, its own alias). The approved alias is `gpt-5.4-mini`, the one Cipher, Charlie and Pixel already use. No Anthropic Claude model, directly or through any provider, until organizational and licensing approval is confirmed. This is enforced in code (an allowlist of aliases, failing closed) and in IAM: the runtime, deployer and boundary policies explicitly deny `bedrock:InvokeModel*`, so Kepler cannot call a Bedrock-hosted model even by mistake. LiteLLM itself is not modified.

## Tags and billing

Every taggable resource gets the same five tags, so the whole agent shows up as one line in the billing profile:

| Key | Value |
| --- | --- |
| `project` | `viharbot` |
| `agent` | `kepler` |
| `environment` | `lab` |
| `owner` | the founder |
| `managed-by` | `human-supervised` |

The deployer policy only allows creating an AgentCore runtime that carries `project=viharbot` and `agent=kepler`, so an untagged runtime cannot be created by that role. The lab account is a member of an AWS Organization, so **activating `project` and `agent` as cost allocation tags must be done by the management account**. Until then they won't appear in Cost Explorer or Budgets filters, and tags only show costs from the day they are activated. This is a founder or Cyera billing task that I cannot do from here.

## Roles and how they are created

1. **You, as administrator, once:** create the permissions-boundary policy `viharbot-kepler-boundary` and the role `viharbot-kepler-deployer` (trust and permissions in `deploy/agentcore/kepler/iam/`). The deployer cannot create or change the boundary, which is why this step is yours.
2. **The deployer role, from then on:** creates everything else. It can create IAM roles only if they are named `viharbot-kepler-*` and carry the boundary, and it can pass only those roles, only to AgentCore, Scheduler and Lambda. It cannot remove the boundary, create users or keys, or touch other accounts' resources.
3. **`viharbot-kepler-runtime`:** the AgentCore execution role. Pull its own image, write logs, put metrics in its own namespace, read two secrets, append to its state table, invoke the alert writer. Denied: deleting audit rows, reading the alert-writer secret, invoking any foundation model.

The policies passed IAM Access Analyzer validation with no findings (syntax and action names only; that does not prove sufficiency). Validation caught three mistakes in my first draft, now fixed.

## Network boundary

Two options, and a decision is needed. AgentCore supports `PUBLIC` and `VPC` network modes.
- **Start `PUBLIC`, restrict in code:** no NAT cost. The runtime's HTTP client only ever connects to `api.github.com` and the LiteLLM host, enforced by an allowlist in the code. This does **not** restrict egress at the network layer, so a bug or a compromised dependency could reach elsewhere.
- **`VPC`, egress on 443 only:** a security group allowing only outbound 443, private subnets and a NAT gateway (a recurring charge). A security group cannot filter by domain; domain filtering needs DNS Firewall or Network Firewall, at more cost.

I recommend `PUBLIC` plus the code allowlist for the lab, with the limitation recorded, and `VPC` when the agent gets any write capability.

## Verified, and not yet verified

Verified on 2026-11-05 with read-only calls: AgentCore control-plane API answers in `us-west-2` (no runtimes exist); the CLI supports tags and `PUBLIC` or `VPC` network mode on `create-agent-runtime`; the account has no ViharBot roles, ECR repositories, DynamoDB tables or tagged resources.

Not verified: the exact log-group naming and any further permissions AgentCore requires of an execution role (check the current AWS execution-role guide before the first deploy, since a validated policy can still be insufficient); AgentCore pricing and quotas for this account; that the gateway routes `gpt-5.4-mini` for a Kepler key; that a GitHub App with the planned permissions can be installed on the repository.

## Rollback

Everything is created by the deployer role with the common tags, so teardown is: delete the schedule, runtime and endpoint, the Lambda, the alarms and topic, the secrets (with their recovery window), the log groups, the ECR repository, then the roles and, last, the state table. The DynamoDB table holds the audit trail, so export it before deleting. Source changes roll back by reverting the PR. Nothing deploys on merge.

## Open decisions

Network mode (above); the monthly budget limit and alert email; log and audit retention; the polling interval; and the governance wording for this mission, which will be proposed as a separate human-reviewed PR. The change record is [aws-lab-change-record.md](aws-lab-change-record.md).
