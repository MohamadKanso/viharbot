# Threat model: agent communication across clouds

**Status: Proposed.** This describes threats and the controls ADR-006 proposes. The controls are not implemented. Where a claim about a cloud product is not verified, it says so.

## Assets

User prompts and engagement data; research findings and agent memory; LiteLLM keys; the execution bindings and authorization policy; deployment credentials (future); generated decks and other artifacts.

## Trust boundaries

| Boundary | Today |
|---|---|
| Browser to web/Charlie | One shared Basic-auth login, off unless enabled |
| Charlie to specialist agents | No authentication; network position is the only control |
| Specialists to external LiteLLM | HTTPS with a per-service key (every service currently receives the same variable) |
| Web/coordinator to AgentCore | AWS SigV4 with ambient credentials; nothing is provisioned |
| DigitalOcean containers | Internal ports only; internal-routing docs describe no authentication or encryption |
| ECS/Fargate | Does not exist yet; everything about it is an assumption |
| Memory | Local append-only files, ephemeral on DigitalOcean; nothing reads them back |
| Deployment infrastructure | Manual `doctl`; a push to `main` deploys nothing; Orbit has no credentials |

The web tier holds the planner key when the live team is enabled, so it is both the most exposed component and the most privileged caller.

## Threats and controls

| Threat | Control (ADR-006) | Residual risk |
|---|---|---|
| **Impersonation:** any workload on the network calls any agent | Callee verifies a signed token; each key is bound to one subject; `sub` comes only from the token | Compromise of the issuer key |
| **Interception of traffic** (prompts, artifacts, bearer token) | TLS across platform or cloud boundaries; request binding and short expiry limit a stolen token | Plaintext between containers on one private network is an accepted risk, not a control |
| **Unauthorized invocation** of agent endpoints and artifact routes | Token required on all but `/health`; capability check at the callee | A misconfigured route left open; covered by a route-enumeration test |
| **Credential compromise** | Short expiry, per-service keys, `kid` rotation and revocation, secrets from the platform store | A stolen key is valid until revoked |
| **Replay** | `exp` of 60 s or less, `jti` cache, audience and body-hash binding | Multi-replica callees need shared replay state |
| **Lateral movement** from one compromised agent | Callee-side allowlist of caller to capability; internal ports only; per-agent keys | Agents that legitimately call each other |
| **Excessive privilege** | Bindings grant suitability only; policy decides; DigitalOcean and AWS credentials never go to ordinary agents | Policy mistakes; reviewed by humans |
| **Unauthorized or laundered delegation (confused deputy via Charlie)** | Platform authorizes the tuple independently; the user's engagement scope bounds data and tools; only the issuer signs delegation grants, and the effective scope is an intersection | A prompt-injected plan can still request something allowed |
| **Cross-agent data leakage** | Per-engagement and per-agent scope checks at memory and data boundaries (ADR-009) | Content copied into prompts is outside this control |
| **Cross-runtime trust** (DigitalOcean, AgentCore, Fargate) | The same in-app token check on every runtime; no reliance on cloud identity across clouds | Differences in what each cloud logs |
| **Prompt injection** | Retrieved content is data; authorization never reads model output | Model may still be misled in what it writes |
| **Compromised workload** | Short-lived tokens, key rotation, no prompts or secrets in logs | Data the workload could already read |
| **Unauthorized infrastructure deployment** | Only the deployment controller holds provider credentials; Orbit requests, the controller authorizes (ADR-007) | Controller compromise |
| **Issuer-key theft from the public tier** | Move signing to a separate internal service (ADR-006 option B) | One more trust point |
| **Direct Bedrock calls bypassing LiteLLM** | AWS roles for Nova grant no `bedrock:InvokeModel` (ADR-008) | Role drift; add a check |

## What is not covered

Denial of service, supply-chain compromise of dependencies or images, the security of the external LiteLLM gateway, user-level identity on the web tier, and physical or account-level compromise of a cloud provider.

## Verification status

Sources are listed with retrieval dates (2026-11-02) in `docs/adr/ADR-006-workload-identity-and-authorization.md`, section "Sources and verification dates". In short:

- **Verified in the repository:** the current absence of agent authentication and where authorization runs.
- **Documented by AWS** (https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-oauth.html): AgentCore accepts SigV4 or a JWT authorizer, not both on one runtime.
- **Not documented by DigitalOcean** (https://docs.digitalocean.com/products/app-platform/how-to/manage-internal-routing/): the internal-routing page says nothing about authentication or encryption. That is the absence of documentation, not proof the feature does not exist.
- **Unverified:** whether AgentCore's JWT authorizer accepts a self-hosted issuer, and any DigitalOcean workload-identity or internal-encryption feature.

Transport confidentiality is a separate matter from authentication; ADR-006 records the private-network plaintext exception as an accepted risk.
