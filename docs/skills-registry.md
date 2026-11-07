# Skills registry

The skills registry is a declarative, versioned catalogue of the professional work each of the agent identities could do. It implements [Agent Architecture v1.1](governance/agent-architecture-v1.1.md) section 4 and [Skills Registry v1.0](governance/agent-skills-registry-v1.md). **It is metadata only.** Nothing in the application reads it, no skill is executable, and it adds no service, model call or REST/SSE contract.

## Identity, skill, capability, binding

| Concept | Answers | Where it lives | Grants access? |
|---|---|---|---|
| Identity | Who is the agent? | `src/viharbot/agent_identities/*.toml` | No |
| Skill | What professional work could it do? | `src/viharbot/skills/definitions/*.toml` | No |
| Capability | What is it suited to do, and does a service exist? | `Capability` and `CAPABILITY_STATUS` in `identity.py` | No |
| Execution binding | Which operational service runs a capability for an identity? | `src/viharbot/execution_config/bindings.toml` | Only for the six existing services |

A skill may name an existing capability in its optional `capability` field. That link is informational. A skill is executable only if its lifecycle status is `enabled` **and** an approved binding exists; today no skill is enabled and the bindings are unchanged.

## What exists today

All 78 skills from Registry v1.0 are declared with status `proposed`: an id, version, owner, optional eligible agents, category and expected deliverable. Inputs, tools, scopes, data classifications, dependencies and evaluation criteria are empty because the approved registry does not define them yet. Eight skills have eligible agents (from the registry's shared-skills table), and seven link to an existing capability: `consulting.synthesis`, `research.technical`, `verification.fact_check`, `verification.source_quality`, `security.risk_assessment`, `security.privacy` and `strategy.logistics`.

Operational today: Nova (research), Scout (fact check, source quality), Cipher (security assessment, privacy review) and Charlie (synthesis, opt-in planning). Planned, with no connected service: Atlas, Sage, Pixel and Orbit. The registry does not change any of this.

## Lifecycle

`proposed` → `defined` → `implemented` → `tested` → `enabled` → `deprecated`. Validation enforces what each state needs:

* `defined` and later need `inputs`, `outputs` and `evaluation`.
* `implemented`, `tested` and `enabled` need a `capability` the owner declares and that is implemented, so a skill cannot outrun the code behind it.
* A skill past `proposed` cannot depend on a skill that is only `proposed`, and only a deprecated skill may depend on a deprecated one.

## Add a skill

1. Add a `[[skill]]` table to `definitions/<owner>.toml`. The file name must be the owner's agent id.
2. Use a unique `skill_id` of the form `domain.name`; `category` must equal the domain. Start at version `0.1.0` and status `proposed`.
3. Name additional agents in `eligible_agents` (never the owner). Express dependencies as `skill:<id>` or `capability:<name>`.
4. Unknown fields are rejected. Never add tools, scopes or permissions to make a skill work; those are granted elsewhere.
5. Update the registry document if the change is material, and run the validation below.

## Validate

```bash
python -c "from viharbot.skills import load_skill_registry as l; print(len(l()), 'skills valid')"
python -m pytest tests/test_skills_registry.py
```

Errors name the file, entry and field, and never echo rejected values. Loading is deterministic (skills are ordered by id) and makes no network, LiteLLM, AWS or Docker calls.

## How a skill will become executable (proposed, not built)

Per Registry v1.0 section 13, a skill may become executable only after its implementation, permissions, tests and execution binding are approved, each as a reviewed change. Concretely, a future phase would: define the contract (`defined`); connect it to an implemented capability and an approved binding; add automated evaluations (`tested`); and then change `status` to `enabled` in a pull request. Removing or materially changing an existing skill needs a compatibility review. None of this exists yet; the required next phase is a skill-to-capability resolver and a binding extension that consult `executable()`.
