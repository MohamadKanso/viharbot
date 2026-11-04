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

## Known limitations of A2

- **Enabling it today breaks the web tier's calls to that agent**: outbound token minting is not
  wired into `demo.py` or the orchestrator yet, so only a caller that mints tokens can reach it.
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
