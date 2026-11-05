# Development status

An evidence-based snapshot. Maturity levels are kept separate, because each says something different:

**Implemented** (code exists) → **Tested** (automated tests cover it) → **Integrated** (wired into the running application) → **Deployed** (ran in a deployed environment) → **Operationally validated** (observed working against real inference).

| Item | Value |
|---|---|
| Audited revision | `f6fa545d84b2e005d5d48f7935a77bfa5a2e5ac3` on `main` (merge of PR #14) |
| Audit date (UTC) | 2026-10-28 |
| Working tree | clean (one unrelated untracked file) |
| Details and evidence | [Phase 6.5 architecture audit](audits/phase-6.5-architecture-audit.md) |

## Phases

| Phase | PR | What it added | Maturity reached |
|---|---|---|---|
| 1. Typed pipeline state | #1 | validated agent contracts, `PipelineState` | integrated; tested |
| 2. Agent identities | #2 | eight identities, capabilities | integrated (declarative); tested |
| 3. Identity execution | #3 | bindings, identity-attributed pipeline | integrated behind a flag; tested |
| 4. Personality-aware inference | #4 | identity prompts on services | implemented, tested; **flag set nowhere** |
| 5. Controlled delegation | #5 | Charlie picks a workflow | integrated behind a flag; tested |
| 6. Iterative orchestration | #6 | stateful loop, policy, planner | integrated behind flags; tested |
| 6.1 Hardening | #7 | deadline, freshness, completion kinds | tested |
| 6.2 UI integration | #8 | `orchestration` object and panel | tested (Node fake DOM) |
| 6.3 Local Dev validation | #9 | web receives flags, planner alias, opt-in key | integrated; locally exercised |
| Meet the team | #10 | team page and portraits | tested |
| 6.4 Live observability | #11 | `/api/chat/stream`, 14 events, live UI | tested; one Stage stream check (deleted) |
| Follow-ups | #12, #13, #14 | full-team prompts, chat panel, Gemini for Scout's agents, model labels, Fact Check citations and eval | tested; Gemini agents answered live calls |
| 6.5 Audit and baseline | this branch | documentation reconciliation only | n/a |

## Maturity by capability

| Capability | Implemented | Tested | Integrated | Deployed | Operationally validated |
|---|---|---|---|---|---|
| Fixed pipeline | yes | yes | yes | no evidence | not evidenced here |
| Identity, delegation, personality modes | yes | yes | yes (flags off by default) | no | no |
| Iterative orchestration | yes | yes | yes (flags off by default) | one Stage run on 2026-10-27, since deleted | one live request through Research and Security |
| Live streaming (`/api/chat/stream`) | yes | yes, including a real loopback server | yes | one Stage stream, since deleted | that one request, events arrived incrementally |
| Chat-style live UI and model labels | yes | yes (Node fake DOM only) | yes | no | **no real-browser check recorded** |
| Fact Check on Gemini | yes | yes (mocked) | yes | no | direct calls and an evaluation run on 2026-10-27 |
| Fact Check citation guard | yes | yes (unit and service tests) | yes | no | the downgrade path was never triggered by a live model |
| Privacy Review, Synthesis (live inference) | yes | yes (mocked) | yes | no | no evidence in this audit |
| AgentCore compatibility | adapters | partly | no | no | no |
| Stage deployment | template | shipped-config tests | n/a | deleted | n/a |

## Feature flags

All default to `false`. None is set in the committed `compose.yaml` defaults or `deploy/digitalocean/app.yaml`.

| Flag | Effect | Needs |
|---|---|---|
| *(no flag)* the live team conversation | iterative loop, the live stream and Pixel in the conversation | a planner alias (`gpt-6-luna` by default) and `WEB_LITELLM_API_KEY` for the web container |
| `VIHARBOT_PPT_GENERATION_ENABLED` | slide-deck generation by the pixel service (true in local Compose while testing) | `PIXEL_MODEL`, `LITELLM_API_KEY` on the pixel service |
| `VIHARBOT_PERSONALITY_INFERENCE_ENABLED` | identity prompts on services | identity routing |
| `VIHARBOT_DEV_DIAGNOSTICS_ENABLED` | planner rationale and model aliases in the stream | iterative mode |

A developer's `.env` can turn these on and changes test outcomes (see below).

## Test results

Run on 2026-10-28 against the audited revision plus the documentation changes of this branch. No billable call was made.

| Environment and command | Executed | Passed | Failed | Skipped | Notes |
|---|---|---|---|---|---|
| Host, `python -m pytest -q`, `.env` moved aside | 1198 | 1195 | 0 | 3 | the three skips are the opt-in live checks (`RUN_LITELLM_INTEGRATION`, `RUN_LITELLM_INFERENCE`) |
| Host, same, with the developer's `.env` present | 1198 | 1180 | 15 | 3 | `.env` turns the orchestration flags on; python-dotenv loads it on import |
| Docker, `docker compose --profile tests run --build --rm tests` | 0 | 0 | 1 collection error | 0 | `tests/test_dev_configuration.py` cannot find `/app/compose.yaml` |
| Docker, same with `--ignore=tests/test_dev_configuration.py` | 1190 | 1112 | 7 | 71 | 3 tests need `.env.example`, 3 need `images/`, 1 UI test lacks its Node guard (D2); 51 skips are Node-dependent UI tests and the 3 opt-in checks, plus 20 documentation checks that skip because `docs/` is not in the image |

By area on the host (clean environment): streaming events, emitter and SSE transport 289 passed; frontend and team status 72 passed; Fact Check guard and eval scoring 27 passed; orchestration policy, state, freshness, deadline 315 passed; identity, delegation, personality, configuration and API contract 220 passed with 3 skipped. The frontend tests run the page's own script against a fake DOM in Node 24; they do not use a real browser.

Why the Docker profile differed on 2026-10-28: `Dockerfile.tests` copied only `pyproject.toml`, `README.md`, `src`, `tests` and `scripts`, and had no Node. Issues #19 and #20 change this: the image now carries Node 24, `git` and the repository files the suite reads, and the Docker profile runs the whole offline suite with no network. The table above is the dated record and is not rewritten; the fresh Docker and host results for the fix are in its pull request, and the fix is not in a release until that pull request is merged. The host suite remains the reference.

## Verified and unverified

**Verified by code or tests:** every modeled behavior in the audit (dependencies, freshness, budgets, deadline, event schema and ordering, SSE framing, terminal rules, citation verification, sanitized rendering, shipped config keeping modes off); the sidebar status labels against the registry and bindings.

**Verified by observation:** the seven Compose services healthy locally; one Stage deployment and stream on 2026-10-27 (now deleted); direct Gemini calls and a 62-call evaluation on 2026-10-27.

**Not verified:** the UI in a real browser (layout, mobile), a live end-to-end run of all six agents, the guard's downgrade path against a live model, AgentCore end to end, any AWS resource, token and cost metrics, behavior under many concurrent users.

## Fact Check evaluation

Assets: `scripts/fact_check_eval/cases.json` (31 cases, 37 claims) and `run.py` (needs `--confirm-billable`, LiteLLM settings, and a container for the gateway CA). No results are committed. An uncommitted run on 2026-10-27 gave 97% exact and 100% acceptable for `gemini/gemini-3.5-flash` and 86% and 95% for `openai/gpt-5.4-mini`, with zero false support and 100% injection pass for both. That is one run on a self-written set with debatable labels; it is indicative only and was not repeated for this audit.

## Known limitations and defects

* Agents have no authentication; the web uses one shared Basic credential.
* Agent memory is written and never read, and is ephemeral on App Platform.
* Execution is sequential; cancellation is cooperative and does not stop in-flight model or agent calls.
* The live stream cannot send a terminal event after a lost connection, and would also end without one if the emitter hit an internal contract error (untested path).
* The failure code `cancelled` is declared, reserved and never emitted; the emitter refuses it (issue #22, pull request pending merge).
* Aliases in `agent_activity` of the final response are present even when diagnostics are off.
* **D1** the Docker test profile does not run the suite (fixed in #192, merged). **D2** `test_the_fact_check_section_shows_the_cited_quote_and_whether_it_was_found` lacks `@needs_node` (fixed in #192, merged). **D3** a local `.env` changes test results (fixed by the pull request for #21, pending merge). Details in the audit.
* Model availability is account-specific: on this account Gemini 2.5 aliases return 404, 3.7 and 3.8 flash returned 503, and the Pro models returned 429 on 2026-10-27.
* Streaming is not bridged through AgentCore.

## Deployment status

| Environment | State |
|---|---|
| Local Dev | running and healthy; the reference environment |
| Stage (DigitalOcean) | **no app exists**; deleted after the 2026-10-27 checks; the committed template keeps live mode off |
| AWS | nothing deployed |

A push to `main` deploys nothing. To deploy Stage, follow the promotion gate in [deployment strategy](deployment-strategy.md).

## Recommended next milestone

**Phase 6.6: test and baseline stabilization** (details in the audit): fix D1 to D3, guarantee a terminal event on emitter errors, then run a committed, repeatable Fact Check evaluation and one live end-to-end run with explicit approval. Authenticated agent-to-agent calls come before any multi-user or distributed deployment. No Phase 7 feature work is recommended until the baseline tests are trustworthy.
