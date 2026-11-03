# ADR-010: Resource-scope language for delegation grants

**Status: Proposed.** Nothing here is implemented. It resolves issue #94, which blocks A3 (delegation-based authorization, #38). It does not edit ADR-006; it fills the gap ADR-006 leaves ("path or resource patterns the capability may touch"). The founder confirms the numbering.

## Context

Verified in the repository:

- ADR-006 gives each grant `caps`, each capability with an optional resource scope. A3 requires the request's `tgt` to fall inside that scope, and requires redelegation and the effective scope to be the **intersection** of parent, registry and policy. ADR-006 does not say what a scope is, so two implementations could disagree on descendants, query strings, wildcards or encoded identifiers.
- `canonical_target` in `src/viharbot/service_auth.py` (A2, merged) defines `tgt`: the path exactly as received (no normalization), then `?` and the query pairs in received order. It already rejects non-printable or non-ASCII bytes, a relative path, lowercase percent hex, percent-encoded unreserved characters, encoded `/`, `\` and NUL, malformed `%`, dot segments, empty segments (except one trailing one), and `;` or empty pairs in the query. A verified `tgt` path is therefore already canonical and unambiguous.
- A2 makes no grant decisions; `dlg` is ignored until A3.

Assumed, not verified: the JSON shape of a capability entry (a `cap` plus a `res` list of scopes) is illustrative; A3 fixes the key names. The language is what this ADR decides.

## Decision

A scope is a plain ASCII string. There are exactly **two forms and no other wildcards**.

| Form | Syntax | Covers |
|---|---|---|
| Exact | a canonical path, e.g. `/decks/a` | that one path only |
| Subtree | a canonical base path followed by `/*`, e.g. `/decks/a/*` | every **strict descendant** of the base, not the base itself |

`*` has no meaning anywhere else. A scope with `*` anywhere other than as the entire final segment is malformed. A literal `*` path segment therefore cannot be named by a scope (a subtree above it can still admit it). Comparison is case-sensitive. There is no regex, glob, `**`, character class or per-segment wildcard.

### Matching

1. Take the path of the verified `tgt` (everything before the first `?`). Split it on `/`, dropping the leading empty piece, into segments. The root `/` has zero segments. `/x/` has the segments `x` and an empty segment, so it is a different resource from `/x`.
2. An **exact** scope matches iff the segment lists are byte-for-byte equal.
3. A **subtree** scope with base segments `B` matches iff the request has strictly more segments than `B` and its first `len(B)` segments equal `B`, compared whole segment by whole segment. `/decks/a/*` covers `/decks/a/x` and `/decks/a/x/y` but neither `/decks/a` (the base) nor `/decks/ab`. Segment comparison is the only comparison; no string-prefix test is ever made. A request `/decks/a/` has an extra empty segment and is covered by `/decks/a/*`; the exact scope `/decks/a` does not cover it. `/*` covers every path except `/`.
4. A request is allowed for a capability iff it matches **at least one** scope in that capability's list. Scopes only grant; there is no deny entry.

### Query parameters

**A scope names a path only; any query string is allowed.** Reasons: the canonical query is order-sensitive, so pinning pairs would need its own ordering, repetition and encoding rules (more surface for disagreement); and ADR-006 already says authorization must not rest on request-supplied values (the resource's engagement comes from the callee's resource mapping). Rule that follows: **a route whose resource identity is carried in the query is not scopable.** It must move the identifier into the path before its capability can carry a meaningful resource scope. A3 should register, per route, the path template that identifies the resource.

### Percent-encoding

A scope must already be in the canonical path form that `canonical_target` produces (uppercase hex, no encoded unreserved characters, no encoded `/`, `\` or NUL, no dot segments, no empty segment except one trailing empty segment in an exact scope). Comparison is byte equality of the encoded form and **nothing is ever decoded**. So `/decks/a%20b` and `/decks/a+b` are different scopes, and `%2F` can never act as a separator. A scope must be issued in the encoding the caller's requests will use; an identifier reachable in two encodings needs two scopes.

### Attenuation and intersection

`covers(P, C)` means scope `P` covers every path that scope `C` covers:

- exact `P` covers exact `C` iff equal; exact `P` never covers a subtree;
- subtree `P` (base `Bp`) covers exact `C` iff `C` has more segments than `Bp` and starts with `Bp`;
- subtree `P` covers subtree `C` (base `Bc`) iff `Bp` is a segment-wise prefix of `Bc` (equal bases included).

**Attenuation check** (issuer, at redelegation): a child list is no wider than the parent iff every child scope is covered by some single parent scope. A single scope suffices: a subtree covers infinitely many paths, and a finite set of scopes can cover a whole subtree only if one of them has a prefix base.

**Intersection** of two scopes `a` and `b`:

- exact and exact: `a` if equal, else empty;
- exact `e` and subtree `s`: `e` if `s` covers `e`, else empty;
- subtree and subtree: the one with the longer base if the other's base is a segment-wise prefix of it, else empty.

Intersection of two lists: take the pairwise intersections, drop empties and duplicates, and drop any scope covered by another survivor. If the result exceeds the limits below the issuer **refuses** the grant; it never truncates.

Representation: an **absent** `res` means no resource restriction for that capability (ADR-006's "optional" scope); a **present empty** list permits nothing. Absent intersected with a list `X` is `X`; absent with absent is absent; anything with an empty list is empty.

**Why it never widens.** Each case yields either empty, one of the two operands, or an exact scope covered by both. So every path the result matches is matched by both inputs: the result is a subset of each. Parent, registry entry and policy are intersected two at a time, so the child is within all three. Absent can only restore the other operand, never exceed it.

### Fail closed

The whole grant (not only the offending scope) is **denied** and the decision logged as a malformed scope when any scope: is not a string or not printable ASCII; fails the canonical-path checks above; is empty or does not start with `/`; contains `?`, `#`, `;` or a misplaced `*`; is duplicated in its list; or exceeds a limit. A present empty list denies every request for that capability. A callee never interprets a scope loosely and never guesses a missing `res`: it is either present and valid, or deliberately absent.

### Limits

At most **16** scopes per capability, each at most **256** bytes and **16** segments, and at most **2048** bytes of scope text across a whole grant. These bound intersection work (16 x 16 pairs) and grant size. The numbers are an assumption to check against real route templates in A3; they may be changed by amendment, never silently ignored.

## Test vectors

| # | Scope | Request target | Result | Reason |
|---|---|---|---|---|
| 1 | `/decks/a` | `/decks/a` | allow | exact |
| 2 | `/decks/a` | `/decks/a/x` | deny | exact never covers descendants |
| 3 | `/decks/a` | `/decks/a/` | deny | trailing slash is another resource |
| 4 | `/decks/a/*` | `/decks/a/x` | allow | descendant |
| 5 | `/decks/a/*` | `/decks/a/x/y` | allow | deep descendant |
| 6 | `/decks/a/*` | `/decks/a` | deny | subtree excludes its base |
| 7 | `/decks/a/*` | `/decks/ab` | deny | segment boundary |
| 8 | `/decks/a/*` | `/decks/ab/x` | deny | segment boundary |
| 9 | `/decks/a/*` | `/decks/a/` | allow | extra empty segment is a descendant |
| 10 | `/*` | `/` | deny | root is not a descendant |
| 11 | `/*` | `/decks` | allow | any non-root path |
| 12 | `/decks/a` | `/decks/a?x=1&y=2` | allow | query ignored |
| 13 | `/decks/a/*` | `/decks/a/x?next=/etc` | allow | query ignored |
| 14 | `/decks/a%20b` | `/decks/a%20b` | allow | same encoded form |
| 15 | `/decks/a%20b` | `/decks/a+b` | deny | different bytes, never decoded |
| 16 | `/decks/a%2Fb` | any | deny, malformed scope | encoded `/` rejected by canonical rules |
| 17 | `/decks/a%2fb` | any | deny, malformed scope | lowercase hex |
| 18 | `/decks/%61` | any | deny, malformed scope | encoded unreserved |
| 19 | `[]` (present, empty) | any | deny | empty permits nothing |
| 20 | `res` absent | any | allowed by `cap` alone | no resource restriction |
| 21 | `/decks/*/x` | any | deny, malformed scope | `*` not the final segment |
| 22 | `/decks/a*` | any | deny, malformed scope | `*` not a whole segment |
| 23 | `/decks/**` | any | deny, malformed scope | no `**` form |
| 24 | `decks/a` | any | deny, malformed scope | not absolute |
| 25 | `/decks/a?x=1` | any | deny, malformed scope | query inside a scope |
| 26 | `/decks/../a` | any | deny, malformed scope | dot segment |
| 27 | 17 scopes, or one of 257 bytes | any | deny, oversized | limit |
| 28 | `["/decks/a","/decks/a"]` | any | deny, malformed | duplicate |
| 29 | `["/decks/a", "/bad*"]` | `/decks/a` | deny | one bad scope denies the whole grant |

Intersection and attenuation vectors:

| # | Operand 1 | Operand 2 | Result | Reason |
|---|---|---|---|---|
| 30 | `/decks/*` | `/decks/a/*` | `/decks/a/*` | longer base wins |
| 31 | `/decks/a/*` | `/decks/b/*` | empty | disjoint bases |
| 32 | `/decks/a/*` | `/decks/ab/*` | empty | not a segment prefix |
| 33 | `/decks/a/*` | `/decks/a` | empty | exact is the base, not a descendant |
| 34 | `/decks/a/*` | `/decks/a/x` | `/decks/a/x` | exact inside subtree |
| 35 | `/decks/a` | `/decks/a` | `/decks/a` | equal exacts |
| 36 | parent `[/decks/a/*]` | child `[/decks/*]` | refuse | child wider |
| 37 | parent `[/decks/a/*]` | child `[/decks/a/*]` | allow | equal |
| 38 | parent `[/decks/a]` | child `[/decks/a/*]` | refuse | exact cannot cover a subtree |
| 39 | parent `[/decks/a/*]` | child absent | refuse | absent is wider than a list |
| 40 | parent absent | child `[/decks/a]` | allow | narrowing from unrestricted |
| 41 | parent `[]` | child `[/decks/a]` | refuse | nothing to inherit |
| 42 | `[/a/*]` | `[/b/*]` | `[]` | empty result denies all |

## Required tests for A3 (deterministic, no inference, no cloud)

- Every vector above as a table-driven test against one shared parser and matcher used by both issuer and callee, so the two cannot drift.
- A property test over generated scope lists: each intersection result is covered by both operands, and every path allowed by the result is allowed by each input.
- A refused attenuation never produces a signed grant; a narrowed grant never allows a path its parent denies.
- `canonical_target` output is accepted by the matcher unchanged, and a request whose path is not canonical never reaches the matcher.
- A malformed scope is logged with a bounded prefix, not the full scope text, and denies even requests that the valid scopes would allow.

## Not decided

- **Per-method scoping.** A scope names a path, not a method. `mth` is already bound in the assertion and each route has one capability; method-specific grants would be a later amendment.
- Non-HTTP resources (memory keys, model-call sites) and their naming.
- Whether registry entries and callee policy use this same language. It is assumed, so that intersection is defined; A3 confirms.
- Gateway mount prefixes or path rewriting between caller and callee (the scope is compared with the `tgt` the callee verified).
- Pinning query parameters, and the per-route template registration that makes "identity in the path" enforceable.
- Case-insensitive resources; ids differing only by case are different scopes.

**Dependency:** A3 (#38) implements this language; it is the only stage that evaluates scopes, and ADR-006 already states A3 depends on #94.

## Alternatives considered

- **String-prefix matching:** simple, but `/decks/a` would cover `/decks/ab`. Rejected.
- **Subtree includes its base:** shorter grants, but exact and subtree then overlap and `/*` includes the root. Granting both costs one extra scope.
- **Glob or regex wildcards:** expressive, but each adds escaping rules that interact with percent-encoding. Rejected.
- **Pinned query parameters:** rejected above.
- **Decoding before comparison:** rejected; it reopens the `%2F` and double-encoding ambiguity A2 closed.

## Related

#94, #88 (ADR-006), #38, #39, A3. `docs/adr/ADR-006-workload-identity-and-authorization.md`, `src/viharbot/service_auth.py`.
