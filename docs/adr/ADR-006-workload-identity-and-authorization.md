# ADR-006: Agent workload identity and platform authorization

**Status: Proposed.** Nothing in this document is implemented. It is intended to satisfy the identity and authorization design that issues #38 and #39 depend on, and to inform the ADR-001 reconciliation tracked by #49. The founder confirms the numbering and the decisions below before implementation starts.

## Context

Agents are identities (`src/viharbot/agent_identities`) bound to services (`execution_config/bindings.toml`). The Phase 6.5 audit (section 12) records that agent communication is structured and observable but **unauthenticated**. Verified in the repository, at the level the audit already states publicly:

- Only the browser-facing web tier authenticates callers; agent-to-agent calls carry a correlation ID and no credential, and authorization decisions are made only inside the web process. This is the state the audit and issues #38 and #39 already describe publicly. This ADR deliberately does not enumerate affected routes or source locations, which would be a map of the gap; the maintainer can supply that detail privately to whoever implements A2.
- The constitution requires zero-trust, authentication and audit (Sections 5.4, 7.1) and says delegation must not expand authority (Section 2.3). Infrastructure does not enforce either today.

The platform will soon span DigitalOcean containers, Amazon Bedrock AgentCore, and ECS/Fargate. No single cloud identity system authenticates all three, and AWS IAM does not authenticate DigitalOcean workloads.

## Decision

1. **Every call between ViharBot services carries a short-lived signed token, and every callee verifies it itself.** The callee never trusts a caller, a network position, or a claim in the request body.
2. **Token format: a signed JWT carried in the `X-Viharbot-Assertion` header** (not `Authorization`, which SigV4 and load balancers use, so the two never collide). Claims: `iss`, `sub` (caller identity), `aud` (target service), `exp` (60 seconds or less), `iat`, `jti`, `cap` (capability), `eng` (engagement), `cls` (data-classification ceiling), `dlg` (the issuer-signed delegation grant, see below), `mth` (HTTP method), `tgt` (canonical request target: path plus the sorted query string), `req_sha256` (hash of the body bytes as the callee receives them, including the empty body).
   - **Verification profile (normative).** Both token types are JWS compact JWTs and must pass all of these, or the callee rejects them:
     - **Algorithm allowlist:** `EdDSA` (Ed25519) only. `none`, every `HS*` algorithm and any algorithm not on the list are rejected, whatever the `alg` header says.
     - **Key resolution:** the `kid` header is required and is looked up **only** in the callee's local key registry (repository configuration, loaded at start and refreshed as below). Headers that point elsewhere or carry a key (`jku`, `x5u`, `x5c`, `jwk`) are ignored and never fetched or trusted.
     - **Token type:** the `typ` header must be `viharbot-assertion+jwt` for an assertion and `viharbot-grant+jwt` for a grant, and a token is rejected if used as the other type.
     - **Issuer:** an assertion's `iss` must equal the `sub` the registry binds to its `kid`; a grant's `iss` must equal the configured issuer ID and be signed by an active issuer `kid`.
     - **Time:** `exp`, `iat` and not-before are checked with at most 10 seconds of clock skew.
     - **Required claims by type.** An assertion must carry `iss`, `sub`, `aud`, `exp`, `iat`, `jti`, `cap`, `eng`, `cls`, `dlg`, `mth`, `tgt` and `req_sha256`. A grant must carry exactly the grant-schema fields below (`gid`, `rcp`, `par`, `depth`, `depth_max`, `eng`, `caps`, `cls`, `exp`) plus `iss` and `iat`; it carries no request-binding claims and no embedded `dlg`. Unknown claims are ignored, but every required claim must be present and well-typed.
   - `sub` and every other identity claim come from the verified token. A value in the body or in an `X-Agent` header is ignored.
   - **Request binding.** The callee recomputes `mth`, `tgt` and `req_sha256` from the request it actually received, using the canonical form below, and rejects any mismatch. A token minted for one route, artifact ID, method or security-relevant query parameter is therefore unusable for another, even when the body is empty (for example two downloads of different artifacts).
   - **Canonical request target (`tgt`), normative.** Issuers and callees must produce identical bytes, and the callee derives them from the raw request target exactly as its application receives it (a proxy that rewrites the target must be configured to preserve it, or requests fail the match). `mth` is the upper-case method.
     - **Path:** used as received, with **no normalization**. The callee rejects the request (400) if the path contains an empty segment (`//`), a `.` or `..` segment, a percent-encoded `.`, `/`, `\` or NUL (`%2e`, `%2f`, `%5c`, `%00`, in any case), a non-canonical percent-encoding (lower-case hex digits, or an encoded unreserved character), or any byte outside printable ASCII. An issuer never produces such a path.
     - **Query:** split on `&` into pairs; each pair is `name` or `name=value`, with the name and value percent-encoded canonically (upper-case hex, every byte encoded except the unreserved characters `A-Z a-z 0-9 - . _ ~`). Pairs are kept **in the order received, with duplicates kept and not merged or sorted**, because many applications give meaning to order (first or last value wins, or values are positional), and reordering would let two different requests share one signed target. A bare `name` and `name=` are different and stay different. A query string with an empty pair, or `;` as a separator, is rejected. Canonicalization therefore only normalizes encoding; it never changes which request the application sees.
     - **Result:** `tgt` is the path, followed by `?` and the sorted query only when a query exists. Fragments are never sent and are never part of it.
     - **Test vectors** (both sides must agree): `/files/report.bin` gives `/files/report.bin`; `/x?b=2&a=1&a=0` stays `/x?b=2&a=1&a=0`, and `/x?a=0&a=1` and `/x?a=1&a=0` are different targets; `/x?q=a%20b` and `/x?q=a+b` are different targets; `/x?a` and `/x?a=` are different targets; `/a//b`, `/a/../b`, `/a/%2e%2e/b` and `/a/b%2Fc` are all rejected; `/x?q=%7e` (an encoded unreserved character) is rejected.
3. **Authorization tuple, enforced by the callee independently of Charlie:** caller identity, target service, capability, resource, engagement, data classification, delegation context. The callee compares `cap` with its own fixed capability and computes the effective scope as the intersection of the issuer-signed grant, the caller's registry entry and its own policy (see "Key authority"). Charlie's existing checks remain a first pass, not the authority.
4. **Policy lives in versioned repository configuration**, in the style of `bindings.toml`, not in prompts. Models never decide authorization.
5. **Transport.** A token is not confidentiality: it does not stop an observer on the path from reading prompts, artifacts or the bearer token, and it does not stop an intercepted request from being replayed once before the original arrives. Therefore:
   - **TLS is required** for every connection that crosses a platform or cloud boundary or any public path: DigitalOcean to AWS, anything through an ALB or API endpoint, and any call to LiteLLM.
   - Plaintext HTTP between two containers on the same private network is the one exception `AGENTS.md` already allows (credentials over HTTP only to a literal loopback or private IP). This ADR does not relax that rule, and it **records the exception as an accepted confidentiality and interception risk**, not a protected path. The JWT, the audience and the body hash narrow what an interceptor can do, but do not remove the risk. The founder decides whether to tighten `AGENTS.md` to require TLS everywhere once the platform allows it. The verified DigitalOcean internal-routing documentation neither promises nor denies in-transit encryption.
6. **Fail closed.** A missing, malformed, expired, wrong-audience, wrong-capability or unknown-key token returns 401 or 403. A service started with authentication enabled and no verification keys refuses to start. `/health` stays open and returns nothing sensitive; every other route, including artifact downloads, requires a token.
7. **Replay protection:** short expiry, a per-process cache of `jti` values held until expiry plus clock skew (10 seconds at most), audience binding, and the body hash. A callee with several replicas needs shared state for this cache (open question 5).
8. **Keys and who may sign what** (see the next section): asymmetric signing keys, one issuer key and one key per service, two public keys valid at a time (`kid`) so rotation overlaps, 30 to 90 day rotation, revocation by removing a `kid`. Secrets come from the platform's secret store, never from source.
9. **Logs record allow and deny decisions** with `request_id`, caller, target, capability and reason. They never record tokens, keys, prompts or memory content.

### Key authority: which key may assert what

Verifying a signature proves who signed, not that the signer may assert a given subject or scope. Two token types keep those separate.

| Token | Signed by | May assert |
|---|---|---|
| **Caller assertion** (the `X-Viharbot-Assertion` token on a call) | The calling service's own key | Only that service's fixed identity. Each callee holds a registry mapping `kid` to one `sub` and the capabilities that identity may ever request. A token whose `sub` differs from the registry entry for its `kid` is rejected. |
| **Delegation grant** (`dlg`, embedded in the assertion) | The issuer key only (the web process or the internal signer, option A or B below) | The scope of one delegation to one recipient, per the schema below. |

**Delegation grant schema (normative).** Every field is covered by the issuer's signature.

| Field | Meaning |
|---|---|
| `gid` | Unique grant ID |
| `rcp` | The one recipient `sub` this grant was issued to |
| `par` | The `gid` of the grant it narrows, or the ID of the authenticated user request for a first-hop grant (the originating authorization) |
| `depth` and `depth_max` | Hops from the user request, and the maximum allowed |
| `eng` | Engagement |
| `caps` | Capabilities permitted, each with an optional resource scope (path or resource patterns the capability may touch) |
| `cls` | Data-classification ceiling |
| `exp` | Expiry; never later than the parent's expiry |

**Redelegation is issuer-mediated. Services never forward or re-sign grants.** When a service needs another service to act, it sends the issuer a redelegation request carrying **its own caller assertion and the parent grant it holds**, and naming the new recipient. The issuer signs a child grant only if all of these hold:

- the requester's assertion verifies and its `sub` **equals the parent grant's `rcp`** (so someone who merely obtained another service's grant cannot use it to mint a child for an identity they control);
- the parent grant verifies, is unexpired and has not been revoked (see "Revocation" below);
- **the requester's own authority bounds the child:** every capability and resource scope in the child must lie within the **intersection** of the parent grant, the requester's registry entry and the applicable policy, so a service can never obtain for a peer a capability it could not exercise itself (a capability in the parent grant that the requester's registry entry excludes is not passed on);
- the child's `eng` **equals the parent's `eng`** (a delegation never moves to another engagement), its `rcp` is the named recipient, its `par` is the parent's `gid`, and its `caps`, resource scope, `cls` and `exp` are each equal to or narrower than the parent's;
- **depth strictly increases**: `child.depth = parent.depth + 1`, and `child.depth <= parent.depth_max`;
- **`depth_max` can only stay the same or shrink**: `child.depth_max <= parent.depth_max`, so redelegation can never raise the limit or keep depth flat.

The issuer enforces that attenuation before signing. This removes the need for a hop list that could be truncated or reordered: the issuer-signed `par` and `depth` are the chain.

**Revocation.** Revoking a grant stops the issuer from minting new children of it. It does **not** retroactively invalidate grants already issued, because callees verify signatures offline and keep no revocation list. The exposure is bounded by lifetime: a grant's `exp` is capped at **5 minutes**, and a child never outlives its parent. This is an accepted, bounded risk. For an emergency (a compromised issuer key or service), the cutoff is to retire the affected key's `kid` from the registry.

**Key-registry propagation bound.** The registry is repository configuration delivered to each callee by deployment. A callee re-reads it from its mounted configuration **at least every 60 seconds** and additionally once, rate-limited to every 10 seconds, when it sees an unknown `kid`. If a refresh fails, the callee keeps using its last-known registry for **at most 15 minutes** and then **fails closed**, rejecting every request. An emergency retirement therefore takes effect on each callee within the time to deliver the new registry plus 60 seconds, and the stated target for delivering it is 15 minutes, which must be measured, not assumed. Tests: a retired `kid` is refused after a refresh; a refresh failure within the grace period keeps the last-known keys; a failure past 15 minutes rejects everything; an unknown `kid` triggers at most one rate-limited reload; `jku`, `x5u`, `x5c` and `jwk` headers are ignored; `alg=none` and an `HS256` token signed with a public key are refused; a grant presented as an assertion, and the reverse, are refused. Test: a child issued before revocation stays valid only until its `exp`; no child is issued after; a retired issuer `kid` is refused at once.

**Callee validation algorithm.** Reject unless all of these hold: the assertion verifies and its `sub` matches the registry for its `kid`; the grant verifies against an active issuer `kid`; the grant's `rcp` equals the assertion `sub` (so a grant cannot be moved to another caller); the assertion's `eng` **equals the grant's `eng`**, and the resource being accessed belongs to that engagement (checked against the callee's resource-to-engagement mapping, never against a value in the request body); `exp` has not passed; `mth`, `tgt` and `req_sha256` match the actual request; the requested capability is in `caps` and the request's `tgt` falls inside that capability's resource scope; `cls` covers the data; `depth` is within `depth_max`; and the effective scope, the **intersection** of the grant, the caller's registry entry and the callee's own policy, still permits the action.

Rules:

- A service key can never sign a grant, and a service cannot hand its grant to a peer, so a compromised specialist can neither forge another subject nor reuse a grant meant for someone else.
- Only the issuer creates a first-hop grant, only for an authenticated user request and within platform policy for that user's engagement, never on the strength of a model's plan.
- Both registries and the policy are repository configuration reviewed like `bindings.toml`.

Negative tests: a service key used to claim another `sub`; a service key used to sign a grant; a grant transplanted to a different caller (`rcp` mismatch); resource substitution (a grant scoped to one resource used against another); an expired grant, and a child grant outliving its parent; a redelegation request wider than its parent in capabilities, engagement, classification, resource scope or depth (the issuer refuses); a grant past `depth_max`; a redelegation request whose requester `sub` is not the parent grant's `rcp` (a stolen parent grant); a redelegation request for a capability that the parent grant allows but the requester's registry entry or policy excludes (refused); a child with unchanged or lower `depth`, or with a raised `depth_max`; a child grant naming a different engagement than its parent; an assertion whose `eng` differs from its grant's; a resource that belongs to another engagement than the grant names; a grant signed by a retired issuer `kid`; a grant whose `par` is unknown to the issuer; a child issued before revocation that outlives its `exp`; an envelope with an altered `input` or `request_id` under the fallback carrier.

### Runtime-specific rules

- **DigitalOcean:** private service-to-service traffic plus the token. The verified DigitalOcean internal-routing documentation says nothing about authentication or encryption, so the token is the control, not the network. No DigitalOcean workload identity offering was found; none is assumed.
- **Amazon Bedrock AgentCore:** a runtime accepts IAM SigV4 or a custom JWT authorizer, not both (verified, AWS docs). Keep SigV4 for coordinator to runtime, which `agentcore_client.py` already uses, and **also** verify the application assertion inside the container, so one verifier serves every runtime. Two credentials, two carriers: the **outer** SigV4 signature authenticates the coordinator's AWS principal to the AgentCore service, and the **inner** assertion travels in `X-Viharbot-Assertion` (never `Authorization`), which requires adding that header to the runtime's request-header allowlist. Whether the invocation path forwards arbitrary headers, and under what naming rule, is **not yet verified**. If it does not, the fallback carrier is an `assertion` field in the `/invocations` envelope, and the token then binds `POST`, the target `/invocations`, and `req_sha256` computed over the **RFC 8785 JSON Canonicalization Scheme (JCS)** form of the whole envelope **except the `assertion` field itself** (that is, `request_id` and `input` today, and any envelope field added later). Every field the container uses is therefore protected, and independent issuers and adapters produce the same bytes. An offline adapter test must prove that the container sees and validates the assertion for whichever carrier is used; live compatibility is an explicit verification gate before AgentCore is relied on. Do **not** depend on the JWT authorizer accepting a self-hosted issuer: the AWS example shows only Cognito, so that is unverified.
- **ECS/Fargate:** an Application Load Balancer cannot verify SigV4, so the in-app token check is mandatory there.
- **LiteLLM:** unchanged. Each model-using service keeps its own key and alias, and per-agent keys depend on how the external gateway is configured, which ViharBot does not manage.

### Where the issuer's key lives (founder decision)

| Option | Effect |
|---|---|
| A. The web process signs | Simplest. A compromise of the public-facing tier can mint tokens for every agent. |
| B. A separate internal signer service ("Charlie service") | Smaller blast radius. One more service and one more trust point. |

Recommendation: implement A first behind an interface, and move to B before any agent is reachable from outside the private network or deployed to AWS.

**First-hop grant contract for the separate signer (option B).** A separate signer reduces the blast radius only if it does not simply believe the web process. The signer therefore decides first-hop grants from **its own policy**, not from claims the web process makes:

- The web process may present only: an identifier for the authenticated user session, the engagement the user selected, and the capabilities the request needs. It never states what the user is allowed to do.
- The signer holds a repository-configured **policy** mapping (user or session class, engagement) to the capabilities, resource scopes and classification ceiling that may ever be granted. It issues a first-hop grant for the **intersection** of the request and that policy, never more.
- The signer verifies that the session identifier is a live session **that the signer itself issued or can verify** at login, and that the user belongs to the engagement. Until the web tier has per-user identity (today there is one shared login), sessions are all one class, and the policy for that class is the ceiling.
- First-hop grants are rate-limited per session and per engagement, short-lived (the 5-minute cap above), and every issuance is audited with the session, engagement and resulting grant ID.
- **Residual risk, stated plainly:** a fully compromised web process can still obtain every grant that policy allows for any live session it can name, up to the rate limit, because it holds the session credentials. Option B bounds the damage to the policy ceiling and to the sessions that exist, and makes mass minting visible through rate limits and audit; it does not make the web tier trustworthy. Per-user identity and a signer-controlled login (so the web process never holds a reusable session credential) are the way to shrink this further and are tracked as an open question.
- Tests: a request naming capabilities beyond policy receives only the intersection; an unknown or expired session receives nothing; a session not in the engagement receives nothing; the rate limit stops mass issuance and is audited; the signer never issues a first-hop grant when the web process asserts permissions the policy does not give.

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
- **A3:** capability authorization at the callee and at the model and memory call sites (#38), with the issuer-signed delegation grant.
- Per-agent LiteLLM keys and audit-log fields follow.

## Required tests (deterministic, no inference, no cloud calls)

No token, malformed token, unknown `kid`; expired and not-yet-valid token with an injected clock; wrong audience; tampered body; replayed `jti` and eviction after expiry; identity supplied in body or header is ignored; capability the callee does not own; disabled or missing binding; delegation grant missing, forged, past the depth limit or exceeding scope; a token reused against another route, method, artifact ID, query parameter or body (including an empty body); a service key used to claim another subject or to sign a grant; classification above the ceiling; cross-agent denial (a Cipher token sent to Nova) and each approved binding's happy path; key rotation overlap and revocation; authentication enabled with no keys refuses to start; unreadable keys returns 503; `/health` open while every other route, including artifact downloads, needs a token; log hygiene; no key material in source.

## Open questions

1. Does AgentCore's JWT authorizer accept a non-Cognito issuer? (Verify before relying on it.)
2. Does DigitalOcean offer workload identity or internal encryption? (None found.)
3. Where does the issuer key live, and who rotates it? (Option A or B above.) For option B, how does login become signer-controlled so the web process never holds a reusable session credential?
4. How are engagement and classification established when the web tier has one shared user?
5. How many replicas does each agent run, given the in-memory replay cache?
6. Which signing library is acceptable as a dependency?

## Sources and verification dates

Retrieved 2026-11-02 by a read-only assessment. These pages are summarised here, not quoted, and the specific passages were not re-captured; re-check them before implementation. "Not documented" below means the page says nothing, which is not evidence that a feature is unsupported.

| Claim | Source | Status |
|---|---|---|
| AgentCore Runtime inbound auth is IAM SigV4 or a custom JWT authorizer, not both on one runtime; a JWT authorizer takes a discovery URL, allowed clients, audiences and scopes; passing `Authorization` to the agent needs a request-header allowlist | https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-oauth.html | Documented |
| Whether the JWT authorizer accepts a non-Cognito, self-hosted issuer | the same page shows only a Cognito example | **Not documented; unverified** |
| DigitalOcean App Platform service-to-service routing by internal name | https://docs.digitalocean.com/products/app-platform/how-to/manage-internal-routing/ | Documented; the page does not mention authentication, mutual TLS, workload identity or in-transit encryption |
| Any DigitalOcean workload-identity or OIDC offering | none found | **Not documented; assumed absent** |
| Repository facts (no agent authentication, authorization only in the web process) | the files cited under Context | Verified in the repository at commit `main` of 2026-11-02 |

## Related

#38, #39, #40, #44, #49, ADR-001. Threat model: `docs/threat-model-agent-communication.md`. Roadmap: `docs/roadmap-distributed-agents.md`.
