# AWS lab change record

An append-only log of what was inspected or changed in the Cyera AWS lab account for ViharBot (account ending 3600, region `us-west-2`, first agent: Kepler, issue [#198](https://github.com/vchokshi/viharbot/issues/198)). Add a new entry for every change; do not edit or remove earlier entries. Record the approver and the tag set. Never record credentials, keys, secret values or personal data.

| # | Date (UTC) | Who | Type | What | Approved by | Result |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 2026-11-05 | Claude Code (supervised) | read-only | `sts get-caller-identity`; AgentCore `list-agent-runtimes`; list ViharBot roles, ECR repositories, DynamoDB tables and tagged resources; organization membership; Cost Explorer read; Access Analyzer `validate-policy` on the five draft policies. No resources created or changed. | founder, in the supervising session (confirmed the account is Cyera's lab, authorized for ViharBot) | Account is a member of an AWS Organization; AgentCore reachable; nothing ViharBot-related exists; draft policies validate with no findings. |

## Entries to be added when the work happens

Planned, not yet done, each needing founder approval first: (a) create `viharbot-kepler-boundary` and `viharbot-kepler-deployer` as administrator; (b) deployer creates the runtime role, state table, secrets (empty), ECR repository, log group and alarms; (c) founder sets secret values; (d) cost allocation tags activated by the management account; (e) AgentCore runtime and schedule created; (f) alert writer created. Record each as its own row, with the tag set `project=viharbot, agent=kepler, environment=lab`.
