# ADR-008: One logical Nova on AgentCore and ECS/Fargate

**Status: Proposed.** Nothing here is implemented or provisioned, and no AWS API was called to write it. Under `AGENTS.md`, AgentCore capabilities must be verified before the project depends on them, and no AWS resource may be created without an explicit request.

## Context

Verified in the repository:

- `agentcore_runtime.py` adds `GET /ping` and `POST /invocations` (`{request_id, input}`) to a service; `research_agent.py:149` wires it to `/research` only. `agentcore_client.py` calls boto3 `invoke_agent_runtime` over SigV4, is deadline-aware and does not retry. The client is chosen when `RESEARCH_AGENTCORE_ARN` is set.
- AgentCore support is "compatible in code, not tested end to end, not operational" (`docs/development-status.md`, audit section 8.3). No ECR image, role or runtime exists. Streaming is not bridged.
- There is no runtime-instance field anywhere, no inbound authentication, and memory is local JSONL.
- Nova's inference already goes through LiteLLM (`model_agent.build_agent`); the audit found no provider SDK imports.
- ECS/Fargate does not appear in the repository.

Verified in AWS documentation (sources and dates in "Sources" below): the AgentCore container contract is an ARM64 image serving `POST /invocations` and `GET /ping` on port 8080; a runtime accepts IAM SigV4 or a custom JWT authorizer but not both;  synchronous requests time out at 15 minutes; `runtimeSessionId` must be at least 33 characters; sessions are ephemeral microVMs; billing is per second and CPU is not billed during I/O wait. **Unverified, and treated as assumptions until a source or an approved smoke test establishes them:** whether the default public network mode gives a runtime outbound access to an external HTTPS endpoint such as LiteLLM with no VPC or NAT (the AWS VPC page says a VPC runtime has no internet by default, and the public-mode statement was read only in a search summary); whether a non-Cognito issuer works with the JWT authorizer; and native secrets injection for AgentCore.

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
4. **Inference.** Both instances use the same external LiteLLM over HTTPS, with their own alias and key. As a **proposed restriction** (not yet a demonstrated control), the AgentCore execution role and the Fargate task role grant no model-invocation permission and the AgentCore full-access managed policy is not used. Omitting an allow is not a denial, so the proposal also adds an **explicit Deny** on model-invocation actions (at least `bedrock:InvokeModel` and `bedrock:InvokeModelWithResponseStream`; the complete action list, the effective-policy boundary and any additional attached policies or credentials are for a security review to define). The invariant is "demonstrated" only when negative tests show a model call from each runtime is refused. Boto3 clients are created inside the handler, not at module scope, because AgentCore's snapshot behavior can otherwise miss session credentials.
5. **Runtime discovery: a static manifest.** A versioned file such as `deploy/nova/instances.json` lists each instance's id, runtime kind, priority, endpoint or ARN, auth type, region, contract version and allowed operation classes. The coordinator loads it at startup. AWS Agent Registry is unnecessary for two instances. Charlie asks for the research capability and the platform picks an authorized, healthy instance, so Charlie never holds an infrastructure endpoint.
6. **Health.** Fargate: load-balancer target health plus `/health`. AgentCore: no externally callable ping was found (`/ping` is for the platform), and a synthetic invoke starts a billed session, so use passive health from real outcomes (a circuit breaker) plus a rare, cheap synthetic invoke.
7. **Active-passive failover.** The primary is `nova-agentcore-01`; the secondary is `nova-fargate-01`. One table decides every case. The failure is classified first, and the operation class second.

| Outcome of the attempt | Meaning | `read_only` | `idempotent` | `stateful` | `side-effecting` |
|---|---|---|---|---|---|
| **Known not accepted**: connection refused or failed before the request was sent, 429, an AWS-side 5xx returned before the agent ran, a retryable conflict | The target provably did not run the work | Fail over now | Fail over now | Fail over only to an instance holding the same state; otherwise fail clearly | Fail over only if the target honors `idempotency_key`; otherwise fail clearly |
| **Unknown outcome**: timeout, connection reset after the request was sent, a response lost | The primary may still be running the work, and a timeout does not cancel it | At most **one** sequential retry on the secondary, only while `deadline_ms` remains **and the budget rule below admits it**; duplicate inference is **accepted and logged** (`possible_duplicate=true`, `attempt`) | **Not allowed** until a target honors `idempotency_key` and deduplicates; none does today | Never | Never |
| **Known failure after running**: the agent returned an application error | The work ran | No failover; report the error | No failover | No failover | No failover |

**Budget rule for a retry after an unknown outcome.** `budget` has two units: `max_output_tokens` and `max_cost_usd`, each a ceiling for one attempt. The primary may already have spent its full allowance, so the retry is admitted only if the request's `budget` is present and its total is at least **twice** one attempt's ceiling (one allowance reserved for the unresolved primary, one for the secondary). If `budget` is absent or cannot cover that, there is **no retry**: the request fails with `possible_duplicate=unknown`, never a guess. Known-not-accepted failures need no reservation. Deterministic tests: unknown outcome with and without a sufficient budget, with an absent budget, with the budget exhausted exactly, and a second unknown outcome (no third attempt).

Rules that apply to every row: never send the same request to both instances in parallel (no hedging); at most 2 attempts in total; never fail over to an instance whose identity is not authorized for the request's data; the breaker only decides the initial target, not retries after an unknown outcome. Nova research today is `read_only` (an inference call and a local append), so it uses the first two columns. "Idempotent" work is safe to replay only once a deduplication mechanism exists; this ADR does not assume one.

Failback happens automatically after consecutive healthy probes, never in the middle of a request. **Failover must not widen permissions** (see the future lab, issue #87).
8. **Observability.** Log `logical_agent`, `runtime_instance`, `runtime_kind`, `region`, `image_digest`, `contract_version`, `session_id`, `attempt`, `failover_from`, `failover_reason`, `routing_decision`, `breaker_state`, `deadline_remaining_ms`, `duration_ms`, `model_alias`, `error`, and the AWS request id on coordinator failures. The coordinator logs the routing decision and each instance logs completion, so joining on `request_id` answers "which instance handled this, and why". No prompts or secrets.
9. **Fix before relying on AgentCore:** derive `runtimeSessionId` as `"nova-" + uuid4()` and keep `request_id` in the payload, because the 33-character minimum rejects short caller-supplied request IDs (`observability.REQUEST_ID_PATTERN` allows 1 to 128 characters).

## Fargate minimal shape (assumptions unless noted)

One ECS cluster, one ARM64 task (0.25 to 0.5 vCPU, 0.5 to 1 GB) in a service with desired count 1, the same image listening on 8080, public subnets with a public IP and no NAT gateway (documented by AWS), an Application Load Balancer with an ACM certificate for HTTPS to a stable name, a security group that allows 443 only from the coordinator and 8080 only from the load balancer, CloudWatch JSON logs, and the LiteLLM key injected from Secrets Manager (documented; a rotated secret needs a new deployment).

## Alternatives considered

Both instances on one platform (does not test cross-service redundancy); active-active (duplicates inference spend and complicates side effects); EKS (explicitly excluded); AWS Agent Registry or a mesh for discovery (unnecessary for two instances); direct Bedrock inference (forbidden by the LiteLLM invariant).

## Consequences

A stateless failover works only while memory is not read back; real shared memory arrives with ADR-009. AgentCore is not verified end to end here, so one smoke test of a single runtime, with explicit approval, comes before anything depends on it. Where the coordinator runs matters: Stage is on DigitalOcean, so it needs AWS credentials to call AgentCore and a bearer secret for Fargate; credential handling and source-IP restriction are open. `/ping` reports Healthy even when LiteLLM is down, so rely on the breaker.

## Cost (estimates; confirm in the AWS calculator before approval)

**Basis:** US East; rates retrieved 2026-11-02 from the AWS pricing pages listed under "Sources"; 1,000 requests per day; about 30 seconds of wall time per request, mostly LLM wait, with about 3 seconds of active CPU at 1 vCPU and 1 GB.

**Unit rates used:**

| Item | Rate |
|---|---|
| AgentCore Runtime, platform V1 | $0.0895 per vCPU-hour and $0.00945 per GB-hour |
| AgentCore Runtime, platform V2 | $0.1276 per vCPU-hour and $0.0169 per GB-hour |
| Fargate, ARM | $0.0324 per vCPU-hour and $0.0036 per GB-hour |
| Application Load Balancer | $0.0225 per hour plus $0.008 per LCU-hour |
| Public IPv4 address | about $3.65 per month each ($0.005 per hour); the task has one, and the load balancer has one **per Availability Zone it spans** (two assumed) |
| NAT gateway (avoided) | about $0.045 per hour, about $33 per month |

**Included:** the compute and load balancer lines in the totals below. **Excluded:** data transfer, ECR storage, CloudWatch Logs, Secrets Manager (about $0.40 per secret per month, unverified), certificates, and all LiteLLM inference, which is external.

- AgentCore on demand: about $7.50 per month, range $3 to $40 depending on the active CPU share and platform version. No idle floor.
- Fargate always on, ARM: 0.25 vCPU and 0.5 GB about $7 per month, 0.5 vCPU and 1 GB about $14.50; the task's public IPv4 about $3.65; the load balancer is the largest fixed item at about $23 to $30 per month, itemized as the hourly charge (about $16), at least one LCU-hour charge (about $6) and two public IPv4 addresses (about $7.30, assuming two Availability Zones; confirm the zone count and address behavior in the AWS calculator). A NAT gateway would add about $33 and is avoided.
- Standby totals: Fargate stack about $31 to $52 per month; AgentCore plus a warm Fargate standby about $38 to $92. With the Fargate service at desired count 0, only the load balancer remains (about $23 to $30).
- Temporary: the load balancer and service can be created and deleted per test for about $0.05 per hour, so a four-hour test is about $0.20 plus inference.

## Validation plan

Deterministic tests with mocked responses for: primary available, primary unavailable, secondary available, secondary unavailable, routing decisions, timeout handling, retry eligibility, authorization, request correlation, failure reporting. No repeated paid inference. After approval: verify both instances' health, run one authorized research request, make the primary unavailable, confirm eligible failover, confirm correlation and that inference went through LiteLLM. Use the fewest live calls possible.

## Open questions

**Smoke-test prerequisite:** outbound access from an AgentCore runtime to the external LiteLLM endpoint must be demonstrated in the approved smoke test before AgentCore is relied on.

Region choice (the newer AgentCore platform version is not available in every region); the domain and certificate for the load balancer; where the coordinator runs and how it authenticates to both; the ARM64 build story in CI; whether the Cloudflare gateway CA baked into `Dockerfile.research` is needed on AWS; AgentCore secrets injection.

## Sources

Retrieved 2026-11-02 by a read-only assessment; summarised, not quoted, so re-check before implementation. "Unverified" means the page does not say.

- AgentCore HTTP contract: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-http-protocol-contract.html
- AgentCore quotas and limits: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/bedrock-agentcore-limits.html
- AgentCore authentication (SigV4 or JWT, not both): https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-oauth.html
- AgentCore sessions: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-sessions.html
- AgentCore platform behavior: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-how-it-works.html
- AgentCore VPC and networking: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-vpc.html (the statement that the default PUBLIC mode allows outbound internet access was read in a search summary, not on this page; confirm it before relying on it)
- AgentCore registry: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/registry.html
- AgentCore pricing: https://aws.amazon.com/bedrock/agentcore/pricing/
- ECS outbound networking without NAT: https://docs.aws.amazon.com/AmazonECS/latest/developerguide/networking-outbound.html
- ECS secrets from Secrets Manager: https://docs.aws.amazon.com/AmazonECS/latest/developerguide/secrets-envvar-secrets-manager.html
- Fargate pricing: https://aws.amazon.com/fargate/pricing/
- Load balancer pricing: https://aws.amazon.com/elasticloadbalancing/pricing/
- VPC (public IPv4, NAT) pricing: https://aws.amazon.com/vpc/pricing/

## Related

ADR-006, ADR-009, #44, #87, `deploy/agentcore/README.md`, `docs/roadmap-distributed-agents.md`.
