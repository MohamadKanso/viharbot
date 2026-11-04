# Deployment controller core (ADR-007, phase B2a)

Status: the core logic exists and is tested against an in-memory fake. Nothing is deployed, no
DigitalOcean API exists in this code, and nothing here can reach a network. Read
`docs/adr/ADR-007-orbit-deployment-controller.md` first; this page only says what the code does and
does not do.

## What exists (`src/viharbot/deployment_controller/`)

- `policy.py`: the request model (unknown fields such as commands, volumes, specs and secrets are
  rejected), the controller-owned `Policy` (registry and repository prefix, digest-only images,
  one region and size, the `orbit-` name prefix, allowlisted non-secret environment keys, caps and
  quotas, TTL maximum), the canonical request hash (full environment mapping, spec-template
  version, provider team, and for a replacement the immutable app id and expected version) and the
  fixed spec template. Registry, repository, size and env keys are placeholders, not verified
  provider values.
- `controller.py`: the approval state machine `pending -> reserved(claim_id) -> executed | expired`
  with a durable reservation before any provider call; a provider timeout is an unknown outcome
  that is reconciled and never produces a second create; a wiped store refuses old approvals;
  replacement preflight plus a conditional update, with read-back detection when the provider has no
  conditional update; stop, delete and capped logs, each only for an app whose bound marker
  verifies; and `reconcile`, which is both the startup pass and the TTL sweep.
  Binding re-reads the app immediately before the bound-marker write and never writes over a stale
  copy: with a conditional provider the just-read version is the condition (a conflict is
  `failed_uncertain` plus an alert, never retried); without one (`Provider.conditional_update` is
  false) the controller writes under the store lock and verifies by read-back, and a foreign
  version is `failed_uncertain` plus an alert (detection, not prevention, per ADR-007). The claim
  expiry is rechecked with the clock immediately before every bind and replacement write; an
  expired claim is never bound, is alerted as `bind_refused:claim_expired`, and its record is
  `failed_uncertain` (the app is left untouched). A lookup failure during immediate timeout
  recovery keeps the reservation and records `failed_uncertain` with an alert. The log limit
  counts UTF-8 bytes of the returned text (not the serialized response) and never splits a
  character.
- `markers.py`: the two-phase HMAC ownership marker (claim, then bound to the app id and team) and
  the classifier. Only a verified bound marker authorizes anything; a copied marker, a claim-only
  app, a foreign team or a tampered marker is left alone and alerted. A claim is bound only with
  independent app-id evidence: the app id recorded from the create response, or a provider-backed
  operation lookup. Nothing is deleted on a name prefix.
- `store.py`: one JSON file, atomic and fsynced writes, no database. A missing or corrupt file is
  treated as lost state. `RecordStore.locked()` takes an exclusive advisory file lock (POSIX
  `flock`) and re-reads the file, so controllers in one or several processes on the same file
  consume an approval at most once. It does not work across hosts or network filesystems.
- Deployment records: one app id has one authoritative record. A successful replacement marks the
  earlier record `superseded` (history); expiry and stop update every live record of the app. A
  `failed_uncertain` record is never promoted, overwritten by stop or adopted by reconciliation;
  it is alerted on every pass until a person resolves it. A claim-only app is never bound once its
  claim has expired, and is alerted instead.
- `audit.py`: structured events with the ADR-007 fields only; the token, environment values and
  bodies are never accepted as fields.
- `api.py`: the ADR endpoints, authenticated with the existing `service_auth` dependency. Deployment
  routes need the `deployment.request` capability and a subject named in `Policy.requesters`;
  approval routes need `deployment.approve` and a subject in `Policy.approvers`. There is no switch
  that disables authentication. The approval routes (`GET /v1/approvals/{id}`,
  `POST /v1/approvals/{id}:approve`) are a minimal interface for the approval channel that ADR-007
  leaves open; the approver sends the request hash it reviewed, and approval then executes.

## What is mocked

`FakeProvider` is in-memory and can simulate timeouts before and after an operation, copied
markers, foreign apps, version changes between preflight and update, and providers with or without
conditional updates and operation lookup. Its response shapes are invented for tests and are not
DigitalOcean's.

## What is NOT done

- No real DigitalOcean provider. It is a later change that must wait for a separately approved
  live test and contract fixtures captured from real responses (ADR-007, "Staging validation").
  Whether markers are returned on read, whether updates are conditional, and whether an operation
  lookup exists are still assumptions.
- No identity binding. The `deployment.request` and `deployment.approve` capability names are used
  only by this package and its tests. No capability, binding, registry entry, agent identity file
  or Orbit prompt is changed, so Orbit cannot call the controller.
- Not wired into `compose.yaml`, a Dockerfile or deploy files, and there is no module-level `app`
  or environment-based startup; nothing can deploy or run it.
- The separate DigitalOcean team that ADR-007 requires before provisioning is a prerequisite that
  has not been created, and the negative test that the token cannot touch `viharbot-stage` has not
  been run.
- No scheduled sweeper (reconcile runs at startup and when called), no durable rate counters across
  a redeploy, no approval user interface, no log redaction (an open ADR question), and the
  60 second provider timeout and 10 minute rollout poll belong to the real provider.
- Alerts are audit events and an in-memory list; no notification channel exists.
