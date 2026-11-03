# ADR-007: Orbit requests deployments; a dedicated controller executes them

**Status: Proposed.** Nothing here is implemented, and no DigitalOcean API was called to write it. It builds on the identity model in ADR-006 and does not change `deploy_on_push: false` or the "ask before promoting to Stage" rule in `AGENTS.md`.

## Context

Verified in the repository:

- Stage is one App Platform app (`deploy/digitalocean/app.yaml`, region `nyc`): a public `web` component and six private agents on internal ports. Every component sets `deploy_on_push: false`. No Stage app exists today (`docs/deployment-strategy.md`).
- Compose mounts no Docker socket, and there is no Orbit service.
- Orbit declares only `automation_engineering`, which is a **planned** capability, and its prompt says it has no infrastructure access and never handles credentials. `execution_bindings.py` refuses a binding for a planned capability and requires one service per capability.
- Controlled delegation treats model output as untrusted and only allows allowlisted workflows (`docs/controlled-delegation.md`).

Verified in DigitalOcean documentation: App Platform specs accept image sources from DOCR, Docker Hub or GHCR, and a `digest` mutually exclusive with `tag`; billing is per second; API token scopes use `resource:action` names such as `app:create`, `app:read`, `app:update`, `app:delete`. **Not verified:** whether scopes can be restricted to specific apps or tags (the page says nothing, so none is assumed), which endpoint needs which scope, and any stop or scale-to-zero operation (none is documented).

## Decision

**Orbit requests. A deployment controller authenticates, authorizes and executes.** Orbit and every other agent never hold DigitalOcean credentials.

1. **Mechanism:** the App Platform API with image-sourced components. No Docker daemon, no socket, no Droplets, no Kubernetes.
2. **The controller** is one small FastAPI service and its own App Platform component, on an internal port only, speaking plain JSON REST.
   - `POST /v1/deployments` (create or replace), `GET /v1/deployments/{id}`, `GET /v1/deployments/{id}/logs?lines=N` (capped), `POST /v1/deployments/{id}:stop`, `DELETE /v1/deployments/{id}`, `GET /health`.
   - "Stop" is destroy-and-keep-the-record unless a real stop operation is verified.
3. **A request is data, not a spec.** It carries `request_id`, `name`, an image `{registry, repository, digest}`, a size, a region, an optional port, a short allowlist of environment keys, and `ttl_minutes`. The controller builds the app spec from a fixed template. Free-form specs, commands, volumes and secret values are not accepted.
4. **Controller-owned policy** (repository configuration, reviewed like `bindings.toml`):
   - Images from the project's registry prefix only, by digest. Tags are rejected.
   - One approved region and one size.
   - Names must start with `orbit-`. The controller refuses to touch any app it did not create.
   - At most 3 concurrent apps, 5 creates per hour, about 10 creates or replacements per day, 60 second provider-call timeout, 10 minute rollout poll.
   - Default lifetime 120 minutes, maximum 24 hours. A sweeper destroys expired apps, and startup reconciles the controller's records against `orbit-` apps and destroys orphans.
5. **Authentication and authorization:** the controller verifies Orbit's token under ADR-006 and authorizes the tuple (caller, capability, resource, classification) itself. Orbit gets only the controller URL and a token.
6. **The DigitalOcean API token** is a secret on the controller component only, with the narrowest scopes that work (`app:*` and read access to the registry, never `droplet:*` or registry write or delete), preferably in a **separate DigitalOcean team** from Stage, because scope-to-resource limits are not documented and the token could otherwise modify any app in its team, including `viharbot-stage`. This isolation is an assumption to verify.
7. **Human approval gate:** a request stays `pending` until a person approves it, consistent with the repository's promotion rule. Orbit can ask; it cannot cause a deployment alone.
8. **Audit:** a structured event per request with timestamp, `request_id`, caller, action, decision, deny reason, deployment and app IDs, image digest, size, region, TTL, provider status, duration and error. Never the token, environment values or full bodies.

## Mapping to the identity model

Add a new capability (for example `DEPLOYMENT_REQUEST`) bound to Orbit and fulfilled by a `deployment-controller` service. Suitability is not authorization, so do not reuse `automation_engineering`. Treat it like `code_review`: it is never delegated through chat and never enters Charlie's planner vocabulary. Mark it implemented only when the controller exists. Bindings never grant access; controller policy does. Orbit's "never handle credentials" prompt does not change.

## Alternatives considered

- **Droplets with Docker.** Needs a root-equivalent Docker daemon, which is the unrestricted-socket risk, plus OS patching and firewalling.
- **DigitalOcean Kubernetes.** A cluster cost floor plus RBAC and operator complexity, the opposite of the smallest controller.
- **Orbit holds the token.** Rejected by the constraint that ordinary agents never receive infrastructure credentials.
- **AWS instead of DigitalOcean for staging.** A separate, explicit provider choice (Phase C); not a replacement here.

## Consequences

The controller becomes a trust anchor: a compromise can destroy the apps it created (and anything its token can reach, hence the separate team). Its record store is a small JSON file, lost on redeploy, which is why startup reconciliation exists; no database is added. Orbit needs a runtime service before the flow works end to end, because it has none today.

## Staging validation

1. Mocked-provider tests of controller policy: each allowlist denial, quota exhaustion, TTL sweep, orphan reconciliation, audit fields, token redaction. No network.
2. Contract tests using fixtures captured from the first real responses. Do not invent DigitalOcean response shapes.
3. Minimal live test, **after explicit approval**, in a throwaway team: create one 512 MiB app from a tiny image by digest, inspect it, read logs, destroy it, confirm billing stops. This also establishes the real scope-to-endpoint mapping and stop behavior.
4. Negative live test: confirm the token cannot touch `viharbot-stage`.

## Cost

Estimates, not commitments: the controller is one shared 512 MiB component at about $5 per month; each test app about $0.01 per hour; with the 3-app cap the ceiling is about $15 per month; registry free (starter) or $5. Total about $5 to $10 per month idle and about $25 to $30 at the ceiling. These need confirming on DigitalOcean's pricing pages before approval.

## Open questions

Separate DigitalOcean team or the same one? Which repositories may the image allowlist name? What concurrency cap? Where does the human approval happen? Which scope does the logs endpoint need, how large may logs be, and can deployed apps leak secrets into logs? Should AWS be considered for staging instead?

## Related

ADR-006, #38, #39, `docs/deployment-strategy.md`, `docs/roadmap-distributed-agents.md`.
