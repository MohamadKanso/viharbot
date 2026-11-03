# Education agents: Archie and Ada

**Status:** Phase 1 (identity and registry) is implemented. Everything else here is proposed.

ViharBot adds two identities that will turn its verified engineering history into teaching material:

- **Archie** (`archie`, Professor Archibald, Chief Knowledge & Learning Officer): institutional historian and future lead instructor. Intended home: `vchokshi/viharbot-onboarding-instructor`.
- **Ada** (`ada`, Director of Curriculum & Learning Experience): curriculum, exercises, assessments and learner progression. Intended home: `vchokshi/viharbot-onboarding`.

## What Phase 1 implements

- Identity files and system prompts for both agents, loaded by the existing identity registry. Ten identities now load; the original eight keep their IDs and files.
- Two new planned capabilities, `knowledge_curation` and `curriculum_design`. Nothing executes them.
- 20 declarative skills (10 each, domain `education`), all `proposed`.
- No execution binding, endpoint, service, GitHub access, memory access or model alias. The team page gains a coming-soon section with their portrait; the chat sidebar and every execution path are unchanged. Neither agent can be reached by orchestration or chat.

## What is not implemented

Pull request ingestion, learning artifacts, institutional memory, curriculum generation, learner records, lessons, assessments, `learning.*`/`curriculum.*`/`lesson.*` events and repository credentials. These are phases 2 to 6 of the specification and each needs its own reviewed change.

## Findings that shape later phases

- The two onboarding repositories currently hold only a Git-fundamentals lesson plan, a rubric and playbook, an operations note, a README, a security acknowledgment and one exercise. There is no knowledge base or curriculum structure yet, so the eight proposed tracks must be reconciled with that content before Ada writes anything.
- There is no durable agent memory. `memory.py` and the memory ADR are a plan, not a retrieval system, so Phase 3 must introduce interfaces before Archie can read anything.
- There is no GitHub ingestion path except Sage's review adapter. Webhooks and idempotent event records are new work and must respect the no-broker, JSON-REST rules in `AGENTS.md`.
- Architecture v1.1 says the roster is eight identities; adopting Archie and Ada is a governance amendment the founders must approve.
- The team page has a "coming soon" section with their portrait. It says Archie will build curriculum in near-real time and Ada will train learners on it from a separate site as a human-like avatar. Both are plans, not features. The chat sidebar and the crew counts are unchanged because neither agent is usable.
