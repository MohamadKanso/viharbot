# ADR-006: Agent workload identity and platform authorization

**Status: Proposed.** Nothing in this document is implemented. It is intended to satisfy the identity and authorization design that issues #38 and #39 depend on, and to inform the ADR-001 reconciliation tracked by #49. The founder confirms the numbering and the decisions below before implementation starts.

## Context

Agents are identities (`src/viharbot/agent_identities`) bound to services (`execution_config/bindings.toml`). The Phase 6.5 audit (section 12) records that agent communication is structured and observable but **unauthenticated**. Facts verified in the repository:

- Only the browser-facing web tier authenticates, with one shared Basic-auth login that is off unless `WEB_AUTH_ENABLED` is set (`web_app.py:55-87`).
- Every agent endpoint, including `/process` and `/model`, accepts any caller (`specialist_agent.py:171-245`, `sage_agent.py`, `pixel_agent.py`). Pixel's `/decks/{id}.pptx` and `.html` routes are open too (`pixel_agent.py:190,202`).
- Outgoing calls carry only `Content-Type` and `X-Request-ID` (`demo.py:123-127`). The request ID is validated for shape only (`observability.py:14-17`), so it is correlation, not authentication.
- Authorization exists only inside the web process: `validate_delegation` (`delegation.py`) and `authorize_action` (`orchestration_policy.py`). No agent re-checks it.
- The AgentCore adapter has an unused hook for an auth dependency (`agentcore_runtime.py:18,26`).
- The constitution requires zero-trust, authentication and audit (Sections 5.4, 7.1) and says delegation must not expand authority (Section 2.3). Infrastructure does not enforce either today.

The platform will soon span DigitalOcean containers, Amazon Bedrock AgentCore, and ECS/Fargate. No single cloud identity system authenticates all three, and AWS IAM does not authenticate DigitalOcean workloads.

## Decision

1. **Every call between ViharBot services carries a short-lived signed token, and every callee verifies it itself.** The callee never trusts a caller, a network position, or a claim in the request body.
2. **Token format: a signed JWT** (`Authorization: Bearer`), over HTTPS, or HTTP to a literal loopback or private IP only, as `AGENTS.md` already requires.
   - Claims: `iss`, `sub` (caller identity), `aud` (target service), `exp` (60 seconds or less), `iat`, `jti`, `cap` (capability), `eng` (engagement), `cls` (data-classification ceiling), `chain` (delegation hops), `req_sha256` (hash of the request body).
   - `sub` and every other identity claim come from the verified token. A value in the body or in an `X-Agent` header is ignored.
3. **Authorization tuple, enforced by the callee independently of Charlie:** caller identity, target service, capability, resource, engagement, data classification, delegation context. The callee compares `cap` with its own fixed capability and checks the effective scope as the intersection of scopes along `chain`. Charlie's existing checks remain a first pass, not the authority.
4. **Policy lives in versioned repository configuration**, in the style of `bindings.toml`, not in prompts. Models never decide authorization.
5. **Fail closed.** A missing, malformed, expired, wrong-audience, wrong-capability or unknown-key token returns 401 or 403. A service started with authentication enabled and no verification keys refuses to start. `/health` stays open and returns nothing sensitive; `/model` and Pixel's deck routes require a token.
6. **Replay protection:** short expiry, a per-process cache of `jti` values held until expiry plus clock skew (10 seconds at most), audience binding, and the body hash. A callee with several replicas needs shared state for this cache (open question 5).
7. **Keys:** asymmetric signing keys, one issuer key and one key per service, two public keys valid at a time (`kid`) so rotation overlaps, 30 to 90 day rotation, revocation by removing a `kid`. Secrets come from the platform's secret store, never from source.
8. **Logs record allow and deny decisions** with `request_id`, caller, target, capability and reason. They never record tokens, keys, prompts or memory content.

### Runtime-specific rules

- **DigitalOcean:** private service-to-service traffic plus the token. The verified DigitalOcean internal-routing documentation says nothing about authentication or encryption, so the token is the control, not the network. No DigitalOcean workload identity offering was found; none is assumed.
- **Amazon Bedrock AgentCore:** a runtime accepts IAM SigV4 or a custom JWT authorizer, not both (verified, AWS docs). Keep SigV4 for coordinator to runtime, which `agentcore_client.py` already uses, and **also** verify the same token inside the container, so one verifier serves every runtime. Do **not** depend on the JWT authorizer accepting a self-hosted issuer: the AWS example shows only Cognito, so that is unverified.
- **ECS/Fargate:** an Application Load Balancer cannot verify SigV4, so the in-app token check is mandatory there.
- **LiteLLM:** unchanged. Each model-using service keeps its own key and alias, and per-agent keys depend on how the external gateway is configured, which ViharBot does not manage.

### Where the issuer's key lives (founder decision)

| Option | Effect |
|---|---|
| A. The web process signs | Simplest. A compromise of the public-facing tier can mint tokens for every agent. |
| B. A separate internal signer service ("Charlie service") | Smaller blast radius. One more service and one more trust point. |

Recommendation: implement A first behind an interface, and move to B before any agent is reachable from outside the private network or deployed to AWS.

## Alternatives considered

- **mTLS with a private CA.** Rejected for now: the way App Platform terminates internal traffic is undocumented, a certificate authority is a new system to operate, and AgentCore's data plane is not known to pass client certificates to the container.
- **SigV4 everywhere.** Rejected: it authenticates AWS principals only, so a DigitalOcean workload would need long-lived AWS keys.
- **A service mesh or identity broker.** Excluded by `AGENTS.md`.
- **Shared symmetric secret per pair.** Dependency-free (`hmac` in the standard library) and a fallback if a signing library is not acceptable, but any holder of the secret can mint tokens, which defeats per-service revocation. Asymmetric keys need a library such as `cryptography` or PyJWT; whether it is already installed transitively must be checked before adding a dependency.

## Consequences

- Every agent gains one request dependency and one policy file. Services that today run without keys will refuse to start when authentication is enabled.
- Operators must distribute public keys and rotate keys on a schedule.
- Tests must mint tokens with an injected clock, so tests stay offline and deterministic.

## Implementation sequence

- **A2:** authentication (token issue and verification, fail closed, replay cache, tests).
- **A3:** capability authorization at the callee and at the model and memory call sites (#38), with the delegation `chain`.
- Per-agent LiteLLM keys and audit-log fields follow.

## Required tests (deterministic, no inference, no cloud calls)

No token, malformed token, unknown `kid`; expired and not-yet-valid token with an injected clock; wrong audience; tampered body; replayed `jti` and eviction after expiry; identity supplied in body or header is ignored; capability the callee does not own; disabled or missing binding; delegation chain too deep, missing or exceeding scope; classification above the ceiling; cross-agent denial (a Cipher token sent to Nova) and each approved binding's happy path; key rotation overlap and revocation; authentication enabled with no keys refuses to start; unreadable keys returns 503; `/health` open while `/model` and deck routes need a token; log hygiene; no key material in source.

## Open questions

1. Does AgentCore's JWT authorizer accept a non-Cognito issuer? (Verify before relying on it.)
2. Does DigitalOcean offer workload identity or internal encryption? (None found.)
3. Where does the issuer key live, and who rotates it? (Option A or B above.)
4. How are engagement and classification established when the web tier has one shared user?
5. How many replicas does each agent run, given the in-memory replay cache?
6. Which signing library is acceptable as a dependency?

## Related

#38, #39, #40, #44, #49, ADR-001. Threat model: `docs/threat-model-agent-communication.md`. Roadmap: `docs/roadmap-distributed-agents.md`.
