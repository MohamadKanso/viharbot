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

**Issuer location is not decided.** The founder has not finalized where the issuer key lives
(ADR-006, "Where the issuer's key lives"). `AssertionIssuer` is a one-method interface and
`InProcessIssuer` (option A: the web/orchestrator process signs) is its only implementation, as the
ADR recommends first. A separate internal signer (option B) can replace it without changing callers.

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
- Redirects: a signed request is sent through the no-redirect opener (`litellm_client.urlopen`), so the token is never forwarded to another origin; a redirecting callee surfaces as an HTTP error. Unsigned calls keep the default behavior.
- `eng` and `cls` are fixed placeholders (`unscoped`, `unclassified`); there is no `dlg` (A3).
- No sustained rotation, no separate signer, no per-user identity.

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
- **No body-size cap before authentication.** The request body is buffered before the assertion is
  checked (the assertion binds a hash of the body, so it must be read first), and no body-size cap
  exists before authentication. This is tracked in
  https://github.com/vchokshi/viharbot/issues/101 and must be settled before authentication is
  enabled on any service reachable beyond the private network. It is not implemented in this PR.
