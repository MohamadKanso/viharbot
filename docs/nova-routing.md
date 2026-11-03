# Nova routing decision engine

`src/viharbot/nova_routing.py` implements the pure decision logic of ADR-008 (one logical Nova, two runtime instances). It is **not wired into `web_app.py` or any runtime path**, and nothing here has been run against AgentCore, Fargate, LiteLLM or AWS.

## What it decides

- **Manifest** (`parse_manifest`, `load_manifest`): validates a static instance manifest and fails closed (`ManifestError`) on any defect, including unknown fields. `metered` budget enforcement is rejected unless every acceptance-evidence field ADR-008 lists is recorded. `deploy/nova/instances.example.json` is an example with fake endpoints and placeholder ARNs and `budget_enforcement: none`; it is not a deployment file.
- **Circuit breaker** (`CircuitBreaker`): closed, open, half-open with an injectable clock. Failback needs N consecutive healthy probes supplied by the caller. A request reads the breaker once to choose its initial target; the breaker does not decide retries and cannot change mid-request. If no candidate has a closed breaker, the first candidate in priority order is still tried.
- **Classification** (`classify`): only a failure before any request bytes were sent, or a single `X-Nova-Not-Accepted: 1` marker on an authenticated response from the expected instance with no result body or application error, is "known not accepted". The `not_accepted` body field is honored for AgentCore instances only. Everything else that is not a completed or application-error response (bare 429, 409, 5xx, timeouts, resets) is an unknown outcome.
- **Decision table** (`next_action`) by operation class, exactly as ADR-008. `idempotent` unknown-outcome retries need a target with `honors_idempotency_key` set in the manifest and a request key; none does today.
- **Router** (`NovaRouter`): at most 2 attempts, strictly sequential, no hedging, deadline checked before each attempt, and an injected eligibility predicate so a request is never sent to an instance not authorized for its data classification. Failover can only narrow the candidate set, and stateful work starts on a non-primary only if the injected `shares_state` predicate allows it. Terminal `possible_duplicate` is cumulative across attempts.
- **Budget admission** (`retry_budget_admitted`): after an unknown outcome, in integer tokens and micro-USD, `total - spent - attempt >= attempt` for tokens and cost separately, with the unresolved primary reserved at its full attempt allowance. Absent defaults, zero, negative, non-integer or missing values refuse. Both the instance with the unknown outcome and the retry target must be `metered` in the manifest. A known-not-accepted failover reserves nothing.
- **Decision records** (`RoutingDecision.to_log`): logical agent, instance, kind, attempt, `failover_from`, `failover_reason`, `routing_decision`, `breaker_state`, `deadline_remaining_ms`, `possible_duplicate` and request id. No prompts or secrets.

## What it deliberately does not do

No network calls, no AWS or LiteLLM calls, no health probing (callers feed probe results in), no cost metering or price tables (ADR-008's `nova_core` meter is separate and unimplemented), no `nova_core` adapters, no Fargate or AgentCore provisioning, and no wiring into the coordinator. Retries are admitted only on paper until a real meter exists and `metered` evidence is recorded.

## Tests

`tests/test_nova_routing.py` uses a scripted fake `Transport` and a fake clock. It is fully offline.
