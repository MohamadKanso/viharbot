# Roadmap: secure, observable, distributed agents

**Status: Proposed plan. Nothing here is implemented, deployed or approved.** It sequences five phases and the small pull requests that carry them. No phase provisions billable infrastructure without explicit human approval, and no pull request is merged without a human.

## Phases and dependencies

| Phase | Objective | Depends on |
|---|---|---|
| A | Authenticated, authorized agent communication | none |
| B | DigitalOcean staging through Orbit and a deployment controller | A |
| C | Nova on Amazon Bedrock AgentCore and ECS/Fargate | A |
| D | Persistent, governed memory | A (identity), C for cross-runtime access |
| E | Synthetic enterprise data and a Cyera security lab | A, C, D for several scenarios |

Design documents (these can proceed in parallel): `ADR-006` identity and authorization, `ADR-007` Orbit deployment controller, `ADR-008` Nova dual runtime, `ADR-009` persistent memory, and the Phase E lab plan. The ADR numbers avoid those already cited by issues (ADR-001 to ADR-005); the founder confirms whether ADR-009 should be renumbered to satisfy the "ADR-003" dependency in #40 and ADR-008 to satisfy "ADR-004" in #44.

Implementation order: A2 (authentication), A3 (authorization), B2 (controller), C2 (shared Nova adapters), C3 (Fargate), C4 (discovery and failover), D2 (memory service), D3/D4 (cross-runtime access, authorization, consistency), E1 to E3. Kubernetes and EKS are out of scope.

## Decisions needed from the founder

1. **Issuer key location** (ADR-006): web process, or a separate internal signer.
2. **DigitalOcean team** (ADR-007): a separate team for the controller, so its token cannot reach Stage.
3. **Memory service location** (ADR-009): AWS or DigitalOcean.
4. **Review of the educational repositories:** neither has workflows, so Sage cannot review PRs there. Options: copy the Sage workflow into each (the instructor repo must never receive trainee-visible output), or review them manually.
5. **ADR numbering** as above.
6. **Approval gates:** which of the cost items below to approve, one at a time.

## Estimated costs (estimates from the assessments; confirm with provider calculators before approving)

| Item | Estimate | Notes |
|---|---|---|
| DigitalOcean deployment controller | about $5/month | one 512 MiB App Platform component |
| DigitalOcean test apps | about $0.01 per hour each; capped at 3 | destroyed by expiry sweep |
| DigitalOcean container registry | $0 (starter) or $5/month | image allowlist source |
| AgentCore Nova (on demand) | about $3 to $40/month at 1,000 requests/day | no idle floor; assumptions in ADR-008 |
| Fargate Nova, always on, one small task plus ALB | about $28 to $45/month | the load balancer is the largest fixed item |
| Fargate with desired count 0 | about $17 to $22/month (ALB only) | tear down the ALB between tests for about $0.05/hour |
| Memory: DynamoDB plus a small memory API | under $5/month | recommended |
| Memory: add Aurora/pgvector | about $45 to $60/month | deferred |
| Memory: add OpenSearch Serverless | about $175 to $350/month | deferred |
| Phase E lab (S3, DynamoDB, CloudTrail, brief Fargate) | about $5 to $25 per short run; $40 to $120/month if a database and a task stay up | Cyera licensing is separate and not public |
| Live inference | unknown, kept small | all automated tests use mocked inference |

Resources that can be temporary: Fargate service and ALB, test apps, the Phase E lab, AgentCore runtimes (no idle cost).

## Cost and safety rules for every phase

Automated tests use mocked inference and mocked cloud APIs. Live checks are minimal, run only after approval, and report their cost. Nothing deploys on a push to `main`.

## What the assessments could not verify

Whether AgentCore's JWT authorizer accepts a non-Cognito issuer; whether DigitalOcean offers workload identity or internal encryption; whether DigitalOcean API token scopes can be limited to specific apps; which Cyera products can scan DynamoDB, Aurora or OpenSearch, and whether Cyera sees application-level memory requests or agents on AgentCore or Fargate. Each is recorded as an open question in the relevant ADR and is not assumed.
