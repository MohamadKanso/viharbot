# ADR-007: Orbit requests deployments; a dedicated controller executes them

**Status: Proposed.** Nothing here is implemented, and no DigitalOcean API was called to write it. It builds on the identity model in ADR-006 and does not change `deploy_on_push: false` or the "ask before promoting to Stage" rule in `AGENTS.md`.

## Context

Verified in the repository:

- Stage is one App Platform app (`deploy/digitalocean/app.yaml`, region `nyc`): a public `web` component and six private agents on internal ports. Every component sets `deploy_on_push: false`. No Stage app exists today (`docs/deployment-strategy.md`).
- Compose mounts no Docker socket, and there is no Orbit service.
- Orbit declares only `automation_engineering`, which is a **planned** capability, and its prompt says it has no infrastructure access and never handles credentials. `execution_bindings.py` refuses a binding for a planned capability and requires one service per capability.
- Controlled delegation treats model output as untrusted and only allows allowlisted workflows (`docs/controlled-delegation.md`).

**Documented** (summarised from the pages listed under "Sources", retrieved 2026-11-02; passages not re-captured here): App Platform specs accept image sources from DOCR, Docker Hub or GHCR, and a `digest` mutually exclusive with `tag`; App Platform billing is per second; API token scopes use `resource:action` names such as `app:create`, `app:read`, `app:update`, `app:delete`. **Not documented on those pages:** whether scopes can be restricted to specific apps or tags, which endpoint needs which scope, and any stop or scale-to-zero operation. Not documented is not evidence that a capability is unsupported; it means this ADR assumes it is unavailable until confirmed in the live test. Everything else about provider behavior in this ADR is **assumed** until then.

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
   - is **bound to the exact request**: it stores a hash of the complete canonical request, namely the image digest, size, region, port, name, TTL, the **full environment mapping of keys and values** (every value is non-secret and allowlisted), and the identifier and version of the controller's spec template that will build the app. If a value is instead controller-owned, the approval binds the resolved value or its immutable version. **For a replacement, the hash also includes the provider team, the immutable target `app_id` and the expected current deployment or spec version**, so approval cannot transfer to a different resource that now sits behind the same name. Immediately before updating, the controller rechecks ownership (a verified bound marker for that `app_id` and team) and that the current version equals the expected one; on any mismatch it refuses and a fresh approval is required. **That recheck is only a preflight and does not close the check-to-update race.** The update must therefore be **provider-enforced conditional on the approved version** (for example an if-match or version precondition). Whether App Platform offers such a precondition is **not documented and is ASSUMED unavailable until the live test shows otherwise**. If it is unavailable, the ADR does not promise unconditional refusal of an intervening update. It instead requires, as a documented limitation: (a) the controller is the only holder of the provider token and the token lives in the separate team (decision 6), the only writers the design admits, plus a per-`app_id` lock in the controller so its own replacements are serialized; (b) after the update, the controller reads the app back and, if the resulting version lineage is not exactly approved version then replacement (an unexpected writer intervened), records the replacement as failed-uncertain and alerts a person instead of reporting success. This is detection, not prevention, and a human with the token can still race it. Any change to any of these invalidates the approval;
   - **expires** (default 30 minutes) and is **single use** under the reserve-then-execute protocol below, which also covers provider timeouts and loss of controller state;
   - records the approver, the approved request hash, the time and the resulting deployment ID in the audit event.

   **Reserve-then-execute (applies to creates and to replacements alike).** Approval states are `pending -> reserved(claim_id) -> executed | expired`. A reservation is a one-way step.
   1. **Reserve.** The controller checks the approval is `pending`, unexpired and matches the request hash, then generates a `claim_id` and **durably records** `reserved(claim_id)` (written and flushed) **before any provider call**. If the write fails, no provider call is made.
   2. **Execute.** The app name or claim marker is derived deterministically from the `claim_id` (the claim marker in "Ownership and recovery" is this same `claim_id`), so the operation is detectable and a retry is idempotent. For a replacement, the same `claim_id` is written into the replacement spec so the controller can tell whether that update landed.
   3. **Complete.** On a confirmed result the controller records `executed` with the app ID. A `reserved` approval is never returned to `pending`, and a second reservation under the same approval is refused.
   4. **Timeout means UNKNOWN.** A provider timeout or transport error is not a failure. The outcome is unknown, and the controller must first **reconcile**: look for an app (or, for a replacement, a spec) carrying that `claim_id`. If found, the controller applies the provenance rule in "Ownership and recovery" (interrupted creation) before recording `executed` or binding; a `claim_id` match alone is not enough, because an authentic claim can be copied. If not found after the reconciliation window, the approval stays `reserved`, the request is marked failed-uncertain, and a person is alerted. The controller **never issues a second create under the same approval**; further work needs a new approval, which may be issued after a person confirms nothing was created.
   5. **Lost state fails closed.** If controller state is lost after reservation (a redeploy wipes the JSON store), the approval is treated as **consumed** and a new approval is needed. A missing reservation record is never read as `pending`. For this to hold across a wipe, approvals must not be re-presentable from outside the store: an approval is accepted only if the store holds it as `pending`, so a store that was wiped holds no `pending` approvals and any older approval is refused. (How approvals are persisted before and after a wipe, and whether stored approvals need to be re-issued after a redeploy, is an implementation question to settle; the requirement here is the fail-closed outcome.) An app created by an unrecorded reservation is handled as an interrupted creation under "Ownership and recovery": alerted, never adopted or deleted by the controller.

   Required tests: changing an environment value after approval invalidates it; an unauthorized approver is refused; an expired approval is refused; an approval replayed for a second deployment is refused; a request changed after approval is refused; Orbit cannot approve its own request. Crash and timeout tests with a mocked provider: **timeout after the provider created the app** (reconciliation finds the claim marker, records `executed`, no second create); **timeout before the provider created the app** (reconciliation finds nothing, state stays `reserved`, no second create, a person is alerted); **crash between reservation and create** (restart finds `reserved` with no app, no automatic create, a person is alerted); **lost state** (store wiped after reservation, the old approval is refused and no create happens); **replay of a consumed approval** (a `reserved` or `executed` approval is refused). Each case is run for a create and for a replacement. Replacement-only tests: **target substitution** (the app behind the name or ID differs from the approved `app_id` or team, refused); **intervening update before the preflight** (the current version differs from the expected version, refused and a fresh approval required); and **version changed after the preflight but before mutation** (a mocked provider changes the version between the check and the update: with a conditional update the provider rejects it and nothing is applied; without one, the read-back detects the foreign version, the replacement is marked failed-uncertain and a person is alerted, never reported as success).
8. **Audit:** a structured event per request with timestamp, `request_id`, caller, action, decision, deny reason, deployment and app IDs, image digest, size, region, TTL, provider status, duration and error. Never the token, environment values or full bodies.

## Ownership and recovery

A name prefix is not proof of ownership, and the record store (a small JSON file) is lost on redeploy. So ownership and expiry must be recoverable from the provider, and ambiguity must never lead to deletion.

- **Two-phase signed ownership marker, bound to the immutable app.** A marker copied from one app to another must never authorize anything, so ownership is tied to the provider's app ID and team, which cannot be forged by copying text.
  1. **Claim.** The `claim_id` is the one generated when the approval was reserved (decision 7), recorded durably before any provider call. It creates the app with a claim marker in the spec (non-secret environment entries): `claim_id`, controller instance ID, team ID, `created_at`, `expires_at`, the approved request hash, and an HMAC over those fields made with a key only the controller holds.
  2. **Bind.** As soon as the provider returns the immutable `app_id`, the controller updates the spec with a bound marker: an HMAC over the claim fields **and** the `app_id`.
- Only a bound marker whose HMAC verifies for **this** `app_id` and **this** team is ownership evidence. Whether DigitalOcean returns these markers on read, and whether an app's own ID is stable across spec updates, are assumptions to confirm in the live test.
- **Interrupted creation (a claim marker with no bound marker).** The claim marker does not contain the `app_id`, and an authentic claim can be copied onto another app, so a matching `claim_id` alone is **not** enough to bind. The controller binds only when it has independent evidence tying the reserved operation to that immutable app: the `app_id` returned to the controller by the create call and durably recorded with the reservation before the bind, or, if that response was lost, **provider-backed evidence that links the reserved operation to that `app_id`**, such as a verifiable operation result or a provider-supported idempotency lookup for the create. Creation time and uniqueness are **not** such evidence: a timestamp shows when an app appeared, not which operation made it, and a copied authentic claim on a different app could be the sole candidate in the window if the original is absent. Whether DigitalOcean offers any such operation lookup is **not documented and is ASSUMED unavailable**; if it is, the lost-response case simply fails closed. If that evidence cannot be recovered, if two or more apps carry the same claim, or if the claim is on an app the reservation does not match, the controller leaves every such app unbound and untouched and raises an alert for a person. Whether the provider returns the marker on read and offers operation-to-app evidence are assumptions to confirm in the live test. Precedence: this rule applies to a claim-only app before the restart rule below, and a claim-only app that fails it is treated as claim-only under that rule (leave alone, alert). If the store was lost, the app is **never adopted and never deleted by the controller**: it raises an alert for a person, and the claim marker's `expires_at` bounds how long the person has before the cost matters. A claim marker alone never authorizes deletion.
- **Reconciliation after a restart** lists `orbit-` apps and classifies each: a verified bound marker that has expired means destroy; a verified bound marker that has not expired means adopt, recovering its expiry from the marker; **a missing, invalid, foreign, claim-only or app-ID-mismatched marker means leave it alone and raise an alert for a person to review**. Nothing is deleted on a prefix match alone.
- **Quota history.** Concurrent-app limits are enforced from the live provider listing, so they survive a restart. Rolling create-per-hour and per-day counters live in the record store and reset on redeploy; this is an accepted, documented risk, bounded by the concurrent cap and the maximum lifetime. Durable counters would need a small managed store (for example a DigitalOcean Spaces object), a new billable resource that needs separate approval and is not assumed here.
- **Tests:** restart with valid, expired, missing-marker and foreign-marker apps; a foreign `orbit-` resource is never destroyed; a tampered marker is treated as foreign; **copying a valid bound marker onto a different app never authorizes adoption or deletion**; a crash between creation and binding leaves the app alerted but untouched when the store is lost, and completes the bind only with independent app-ID evidence (the recorded create response or provider-backed operation evidence, never creation time); a **sole copied claim inside the reserved window with the original absent** is left unbound, untouched and alerted; a **valid unbound claim copied onto a second app never produces a bound marker**, and **duplicate claim matches** are left untouched and alerted; expiry is recovered from the marker.

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
5. Restart and recovery tests from "Ownership and recovery", and the approval and reserve-then-execute tests from decision 7.

## Cost

Estimates, not commitments: the controller is one shared 512 MiB component at about $5 per month; each test app about $0.01 per hour; with the 3-app cap the ceiling is about $15 per month; registry free (starter) or $5. Total about $5 to $10 per month idle and about $25 to $30 at the ceiling. These need confirming on DigitalOcean's pricing pages before approval.

## Open questions

Which repositories may the image allowlist name? What concurrency cap? Where does the human approval happen? Which scope does the logs endpoint need, how large may logs be, and can deployed apps leak secrets into logs? Should AWS be considered for staging instead?

## Sources

Retrieved 2026-11-02; pages summarised, passages not re-captured. "Documented" means the page states it; "not documented" is not evidence of unsupported.

- App spec reference, https://docs.digitalocean.com/products/app-platform/reference/app-spec/ : documented image sources (DOCR, Docker Hub, GHCR) and `digest` versus `tag`. Not documented there: how markers or environment entries are returned on read.
- API scopes, https://docs.digitalocean.com/reference/api/scopes/ : documented `resource:action` scope names such as `app:create`, `app:read`, `app:update`, `app:delete`. Not documented there: restriction of a scope to specific apps or tags, or an endpoint-to-scope mapping.
- App Platform pricing, https://docs.digitalocean.com/products/app-platform/details/pricing/ : documented per-second billing. The dollar figures in "Cost" are estimates still to be confirmed against this page.
- Container Registry pricing, https://docs.digitalocean.com/products/container-registry/details/pricing/ : basis for the "free (starter) or $5" registry estimate, to be confirmed before approval.
- Internal routing, https://docs.digitalocean.com/products/app-platform/how-to/manage-internal-routing/ : basis for private component-to-component calls on internal ports.

Provider behavior on provider timeouts (whether a create can succeed after the client times out) is **assumed** possible, which is why the protocol treats it as unknown.

## Related

ADR-006, #38, #39, `docs/deployment-strategy.md`, `docs/roadmap-distributed-agents.md`.
