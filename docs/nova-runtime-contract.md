# Nova runtime contract (C2a)

This implements the runtime-neutral groundwork of [ADR-008](adr/ADR-008-nova-dual-runtime.md), which is still **Proposed**. No AWS resource exists and nothing here was tested against AgentCore or Fargate.

## What is implemented

Only the research agent's `/invocations` route (logical agent `nova`) uses the new behavior. Other agents' `/invocations` routes are unchanged. A bare `{request_id, input}` invocation works exactly as before.

- **Session id fix.** `agentcore_client.invoke_agentcore` now sends `runtimeSessionId = "nova-" + uuid4()` (41 characters; AgentCore requires at least 33). The request id (1 to 128 characters) stays in the payload.
- **Optional envelope fields**, validated strictly (strict types, bounds, unknown values refused with 422): `logical_agent` (must equal `nova`), `contract_version` (`"1"`), `attempt` (1 or 2), `deadline_ms` (1 to 900000), `attempt_max_output_tokens` and `total_max_output_tokens` (0 to 1,000,000), `attempt_max_cost_usd` and `total_max_cost_usd` (integer micro-USD, 0 to 100,000,000), `idempotency_key` (same character set as a request id), `operation_class` (`read_only` or `idempotent`). Unknown top-level keys are ignored as before. The ADR sketches the budget as one object; this uses four flat fields with the ADR's units.
- **Instance identity** from `NOVA_INSTANCE_ID` (default `nova-local-01`) and `NOVA_RUNTIME` (`local`, `agentcore` or `fargate`; default `local`). These are identity, not feature flags. A malformed value makes the route refuse with 503 before any work.
- **Additive response fields** `runtime_instance`, `runtime_kind`, `duration_ms`, `attempt` (1 when absent) and an `X-Nova-Instance` header. Existing fields are never overwritten.
- **`X-Nova-Not-Accepted: 1`** is set on refusals made before any work: envelope or payload validation failure (including malformed JSON, a missing body or a non-object body rejected by the framework), an authentication dependency refusal (including nested and generator dependencies, which FastAPI still runs unmodified), and the identity (startup) refusal. The marking is done by a route class that wraps the whole route, not by rewrapping dependencies. It is never set once the handler has started, so an inference failure (502) never carries it.
- **Logs.** The JSON formatter adds `logical_agent`, `runtime_instance`, `runtime_kind`, `attempt`, and the carried envelope fields. Nova's `research completed` and `research failed` logs include them when an envelope invocation is in progress. No prompts or secrets are logged.

## What is NOT done

- No budget meter, no admission rule and no enforcement: the budget fields are accepted and logged only. `budget_enforcement` stays unestablished.
- No retry, failover, routing, discovery, manifest or circuit breaker, and no coordinator change; the client does not yet send the envelope fields.
- No `nova_core` package split, no Fargate adapter, no `/health` token check (ADR-006 inbound authentication is owned elsewhere).
- Response-header pass-through by AgentCore's `invoke_agent_runtime` is unverified; the body-field fallback (`not_accepted: true`) is not implemented. Until demonstrated, every AgentCore error without a parsed marker must be treated as an unknown outcome.
- The marker is a claim by this adapter, not a guarantee from any AWS service.
