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
- **No body-size cap before authentication.** The request body is buffered before the assertion is
  checked (the assertion binds a hash of the body, so it must be read first), and no body-size cap
  exists before authentication. This is tracked in
  https://github.com/vchokshi/viharbot/issues/101 and must be settled before authentication is
  enabled on any service reachable beyond the private network. It is not implemented in this PR.
