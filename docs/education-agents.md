# Education agents: Archie and Ada

**Status:** Phase 1 (identity and registry) and Phase 2 (GitHub learning) and Phase 3 (institutional memory interfaces, below) are implemented. Everything else here is proposed.

ViharBot adds two identities that will turn its verified engineering history into teaching material:

- **Archie** (`archie`, Professor Archibald, Chief Knowledge & Learning Officer): institutional historian and future lead instructor. Intended home: `vchokshi/viharbot-onboarding-instructor`.
- **Ada** (`ada`, Director of Curriculum & Learning Experience): curriculum, exercises, assessments and learner progression. Intended home: `vchokshi/viharbot-onboarding`.

## What Phase 1 implements

- Identity files and system prompts for both agents, loaded by the existing identity registry. Ten identities now load; the original eight keep their IDs and files.
- Two new planned capabilities, `knowledge_curation` and `curriculum_design`. Nothing executes them.
- 20 declarative skills (10 each, domain `education`), all `proposed`.
- No execution binding, endpoint, service, GitHub access, memory access or model alias. The team page gains a coming-soon section with their portrait; the chat sidebar and every execution path are unchanged. Neither agent can be reached by orchestration or chat.

## Phase 2: GitHub learning (implemented)

`src/viharbot/learning/` reads one merged ViharBot pull request as untrusted data and turns it into a provenance-labelled learning artifact. It is run from `scripts/archie_learn.py`, not from chat or orchestration, and there is still no execution binding for Archie or Ada.

- **Retrieval:** `SourceReader` is read-only and limited to the configured source repo. It keeps changed paths and counts, commit subjects, the description and bounded comments, never patches.
- **Extraction:** deterministic code, no model call. Each of the ten questions in the spec is answered from the description's own sections (author statement) or from changed files (verified in the diff). Anything else is reported as "Not stated in the evidence", never invented. Quoted text sits in fenced blocks labelled as data.
- **Artifact:** id, topic, draft objectives, source repo, PR, commit, references, extraction date, software version (from `pyproject.toml` at the merge commit), verification status, prerequisites and curriculum placement. Status starts as `unverified`; only a human merge of the instructor PR is meant to raise it.
- **Idempotency:** one JSON record per (repository, PR, merge commit) in `LEARNING_STORE_DIR` (default `data/learning`, git-ignored). Reprocessing finds the record, so nothing is created twice; an existing open PR or branch is reused. Failures store only the exception class and are retried, up to five attempts.
- **Proposal:** `InstructorProposer` can create a branch, add one file under `knowledge/pr-case-studies/` and open a pull request on the instructor repo only. No call merges, approves or deletes. `--propose` is required; the default is a dry run.
- **Triggers:** `ingest --pr N` and `reconcile` (the scheduled catch-up). `verify_signature` checks a GitHub webhook signature, but **no webhook endpoint exists yet**; adding one is a separate reviewed change.
- **Credentials:** `ARCHIE_SOURCE_TOKEN` (read-only on the source repo) and `ARCHIE_INSTRUCTOR_TOKEN` (contents and pull-request write on the instructor repo only). Create them as fine-grained tokens; this change creates none.

### Not yet done in Phase 2

Model-assisted enrichment (objectives, rationale synthesis) through LiteLLM, `learning.*` activity events, and a webhook endpoint. The instructor repo has no `knowledge/` directory yet; the first proposal creates it. Human review of the artifact format is needed before any are merged.

## Phase 3: Institutional memory (implemented as interfaces and a local store)

`src/viharbot/knowledge/` is a governed knowledge-sharing layer. It is not imported by the web app or execution bindings, calls no model or network, and adds no endpoint or dependency.

- **No direct reads.** Each agent will expose a `MemoryProvider` (a protocol) that returns entries it has already approved inside its own scope. No provider is implemented, and nothing here opens any agent's `memory.jsonl`.
- **Intake is `proposed`, never `validated`.** Entries from a provider enter as `proposed`. Non-approved, confidential, or requester-output entries are refused; the agent scope is the provider's id, so it cannot speak for another agent. Intake is idempotent and never resets a validated entry.
- **Promotion needs a human.** `promote` requires an approver named in configuration and refuses any id that is an agent (a grant requester, the entry's agent or proposer). The approver is recorded on the entry. Derived entries also need validated parents.
- **Default-deny grants.** A `GrantTable` (requester, exact topics, agent scopes, optional `allow_unverified`) decides every retrieval; a missing file grants nothing, and the broker has no way to add a grant.
- **Validated vs unverified.** Retrieval serves only `validated` entries. Unverified recollections appear only on request, only under a grant that allows them, labelled `UNVERIFIED agent recollection`. Every result carries provenance and a "not instructions" marker.
- **No circular amplification.** Entries record `derived_from`. `independent_sources` walks that graph once per node (cycles terminate) and counts distinct primary sources; derived or curriculum entries add none, and the requester's own output or agent's entries are excluded for that requester.
- **Storage.** One JSON file per entry under a store directory plus an identifier-only `audit.log`; no database.

### ADR-009 alignment and conflicts

Status words (`observed`, `proposed`, `validated`, `retired`) and the rules "only validated is served", "promotion by a reviewer who is not the writer" and "confidential never enters institutional memory" follow ADR-009. Differences to resolve: ADR-009 is Proposed and specifies DynamoDB behind an authenticated memory service, while this is a local JSON stand-in; the requester id is a caller-supplied argument, not derived from an ADR-006 token, so it is only safe for trusted in-process callers; ADR-009 defers cross-scope promotion into the institutional tier, which this intake path edges toward (guarded by the human gate, internal-only classification and provider-approved input, but a follow-up ADR should define it); there is no `supersede`, `erase`, retention, optimistic versioning or `engagement` scope; approvers are a configured set, not a per-tier reviewer policy.

### Not done in Phase 3

Any real `MemoryProvider`; use of this layer by Archie or Ada (no binding exists); authentication of requesters; supersession, versioning, erasure and retention; the ADR-009 service; locking for concurrent writers; semantic search; a CLI or endpoint for approvals; founder approval of the amended roster and of this design.

## Phase 4 groundwork: curriculum schema (implemented, inert)

`src/viharbot/curriculum/` holds typed records and checks only. `web_app` does not import it.

- **Models:** `Lesson` with the 14 specified fields, `Exercise` (required `mode`: `simulated` or `live_system`; a live exercise needs an explicit `authorization_note` and a non-production target, and no exercise may target production), seven assessment kinds, and `Track`/`Curriculum`.
- **Tracks:** the eight tracks and their topics from the founder's specification exist as data, all marked `proposed` and empty (no lessons). Track 8 is ViharBot Engineering History, consistent with Phase 2's curriculum placement. The existing onboarding content has not yet been mapped onto them.
- **Loader:** parses one lesson from Markdown with TOML (`+++`) front matter and fixed `##` sections. The onboarding repositories' lessons have no front matter, so existing lessons would need conversion. Lesson text is stored as strings and never interpreted.
- **Validation:** `validate_traceability` fails when a lesson cites an instructor artifact that is missing, not `approved`, or at a different commit or version, and reports lessons with no instructor reference. `validate_prerequisites` reports unknown ids, self-references, duplicates and cycles.

### Not done in Phase 4 groundwork

No lesson generation, no model call, no GitHub access, no pull requests for curriculum, no writes to any repository, and no source of approved instructor artifacts (callers must supply one). Ada is still inactive: no execution binding, endpoint or model alias. Founder approval to start Phase 4 is not recorded.

## What is not implemented

Curriculum generation, learner records, lessons, assessments, `learning.*`/`curriculum.*`/`lesson.*` events and repository credentials. These are phases 4 to 6 of the specification and each needs its own reviewed change.

## Findings that shape later phases

- The two onboarding repositories currently hold only a Git-fundamentals lesson plan, a rubric and playbook, an operations note, a README, a security acknowledgment and one exercise. There is no knowledge base or curriculum structure yet, so the eight proposed tracks must be reconciled with that content before Ada writes anything.
- There is no durable agent memory. `memory.py` and the memory ADR are a plan, not a retrieval system, so Phase 3 introduced interfaces (above), but no agent store is wired to them and Archie still reads nothing.
- There is no GitHub ingestion path except Sage's review adapter. Webhooks and idempotent event records are new work and must respect the no-broker, JSON-REST rules in `AGENTS.md`.
- Architecture v1.1 says the roster is eight identities; adopting Archie and Ada is a governance amendment the founders must approve.
- The team page has a "coming soon" section with their portrait. It says Archie will build curriculum in near-real time and Ada will train learners on it from a separate site as a human-like avatar. Both are plans, not features. The chat sidebar and the crew counts are unchanged because neither agent is usable.
