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
| Engagement | DynamoDB, large bodies in S3 | `eng#<id>` | Retained per engagement; at close it is retired (no longer served, history kept to `retention_until`); erased only on consent withdrawal or another policy-named reason |
| Institutional | DynamoDB, `validated` only | `inst#<topic>` | Written only after human or reviewer approval; versioned and retired (or erased under policy), never overwritten |
| Founder | Separate table or prefix, owner write only | `founder#` | Owner writes; agents read only by explicit ACL |

### Record metadata

`pk`, `sk`, `version`, `owner`, `agent_scope`, `engagement_scope`, `classification`, `authorized_principals[]`, `provenance{source, author, request_id}`, `retention_until`, `validated_at`, `validated_by`, `status` (`observed`, `proposed`, `validated`, `superseded`, `retired`), `supersedes`, `idempotency_key`, `body_ref`. The record-level policy fields (`authorized_principals`, `classification`, engagement closure, `retention_until` and the record `status`) live on the `current` item and are authoritative; copies on `v#<n>` items are history only and are never used to decide access.

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
| Supersede, `retire`, `erase` | Owner or a policy-named principal only; each is audited. `retire` and `erase` are different operations (see Retention, retirement and erasure). |

Required negative tests: forged scope, owner or engagement; classification lowered by a writer; ACL widened by a non-authorized writer; validation fields in an ordinary write; promotion by the writer, by an ineligible principal, and by a reviewer of the wrong tier; retirement or erasure by an unrelated principal.

### Historical versions follow the record's current access policy

Authorization for a record is decided from the policy fields on the record-level `current` item (`authorized_principals`, `classification`, engagement closure, `retention_until`, `status`), **never from the access metadata stored on an older version**. A policy change (revoking a principal, raising `classification`, closing an engagement) is one conditional update of `current`, so it applies to every version and every body of that record from the moment it commits. Old versions are readable only by principals the current policy allows, and a body object is never reachable except through the service: S3 buckets are private, there are no presigned URLs and no direct object access for agents.

**Read-consistency contract (recommended).** Every authorization decision, for any version or body, reads `current` with a **strongly consistent read** (DynamoDB `ConsistentRead=true`), and authorization state is **never cached** in the service, an agent or a proxy. A read that starts after a policy change commits therefore sees the change; a read already past its policy check when the change commits may complete, and that bounded in-flight window is the revocation boundary. Cost, stated as ASSUMED from DynamoDB's published model and to be confirmed in the AWS calculator: a strongly consistent read uses twice the read capacity of an eventually consistent one, and the service needs one such read per request at this low volume, so the extra cost is small. The alternative is a cache with an explicit, bounded staleness window (a stated number of seconds) and an invalidation rule on every policy-changing write; during that window revocation is not immediate, and the ADR would then have to say so wherever it promises revocation. This ADR recommends the strongly consistent read with no cache, and the founder decides.

Tests: after access is revoked, the very next read of the record, an older version and its S3 body is refused; after classification is raised, an older, lower-classified version is refused to a principal below the new ceiling; after engagement closure the next ordinary read is refused; a read issued after a policy change never uses a cached or eventually consistent copy (the test fixture serves a stale `current` to any non-consistent read and the service must still refuse); if a cache is ever introduced, a read after the staleness window or after invalidation does not use the stale entry.

### Promotion across scopes is deferred and denied

Promotion in the table above moves a record's status inside the scope it already lives in. **Moving engagement-derived content into institutional memory is a different, cross-scope operation, and this ADR does not define it.** Until a follow-up ADR does, any attempt to publish engagement or agent content into the institutional tier is denied. That follow-up would need authorized source and destination scopes, a confidentiality review or redaction step before publication (constitution 8.4 forbids confidential client details entering global memory), destination ACL checks, and preserved provenance. Required negative test: engagement content marked confidential cannot enter institutional memory by any operation.

### Retention, retirement and erasure

`retention_until` and engagement closure are enforced **at read time**, independent of DynamoDB's asynchronous TTL (which only reclaims storage some time after expiry, so an expired record can remain queryable). The service refuses to return an expired record, and refuses ordinary retrieval of any record of a closed engagement (closure retires the records; the history-read rule below still applies until `retention_until`). Two distinct operations end a record's life:

- **`retire`** stops normal serving: the record no longer appears in ordinary retrieval and `current.status` becomes `retired`. History is kept: old versions and **all bodies are retained until `retention_until`**, readable only by principals the current policy allows for history. When `retention_until` passes, the record is expired and its bodies are deleted by the delete job below. Engagement close retires that engagement's records.
- **`erase`** is immediate deletion of the record, every version and every body, requested by the owner or a policy-named principal, audited, for consent withdrawal and similar cases. It overrides retention. The audit entry carries identifiers and the reason, never content.

Body deletion (after `erase`, or after `retention_until` for a retired or expired record) runs as an idempotent delete job with retries, and an orphan sweep finds bodies with no live record (protocol below). Whether backups and point-in-time recovery are enabled, and for how long, is an open decision; until it is made they are off and the gap is stated. Because erasure of backups is not covered, an `erase` does not claim to remove backup copies. Tests: an expired-but-present record is not returned; a closed engagement's records are not returned by ordinary retrieval; a retired record is not in ordinary retrieval, its history and bodies remain readable to authorized principals until `retention_until`, and its bodies are deleted after it; an erased record, all its versions and all its bodies are gone at once and the erase is audited; `erase` by an unrelated principal is refused; an orphaned body is found by the sweep.

### Consistency rules

A write is one atomic **DynamoDB** transaction, so version checks, supersession and de-duplication of the metadata cannot be partly applied. The atomicity claim covers DynamoDB only; S3 bodies follow the ordering below. Layout, per logical record `pk = <scope>#<record_id>`:

| Item (`sk`) | Contents |
|---|---|
| `v#<n>` | An immutable version: body or `body_ref`, metadata, `status` |
| `current` | The current version number, its `status`, and the authoritative record-level policy: `authorized_principals`, `classification`, engagement closure, `retention_until`. Authorization reads this item with a strongly consistent read |
| `idem#<key>` | The result of an earlier write with this idempotency key (the version it created); the key is scoped to the principal and the record, for example `request_id#kind` |

- **Write path:** `TransactWriteItems` with a condition that `current.version` equals the version the caller read, a put of `v#<n+1>`, an update of `current` to `n+1` (policy fields on `current` change only through a separate authorized policy-update transaction that is conditional on the same version check), an update of `v#<n>` to `superseded`, and a put of `idem#<key>` with `attribute_not_exists`. Either all of it commits or none does, so there is never more than one current version and the history is never inconsistent.
- **Bodies in S3 are immutable and uploaded first.** The service uploads the body to a deterministic key `bodies/<record>/<hash of the idempotency key>` with server-side encryption and no overwrite, and only then runs the transaction that publishes `body_ref`. A retry after a lost response reuses the same key, so it neither duplicates nor overwrites the object. A `body_ref` is never committed before its object exists. If the upload succeeds but the transaction fails, the object is an unreferenced orphan.
- **Sweep and retry coordination.** A grace period alone does not stop a sweep from deleting a body that a retrying writer is about to reference, so both sides re-check. (1) The writer, immediately before the transaction, verifies the object still exists (`HeadObject`) and re-uploads it to the same deterministic key if it is missing. (2) The sweep deletes an object only if, at deletion time, it re-checks that no `v#` item (and no pending claim) references it and the object is older than a grace period longer than the maximum write time, and it deletes with a conditional or version-specific delete. (3) The writer sets a short-lived claim (an item `claim#<key>` with a TTL, written before the existence check and included in the transaction condition set) that the sweep must see absent before deleting. If the sweep wins the race after the writer's check, the transaction's final condition fails or the post-commit read-back finds the body missing, and the writer re-uploads and retries; a committed reference is never left without a body. The exact mechanism (claim item versus versioned delete) is an implementation choice to be settled with the tests below; the invariant is that a concurrent writer wins.
- **Racing writers:** exactly one transaction passes the version condition; the other fails, re-reads and retries or reports a conflict.
- **Retry after a committed write whose response was lost:** the retry's `idem#<key>` put fails its condition, so nothing is written twice; the service reads that item and returns the original result.
- **Supersession:** never overwrite; old versions stay readable by authorized principals for history until `retention_until`, including after `retire`. Only `erase` removes history early.
- **Promotion:** `observed` or `proposed` to `validated` needs a reviewer principal different from the writer. No code path promotes automatically (constitution 8.3).
- **Failover:** state is keyed by the logical agent and engagement, never by instance.
- **Tests:** an upload that succeeds followed by a failed transaction (orphan swept, current version unchanged); a retry after a lost response reusing the same object; a transaction never committing a `body_ref` whose object is absent; two racing writers; a retry after a committed write with a lost response; a retry before commit; an idempotency key reused by a different principal (refused); a simulated partial failure leaves the old current version unchanged; sweep versus retry: an aged orphan is retried while the sweep runs, interleaved at each step (sweep lists, writer verifies, sweep deletes, writer transacts), and no committed `body_ref` ever lacks its body; the sweep refuses to delete an object referenced by a `v#` item or a live claim; a retry whose object was swept re-uploads to the same key before committing.

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
