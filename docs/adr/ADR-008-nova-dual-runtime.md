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
2. **Additive request envelope** (today's `{request_id, input}` keeps working): `logical_agent`, `contract_version`, `request_id`, `attempt`, `deadline_ms` (the existing `RequestDeadline`), optional `budget` (an object with four fields, defined under "Budget rule" below), `idempotency_key`, `operation_class` (`read_only` or `idempotent`), and `input`. The response adds `runtime_instance`, `runtime_kind`, `duration_ms`, `attempt`, and an `X-Nova-Instance` header. A rejection that Nova's own adapter makes before any work starts also carries the not-accepted marker (see the table).
3. **Identity.** Each instance has its own workload identity. Logical identity names the capability requested; runtime identity names the process that ran it. They are separate fields everywhere. Authentication follows ADR-006: SigV4 stays for coordinator to AgentCore, and the same token is also verified inside the container. Fargate sits behind an Application Load Balancer, which cannot verify SigV4, so the in-app token check is mandatory there.
4. **Inference.** Both instances use the same external LiteLLM over HTTPS, with their own alias and key. As a **proposed restriction** (not yet a demonstrated control), the AgentCore execution role and the Fargate task role grant no model-invocation permission and the AgentCore full-access managed policy is not used. Omitting an allow is not a denial, so the proposal also adds an **explicit Deny** on model-invocation actions (at least `bedrock:InvokeModel` and `bedrock:InvokeModelWithResponseStream`; the complete action list, the effective-policy boundary and any additional attached policies or credentials are for a security review to define). The invariant is "demonstrated" only when negative tests show a model call from each runtime is refused. Boto3 clients are created inside the handler, not at module scope, because AgentCore's snapshot behavior can otherwise miss session credentials.
5. **Runtime discovery: a static manifest.** A versioned file such as `deploy/nova/instances.json` lists each instance's id, runtime kind, priority, endpoint or ARN, auth type, region, contract version, allowed operation classes and `budget_enforcement` (`metered` or `none`; `none` until the acceptance evidence under "Enforcement" below is recorded for that alias and configuration). The coordinator loads it at startup. AWS Agent Registry is unnecessary for two instances. Charlie asks for the research capability and the platform picks an authorized, healthy instance, so Charlie never holds an infrastructure endpoint.
6. **Health.** Fargate: load-balancer target health plus `/health`. AgentCore: no externally callable ping was found (`/ping` is for the platform), and a synthetic invoke starts a billed session, so use passive health from real outcomes (a circuit breaker) plus a rare, cheap synthetic invoke.
7. **Active-passive failover.** The primary is `nova-agentcore-01`; the secondary is `nova-fargate-01`. One table decides every case. The failure is classified first, and the operation class second.

| Outcome of the attempt | Meaning | `read_only` | `idempotent` | `stateful` | `side-effecting` |
|---|---|---|---|---|---|
| **Known not accepted**, only these two cases: (a) a transport failure before any request bytes were sent (DNS failure, connection refused, or a failed connection or TLS setup, so the request was never written); (b) a response carrying the explicit marker `X-Nova-Not-Accepted: 1`, set by Nova's own adapter before any handler, inference call or memory write starts (for example a failed token check, a malformed envelope, or an admission refusal) | The target provably did not run the work, because either nothing was sent or Nova itself says it refused first | Fail over now | Fail over now | Fail over only to an instance holding the same state; otherwise fail clearly | Fail over only if the target honors `idempotency_key`; otherwise fail clearly |
| **Unknown outcome**: everything else that is not a completed application response, including a timeout, a connection reset after bytes were sent, a lost response, a **bare 429**, a bare 409 or any "retryable" label, and any platform, load-balancer or gateway 5xx without the marker | A status code alone does not prove no handler ran: an intermediary can emit it after acceptance, and the primary may still be running the work (a timeout does not cancel it) | At most **one** sequential retry on the secondary, only while `deadline_ms` remains **and the budget rule below admits it**; duplicate inference is **accepted and logged** (`possible_duplicate=true`, `attempt`) | **Not allowed** until a target honors `idempotency_key` and deduplicates; none does today | Never | Never |
| **Known failure after running**: the agent returned an application error | The work ran | No failover; report the error | No failover | No failover | No failover |

**Not-accepted marker.** The marker is trusted only on a response that arrived over the authenticated channel from the expected instance; a missing, malformed or duplicated marker means unknown outcome. The adapter sets it only on code paths that run before any work, and an application error after work starts never carries it. **Assumed, not verified:** that AgentCore's `invoke_agent_runtime` passes a custom response header through to the caller; if it does not, the AgentCore adapter uses an equivalent top-level body field (`not_accepted: true`), and until that is demonstrated in an approved smoke test, every AgentCore error without a parsed marker is an unknown outcome. **Assumed, not verified:** that any AWS service (AgentCore, ALB, ECS) guarantees a particular status code is returned before the workload ran. No AWS documentation is cited for such a guarantee, so none is claimed; if the provider documents one for a specific code, a later revision may add it with the URL and retrieval date.

**Budget rule for a retry after an unknown outcome.** `budget` has four fields, all non-negative integers (tokens, and cost in micro-USD so no floating point is compared): `attempt_max_output_tokens` and `attempt_max_cost_usd` are the most a single attempt may use, and `total_max_output_tokens` and `total_max_cost_usd` are the most all attempts of the request together may use. Each attempt is sent only its own attempt limits.

- **Defaults when absent:** a missing attempt field takes the agent's configured per-attempt default; a missing total field takes the attempt value (a total of one attempt). A field that is zero, negative, non-integer or unparseable is treated as not established. Defaults therefore never create room for a retry.
- **Admission, evaluated separately for tokens and for cost, and both must pass:** `remaining = total - spent_by_resolved_attempts - attempt_reserve_for_unresolved_primary`, where the unresolved primary is charged its **full** attempt allowance because it may have used all of it. The secondary is admitted only if `remaining >= attempt` for that unit. For the first retry this reduces to `total >= 2 x attempt`.
- **Enforcement (proposed; not implemented, and the reservation is only as good as this):** the shared `nova_core` owns one per-attempt meter, created when an attempt starts from that attempt's limits. Every LiteLLM call in the attempt, including repeated or tool-driven calls, passes through it. The meter does **pre-call cost admission**, not only a check that limits are already reached:
  1. **Reserve before the call.** Before each call the meter computes a worst-case charge: worst-case input cost, from a conservative upper bound on the prompt's tokens (a counted or length-based overestimate, never a guess below the real count; if no safe bound can be computed the call is refused) times the configured input rate, plus worst-case output cost, from the call's `max_tokens` times the configured output rate. Rates come from the configured per-alias price table, which is **assumed, not verified** to be correct and current; an alias with no price entry, or with a missing input or output rate, has no cost bound and its calls are refused. Any other billable usage the client or provider can add (cached, reasoning or tool tokens) must be covered by a configured rate or bound, or the call is refused. The meter refuses the call, before any model call, if adding the worst-case charge to the cost already spent and reserved would exceed `attempt_max_cost_usd`, or would exceed the request's `total_max_cost_usd` together with the reserve held for any unresolved primary. `max_tokens` is the smaller of the tokens still unspent and the output tokens affordable from the remaining cost allowance; if that is zero the call is refused. Tokens are reserved the same way against the attempt and total token limits.
  2. **Atomic within a request.** The reservation is one check-and-add under a lock (or compare-and-set) on a single meter object, so concurrent calls in one attempt cannot jointly reserve more than the allowance. The meter lives in one process. **No cross-instance sharing exists**: a primary running in another process cannot be observed or stopped, which is why its full attempt allowance is reserved on an unknown outcome, as specified in the admission rule above.
  3. **Settle after the call.** When the client reports usage, the meter replaces the reservation with the actual charge computed from the same rates, freeing the unused part. When usage is missing or unparseable, the full reservation stays charged. A call that fails with an unknown outcome keeps its full reservation.
  4. **The cost bound is UNESTABLISHED.** None of this is demonstrated. The bound depends on the price table, on the prompt-token upper bound, on LiteLLM's client reporting, and on the model honoring `max_tokens`, and none is verified here. LiteLLM is an external project we do not manage, and no LiteLLM budget enforcement is assumed. A refused call in an approved live smoke test with a tiny budget is **necessary but not sufficient**. `metered` may be set for a given alias and provider configuration only when separate recorded evidence exists for each assumption: (i) the configured input and output rates are compared with the provider's published rates or the observed billed amount for that alias, and the configured rates are not lower; (ii) the prompt-token upper bound is checked against provider-reported prompt tokens on representative and worst-case inputs and is never lower; (iii) the provider or model is observed honoring `max_tokens` (generated tokens never exceed it) and any other billable usage is covered by a rate or bound; (iv) settlement is checked, so reported usage never frees more than was actually billed, and missing usage keeps the full reservation; (v) the concurrency and failure test cases below pass against the real meter; and (vi) a call is observed refused by the reservation. Until all six are recorded, the manifest must say `none` for `budget_enforcement`, and with `none` no retry after an unknown outcome is admitted. The evidence is scoped to the alias, provider, price table and meter version it was gathered for; any change to one of them resets the entry to `none` until the evidence is repeated.
  Because an attempt cannot be proven to stop at its limit if the unresolved primary is running in another process, retry admission **fails closed**: the coordinator admits a retry only when the instance manifest marks the primary's `budget_enforcement` as `metered` (after the demonstration above) and the cost bound is established for the alias; otherwise no retry. A wall-clock deadline does not bound spend and is not used for this.
- **No retry** if any of the four values or the spend of a resolved attempt cannot be established, if either unit fails, or if `deadline_ms` has passed. The request then fails with `possible_duplicate=unknown`, never a guess. A known-not-accepted failure reserves nothing, because nothing ran.
- **Deterministic test cases** (attempt = 1,000 tokens and 100,000 micro-USD): total 2,000 tokens and 200,000 micro-USD admits the retry (exactly two allowances); total 1,999 tokens with sufficient cost refuses; sufficient tokens with total 199,999 micro-USD refuses; totals absent (default to one attempt) refuses; `budget` absent entirely refuses; attempt fields absent with a configured default and a sufficient total admits; a zero or non-integer field refuses; total 5,000 with a second unknown outcome after the retry makes no third attempt (at most 2 attempts); a known-not-accepted failure with total equal to one attempt still fails over, with no reservation. Enforcement cases (mocked LiteLLM): a run making three calls stops starting new calls once the attempt's token limit is reached; a call whose response has no usage is charged its full `max_tokens`; an alias with no price entry has no cost bound, so a retry is refused; a primary not marked `metered` in the manifest gets no retry; no mocked attempt exceeds its limit by more than zero tokens of started calls. Pre-call reservation cases (mocked LiteLLM and price table): a call whose worst-case charge (input upper bound times input rate plus `max_tokens` times output rate) exceeds the remaining attempt cost allowance is refused before any model call is made (zero mocked calls recorded); the same holds when only the total allowance would be exceeded; a call whose input cost alone exceeds the remaining balance is refused; a call near the limit is sent with `max_tokens` reduced to the output affordable from the remaining cost, and refused when that is zero; many concurrent calls (threads or tasks) against one meter never reserve more than the allowance in total and the excess calls are refused; a response with no usage keeps the full reservation, so later calls see the reduced balance; a response with usage lower than the reservation frees the difference for later calls; a missing input or output rate refuses the call; a manifest `metered` value with no recorded acceptance evidence for the alias and current configuration is rejected at load.
- **Classification test cases** (mocked responses only): connection refused before send is known not accepted; a 429 without the marker is unknown; a 429 with the marker is known not accepted; a 409, a 502, a 503 and a 504 without the marker are unknown; a timeout and a reset after send are unknown; a marker on a response from an unexpected instance, or a marker alongside a result body, is unknown; an application error is "failure after running"; a `read_only` unknown outcome with a sufficient budget retries once.

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

Recomputed from the unit rates above at 730 hours per month. **Assumed, not verified:** two Availability Zones for the load balancer, and one LCU-hour as the load-balancer usage charge. One LCU is a floor for this light load, not a measured figure. Each additional LCU-hour sustained adds about $5.84 per month.

| Component | Arithmetic | Per month |
|---|---|---|
| ALB hourly charge | $0.0225 x 730 | $16.43 |
| ALB usage, 1 LCU-hour (assumed) | $0.008 x 730 | $5.84 |
| ALB public IPv4, 2 addresses (assumed, two AZs) | 2 x $0.005 x 730 | $7.30 |
| **ALB subtotal** | | **$29.57** |
| Fargate ARM task, 0.25 vCPU and 0.5 GB | (0.25 x $0.0324 + 0.5 x $0.0036) x 730 = $0.0099 x 730 | $7.23 |
| Fargate ARM task, 0.5 vCPU and 1 GB | (0.5 x $0.0324 + 1 x $0.0036) x 730 = $0.0198 x 730 | $14.45 |
| Task public IPv4, 1 address | $0.005 x 730 | $3.65 |
| AgentCore on demand (range) | per the basis above; platform version and active-CPU share vary | $7.50 (range $3 to $40) |

| Scenario | Arithmetic | Per month |
|---|---|---|
| Fargate stack always on | $29.57 + $3.65 + ($7.23 to $14.45) | **$40.45 to $47.67** |
| AgentCore plus warm Fargate standby | AgentCore $3 to $40 + Fargate stack $40.45 to $47.67 (typical: $7.50 + $40.45 = $47.95) | **$43.45 to $87.67** |
| Fargate desired count 0 (ALB only) | $29.57 (the task and its IPv4 are gone; the ALB and its addresses remain) | **$29.57** |
| AgentCore only | no idle floor | $3 to $40 |

A NAT gateway would add about $33 per month and is avoided.

- Temporary: the load balancer and service can be created and deleted per test. Per hour: ALB $0.0225 + 1 LCU $0.008 + 2 IPv4 $0.010 = $0.0405, plus the task ($0.0099 or $0.0198) and its IPv4 ($0.005), so about **$0.055 per hour** for the small task and **$0.065** for the larger one. A four-hour test is about $0.22 to $0.26 plus inference, under the same two-AZ and one-LCU assumptions. Usage-based ALB charges are billed per started hour, which is unverified here.

## Validation plan

Deterministic tests with mocked responses for: primary available, primary unavailable, secondary available, secondary unavailable, routing decisions, timeout handling, retry eligibility (the budget-boundary and not-accepted classification cases in the failover section), authorization, request correlation, failure reporting. No repeated paid inference. After approval: verify both instances' health, run one authorized research request, make the primary unavailable, confirm eligible failover, confirm correlation and that inference went through LiteLLM. Use the fewest live calls possible.

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
