# ADR-008: One logical Nova on AgentCore and ECS/Fargate

**Status: Proposed.** Nothing here is implemented or provisioned, and no AWS API was called to write it. Under `AGENTS.md`, AgentCore capabilities must be verified before the project depends on them, and no AWS resource may be created without an explicit request.

## Context

Verified in the repository:

- `agentcore_runtime.py` adds `GET /ping` and `POST /invocations` (`{request_id, input}`) to a service; `research_agent.py:149` wires it to `/research` only. `agentcore_client.py` calls boto3 `invoke_agent_runtime` over SigV4, is deadline-aware and does not retry. The client is chosen when `RESEARCH_AGENTCORE_ARN` is set.
- AgentCore support is "compatible in code, not tested end to end, not operational" (`docs/development-status.md`, audit section 8.3). No ECR image, role or runtime exists. Streaming is not bridged.
- There is no runtime-instance field anywhere, no inbound authentication, and memory is local JSONL.
- Nova's inference already goes through LiteLLM (`model_agent.build_agent`); the audit found no provider SDK imports.
- ECS/Fargate does not appear in the repository.

Verified in AWS documentation (links in the assessment notes): the AgentCore container contract is an ARM64 image serving `POST /invocations` and `GET /ping` on port 8080; a runtime accepts IAM SigV4 or a custom JWT authorizer but not both; the default network mode is public, which allows outbound internet access to an external HTTPS endpoint with no VPC or NAT; synchronous requests time out at 15 minutes; `runtimeSessionId` must be at least 33 characters; sessions are ephemeral microVMs; billing is per second and CPU is not billed during I/O wait. **Unverified:** whether a non-Cognito issuer works with the JWT authorizer, and native secrets injection for AgentCore.

## Decision

Nova is **one logical agent** with **two runtime instances**:

| Logical agent | Runtime instance | Hosting service | Role |
|---|---|---|---|
| Nova | `nova-agentcore-01` | Amazon Bedrock AgentCore Runtime | primary |
| Nova | `nova-fargate-01` | Amazon ECS with Fargate | secondary |

Kubernetes, EKS and a service mesh are out of scope.

1. **Shared implementation, thin adapters.** One `nova_core` package holds the research handler and `build_agent`. An AgentCore adapter (the existing routes plus instance stamping) and an HTTP adapter (the same `/invocations` and `/health` plus the ADR-006 token check) wrap it. Both read `NOVA_INSTANCE_ID` and `NOVA_RUNTIME`. The image and the model configuration are otherwise identical. Do not fork Nova.
2. **Additive request envelope** (today's `{request_id, input}` keeps working): `logical_agent`, `contract_version`, `request_id`, `attempt`, `deadline_ms` (the existing `RequestDeadline`), optional `budget`, `idempotency_key`, `operation_class` (`read_only` or `idempotent`), and `input`. The response adds `runtime_instance`, `runtime_kind`, `duration_ms`, `attempt`, and an `X-Nova-Instance` header.
3. **Identity.** Each instance has its own workload identity. Logical identity names the capability requested; runtime identity names the process that ran it. They are separate fields everywhere. Authentication follows ADR-006: SigV4 stays for coordinator to AgentCore, and the same token is also verified inside the container. Fargate sits behind an Application Load Balancer, which cannot verify SigV4, so the in-app token check is mandatory there.
4. **Inference.** Both instances use the same external LiteLLM over HTTPS, with their own alias and key. The AgentCore execution role and the Fargate task role grant **no** `bedrock:InvokeModel`, and the AgentCore full-access managed policy is not used. That is how the "no direct Bedrock" rule is enforced in AWS. Boto3 clients are created inside the handler, not at module scope, because AgentCore's snapshot behavior can otherwise miss session credentials.
5. **Runtime discovery: a static manifest.** A versioned file such as `deploy/nova/instances.json` lists each instance's id, runtime kind, priority, endpoint or ARN, auth type, region, contract version and allowed operation classes. The coordinator loads it at startup. AWS Agent Registry is unnecessary for two instances. Charlie asks for the research capability and the platform picks an authorized, healthy instance, so Charlie never holds an infrastructure endpoint.
6. **Health.** Fargate: load-balancer target health plus `/health`. AgentCore: no externally callable ping was found (`/ping` is for the platform), and a synthetic invoke starts a billed session, so use passive health from real outcomes (a circuit breaker) plus a rare, cheap synthetic invoke.
7. **Active-passive failover.** The primary is `nova-agentcore-01`. Fail over to `nova-fargate-01` when the breaker is open, or inside a request only for failures that happen before the work was accepted: connect error, 429, AWS-side 5xx, or a retryable conflict. A timeout does not cancel the remote run, so a retry can double-spend tokens: never hedge in parallel.

| Operation class | Failover |
|---|---|
| Read-only or idempotent (Nova research today) | Allowed on timeout or 5xx while `deadline_ms` remains |
| Stateful (none today) | No silent failover; pin to the instance holding state or fail clearly |
| Side-effecting (none for Nova) | None, unless the target honors `idempotency_key` |

Failback happens automatically after consecutive healthy probes, never in the middle of a request. **Failover must not widen permissions:** a request is never routed to an instance whose identity is not authorized for its data (see the future lab, issue #87).
8. **Observability.** Log `logical_agent`, `runtime_instance`, `runtime_kind`, `region`, `image_digest`, `contract_version`, `session_id`, `attempt`, `failover_from`, `failover_reason`, `routing_decision`, `breaker_state`, `deadline_remaining_ms`, `duration_ms`, `model_alias`, `error`, and the AWS request id on coordinator failures. The coordinator logs the routing decision and each instance logs completion, so joining on `request_id` answers "which instance handled this, and why". No prompts or secrets.
9. **Fix before relying on AgentCore:** derive `runtimeSessionId` as `"nova-" + uuid4()` and keep `request_id` in the payload, because the 33-character minimum rejects short caller-supplied request IDs (`observability.REQUEST_ID_PATTERN` allows 1 to 128 characters).

## Fargate minimal shape (assumptions unless noted)

One ECS cluster, one ARM64 task (0.25 to 0.5 vCPU, 0.5 to 1 GB) in a service with desired count 1, the same image listening on 8080, public subnets with a public IP and no NAT gateway (documented by AWS), an Application Load Balancer with an ACM certificate for HTTPS to a stable name, a security group that allows 443 only from the coordinator and 8080 only from the load balancer, CloudWatch JSON logs, and the LiteLLM key injected from Secrets Manager (documented; a rotated secret needs a new deployment).

## Alternatives considered

Both instances on one platform (does not test cross-service redundancy); active-active (duplicates inference spend and complicates side effects); EKS (explicitly excluded); AWS Agent Registry or a mesh for discovery (unnecessary for two instances); direct Bedrock inference (forbidden by the LiteLLM invariant).

## Consequences

A stateless failover works only while memory is not read back; real shared memory arrives with ADR-009. AgentCore is not verified end to end here, so one smoke test of a single runtime, with explicit approval, comes before anything depends on it. Where the coordinator runs matters: Stage is on DigitalOcean, so it needs AWS credentials to call AgentCore and a bearer secret for Fargate; credential handling and source-IP restriction are open. `/ping` reports Healthy even when LiteLLM is down, so rely on the breaker.

## Cost (estimates; confirm in the AWS calculator before approval)

Assumes about 30 seconds wall time per request, mostly LLM wait, about 3 seconds of active CPU at 1 vCPU and 1 GB, 1,000 requests per day.

- AgentCore on demand: about $7.50 per month, range $3 to $40 depending on the active CPU share and platform version. No idle floor.
- Fargate always on, ARM: 0.25 vCPU and 0.5 GB about $7 per month, 0.5 vCPU and 1 GB about $14.50; public IPv4 about $3.65; the load balancer, about $16 to $22 per month, is the largest fixed item. A NAT gateway would add about $33 and is avoided.
- Standby totals: Fargate stack about $28 to $45 per month; AgentCore plus a warm Fargate standby about $35 to $85. With the Fargate service at desired count 0, only the load balancer remains (about $17 to $22).
- Temporary: the load balancer and service can be created and deleted per test for about $0.05 per hour, so a four-hour test is about $0.20 plus inference.

## Validation plan

Deterministic tests with mocked responses for: primary available, primary unavailable, secondary available, secondary unavailable, routing decisions, timeout handling, retry eligibility, authorization, request correlation, failure reporting. No repeated paid inference. After approval: verify both instances' health, run one authorized research request, make the primary unavailable, confirm eligible failover, confirm correlation and that inference went through LiteLLM. Use the fewest live calls possible.

## Open questions

Region choice (the newer AgentCore platform version is not available in every region); the domain and certificate for the load balancer; where the coordinator runs and how it authenticates to both; the ARM64 build story in CI; whether the Cloudflare gateway CA baked into `Dockerfile.research` is needed on AWS; AgentCore secrets injection.

## Related

ADR-006, ADR-009, #44, #87, `deploy/agentcore/README.md`, `docs/roadmap-distributed-agents.md`.
