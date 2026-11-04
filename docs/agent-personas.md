# Agent personas

**Status: Phase A of Epic #157. Draft, declarative only.** The eight persona files describe how each established agent works and sounds. Nothing in the application reads them yet: they change no prompt, binding, permission, endpoint, event or model call. Design and phasing: [persona and community specification](persona-community-specification.md).

## Identity, persona, authorization

| Concept | Answers | Where it lives | Grants access? |
|---|---|---|---|
| Identity | Who is the agent? | `src/viharbot/agent_identities/*.toml` | No |
| Persona | How does it work and sound? | `src/viharbot/personas/definitions/*.toml` | **No** |
| Skill | What professional work could it do? | `src/viharbot/skills/definitions/*.toml` | No |
| Execution binding | Which service runs a capability for an identity? | `src/viharbot/execution_config/bindings.toml` | Only for approved services |

The persona references an identity by `agent_id` and never repeats its name, title or role. If the two disagree, the identity file is authoritative.

## The eight personas

| Agent | Voice in one line | Preferred peers | Limits stated in the file |
|---|---|---|---|
| Charlie | Composed and diplomatic; recommendation with conditions | consults Nova, Sage; defers to Cipher | Advisory; the application authorizes and executes |
| Nova | Curious and measured; claim, evidence, unverified assumptions | requests review from Scout | Does not mark its own work validated |
| Atlas | Warm, direct, cost-and-fallback first | reviews Orbit's feasibility | Cannot book or buy; capability not yet connected |
| Pixel | Vivid and message-first; accuracy over aesthetics | consults Scout; asks Sage to review framing | Application validates and renders; no image generation |
| Sage | Calm and unhurried; human impact with a constructive alternative | reviews Orbit's proposals | Advisory only; cannot approve or block |
| Cipher | Precise and restrained; risk, why, mitigation | challenges Orbit's architecture | Cannot scan or verify live systems |
| Scout | Matter-of-fact; the finding and the exact contradicting passage | reviews Nova's evidence | Validates only against supplied material |
| Orbit | Clear and practical; proposal, interfaces, failure handling | requests review from Cipher and Atlas | No infrastructure or execution privileges |

All eight are `status = "draft"` at version `0.1.0`. A persona is moved to `review` and then `approved` (with an `approval_ref`) only by a reviewed pull request and founder decision. `implemented` and `activated` are not persona statuses: whether an agent runs is decided by bindings and founder activation.

## Schema summary

Each file has `agent_id`, `biography`, `values`, `principles`, `strengths`, `limitations`, and the tables `[provenance]`, `[traits]`, `[communication]`, `[reasoning]`, `[disagreement]`, `[self_assessment]`, `[model_preference]`, `[visual_identity]`, plus one or more `[[collaboration]]` entries. Field meanings are in section 3 of the specification.

Rules enforced at load time:

* Unknown fields are rejected, so a persona cannot carry tools, permissions, scopes, capabilities, endpoints, runtime placement, bindings, model aliases, credentials, trust scores or seniority.
* `traits.identity_traits` must appear in the identity's approved `personality` text.
* `model_preference.family` must equal the identity's `preferred_model_family`. There is no alias field.
* `collaboration` must name existing, different agents. A pattern describes a working preference, not trust or access.
* `disagreement.requires_evidence` can only be `true`. This is a configuration invariant, not proof that evidence is present or true; evidence validation is a Phase C control.
* `visual_identity.avatar_file` is a bare image file name, not a path or URL. The loader checks syntax only. A CI test checks that the bundled avatars exist in `images/` and in the web allowlist; loader-level checks are deferred to Phase E.
* `approval_ref` is metadata. It does not verify founder approval; pull request review does.
* All eight established agents must have a persona.
* Errors name the file and field and never echo rejected values.

## Communication examples are illustrations

`communication.illustrative_examples` shows voice for reviewers and for future evaluation. They are not response templates. Persona voice may shape free-text explanations only. It must never shape structured outputs, tool contracts or machine-readable agent-to-agent messages.

## Validate

```bash
python -c "from viharbot.personas import default_persona_registry as d; print(len(d()), 'personas valid')"
python -m pytest tests/test_persona_registry.py
```

Loading is deterministic (ordered by agent id) and makes no network, LiteLLM, AWS, Docker or Strands calls. Each persona has a `content_digest()` (SHA-256 of its canonical content) for provenance and change detection.

## Change a persona

1. Edit `src/viharbot/personas/definitions/<id>.toml` and bump `persona_version`.
2. Keep identity traits and model family consistent with the identity file; change identity first, through its own review, if they must differ.
3. Run the tests above. Do not add authority wording or runtime fields.
4. Open a pull request explaining why. A persona never edits itself.

## Not implemented

Persona-aware prompt composition, peer review, relationship records, collaboration memory, collaboration events and profile UI are proposals (phases B to E). Personas for `archie` and `ada` are not included.
