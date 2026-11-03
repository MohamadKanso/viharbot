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
   - Names must start with `orbit-`. The controller refuses to touch any app it cannot **prove** it created (see "Ownership and recovery").
   - At most 3 concurrent apps, 5 creates per hour, about 10 creates or replacements per day, 60 second provider-call timeout, 10 minute rollout poll.
   - Default lifetime 120 minutes, maximum 24 hours. A sweeper destroys expired apps that carry valid ownership evidence. Startup reconciliation follows "Ownership and recovery"; it never deletes on a name prefix alone.
5. **Authentication and authorization:** the controller verifies Orbit's token under ADR-006 and authorizes the tuple (caller, capability, resource, classification) itself. Orbit gets only the controller URL and a token.
6. **The DigitalOcean API token** is a secret on the controller component only, with the narrowest scopes that work (`app:*` and read access to the registry, never `droplet:*` or registry write or delete).
   - **Isolation from Stage is a prerequisite for provisioning, not a preference.** Scope-to-resource limits are not documented, so the token could modify any app in its team, including `viharbot-stage`, and name or ownership checks inside the controller do not constrain someone who obtains the token. The controller therefore lives in a **separate DigitalOcean team** from Stage, or the project demonstrates an equivalent provider-enforced restriction. If neither is available, nothing is provisioned until the founder records an explicit risk decision, and this ADR's blast-radius claim is withdrawn for that deployment.
   - The negative test (below) that the token cannot touch `viharbot-stage` is an acceptance criterion.
7. **Human approval is a contract, not a gesture.** A request stays `pending` until a person approves it, consistent with the repository's promotion rule; Orbit can ask, never cause a deployment alone. An approval:
   - is an authenticated decision by a principal named in controller policy as an approver (the channel or interface is deliberately left open);
   - is **bound to the exact request**: it stores a hash of the canonical request (image digest, size, region, port, environment keys, TTL, name), and any change to those fields invalidates it;
   - **expires** (default 30 minutes) and is **single use**: once a deployment is created from it, it cannot authorize another;
   - records the approver, the approved request hash, the time and the resulting deployment ID in the audit event.
   Required tests: an unauthorized approver is refused; an expired approval is refused; an approval replayed for a second deployment is refused; a request changed after approval is refused; Orbit cannot approve its own request.
8. **Audit:** a structured event per request with timestamp, `request_id`, caller, action, decision, deny reason, deployment and app IDs, image digest, size, region, TTL, provider status, duration and error. Never the token, environment values or full bodies.

## Ownership and recovery

A name prefix is not proof of ownership, and the record store (a small JSON file) is lost on redeploy. So ownership and expiry must be recoverable from the provider, and ambiguity must never lead to deletion.

- **Signed ownership marker.** When the controller creates an app, it writes non-secret markers into the app spec (for example environment entries): the controller instance ID, `created_at`, `expires_at`, the approved request hash, and an HMAC over those fields made with a key only the controller holds. Whether DigitalOcean returns these markers on read is an assumption to confirm in the live test.
- **Reconciliation after a restart** lists `orbit-` apps and classifies each: valid marker and expired means destroy; valid marker and unexpired means adopt, recovering its expiry from the marker; **missing, invalid or foreign marker means leave it alone and raise an alert for a person to review**. Nothing is deleted on a prefix match alone.
- **Quota history.** Concurrent-app limits are enforced from the live provider listing, so they survive a restart. Rolling create-per-hour and per-day counters live in the record store and reset on redeploy; this is an accepted, documented risk, bounded by the concurrent cap and the maximum lifetime. Durable counters would need a small managed store (for example a DigitalOcean Spaces object), a new billable resource that needs separate approval and is not assumed here.
- **Tests:** restart with valid, expired, missing-marker and foreign-marker apps; a foreign `orbit-` resource is never destroyed; a tampered marker is treated as foreign; expiry is recovered from the marker.

## Mapping to the identity model

Add a new capability (for example `DEPLOYMENT_REQUEST`) bound to Orbit and fulfilled by a `deployment-controller` service. Suitability is not authorization, so do not reuse `automation_engineering`. Treat it like `code_review`: it is never delegated through chat and never enters Charlie's planner vocabulary. Mark it implemented only when the controller exists. Bindings never grant access; controller policy does. Orbit's "never handle credentials" prompt does not change.

## Alternatives considered

- **Droplets with Docker.** Needs a root-equivalent Docker daemon, which is the unrestricted-socket risk, plus OS patching and firewalling.
- **DigitalOcean Kubernetes.** A cluster cost floor plus RBAC and operator complexity, the opposite of the smallest controller.
- **Orbit holds the token.** Rejected by the constraint that ordinary agents never receive infrastructure credentials.
- **AWS instead of DigitalOcean for staging.** A separate, explicit provider choice (Phase C); not a replacement here.

## Consequences

The controller becomes a trust anchor: a compromise can destroy the apps it created (and anything its token can reach, hence the separate team, which is a prerequisite). Its record store is a small JSON file, lost on redeploy; ownership and expiry are recovered from signed markers on the apps, and rolling rate counters reset, as described above. No database is added. Orbit needs a runtime service before the flow works end to end, because it has none today.

## Staging validation

1. Mocked-provider tests of controller policy: each allowlist denial, quota exhaustion, TTL sweep, orphan reconciliation, audit fields, token redaction. No network.
2. Contract tests using fixtures captured from the first real responses. Do not invent DigitalOcean response shapes.
3. Minimal live test, **after explicit approval**, in a throwaway team: create one 512 MiB app from a tiny image by digest, inspect it, read logs, destroy it, confirm billing stops. This also establishes the real scope-to-endpoint mapping and stop behavior.
4. Negative live test, an acceptance criterion: confirm the token cannot touch `viharbot-stage`.
5. Restart and recovery tests from "Ownership and recovery", and the approval tests from decision 7.

## Cost

Estimates, not commitments: the controller is one shared 512 MiB component at about $5 per month; each test app about $0.01 per hour; with the 3-app cap the ceiling is about $15 per month; registry free (starter) or $5. Total about $5 to $10 per month idle and about $25 to $30 at the ceiling. These need confirming on DigitalOcean's pricing pages before approval.

## Open questions

Which repositories may the image allowlist name? What concurrency cap? Where does the human approval happen? Which scope does the logs endpoint need, how large may logs be, and can deployed apps leak secrets into logs? Should AWS be considered for staging instead?

## Related

ADR-006, #38, #39, `docs/deployment-strategy.md`, `docs/roadmap-distributed-agents.md`.
