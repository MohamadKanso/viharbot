# Service authentication (ADR-006, step A2)

**Status: implemented behind a setting that is off by default.** This is authentication only. It
proves which service signed a request and that the request was not altered or replayed. It does
**not** prove the caller is authorized by a delegation grant: that is A3 (issue #95). Design:
`docs/adr/ADR-006-workload-identity-and-authorization.md`. Code: `src/viharbot/service_auth.py`.

## What it does

A callee verifies the `X-Viharbot-Assertion` header (a compact JWT, EdDSA only). It checks the
signature against a public key looked up by `kid` in a local registry file, `typ`, that `iss` and
`sub` equal the subject the registry binds to that `kid`, the audience (the service's own name, for
example `fact-check`), lifetime (at most 60 s, 10 s clock skew), that `cap` is in the registry
entry's capabilities, the method, canonical target and SHA-256 of the body bytes as received, and a
per-process `jti` replay cache (bounded, 10,000 entries; when full it refuses rather than evicts).
Headers `jku`, `x5u`, `x5c` and `jwk` are never read. Identity in a body or in other headers is
never used. Failures return 400 (bad target), 401, 403 (capability), or 503 (registry too stale) with
a generic body. Decisions are logged as JSON (request ID, caller, path without the query, reason,
never a token, key or body).

## Turning it on (specialist agents only)

Off by default, so Compose, Stage and existing tests are unchanged. To enable on one specialist:

- `VIHARBOT_SERVICE_AUTH=on`
- `VIHARBOT_KEY_REGISTRY=/path/to/registry.toml`, a mounted file holding **public keys only**.

The service refuses to start if the registry is missing, malformed or empty. Registry format:

```toml
[[keys]]
kid = "web-2026-11"          # unique
sub = "web"                  # the one subject this key may assert
public_key = "<base64url of the 32 raw Ed25519 public-key bytes>"
capabilities = ["specialist.process"]
```

Two entries for one `sub` give a rotation overlap; removing a `kid` retires it. The file is re-read
every 60 s and once (at most every 10 s) on an unknown `kid`. After a failed refresh the last-known
keys work for 15 minutes, then every request is refused (503).

A caller mints a token with `service_auth.mint_assertion(...)`, which loads the private key at call
time from a file path or `VIHARBOT_SIGNING_KEY` (PEM or base64url seed). Keys are never in source;
inject them from the platform secret store.

## Outbound assertions (A2b)

**Status: implemented for plain HTTP calls through `demo.post_json`, off by default.** Code:
`src/viharbot/outbound_auth.py`.

**Issuer location:** option A is accepted; see "Issuer-key location" below. `AssertionIssuer` is a
one-method interface and `InProcessIssuer` is its only implementation.

Each call mints one token from the actual request: `mth` (POST), `tgt` (the URL's path and query as
sent, canonicalized by `service_auth.canonical_target`), `req_sha256` (the exact body bytes),
`aud` (the destination service name), `cap` (the capability that service's route requires),
`exp` 30 s after `iat`, a fresh `jti`. It is sent in `X-Viharbot-Assertion`, never `Authorization`.

Configuration (all unset means off; requests are byte-for-byte unchanged):

- `VIHARBOT_OUTBOUND_AUTH=on`
- `VIHARBOT_SIGNING_KID`: the `kid` the callee registry holds for this service.
- `VIHARBOT_SERVICE_SUBJECT`: the `sub` (default `web`).
- `VIHARBOT_SIGNING_KEY_FILE` (a path) or `VIHARBOT_SIGNING_KEY` (PEM or base64url seed), read on
  every call and never logged.

When on and the key is unreadable, the kid is missing, or signing fails, the call raises and nothing
is sent (fail closed; no unauthenticated fallback).

Wired: every `demo.post_json` call to `fact-check`, `source-quality`, `privacy-review` and
`synthesis` (capability `specialist.process`), which covers the iterative and identity-execution
paths because they use `post_json`.

Not wired:

- `research`, `security` and `pixel` via `post_json`: those services do not verify assertions yet and
  define no route capability, so no token is minted for them.
- `security_client.send_findings_to_security` (Research to Security) and `pixel_execution.fetch_deck`
  (GET, no body): not wired. The Research process would need its own key and `sub` and Security has no
  verifier yet.
- AgentCore (`agentcore://`): untouched; the SigV4-plus-assertion carrier is separate work.
- No Sage client exists in `src`; Sage is not called by the web tier.
- Redirects: a signed request is sent through the no-redirect opener (`litellm_client.urlopen`), so the token is never forwarded to another origin; a redirecting callee surfaces as the 3xx `HTTPError`, and a loopback test with a real 302 asserts the target is never contacted. Unsigned calls keep the default behavior.
- `eng` and `cls` are fixed placeholders (`unscoped`, `unclassified`); there is no `dlg` (A3).
- No rotation tooling, no separate signer, no per-user identity.
- ADR-006's text still recommends option B before any AWS deployment; a follow-up must update it (see below).

### Issuer-key location (founder decision, 2026-11-03)

Option A (ADR-006) is accepted: the web process holds the signing key and mints assertions. This is a
lab. The same holds if the web service runs in AWS, where the key would be stored in AWS Secrets
Manager and injected into the web service (not verified or provisioned here). Keys are separate per
environment: a Stage key must never be accepted in production. Rotation is every 30 to 90 days with
an overlap window, per ADR-006. The `AssertionIssuer` interface is kept so a separate signer
(option B) can replace `InProcessIssuer` later.

What this does not change: a secret store protects storage and rotation, but the web process still
reads the key at runtime, so compromise of the web process still lets an attacker mint assertions for
every agent. It also relaxes ADR-006's recommendation to move to option B before any AWS deployment.
The ADR text is not updated in this PR; a follow-up is needed.

## Known limitations of A2

- **Outbound minting is partial** (see "Outbound assertions (A2b)"). Enabling authentication on a specialist works only while the caller also has outbound minting on.
- Only the shared specialist factory is wired (`/process` and `/invocations`). `/health`, `/ping`
  and `/model` stay open. Follow-ups: `/model`, Pixel, Sage, Research and Security services.
- `dlg` is optional and ignored; there is no grant, scope, redelegation or engagement/classification
  check (A3, #95, #94). `eng` and `cls` are required but not enforced. The audit claim is "every call
  to a protected route is authenticated while authentication is enabled", not "every call is
  authenticated" and not "every call is authorized". Routes left open (see above) and any service
  with authentication off make no such claim.
- The AgentCore fallback carrier (`assertion` in the `/invocations` envelope with a JCS body hash)
  is not implemented; the body hash is over the raw bytes received.
- The replay cache is per process. A callee with several replicas needs shared state (ADR open
  question 5). The 60 s registry refresh uses wall-clock time.
- Query handling: the target is canonicalized as the ADR lists. Literal characters in the query are
  kept as received (never re-encoded) so `+`, `%20` and `%2B` stay distinct; percent-escape hex is
  upper-cased and an escape of an unreserved character is rejected. An empty query (`/x?`) is signed
  as no query because ASGI does not distinguish it. A space or a `#` in the target is rejected.
- Plaintext HTTP on a private network is still an accepted interception risk (ADR-006, Transport).
- **Body-size cap (issue #101): covered in the application, proxy limits unverified.** The cap
  below refuses an over-limit request to `/process` or `/invocations` with 413 before the assertion
  is checked, so an unauthenticated caller cannot make the specialist buffer more than the limit
  (plus one chunk). It applies only to those two POST routes and only when authentication is on;
  `/health`, `/ping` and `/model` and the other services (Pixel, Sage, Research, Security) are not
  covered. What remains an assumption: whatever proxy sits in front of each runtime may buffer or
  cap bodies itself before the request reaches the app, and I have **not verified** the limits or
  buffering behavior of DigitalOcean App Platform, Amazon Bedrock AgentCore Runtime or any
  load balancer. Treat the in-app cap as the only guarantee and the proxy as unknown.

## Request body size cap

Code: `src/viharbot/body_limit.py` (pure ASGI middleware, installed by the specialist factory only
when `VIHARBOT_SERVICE_AUTH` is on; nothing changes when it is off).

- A request whose `Content-Length` exceeds the limit is answered `413` immediately, before the
  assertion check and without reading any of the body.
- Otherwise bytes are counted as the body streams (chunked or understated `Content-Length`); the
  moment the total crosses the limit the service answers `413` and stops reading. A body of exactly
  the limit passes.
- The 413 body is the generic `{"detail": "request body too large"}`. The log line records the
  agent, reason and limit only, never body, headers or token.
- Setting: `VIHARBOT_MAX_BODY_BYTES`, an integer from 1024 to 16777216 (invalid values stop startup).
  Default **262144 (256 KiB)**.

Default rationale: the largest legitimate specialist request is the synthesis call, which carries
the question (at most 6000 characters, so at most 24,000 bytes of UTF-8) plus the structured
results of the other agents in `context` (research findings, security, fact-check, source-quality,
privacy and Sage; the Sage analysis alone is bounded to roughly 40 KB by its field limits).
The research findings and other results are not length-bounded by their schemas, so this is an
estimate, not a proof; 256 KiB is several times the expected size and small enough to be cheap to
buffer. Raise it per service if a real request is refused.

Per runtime (same in-app limit everywhere; the proxy limits are unverified, see above):

| Runtime | In-app limit | Proxy in front |
| --- | --- | --- |
| Local Docker Compose (dev) | `VIHARBOT_MAX_BODY_BYTES`, default 256 KiB | none |
| DigitalOcean App Platform (Stage) | same | not verified |
| AgentCore Runtime | same, on `/invocations` | not verified |
| Fargate or other load balancer | same | not verified |
