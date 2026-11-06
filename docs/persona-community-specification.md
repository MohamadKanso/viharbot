# Epic #157: Agent persona and community framework, specification report

**Status: specification, plus Phase A implemented as declarative configuration only.** Phases B to E are proposals. Nothing here changes a runtime path, prompt, binding, endpoint, SSE event or model call.

**Guiding principle:** Preserve identity. Develop personality. Enable collaboration. Govern autonomy.

Sources of authority, in order: the repository on `main`, [Founding Charter v2](governance/founding-charter-v2.md), [Agent Constitution v1](governance/agent-constitution-v1.md), [Agent Architecture v1.1](governance/agent-architecture-v1.1.md), [Skills Registry v1](governance/agent-skills-registry-v1.md). Where this report and those documents differ, they win.

## 1. Current-state assessment

### What Epic #157 currently says on GitHub

Issue #157 is titled *Agent Persona Design and Community Development: Now Specifying*. Its children are #158 (public page), #159 (Intent & Clarification profile), #160 (Accessibility & Inclusion profile), #161 (persona rubric, memory, permissions and approvals), #162 (learner-to-Agent Designer pathway) and #163 (content/accessibility/release validation). It concerns **future, unnamed candidate agents** and the governed process for designing them. It states that candidates remain unnamed and inactive, and that the founder alone approves merge, deployment, curriculum and activation. See [Now Specifying](now-specifying-agent-personas.md).

The brief this report answers is broader: persona and community design for the **eight established agents**. That is a different (complementary) scope. This report treats the brief as the instruction and #157 as the parent tracker, and flags the mismatch as decision D1 in section 10. It does not edit #157 or create issues.

### What exists in the repository (verified)

| Area | State | Evidence |
|---|---|---|
| Identities | **Implemented.** Ten TOML identities: the eight established agents plus `archie` and `ada` (education). Strict, frozen, unknown fields rejected. | `src/viharbot/agent_identities/`, `identity.py`, `identity_registry.py` |
| Identity prompts | Implemented as markdown, one per identity (role, style, expertise, evidence, boundaries, limitations, collaboration). | `agent_identities/prompts/*.md` |
| Personality-aware inference | **Implemented, tested, flag off and set nowhere.** Composes identity prompt + precedence clause + service prompt. Only six pipeline services, and only their approved bindings. | `inference_config.py`, [personality-aware inference](personality-aware-inference.md), [development status](development-status.md) |
| Execution bindings | Implemented. Nine enabled bindings: six pipeline services plus `pixel`, `sage`, and in-process `sage-code-review`. Atlas and Orbit have none. | `execution_config/bindings.toml`, `execution_bindings.py` |
| Skills registry | Implemented as metadata. 78 skills, all `proposed`; nothing reads it at runtime. | `skills/`, [skills registry](skills-registry.md) |
| Orchestration policy | Implemented. Opt-in iterative orchestration with decision, execution and time budgets, dependency and freshness rules. Charlie recommends; the application authorizes. | `orchestration_policy.py`, `iterative_execution.py`, [controlled delegation](controlled-delegation.md), [iterative orchestration](iterative-orchestration.md) |
| Event streaming | Implemented. Schema 1.0, 14 chat event types, ordered by `sequence`. | `live_events.py`, [live observability](live-observability.md) |
| Team page / avatars | Implemented. Eight headshots served from an allowlist. | `web_app.py` (`TEAM_IMAGES`), `images/` |
| Memory | **Not implemented as memory.** Append-only JSONL audit trail that nothing reads back. Governed memory is specified in ADR-009 (Proposed). | [memory architecture](memory-architecture.md), [ADR-009](adr/ADR-009-persistent-memory.md) |
| Workload identity and authorization | Specified only (ADR-006, Proposed). | [ADR-006](adr/ADR-006-workload-identity-and-authorization.md) |
| Structured persona model | **Did not exist.** `personality` is one sentence per identity. | this report; Phase A adds it |
| Agent-to-agent peer review, disagreement, relationships, community knowledge | **Did not exist.** Constitution sections 5.5 and 5.6 describe the duties; no code implements them. | none |

Capability status: Nova, Scout, Cipher, Charlie, Pixel (slides) and Sage (analysis, code review) have connected services. Atlas and Orbit are declared but unconnected, so any persona statement about them describes intent.

## 2. Gap analysis

| # | Gap | Impact | Phase |
|---|---|---|---|
| G1 | No structured persona. One-sentence `personality` cannot express values, voice, disagreement style or limits. | Personas are superficial prompts, which the brief rules out. | A |
| G2 | Identity prompts and personality are separate free text with no schema or tests. | No way to test that voices are distinct or stay inside governance. | A, B |
| G3 | Personality inference is wired only to six pipeline services, one prompt per identity. | A persona cannot yet vary by capability or express peer-review behavior. | B |
| G4 | No community interaction contract (peer review, challenge, expertise request). | Collaboration is Charlie-mediated only. | C |
| G5 | No relationship model; outcomes are not recorded. | No basis for collaboration history. | C, D |
| G6 | Memory is audit-only; no scopes, provenance, retention or authorization on read. | Agents cannot learn. | D (depends on ADR-009) |
| G7 | SSE events cover planning, delegation and execution, but not review, challenge or validation. | The user cannot see community activity. | E |
| G8 | No agent profile or community UI. | Personas are invisible. | E |
| G9 | Documentation drift: `agent-identity-architecture.md` lists Pixel as "Gemini multimodal family" and omits Sage's `code_review`; the identity TOML (and its tests) say "GPT mini family" and include it. | Reviewers get conflicting facts. | Not fixed here; see D5 |
| G10 | `Capability.PHILOSOPHICAL_ANALYSIS` and others are implemented for Sage and Pixel, but Atlas/Orbit have no service. | Personas for them cannot be exercised. | Out of scope |

## 3. Persona schema (implemented in Phase A)

Code: `src/viharbot/personas/` (`models.py`, `registry.py`, `definitions/<id>.toml`). Schema version `1.0`.

| Block | Fields | Purpose |
|---|---|---|
| `agent_id` | must equal the file name and an existing identity | Stable identity link. Name, title and role are **not** duplicated; they stay in the identity file. |
| `provenance` | `persona_version` (semver), `schema_version`, `status` (`draft` / `review` / `approved`), `sources`, `approval_ref` | Versioning and configuration provenance. `approval_ref` is allowed only when `approved`; it is metadata and does not itself verify founder approval (review of the pull request does). |
| `biography`, `values`, `principles` | text and lists | Professional biography, core values, guiding principles. |
| `traits` | `identity_traits`, `design_direction_traits`, `behavioral_tendencies` | Identity traits must appear in the approved identity's `personality` (checked at load). Design direction from the brief is additive and never replaces them. |
| `communication` | `tone`, `sentence_style`, `preferred_vocabulary`, `avoid`, `illustrative_examples` | Voice. Examples illustrate; they are not templates and never apply to structured output. |
| `reasoning` | `approach`, `decision_preferences`, `evidence_standard` | Reasoning and decision preferences. |
| `strengths`, `limitations` | lists | Professional strengths and honest limits, including unconnected capabilities. |
| `collaboration[]` | `agent_id`, `pattern`, `description` | Preferred peer patterns: `requests_review_from`, `reviews`, `challenges`, `defers_to`, `consults`, `hands_off_to`. Descriptive only. |
| `disagreement` | `requires_evidence` (must be `true`), `style`, `resolution_preference`, `escalation` | Constitution section 5.5. The schema cannot express disagreement without evidence. |
| `self_assessment` | `uncertainty_reporting`, `confidence_vocabulary` | Uncertainty reporting. |
| `model_preference` | `family`, `rationale` | Must equal the identity's `preferred_model_family`. **No alias field exists**, so model governance stays in central LiteLLM routing. |
| `visual_identity` | `avatar_file`, `alt_text` | A bare image file name (no path or URL). The loader checks syntax only; a CI test checks that bundled avatars exist in `images/` and in the web allowlist. |

**What the schema forbids by construction.** Every model is strict and frozen, and unknown fields are rejected. A persona cannot carry tools, permissions, scopes, capabilities, endpoints, runtime placement, bindings, model aliases, credentials, trust scores or seniority. It cannot have status `implemented` or `activated`. Tests assert each of these, and that no runtime module imports `viharbot.personas`.

## 4. Eight-agent persona design matrix

All eight are `draft`: the structure and wording are proposals for founder review. The identity file remains authoritative where they differ. Full text: [agent personas](agent-personas.md).

| Agent | Role (unchanged) | Identity traits (approved) | Design direction (brief) | Voice | Disagrees by | Typical peer pattern | Connected today |
|---|---|---|---|---|---|---|---|
| Charlie | Chief Strategist & AI Orchestrator | thoughtful, strategic, composed, analytical | diplomatic, decisive | Composed framing, then a recommendation with conditions | Raising it with the specialist, then disclosing it in synthesis | consults Nova and Sage; defers to Cipher on security | Synthesis, opt-in planning |
| Nova | Director of Research & Knowledge | curious, rigorous, methodical | analytical | Claim, evidence, unverified assumptions | Citing the disputed source or assumption | requests review from Scout | Research |
| Atlas | Director of Exploration & Logistics | practical, resourceful, adventurous, detail-oriented | pragmatic | Concrete: cost, risk, fallback | Naming the uncovered constraint | reviews Orbit's feasibility | None |
| Pixel | Director of Creative Intelligence | creative, expressive, imaginative | design-oriented | Vivid, message-first | Showing an alternative layout | consults Scout for validated evidence | Slide generation |
| Sage | Director of Philosophy & Human Perspective | reflective, empathetic, thoughtful | ethical | Calm; acknowledges, then raises the human impact | Distinguishing evidence from value judgment | reviews human impact of Orbit's proposals | Philosophical analysis, code review |
| Cipher | Chief Security & Risk Officer | precise, skeptical, disciplined, evidence-driven | vigilant | Risk, why, mitigation | Naming the boundary and failure consequence | challenges Orbit's architecture | Security, privacy review |
| Scout | Director of Intelligence & Discovery | observant, investigative, persistent, analytical | independent, evidence-driven | Finding, then the exact contradicting passage | Quoting the conflicting source | reviews Nova's evidence | Fact check, source quality |
| Orbit | Chief Automation & Engineering Officer | systematic, pragmatic, inventive, technically rigorous | execution-focused | Proposal, interfaces, failure handling | Offering a concrete alternative design | requests review from Cipher and Atlas | None |

Reconciliation note: the brief says Charlie is "diplomatic, decisive"; the identity says "thoughtful, strategic, composed, analytical, occasionally witty". Both are kept, in separate fields. No existing attribute was removed or renamed.

## 5. Community collaboration model (proposed, Phases C to E)

The community is not a chat room. Every interaction is a bounded, policy-authorized **work item** executed by the existing orchestrator.

| Interaction | Meaning | Governing rule |
|---|---|---|
| Peer collaboration | Share findings, request expertise, contribute to a deliverable | Only between agents whose capabilities are bound and enabled; recorded as ordinary agent steps |
| Peer review | An independent agent evaluates another's output | The reviewer sees the artifact, not the author's reasoning; reviewer is a different identity; review is advisory unless policy makes it a gate |
| Constructive disagreement | An agent challenges a conclusion | Must carry the five Constitution 5.5 parts: disputed conclusion, reason, evidence, consequence, proposed resolution. No evidence, no challenge. Unresolved material disagreement is disclosed (Constitution 5.6) |
| Specialization | Defer to the right expertise | Deference is a recorded recommendation; the deferring agent keeps its own judgment and must disclose any disagreement |
| Community knowledge | Propose a validated insight for reuse | A proposal only. Promotion to institutional memory needs validation and founder approval (Constitution Article VIII; ADR-009) |
| Community development | Propose changes to skills, workflows, practices | A change request through the normal governed PR process. An agent never edits its own persona, skills, bindings or constitution |

**Bounded collaboration.** The existing orchestration budgets (decisions, executions, seconds), repeat limits, dependency and freshness rules, deadline and cancellation apply to every collaboration step. A review or challenge consumes budget like any other step. No agent can start a conversation outside an authorized work item. Charlie normally coordinates, but a policy-authorized review edge (for example Nova's output to Scout, or Orbit's design to Cipher) can be declared in configuration so Charlie need not relay each exchange. The application executes the edge; no agent calls another directly.

**No parallel engine.** Phase C extends `orchestration_policy.py` with additional authorized action kinds and review steps. It does not add a broker, A2A, a service mesh or a second orchestrator ([AGENTS.md](../AGENTS.md) invariants).

### Relationship model (Phase C proposal)

A relationship is a configured, versioned record `(from, to, pattern, outcome_summary)`. It records collaboration preference (already described in each persona) and, later, **observed outcomes** (for example: review found a material issue / found none / disputed). It never carries a trust score, seniority, or access right, and **no code path may read a relationship when making an authorization decision**. Outcomes are counts attached to engagements, not opinions.

## 6. Security and governance implications

Every item below is either enforced in Phase A or must be enforced by the named phase.

| Requirement | How it is met |
|---|---|
| Identity is separate from authorization | A persona cannot contain authorization fields (schema); loader and runtime never consult it for access (test: no runtime importer) |
| Personality grants no tools; skills grant no permissions | Same, plus the existing skills registry rule |
| Delegation is policy-controlled | Phase C adds actions to the existing policy, validated by the application |
| Privileged actions need approved execution bindings | Unchanged. Atlas and Orbit stay unbound; a persona cannot create a binding |
| No autonomous privilege elevation, no self-rewriting | Persona files are repository configuration changed only by reviewed PRs; status cannot be `implemented` or `activated`; Phase D forbids agent writes to persona, bindings or constitution |
| Persona must not override governance | Phase B keeps the existing precedence clause: task instructions outrank persona. Tested with conflicting persona text |
| Persona must not break structured output | Phase B applies voice only to free-text fields. Typed outputs, tool contracts and inter-agent JSON are never persona-shaped |
| LiteLLM only; no new billable calls in unit tests | No inference added. Persona carries no alias. Tests are offline |
| Shared memory respects engagement and data boundaries | Phase D, after ADR-009 is accepted; Phase A adds no memory |
| Runtime placement independent of identity | Personas have no runtime fields |
| Disagreement is evidence-required | `requires_evidence` is a schema-fixed configuration invariant only; it does not prove evidence exists or is true. Phase C adds validation that a challenge cites a supplied artifact, and even a citation does not prove a claim, so behavioral evaluation remains a separate control |
| Avatar references are safe | The loader accepts only bare image file names (syntax). Existence in `images/` and membership in the web allowlist are checked by a CI test on the bundled definitions, not by the loader. Loader-level enforcement needs a shared allowlist module and is deferred to Phase E |
| Logs avoid prompts and private memory | No logging added; Phase B logs persona id and version only |

Threats to address in Phase C testing (see [threat model](threat-model-agent-communication.md)): prompt injection through another agent's output (treat as untrusted data), sycophantic agreement between agents, runaway review loops (budgets), review shopping, and a persona edited through a data path.

## 7. Recommended phases

Each phase is one reviewable PR, backward compatible, and ends with founder approval to proceed.

| Phase | Deliverable | Status |
|---|---|---|
| A | Persona schema, eight persona definitions, loader, tests, docs | **Implemented in this PR (draft)** |
| B | Persona-aware prompt composition; consistent voice; governance precedence tests | **Implemented, flag off** (second flag `VIHARBOT_PERSONA_PROMPT_ENABLED`; see [personality-aware inference](personality-aware-inference.md)) |
| C | Community interaction contracts, peer review, bounded collaboration, relationship records | **C1 implemented** (contracts only, [community contracts](community-contracts.md), #209); C2 (#210) and C3 (#211) proposed |
| D | Collaboration history, governed memory interfaces, measurable outcomes | Proposed; needs ADR-009 decision |
| E | Real collaboration events, agent profile and community UI requirements, avatar metadata | Proposed |

**Phase B (implemented, flag off).** A renderer (`personas/prompt.py`) turns selected persona fields into a section placed after the identity prompt and before the precedence clause, so task instructions outrank it and the service prompt stays verbatim. It is gated by a **second** flag, `VIHARBOT_PERSONA_PROMPT_ENABLED`, that does nothing unless the personality flag is also on, so enabling personality inference alone cannot change behavior. D3 was resolved conservatively: the persona is **additive** to `prompts/<id>.md`, nothing is removed or moved, and the long-term single source of truth is still open. Personas are `draft`; the flag is the opt-in and logs carry persona id, version and status. Real voice quality is not measured here; that is B2.

**Phase C design (proposed).** New review and challenge contracts as pydantic models (`ReviewRequest`, `ReviewFinding`, `Challenge`), each with sender, recipient, engagement, task, message type and correlation ids per Constitution section 5.4, validated by the application. Policy table of allowed review edges. Reuses existing budgets.

**Phase D design (proposed).** Interfaces only unless durable memory is approved: a `CollaborationRecord` (who, what, outcome, provenance, retention, scope) written through the ADR-009 memory API once accepted. Learning is limited to measured outcomes (for example review precision) and approved vocabulary or examples; it cannot change identity, permissions or constitution. Persona changes are PRs.

**Phase E design (proposed).** Additive event types in a new schema minor version (for example `review_requested`, `review_completed`, `challenge_raised`, `challenge_resolved`, `dependency_waiting`, `approval_waiting`), emitted only from real execution. Existing 14 events and `schema_version 1.0` consumers stay valid. UI: a profile panel per agent (persona fields, avatar, alt text), selection reason and contribution list on the final response, collaboration timeline. No redesign in the first PR.

## 8. Proposed GitHub sub-issues

Not created. Titles are proposals for the founder to file under #157. #161 (persona rubric, memory, permissions, approval gates) is related: its lifecycle statuses informed `PersonaStatus`.

| ID | Title | Depends on | Acceptance criteria |
|---|---|---|---|
| A1 | Persona schema and eight persona definitions | none | Schema validated and versioned; eight personas load; identities unchanged; no runtime import; tests offline. **Done in this PR** |
| A2 | Founder review of the eight draft personas | A1 | Each persona reviewed; status moved to `review` then `approved` with an `approval_ref`, by PR |
| A3 | Reconcile identity documentation drift (G9) | none | Pixel family and Sage capabilities consistent across docs, TOML and tests |
| B1 | Persona-aware prompt composition (flagged) | A2, D3 | Off by default; legacy prompt hashes unchanged; persona cannot override task instructions; structured output schemas unchanged |
| B2 | Persona consistency evaluation suite (optional) | B1 | Separate from CI; explicit metrics and a token and cost cap; never run in unit tests |
| C1 (#209, done) | Community interaction contracts. Implemented as `ReviewRequest`, `ReviewReport` (with nested `Finding`s), `Challenge`, `ChallengeResolution`. Started before A2 (founder persona review) because the contracts do not depend on persona wording | A2 | Models carry sender, recipient, engagement, task, type, correlation ids; challenges must cite supplied evidence; unknown fields rejected |
| C2 (#210) | Peer-review edges in orchestration policy | C1 | Declared edges only; consume existing budgets; reviewer differs from author; cancellation and deadline honored |
| C3 (#211) | Relationship records (no authorization use) | C1 | Versioned; no trust score; a test proves no authorization path reads them |
| D1 | Collaboration history and outcome metrics | C2, ADR-009 | Records carry provenance, scope and retention; metrics defined before use |
| D2 | Governed update procedure for persona, skills and practices | A2 | Changes only via reviewed PR; agent writes to these files are impossible by construction |
| E1 | Collaboration SSE events (schema minor version) | C2 | Emitted from real execution only; old clients parse unchanged; docs and fixtures updated |
| E3 | Loader-level avatar validation via a shared allowlist | E1 | Allowlist moved from `web_app.py` to a side-effect-free shared module; loader rejects unknown or non-allowlisted avatars; negative tests for a missing file and an existing but unallowlisted image |
| E2 | Agent profile and community UI requirements, then build | E1, A2 | Requirements doc approved before build; accessibility checks; existing page works unchanged |

## 9. Files expected to change

| Phase | Create | Modify |
|---|---|---|
| A (this PR) | `src/viharbot/personas/{__init__,models,registry}.py`, `personas/definitions/*.toml` (8), `tests/test_persona_registry.py`, `docs/persona-community-specification.md`, `docs/agent-personas.md` | none in `src/` outside the new package |
| B (done) | `personas/prompt.py`, `tests/test_persona_prompt.py` | `inference_config.py`, `tests/test_persona_registry.py` (import allowlist), `docs/personality-aware-inference.md`, `docs/development-status.md` |
| C1 (done) | `community/{__init__,models}.py`, `tests/test_community_contracts.py`, `docs/community-contracts.md` | none outside the new package |
| C2 and C3 | policy, relationship models, tests | `orchestration_policy.py`, `iterative_execution.py`, `orchestration_planner.py` |
| D | collaboration record models, tests | memory client once ADR-009 is accepted |
| E | event models, UI requirements | `live_events.py`, `live_hooks.py`, `docs/live-observability.md`, `static/index.html` |

## 10. Risks, unresolved decisions and testing strategy

### Unresolved decisions (for the founder)

| ID | Decision | Default taken in Phase A |
|---|---|---|
| D1 | Is the brief's scope a new child of #157, or does it replace part of #157's "Now Specifying" scope? | Proceed as a child; reference #157 without closing it |
| D2 | Do the draft persona texts (biographies, values, voice) reflect the intended characters? | Marked `draft`; none approved |
| D3 | After Phase A, is `prompts/<id>.md` or the persona file the source of truth for voice text? | Both coexist; nothing reads personas, so no conflict yet |
| D4 | Should `archie` and `ada` receive personas? Issues #146 and #139 govern them separately | Not included; the loader does not require them |
| D5 | Fix the documentation drift in G9 here or separately? | Separately (A3), to keep this PR to one concern |
| D6 | Is durable memory in scope for #157 or only for the ADR-009 work? | Out of scope; interfaces only in Phase D |

### Risks

| Risk | Mitigation |
|---|---|
| Persona text becomes a second, drifting copy of identity text | Load-time checks that identity traits and model family match the identity file; D3 resolves the long-term owner |
| Persona wording read as authority | Authority-phrase test, schema without authority fields, `draft` status |
| Review and challenge multiply inference cost and latency | Reuse existing budgets; cost note required in each Phase C PR |
| Sycophantic or looping agent exchanges | Different reviewer identity, evidence-required challenges, repeat limits |
| Unconnected agents (Atlas, Orbit) described as capable | Personas state limits; docs mark them unconnected |
| Persona-shaped output breaks parsers | Voice limited to free text; contract tests in Phase B |
| LLM-judged personality tests are costly and flaky | Optional separate suite with a hard cap (B2); unit tests stay offline |

### Testing strategy

* **Phase A (done):** schema, loader, determinism and digest tests; identity preservation; epic-table traits; distinct voices; authority and key lint; cross-checks against identities; runtime-isolation test. All offline.
* **Phase B:** prompt-hash regression for legacy prompts; flag-off equivalence; conflict tests where persona text contradicts task instructions; schema tests that structured outputs are unchanged.
* **Phase C:** contract fuzzing, edge-policy tests, budget exhaustion, cancellation, injection in peer output, evidence-required challenge tests with a fake model.
* **Phase D:** isolation and poisoning tests per ADR-009; forbidden-write tests for persona, bindings and constitution.
* **Phase E:** event schema fixtures, backward-compatibility parse tests, Node UI tests, accessibility checks.
* **Optional evaluation suite:** model-based voice-consistency scoring, run manually, with explicit metrics, a fixed sample size and a spend cap. Never part of CI.
