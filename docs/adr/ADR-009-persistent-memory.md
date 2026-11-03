# ADR-009: Persistent, governed agent memory

**Status: Proposed.** Nothing here is implemented or provisioned. It is intended to satisfy the memory design that issue #40 depends on (cited there as "ADR-003"), and to support the isolation and poisoning tests in #48. The founder confirms the numbering.

## Context

Verified in the repository:

- `remember()` appends `{timestamp, request_id, result}` to `$MEMORY_DIR/<agent>/memory.jsonl` (`memory.py`). Callers are the research, security, specialist and Sage agents; Pixel keeps deck state as files under `MEMORY_DIR/pixel`.
- Nothing reads these files back (`docs/memory-architecture.md`). On DigitalOcean `MEMORY_DIR` is `/tmp/viharbot-memory`, so records are ephemeral. Compose uses bind mounts or named volumes.
- The constitution (Article VIII) requires a defined memory scope per agent, governed cross-scope access, provenance, validation before anything becomes institutional knowledge, and that confidential client details never flow into global memory.
- There is no read path, no metadata, no authorization, no versioning, no idempotency (a retry can append a duplicate), no promotion workflow, and no engagement or principal key. Memory is partitioned only by an agent string.
- `AGENTS.md` says each agent keeps independent memory, to start with simple isolated storage, and not to add a database without a concrete need.

## Decision

**One DynamoDB table behind a small memory API service**, with S3 only for large bodies. Semantic search, AgentCore Memory, OpenSearch, Aurora with pgvector and Bedrock Knowledge Bases are deferred until recall quality is shown to be insufficient. Idle cost is close to zero and conditional writes provide the consistency rules directly.

### Taxonomy

| Type | Store | Scope key | Lifecycle |
|---|---|---|---|
| Task | DynamoDB item with TTL | `task#<id>` | Expires in days; never promoted automatically |
| Logical-agent (for example Nova, shared by both runtimes) | DynamoDB | `agent#nova` | Versioned and superseded; survives failover because it is keyed by the logical agent, never the instance |
| Instance-local | Local disk or process memory | instance id | Disposable; never authoritative |
| Engagement | DynamoDB, large bodies in S3 | `eng#<id>` | Retained per engagement; deleted at close under the consent policy |
| Institutional | DynamoDB, `validated` only | `inst#<topic>` | Written only after human or reviewer approval; versioned and retired, never overwritten |
| Founder | Separate table or prefix, owner write only | `founder#` | Owner writes; agents read only by explicit ACL |

### Record metadata

`pk`, `sk`, `version`, `owner`, `agent_scope`, `engagement_scope`, `classification`, `authorized_principals[]`, `provenance{source, author, request_id}`, `retention_until`, `validated_at`, `validated_by`, `status` (`observed`, `proposed`, `validated`, `superseded`, `retired`), `supersedes`, `idempotency_key`, `body_ref`.

### Authorization at the retrieval boundary

The memory service filters before anything is returned. Prompts never enforce access.

1. The caller authenticates under ADR-006. The service derives principal, agent and engagement scope from the verified token, and the caller cannot assert them.
2. Every query is built from the derived scope keys, and each returned item must also pass an ACL check (the principal is in `authorized_principals` and the scope matches).
3. If semantic search is added later, it must pre-filter on the authorized keys, or over-fetch and post-filter. Unfiltered hits are never returned.
4. Returned content is wrapped as data with its provenance and a "not instructions" marker.
5. Only `validated` items are served from the institutional tier.
6. Every read and write is audited. Logs carry identifiers, never content.

### Write-side authorization

Retrieval filtering alone does not stop an authorized writer from assigning another engagement, widening access or forging validation. Every mutating operation is **deny by default** and checked by the service from the verified token and policy, never from caller-supplied fields.

| Operation | Rule |
|---|---|
| Create or update a record | Allowed only inside the caller's own scope. `owner`, `agent_scope` and `engagement_scope` are **derived server-side** from the token. A request that names a different scope or owner is refused. |
| `classification` | The caller may set it only at or below its classification ceiling, and may raise it freely but never lower it. Lowering needs the owner or a policy-named reviewer. |
| `authorized_principals` | Defaults from policy for the scope. Widening it needs a principal named in policy as allowed to grant access to that scope; a writer cannot add itself or a peer. |
| Validation fields (`status`, `validated_at`, `validated_by`) | Settable only by the promotion operation. A normal write that supplies them is refused. |
| Promotion (`observed` or `proposed` to `validated`) | Needs a principal named in a **reviewer policy** for that tier and type, who is not the writer. Being a different principal is necessary, not sufficient. Reviewer eligibility is configuration reviewed in the repository, never inferred. |
| Supersede, retire, delete | Owner or a policy-named principal only; each is audited. |

Required negative tests: forged scope, owner or engagement; classification lowered by a writer; ACL widened by a non-authorized writer; validation fields in an ordinary write; promotion by the writer, by an ineligible principal, and by a reviewer of the wrong tier; deletion by an unrelated principal.

### Retention and deletion

`retention_until` and engagement closure are enforced **at read time**, independent of DynamoDB's asynchronous TTL (which only reclaims storage some time after expiry, so an expired record can remain queryable). The service refuses to return an expired record or any record of a closed engagement. Deleting or retiring a record also deletes its S3 body and every version's body through an idempotent delete job with retries, and an orphan sweep finds bodies with no live record. Whether backups and point-in-time recovery are enabled, and for how long, is an open decision; until it is made they are off and the gap is stated. Tests: an expired-but-present record is not returned; a closed engagement's records are not returned; a deleted record's body is removed; an orphaned body is found by the sweep.

### Consistency rules

A write is one atomic DynamoDB transaction, so version checks, supersession and de-duplication cannot be partly applied. Layout, per logical record `pk = <scope>#<record_id>`:

| Item (`sk`) | Contents |
|---|---|
| `v#<n>` | An immutable version: body or `body_ref`, metadata, `status` |
| `current` | A pointer holding the current version number and its `status` |
| `idem#<key>` | The result of an earlier write with this idempotency key (the version it created); the key is scoped to the principal and the record, for example `request_id#kind` |

- **Write path:** `TransactWriteItems` with a condition that `current.version` equals the version the caller read, a put of `v#<n+1>`, an update of `current` to `n+1`, an update of `v#<n>` to `superseded`, and a put of `idem#<key>` with `attribute_not_exists`. Either all of it commits or none does, so there is never more than one current version and the history is never inconsistent.
- **Racing writers:** exactly one transaction passes the version condition; the other fails, re-reads and retries or reports a conflict.
- **Retry after a committed write whose response was lost:** the retry's `idem#<key>` put fails its condition, so nothing is written twice; the service reads that item and returns the original result.
- **Supersession:** never overwrite; old versions stay readable by authorized principals for history until retention ends.
- **Promotion:** `observed` or `proposed` to `validated` needs a reviewer principal different from the writer. No code path promotes automatically (constitution 8.3).
- **Failover:** state is keyed by the logical agent and engagement, never by instance.
- **Tests:** two racing writers; a retry after a committed write with a lost response; a retry before commit; an idempotency key reused by a different principal (refused); a simulated partial failure leaves the old current version unchanged.

### Cross-cloud access

The memory API service is the only holder of AWS data-plane credentials. DigitalOcean workloads call it over HTTPS with signed service tokens, so no AWS keys spread to agents, authorization lives in one place, and no database is exposed publicly. The service becomes the memory trust anchor and a single point of failure, so it must be hardened and audited. Whether it runs on AWS (local identity to DynamoDB) or on DigitalOcean (a stored key or a federated identity) is a founder decision.

## Evaluation of the stores

Pricing figures are examples taken from provider pages; confirm in the AWS calculator.

| Store | Fit |
|---|---|
| DynamoDB | Chosen. Conditional writes, TTL, transactions; on-demand about $0.625 per million writes and $0.125 per million reads; near-zero idle |
| S3 | Only for large bodies referenced from DynamoDB |
| AgentCore Memory | Managed events and extracted long-term records; usage-based with no idle charge listed; no per-record principal ACL or explicit supersession model, and extraction runs through a model, which makes poisoned content hard to keep out; couples the Fargate runtime and validated tiers to AgentCore. Revisit later |
| OpenSearch Serverless | Vector search, but about $175 to $350 per month minimum (a scale-to-zero collection type exists; vector support unverified) |
| Aurora PostgreSQL with pgvector | SQL and row-level security, about $45 to $60 per month at the minimum; needs private networking |
| Bedrock Knowledge Bases | Document retrieval over a vector store; coarse access control; poor fit for writable memory |

## Alternatives considered

Direct data-plane access from each agent (spreads AWS keys, repeats authorization everywhere, no clean rotation); AgentCore Memory as the single store; a vector database first; keeping local files (no sharing, ephemeral on DigitalOcean).

## Consequences

A new service and a table to operate. Recall is by key and tag until semantic search is justified. Existing `remember()` callers must move to the service or stay as non-authoritative local logs.

## Test plan (no live inference; DynamoDB Local and fixtures)

Isolation across agents and engagements; a benign poisoned instruction is returned only as data; unauthorized read and write refused; the write-authorization, retention and consistency tests above; a stale or unvalidated item is never served as institutional; concurrent-write conflict and idempotent retry; supersession keeps history; retention and deletion; failover reads the same record; logs contain no memory content.

## Cost

Estimates: a DynamoDB table plus a small memory API is well under $5 per month at low volume. Adding Aurora with pgvector is about $45 to $60, and OpenSearch about $175 to $350. AgentCore Memory is usage-based. Approval is required before provisioning.

## Open questions

Where the memory service runs; whether AgentCore Memory's access model suits any tier once verified; who may validate institutional records; retention periods and the consent policy; how founder knowledge is delivered to agents without entering global memory.

## Related

ADR-006, ADR-008, #40, #48, `docs/memory-architecture.md`, `docs/roadmap-distributed-agents.md`.
