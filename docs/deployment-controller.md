# Deployment controller core (ADR-007, phase B2a, part 1 of 2)

This is part 1 of 2, split so each pull request can be reviewed completely. Part 2
(reconciliation, timeout recovery, replacement updates) is a separate pull request, opened after
part 1 merges; this page does not describe it as existing.

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
  whose bound marker verifies. A provider timeout on create is an unknown outcome: the record
  becomes `failed_uncertain` with an alert, the reservation stays, and there is never a second
  create. Resolving such a record from provider evidence is part 2; until then a person does it.
  A request that names a replacement target is rejected as `invalid_request`.
  Binding re-reads the app before the bound-marker write and never writes over a stale copy: with
  a conditional provider the just-read version is the condition (a conflict is `failed_uncertain`
  plus an alert, never retried); without one (`Provider.conditional_update` false) it verifies by
  read-back, and a foreign version is `failed_uncertain` plus an alert (detection, not prevention,
  per ADR-007). The claim expiry is checked with the injected clock after that read and immediately
  before the write, so a slow read cannot carry a bind past expiry; an expired claim is never
  bound, is alerted as `bind_refused:claim_expired` and is `failed_uncertain`. The log limit counts
  UTF-8 bytes and never splits a character.
- `markers.py`: the two-phase HMAC ownership marker (claim, then bound to the app id and team).
  Only a verified bound marker authorizes anything; a copied marker, a claim-only app, a foreign
  team or a tampered marker is never ownership proof. Nothing is destroyed on a name prefix.
- `store.py`: one JSON file, atomic and fsynced writes, no database. A missing or corrupt file is
  lost state. `RecordStore.locked()` takes an exclusive POSIX `flock` and re-reads the file, so
  controllers sharing the file consume an approval at most once. Not for multiple hosts or network
  filesystems.
- A `failed_uncertain` record is never overwritten by stop; a person resolves it.
- `audit.py`: structured events with the ADR-007 fields only; tokens, environment values and bodies
  are never accepted as fields.
- `api.py`: the ADR endpoints, authenticated with the existing `service_auth` dependency. Deployment
  routes need the `deployment.request` capability and a subject named in `Policy.requesters`;
  approval routes need `deployment.approve` and a subject in `Policy.approvers`. The `/health`
  route needs no authentication. There is no switch that disables authentication. The approval
  routes (`GET /v1/approvals/{id}`,
  `POST /v1/approvals/{id}:approve`) are a minimal interface for the approval channel that ADR-007
  leaves open; the approver sends the request hash it reviewed, and approval then executes.

## What is mocked

`FakeProvider` is in-memory and can simulate timeouts before and after an operation, copied
markers, foreign apps, version changes between the bind read and its write, and providers with or
without conditional updates. Its response shapes are invented for tests, not DigitalOcean's.

## What is NOT done

- Part 2, a separate pull request opened after this one merges: startup reconciliation and the TTL
  sweep, adoption, completing a bind after a crash, timeout recovery through `find_operation`, and
  replacement updates. None of that is here, so nothing destroys an expired app automatically.
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
