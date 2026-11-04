# Deployment controller core (ADR-007, phase B2a, part 2 of 2)

This is part 2 of 2: reconciliation, timeout recovery and replacement updates (`reconcile.py` and
`replace.py`, mixins of `DeploymentController`), on top of part 1 (policy, approvals, store,
markers, create, bind, get, logs, stop, delete, API), which merges first.

Status: tested against an in-memory fake only. Nothing is deployed and nothing here can reach a
network. Read `docs/adr/ADR-007-orbit-deployment-controller.md` first; this page says what the code
does and does not do.

## What exists (`src/viharbot/deployment_controller/`)

- `policy.py`: the request model (unknown fields such as commands, volumes, specs and secrets are
  rejected), the controller-owned `Policy` (digest-only images, one region and size, the `orbit-`
  name prefix, allowlisted non-secret environment keys, caps, quotas, TTL maximum), the canonical
  request hash (full environment, spec-template version, provider team) and the fixed spec
  template. Registry, repository, size and env keys are placeholders, not verified provider values.
- `controller.py`: the approval state machine `pending -> reserved(claim_id) -> executed | expired`
  with a durable reservation before any provider call; a wiped store refuses old approvals; the
  create with its two-phase claim, then the bind; stop, delete and capped logs, each only for an app
  whose bound marker verifies. A provider timeout on create is an unknown outcome and never produces
  a second create: the controller looks for the claim, binds only with provider-backed app-id
  evidence (`find_operation`), and otherwise records `failed_uncertain` plus an alert; a lookup
  failure keeps the reservation and is retried by a later `reconcile`. `reconcile` is both the
  startup pass (run by the API lifespan) and the TTL sweep: verified bound and expired is
  destroyed, verified bound and live is adopted, a claim-only app gets its bind completed only
  with durable independent evidence, everything else is left alone and alerted.
  Replacement: preflight against the approved target, then one update. A conditional provider gets
  the approved version as the condition. Without one, the response is never trusted alone: the app
  is read back, and a foreign version, a missing claim, an unreadable app or a spec that is not
  exactly the one written is `failed_uncertain` plus an alert (detection, not prevention).
  One app id has one authoritative record: a successful replacement marks the earlier record
  `superseded`, and expiry and stop update every live record of the app.
  Binding re-reads the app before the bound-marker write and never writes over a stale copy: with
  a conditional provider the just-read version is the condition (a conflict is `failed_uncertain`
  plus an alert, never retried); without one (`Provider.conditional_update` false) it verifies by
  read-back, and a foreign version is `failed_uncertain` plus an alert (detection, not prevention,
  per ADR-007). The just-read spec must equal the approved template exactly (no provider
  normalization is defined); anything else is `failed_uncertain` plus an alert. The claim expiry
  is checked with the injected clock after that read and immediately before the write, so a slow
  read cannot carry a bind past expiry; an expired claim is never bound, is alerted as
  `bind_refused:claim_expired` and is `failed_uncertain`. The guarantee is dispatch-before-expiry,
  not landing-before-expiry: a write already in flight may complete after expiry (the controller
  cannot stop it without a provider-enforced deadline), and such an app is then bound and expired,
  which the TTL sweep destroys. The log limit counts UTF-8 bytes and never splits a
  character.
- `markers.py`: the two-phase HMAC ownership marker (claim, then bound to the app id and team).
  Only a verified bound marker authorizes anything; a copied marker, a claim-only app, a foreign
  team or a tampered marker is never ownership proof and is never adopted or destroyed. The
  classifier lives here. Nothing is destroyed on a name prefix.
- `store.py`: one JSON file, atomic and fsynced writes, no database. A missing or corrupt file is
  lost state. `RecordStore.locked()` takes an exclusive POSIX `flock` and re-reads the file, so
  controllers sharing the file consume an approval at most once. Not for multiple hosts or network
  filesystems.
- A `failed_uncertain` record is never promoted, overwritten by stop or adopted by reconciliation;
  it is alerted on every pass until a person resolves it. The one exception is a create whose
  outcome was unknown for a recoverable reason (`bind_pending`, `reconcile_unavailable`,
  `no_candidate`, `no_operation_evidence`): a later pass may resolve it, but only with
  provider-backed `find_operation` evidence of the exact app id. Duplicate claims, replacement
  conflicts, foreign teams and unexplained specs are human-only.
- The uncertain-outcome safeguard belongs to the app, not to one record. Stop, delete and the TTL
  sweep refuse (`app_has_unresolved_outcome`, an audit deny and an alert) while any record of the
  same app id, from any generation, is `failed_uncertain` or a still-reserved replacement, and a
  `failed`, `expired` or `failed_uncertain` record can never destroy an app. There is no resolve
  route. The manual step: a person inspects the app and then sets that record's status to `failed`
  (or `running`) in the store file while the controller is stopped; the refusal lifts on the next
  call. An expired app with an unresolved record is left running and alerted until then.
- One guard decides all of this: `app_safety.py` (`_guard(action, app_id, record)`). It loads every
  record that shares the app id or whose reservation targets it and returns `allowed` or a
  refusal (`app_has_unresolved_outcome`, `record_not_authoritative`). The `ACTIONS` table in that
  file is the only allow-list: it names the statuses of the record each action may go through
  (`stop`, `delete`, `sweep_destroy`, `replace`, `bind`, `adopt`, `recover_create`,
  `recover_replace`); `failed`, `expired` and a `failed_uncertain` record with a non-recoverable
  reason are never revived by automation. Another record that is `failed_uncertain`, reserved or
  `expiry_destroy_unknown` refuses everything except the sweep that retries the destroy. A
  replacement records its target `app_id` at reservation, so a crash before it finishes still maps
  to the app. Results and audit use one vocabulary: `adopted`, `left_uncertain`,
  `recovery_rejected`, `refused:<reason>`. Every new path that acts on an app MUST call the guard
  before any provider mutation; a test enumerates the public entry points and fails otherwise.
- A destroy that raises during expiry is an unknown outcome: the app is read again. Gone means every
  record is finalized as `expired` once (audited); still present leaves the records for the next
  pass; a failed lookup alerts and marks the records `expiry_destroy_unknown` (never `expired` on a
  guess), and a later pass finalizes only after a direct read confirms the app is gone.
- `reconcile` reports and audits what really happened for an app: `adopted` only when adoption
  or confirmation of a valid record happened, otherwise `left_uncertain` or `recovery_rejected`
  (audited as a deny, with an alert). The `execute` audit decision is `allow` only for `running`.
- Expiry destruction re-reads the app and revalidates ownership and expiry immediately before
  `destroy_app`. The provider has no conditional delete, so a writer renewing the app between that
  read and the delete is a residual race (detection and alerting, not prevention; ADR-007).
  Replacement recovery in `reconcile` accepts a reserved replacement only if the app has exactly
  the approved version + 1 and exactly the rebuilt approved spec, like the original execution.
- `audit.py`: structured events with the ADR-007 fields only; tokens, environment values and bodies
  are never accepted as fields.
- `api.py`: the ADR endpoints, authenticated with the existing `service_auth` dependency. Deployment
  routes need the `deployment.request` capability and a subject named in `Policy.requesters`;
  approval routes need `deployment.approve` and a subject in `Policy.approvers`. The `/health`
  route needs no authentication. There is no switch that disables authentication. The approval
  routes (`GET /v1/approvals/{id}`,
  `POST /v1/approvals/{id}:approve`) are a minimal interface for the approval channel that ADR-007
  leaves open; the approver sends the request hash it reviewed, and approval then executes.
  A missing or malformed hash (anything but 64 lowercase hex digits) is a 422
  `invalid_request_hash`; a well-formed hash for a different request is a 409.

## What is mocked

`FakeProvider` is in-memory and can simulate timeouts before and after an operation, copied
markers, foreign apps, version changes between the bind read and its write, providers with or without
conditional updates, and operation lookup. Its response shapes are invented for tests, not DigitalOcean's.

## What is NOT done

- No scheduled sweeper (reconcile runs at startup and when called).
- No real DigitalOcean provider: a later change that waits for a separately approved live test and
  contract fixtures from real responses (ADR-007, "Staging validation"). Whether markers are
  returned on read and whether updates are conditional are still assumptions.
- No identity binding. The `deployment.request` and `deployment.approve` capability names are used
  only here; no binding, registry entry, agent identity file or Orbit prompt is changed, so Orbit
  cannot call the controller.
- Not wired into `compose.yaml`, a Dockerfile or deploy files; no module-level `app`; nothing can
  deploy or run it.
- The separate DigitalOcean team that ADR-007 requires before provisioning has not been created,
  and the negative test that its token cannot touch `viharbot-stage` has not been run.
- No durable rate counters across a redeploy, no approval user interface, no log redaction (an open
  ADR question); the 60 second provider timeout and 10 minute rollout poll belong to the real
  provider.
- Alerts are audit events and an in-memory list; there is no notification channel.
