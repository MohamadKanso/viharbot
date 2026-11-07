# Phase 6.5 architecture audit

> **Historical (audited 2026-10-28 at `f6fa545`).** Several findings have since changed, notably agent authentication (now implemented and opt-in on four services) and the orchestration flags (removed). See the reconciliation note in [development status](../development-status.md) and the current [architecture](../architecture.md).

An evidence-based review of ViharBot as it exists on `main`. Code is treated as evidence of implementation, tests as evidence of tested behavior, and deployment records as evidence of deployed behavior. These are different things and are kept separate throughout. References use `path:line` against the audited revision.

## 1. Baseline

| Item | Value |
|---|---|
| Repository | `vchokshi/viharbot` (`git@github.com:vchokshi/viharbot.git`) |
| Audit date (UTC) | 2026-10-28 |
| Audited revision | `f6fa545d84b2e005d5d48f7935a77bfa5a2e5ac3` (merge of PR #14) |
| Local `HEAD` before the audit branch | `f6fa545`, equal to `origin/main` after `git fetch --prune` |
| Working tree | clean except one untracked, unrelated file (`images/Meet the Viharbot team.rtf`), left untouched |
| Audit branch | `docs/phase-6.5-architecture-baseline` (from `main`; nothing merged or force-pushed) |
| Package version | `viharbot` 2.3.0 (`pyproject.toml`); no tags |
| Other checkouts | A second worktree exists at `/private/tmp/viharbot-web-ui` on `codex/web-ui` (`d26198d`); it was not audited |
| Open PRs | none; PRs #1 to #14 are all merged |

Environment facts used below: Python 3.13 venv, Node 24.22, Docker running, `doctl` authenticated, `gh` authenticated.

## 2. Pull requests #6 to #14

Merge state and sizes come from `gh pr view`. "Deployment" means evidence that the change ran in a deployed environment; none of these PRs deployed anything by itself.

| PR | Title | Status | Merge commit | Size | Functionality introduced | Tests added or changed | Documentation |
|---|---|---|---|---|---|---|---|
| #6 | Phase 6: stateful, iterative orchestration | merged 2026-10-27 | `0069556` | 16 files, +2353/-31 | `orchestration_state/policy/planner.py`, `iterative_execution.py`; stateless planner; policy authorization | `test_orchestration_state`, `_policy`, `test_iterative_execution` | `iterative-orchestration.md` added, `architecture.md` touched |
| #7 | Phase 6.1: hardening | merged | `87a2932` | 17 files, +2142/-198 | absolute `RequestDeadline`, freshness, planner `max_retries=0`, honest completion kinds | deadline, freshness, completion tests | `iterative-orchestration.md` |
| #8 | Phase 6.2: UI integration | merged | `8e0e56b` | 14 files, +1042/-16 | optional `orchestration` object; coordination panel; Node fake-DOM harness | `test_ui_orchestration`, `test_orchestration_view` | `iterative-orchestration.md` |
| #9 | Phase 6.3: Local Dev validation | merged | `6879f1e` | 9 files, +153/-21 | web service receives flags, planner alias and opt-in key; Cloudflare CA in web image | `test_dev_configuration`, shipped-config guardrails | `iterative-orchestration.md` |
| #10 | Finish "Meet the team" page | merged | `5c5dddc` | 14 files, +562/-110 | team page, portraits, corrected role chips | `test_team_page` | none |
| #11 | Phase 6.4: live observability | merged | `9599b51` | 26 files, +6585/-112 | `POST /api/chat/stream`, 14 events, live UI | 289 stream/contract tests, fixtures, UI tests | `live-observability.md`, `iterative-orchestration.md` |
| #12 | Full-team prompts, chat-style panel | merged | `c646c42` | 3 files, +91/-31 | four suggested prompts; chat layout | `test_ui_live`, `test_team_status_consistency` | none |
| #13 | Gemini for Fact Check/Source Quality, model labels | merged 2026-10-28 | `7f6c587` | 6 files, +36/-12 | aliases to `gemini/gemini-3.5-flash`; speaker model label | UI label tests | `README.md` only |
| #14 | Fact Check citations, question, eval set | merged | `f6fa545` | 14 files, +1336/-16 | `fact_check_guard.py`, question in payload, eval runner | guard and eval tests | none |

### Validation and deployment evidence

* **Code vs description.** PR descriptions for #6 to #11 were compared with the diffs and the current code. They match: the `orchestration` object is additive and free of free text (#8), the web service gets the key only through `WEB_LITELLM_API_KEY` (#9, `compose.yaml:28-31`), and the 14 events and terminal rules in #11 are what `live_events.py` and `live_stream.py` implement. No description claimed behavior the code lacks.
* **Honest gaps in descriptions.** #11 and #14 state what was not verified (real-browser screenshots, mutation checks for the UI, the guard's live downgrade path). Those gaps remain open.
* **Deployment.** Only #11 to #14 have any deployment evidence, and it is thin: on 2026-10-27 a DigitalOcean app was created from a locally modified copy of the spec, redeployed from `c646c42` (all 7 components running, `/health` 200), and one authenticated stream delivered events incrementally through the ingress. That app has since been deleted (`doctl apps list` returns no apps on 2026-10-28). Nothing from #13 and #14 was deployed to Stage. There is no AWS deployment.
* **Remaining gaps by PR.** #10, #12, #14 updated no documentation (corrected in this phase). #13 updated the README only. #14 added `evidence_quote` and `citation_verified` to the public `ClaimReview`; the change is additive, but no OpenAPI diff is recorded.

## 3. Runtime components

Logical identities and physical services are distinct (`identity_registry.py`, `execution_config/bindings.toml`).

| Component | Identity | Capability | Runtime | Entry point | Framework | Alias source | Inference | Memory | API | Auth | Status |
|---|---|---|---|---|---|---|---|---|---|---|---|
| web | Charlie (planner), UI | planning, coordination | Compose / App Platform | `web_app.py` | FastAPI, uvicorn | `CHARLIE_PLANNER_MODEL` | `build_agent()` for the planner only | none | `/`, `/images/*`, `/api/chat`, `/api/chat/stream`, `/health`, `/ping`, `/invocations` | Basic, shared | implemented; planner optional |
| research | Nova | research | Compose / template | `research_agent.py` | FastAPI, Strands | `RESEARCH_MODEL` | `build_agent()` | JSONL | `/research`, `/health` | none | implemented |
| security | Cipher | security assessment | same | `security_agent.py` | FastAPI, Strands | `SECURITY_MODEL` | `build_agent()` | JSONL | `/assess`, `/health` | none | implemented |
| fact-check | Scout | fact check | same | `specialist_agent.py` (`AGENT_KIND`) | FastAPI, Strands | `FACT_CHECK_MODEL` | `build_agent()` | JSONL | `/process`, `/health` | none | implemented |
| source-quality | Scout | source quality | same | same | same | `SOURCE_QUALITY_MODEL` | `build_agent()` | JSONL | same | none | implemented |
| privacy-review | Cipher | privacy review | same | same | same | `PRIVACY_MODEL` | `build_agent()` | JSONL | same | none | implemented |
| synthesis | Charlie | synthesis | same | same | same | `SYNTHESIS_MODEL` | `build_agent()` | JSONL | same | none | implemented |
| orchestrator | n/a | CLI demo | Compose `demo` profile | `Dockerfile.orchestrator` | Python | n/a | via agents | none | none | n/a | optional |
| Atlas, Sage | n/a | none connected | none | none | none | none | none | none | none | none | planned |
| Pixel, Orbit | n/a | none connected | none | none | none | none | none | none | none | none | planned (support roles) |

Compose has seven always-on services plus the `orchestrator` (`demo`) and `tests` profiles (`compose.yaml`). Only `web` publishes a port, `127.0.0.1:8080:8080`. Every service has a health check except `orchestrator` and `tests`.

### Identity registry and bindings

Eight identities exist. Four are bound to services: Nova (research), Scout (fact check, source quality), Cipher (security assessment, privacy review), Charlie (synthesis). Charlie also *declares* `research` (`charlie.toml`), but the binding resolves research to Nova, so declaring a capability does not run it. `capability_status` marks only operational capabilities `implemented` (`identity.py:37-64`); an identity file is not evidence of an operational agent. `authorize_action` re-checks the binding, the registry membership and the service availability on every action (`orchestration_policy.py:133-184`). Identity `model_alias` is unset for all eight identities; runtime aliases come from service environment variables. The `preferred_model_family` values (Charlie, Cipher: GPT-6 family; Nova, Scout: Gemini Pro family) are advisory and differ from the runtime choice. UI status labels were checked: the sidebar, header and footer say four live agents, and `tests/test_team_status_consistency.py` derives that from the registry and bindings.

## 4. Orchestration

### 4.1 Modes (`web_app.py:192-208`)

| Mode | Entry | Flags | Planner | Order | Completion | Response | UI | Tests |
|---|---|---|---|---|---|---|---|---|
| Fixed pipeline | `demo.run_pipeline` (`demo.py:147`) | none (default) | none | Research, Fact Check, Source Quality, Security, Privacy, Synthesis | all run | `ChatResponse` | ordinary card | `test_phase1`, `test_pipeline_state` |
| Identity-attributed | `run_identity_pipeline` (`identity_execution.py:87`) | `VIHARBOT_IDENTITY_ROUTING_ENABLED` | none | fixed workflow `full_review` | all run | unchanged | unchanged | `test_identity_execution` |
| Controlled delegation | `run_delegated_pipeline` (`delegated_execution.py:68`) | + `VIHARBOT_DELEGATION_ENABLED` | one call picks one of two workflows | per workflow | workflow completes | unchanged | unchanged | `test_delegated_execution` |
| Iterative | `run_iterative_pipeline` (`iterative_execution.py:164`) | + `VIHARBOT_ITERATIVE_ORCHESTRATION_ENABLED`, and `CHARLIE_PLANNER_MODEL` plus a key to be usable | one stateless call per decision | planner-chosen, policy-checked | Charlie approves or policy budget | adds optional `orchestration` | coordination panel | many |
| Streaming iterative | `chat_stream` (`web_app.py:265`) | iterative flags | same loop with events | same | `final_response` then `orchestration_completed` | SSE | live chat | `test_live_*`, `test_ui_live` |

`iterative_orchestration_enabled()` requires its own flag and `delegation_enabled()` (`iterative_execution.py:65-68`), and delegation requires identity routing. If any are off a lower mode runs. Every optional flag defaults to `false` and none is set in `compose.yaml` defaults or `deploy/digitalocean/app.yaml`. `VIHARBOT_PERSONALITY_INFERENCE_ENABLED` (Phase 4, `inference_config.py:23`) is also set nowhere.

### 4.2 Charlie's planner

* **How planning starts.** Per decision, `decide_next_action` (`orchestration_planner.py:132`) builds a bounded digest and makes one call. The docstring and code confirm each call is *stateless*: no conversation state persists between decisions (`orchestration_planner.py:3`); continuity comes only from the digest of the last 8 decisions (`DIGEST_HISTORY_LIMIT`, line 31).
* **Model and credentials.** Alias from `CHARLIE_PLANNER_MODEL` (`delegation_planner.py:28`), never inferred from another alias. Credentials via `litellm_credentials()` from `LITELLM_BASE_URL`/`LITELLM_API_KEY`; in Compose the web container's `LITELLM_API_KEY` is `${WEB_LITELLM_API_KEY:-}` (`compose.yaml:31`), empty unless opted in. With no alias or key a request returns the sanitized 502.
* **What it can propose.** A strict `OrchestrationAction`: `delegate` with one of six capabilities, or `finish`, plus a rationale (`orchestration_policy.py:92-114`). There is no field for services, URLs, aliases, tools or credentials. The system prompt tells the model that everything it sees is untrusted data (`orchestration_planner.py:33`).
* **Validation and rejection.** `authorize_action` raises `ActionRejected` with one of 16 fixed codes; the loop records and counts it, and stops after 2 refused actions (`iterative_execution.py:354`, `MAX_REJECTED_ACTIONS` at `orchestration_state.py:53`).
* **Failures.** A planner failure raises a typed error and fails the request (sanitized 502 or `orchestration_failed`). A deadline during planning completes by policy only if the required results are fresh (`iterative_execution.py:276-281`).

### 4.3 Dependencies (`orchestration_policy.py:36-54`)

Required: Research needs nothing; Fact Check, Source Quality and Security need Research; Privacy Review and Synthesis need Research and Security. Optional (used only while fresh): Privacy Review uses Fact Check and Source Quality; Synthesis uses Fact Check, Source Quality and Privacy Review. These match the expected rules. `authorize_action` rejects with `dependency_unmet` when a required result is missing and `dependency_stale` when it is not fresh (lines 168-173), so an agent cannot start without fresh required inputs. `check_completion` requires Research, a Security result, and Security based on the latest Research (lines 187-198).

### 4.4 Evidence freshness

Each result is a versioned `StepResult` that records the input versions it consumed (`orchestration_state.py:95`). `step_is_fresh` (`orchestration_policy.py:57-74`) treats a result as stale when a required input has a newer version, or when a fresh optional input now exists at a different version than the one consumed. `fresh_results` builds the final response from fresh results only, in pipeline order; stale ones are excluded and marked `in_final_result: false`. Repeats are allowed up to `max_repeats_per_capability` (default 2, ceiling 3); a repeat makes dependents stale and re-opens them, and `evidence_superseded` is emitted (`iterative_execution.py:425-460`). The planner digest marks stale results to Charlie (`orchestration_planner.py:123`). No case was found where a stale result reaches the final output. One limit: a stale result is still *visible to the planner* as history, flagged as stale, by design.

### 4.5 Completion, deadline and cancellation

Defaults and ceilings (`orchestration_state.py:56,69-92`): 8 decisions (max 16), 6 executions (12), 2 repeats (3), 120 s (300 s), 2 refused actions. Environment variables `VIHARBOT_ORCH_MAX_*` override within the ceilings; NaN and out-of-range values fall back to defaults. When the execution budget is spent Charlie gets one final evaluation in which only `finish` is accepted (`iterative_execution.py:252-257`). A limit completes the request by policy (`completion_kind=policy_budget`) only if the required results are fresh; otherwise it fails (`by_policy`, `iterative_execution.py:227`). This is never recorded as Charlie's decision.

The deadline is absolute and monotonic (`deadline.py:21`). Planner and REST timeouts are `min(configured, remaining)` (`demo.py:126`, `orchestration_planner.py:157-161`), and late results are discarded. Per-operation caps: planner 30 s default (1 to 120 s), REST 90 s. **Enforcement is cooperative.** A timeout does not cancel work already running in a model call or an agent; a browser disconnect sets `CancelToken`, which is checked before each decision and after planning (`iterative_execution.py:246,305`) so no new delegation starts, but an in-flight call is not interrupted. Remote inference and agent work may therefore continue after the browser has gone.

## 5. Phase 6.4 streaming and observability

### 5.1 Endpoint (`web_app.py:257-307`)

`POST /api/chat/stream` exists on `main`. It is enabled only when iterative orchestration is on (otherwise `409`), uses SSE, enforces the same `require_web_access` as `/api/chat` (`web_app.py:268`), returns `422` for an invalid body before streaming, caps concurrency at 8 (`429`, `live_stream.py:45`), and reports later failures as events. Every event is validated by a strict Pydantic discriminated union (`live_events.py`, `extra="forbid"`). The canonical contract is [live-observability.md](../live-observability.md), generated from the code, with all 14 events and validated JSON examples.

### 5.2 SSE framing

`format_sse` (`live_stream.py:282`) emits `id: <event_id>`, `event: <event_type>`, a single-line JSON `data:` and a blank line, encoded as UTF-8. Keep-alive comments (`: keep-alive`) go out after 15 s of silence (`live_stream.py:365`). There is no `retry:` directive and no `Last-Event-ID` resume. Event names are snake_case (`planner_started`), not dotted.

### 5.3 Event registry

Exactly 14 events (`live_events.py:342`) share one envelope (`schema_version`, `event_id`, `request_id`, `sequence`, `timestamp`, `source`, `correlation`, `event_type`, `data`). Required and optional fields per event, producers, and terminal flags are in the registry table of the streaming reference; `tests/test_docs_consistency.py` fails if that table or any of its 14 examples drifts from the code. Terminal events are `orchestration_completed` and `orchestration_failed` only.

### 5.4 Lifecycle and terminal guarantees

`drive()` (`live_stream.py:265`) turns every outcome into `final_response` plus `orchestration_completed`, or one `orchestration_failed` with a sanitized code. Sequences for success, agent failure, planner failure, rejection, deadline expiry, budget exhaustion and disconnect are tabulated in the streaming reference; the first three are fixtures (A, E, D, B, C). Findings:

* Events cannot arrive out of order from the server: sequence and parent pointers come from one per-request emitter, and `check_stream` verifies the invariants. The page re-checks the sequence and rejects violations (`index.html:816`).
* **Finding F1 (reliability, low).** If `emitter.completed()` raised an `EventContractError`, `drive()` would try `emitter.failed()`, which also raises (a run with a `final_response` may not fail). The worker then records `contract_error` and the stream closes through the queue sentinel with **no terminal event** (`live_stream.py:327-334`). No test covers it, and the page treats it as unverified. It needs an emitter bug to occur.
* **Finding F2 (documentation, low).** The failure code `cancelled` is defined and has a message (`live_events.py:41,52`) but is never emitted: cancelled runs deliberately send no terminal event.
* A terminal event cannot be guaranteed after the connection is lost; this is stated, not hidden.

### 5.5 Frontend consumption (`static/index.html`)

The page consumes real server events: `createFrameParser` (780), `checkEvent` (816), `applyEvent` (842), `runLive` (1335). It validates schema version, event type, request ID, strict sequence, terminal ordering and the single-final-response rule; failures show "Completion could not be verified". Rows are keyed by event and nothing is simulated. Chat lines are **templated** from recorded events by the page (they are not model output) and the panel says so. A speaker's model is shown only when the event carries `model_alias`, which requires `VIHARBOT_DEV_DIAGNOSTICS_ENABLED`; otherwise no model is claimed. The page falls back to `/api/chat` on `409` (`runOrdinary`, 1372). Text is inserted with `textContent` only and tests assert the script contains no `innerHTML`; the Markdown renderer emits no links or raw HTML. Streaming does not change `/api/chat`: its OpenAPI path is unchanged apart from the two optional `ClaimReview` fields added by #14.

## 6. LiteLLM integration

**Inference paths.** A search of `src/` finds exactly one construction of a model: `build_agent()` in `model_agent.py:9-30`, a Strands `LiteLLMModel` with `use_litellm_proxy=True`, used by the planner and all six agents. No `openai`, `anthropic`, `boto3` or Google SDK is imported. The remaining HTTP calls are web-to-agent JSON REST (`demo.py:127`, `security_client.py:61`), AgentCore invocation (`agentcore_client.py:49`, optional), and `litellm_client.check_connection` (`GET /v1/models`, non-inference). No agent bypasses LiteLLM.

### 6.1 Model configuration matrix

"Verified runtime model" is a value an agent or the planner actually reported or answered with; most rows can only be marked configured.

| Identity | Capability | Configured alias (default) | Source | Verified runtime model |
|---|---|---|---|---|
| Charlie | planning | none | `CHARLIE_PLANNER_MODEL` | `openai/gpt-5.4-mini` on a one-request Stage check (2026-10-27), since deleted |
| Nova | research | `openai/gpt-5.4-mini` | `RESEARCH_MODEL`, `compose.yaml:63`, `app.yaml:84` | reported by the service on that Stage request |
| Cipher | security assessment | `openai/gpt-5.4` | `SECURITY_MODEL`, `compose.yaml:85`, `app.yaml:110` | reported on the same request |
| Scout | fact check | `gemini/gemini-3.5-flash` | `FACT_CHECK_MODEL`, `compose.yaml:108`, `app.yaml:140` | answered a direct call and 31 evaluation cases on 2026-10-27 |
| Scout | source quality | `gemini/gemini-3.5-flash` | `SOURCE_QUALITY_MODEL`, `compose.yaml:131`, `app.yaml:170` | answered one direct call on 2026-10-27 |
| Cipher | privacy review | `openai/gpt-5.4-mini` | `PRIVACY_MODEL`, `compose.yaml:154`, `app.yaml:200` | no live evidence in this audit |
| Charlie | synthesis | `openai/gpt-5.4-mini` | `SYNTHESIS_MODEL`, `compose.yaml:177`, `app.yaml:230` | no live evidence in this audit |

Fact Check and Source Quality use Gemini (PR #13). The other agents use the documented OpenAI aliases. Environment overrides apply in Compose through `${VAR:-default}`. A configured alias does not prove a request succeeds: on this account `gemini-2.5-flash` returns 404, `gemini-3.7-flash` and `3.8-flash` returned 503 for demand, and both Pro models returned 429 on 2026-10-27.

### 6.2 Credential boundaries

* **Browser.** No key is ever sent to the browser; events and responses contain no credentials. The page shows aliases only when reported.
* **Secrets in source.** `git grep` for key, AWS and private-key patterns across tracked files found nothing. `.env` is gitignored.
* **Logs.** No logger `extra` carries a prompt, key, password, question or message (searched). Stream logs carry request ID, duration and terminal name only.
* **Web planner key.** Opt-in through `WEB_LITELLM_API_KEY` (`compose.yaml:28-31`). The prior docs said the web container never gets a key; that was outdated and is corrected. The App Platform spec carries no web key, so a Stage built from it cannot plan.
* **Provider keys.** None required. **TLS.** Every Dockerfile installs the Cloudflare gateway CA (`certs/`); a host Python process lacks it and cannot reach LiteLLM, which is why live scripts run in a container.

## 7. Fact Check and evidence verification (PR #14)

Present on the audited revision. `specialist_agent.py:57-75` sends the original question (relevance only, untrusted) and the findings; the prompt defines the four statuses and requires a word-for-word `evidence_quote` for every status except `insufficient_evidence`. The application, not the model, then verifies the quote: `guard_fact_check` (`fact_check_guard.py:50`) matches each review to its supplied claim (by text, else position), normalizes case, spacing and quote style, requires at least 12 characters, and checks the quote against that claim's own evidence. A supported, contradicted or mixed verdict without a confirmed quote is downgraded to `insufficient_evidence` with a note; skipped claims are added back as insufficient; the guard is applied before the result is serialized (`specialist_agent.py:186-187`). Instructions inside evidence cannot make a verdict stand (tested). The prompt, the verifier and the UI display are covered by 27 offline tests.

Evaluation assets: `scripts/fact_check_eval/cases.json` (31 cases, 37 claims: 8 supported, 11 contradicted, 7 mixed, 11 insufficient; three prompt-injection cases) and `run.py`, which requires `--confirm-billable`. **No results are committed.** A run on 2026-10-27, inside the `fact-check` container, on both models gave: `gemini/gemini-3.5-flash` 97% exact and 100% acceptable (36 and 37 of 37 claims), `openai/gpt-5.4-mini` 86% and 95%; zero false-support and 100% injection pass on both; 7.0 s versus 2.8 s per case. These numbers are one run on a set written by the same team, with debatable labels, and are not re-measured here (no billable calls were made in this audit); treat them as indicative. The guard's downgrade path has never been exercised by a live model (neither invented a quote), only by unit tests.

## 8. Deployment

### 8.1 Local Docker

Seven services start from `compose.yaml`; the web depends on the six agents; all use health checks; only `127.0.0.1:8080` is published; agents are reached by service name on `agent-network`. Memory: bind mounts for Research and Security, named volumes for the specialists. Verified in this audit: all seven containers `healthy` after three hours of uptime, rebuilt from this code during the session.

### 8.2 DigitalOcean

`deploy/digitalocean/app.yaml` defines seven components (public `web` on 8080 with `WEB_AUTH_ENABLED=true`; six private services on 8000), `deploy_on_push: false` on all, `/health` checks, memory under `/tmp` (ephemeral), no instance sizes set (defaults apply), aliases as in 6.1, and empty secret placeholders. It sets none of the three mode flags or `CHARLIE_PLANNER_MODEL`, so it deploys the fixed pipeline and `/api/chat/stream` answers `409`. Repository guardrail tests require the shipped configuration to keep optional modes off and commit no planner alias, so live streaming on Stage is a deliberate deployment-time change. **No Stage app exists now.**

### 8.3 AWS and AgentCore

Implemented compatibility: `/ping` and `/invocations` adapters on the web and agent apps (`agentcore_runtime.py:13`), an `agentcore://` client using `InvokeAgentRuntime` (`agentcore_client.py:49`), and ARN-first URL resolution (`web_app.py:45`). Not done: no runtime, ECR image, role or AWS resource exists; the adapter bridges `/api/chat` only, so streaming is unavailable there; the notes themselves say microVM storage is not durable memory and a separate browser-hosting bridge is needed. AgentCore is therefore *compatible in code*, not tested end to end and not operational.

### 8.4 Deployment matrix

| Environment | Runtime | Configuration | Mechanism | Verified status |
|---|---|---|---|---|
| Local Dev | Docker Compose | `compose.yaml`, `.env` | `docker compose up --build` | running and healthy on this machine; full suite passes on the host |
| Stage | DigitalOcean App Platform | `app.yaml` plus a local override | manual, approved `doctl` | created and verified 2026-10-27, **deleted**; nothing running |
| AWS | EC2 / AgentCore | compatibility only | none | not deployed |
| Future | other runtimes | planned | n/a | planned |

## 9. Documentation audit

Classification uses the requested labels. "Action" is what this phase did.

| Document | Classification before | Incorrect or missing | Action |
|---|---|---|---|
| `README.md` | Materially outdated | fixed pipeline only; "web is not a model-backed agent"; "web does not receive this key"; no streaming; stale test command | Rewritten |
| `docs/architecture.md` | Materially outdated | "no model-backed functionality" in web; identities "configuration only"; no streaming, planner or modes | Rewritten with four diagrams |
| `docs/live-observability.md` | Partially outdated | registry omitted required fields; few examples; no lifecycle or limits | Rewritten as the canonical protocol reference, generated from code |
| `docs/iterative-orchestration.md` | Partially outdated | UI section said "no streaming"; no link to events | Updated |
| `docs/deployment-strategy.md` | Partially outdated | no planner-credential exception; Stage state; AgentCore streaming gap | Updated |
| `docs/litellm-integration.md` | Partially outdated | only two aliases, "web container never gets key" | Updated |
| `docs/agent-communication.md` | Historical/partial | fixed-pipeline flow; "web does not receive keys"; Fact Check payload | Updated with notes |
| `docs/identity-and-security.md` | Partially outdated | auth route list; web key | Updated |
| `docs/observability.md` | Partially outdated | no stream events | Updated |
| `docs/memory-architecture.md` | Current but missing | did not say memory is never read back | Updated |
| `docs/agent-identity-architecture.md` | Historical (Phase 2) | "not connected to execution" | Banner added, body kept |
| `docs/identity-execution-integration.md` | Historical (Phase 3) | none | Banner added |
| `docs/personality-aware-inference.md` | Historical (Phase 4) | none | Banner added |
| `docs/controlled-delegation.md` | Historical (Phase 5) | none | Banner added |
| `docs/learning-roadmap.md` | Historical | predates everything after Phase 1 | Banner added |
| `deploy/agentcore/README.md` | Current | states limits accurately | Unchanged |
| `AGENTS.md` | Current | project rules | Unchanged |
| `src/viharbot/agent_identities/prompts/*.md` | Current | prompt files | Unchanged |
| `docs/development-status.md` | n/a | did not exist | Created |
| `docs/audits/phase-6.5-architecture-audit.md` | n/a | did not exist | Created (this file) |

The streaming reference was updated in place instead of creating `docs/streaming-events.md`, because a suitable document already existed and the spec asks to avoid duplicated schemas. Internal links across all tracked Markdown files are checked by `tests/test_docs_consistency.py`.

## 10. Validation results

Exact commands and counts are in [development-status.md](../development-status.md#test-results). In short: the host suite passes (1195 passed, 3 skipped) with `.env` moved aside; with a local `.env` present 15 tests fail; the documented Docker test profile fails to collect one module and fails 7 more tests because its image lacks files and Node. No billable call was made.

## 11. Security findings

| Id | Finding | Class |
|---|---|---|
| S1 | The six agent services have no authentication; they rely on the private network (`research_agent.py`, `security_agent.py`, `specialist_agent.py` have no auth dependency). Documented limitation | Security (known) |
| S2 | The web UI uses one shared Basic credential; no per-user identity, rate limiting or audit attribution. Applied to every web route except `/health` (`web_app.py`) | Security (known) |
| S3 | Opting in to the planner puts a LiteLLM key in the public-facing container; the docs now state the trade-off | Security (documented) |
| S4 | Aliases appear in the final `ChatResponse.agent_activity` regardless of `VIHARBOT_DEV_DIAGNOSTICS_ENABLED`, so the flag only gates the stream's aliases and rationale | Documentation |
| S5 | No secrets, prompts or keys were found in tracked files or log fields | Pass |
| S6 | Model-controlled text is rendered with `textContent`; tests forbid `innerHTML`; hostile Markdown and HTML tests pass | Pass |
| S7 | No destructive security testing was done against any deployed environment | n/a |

## 12. Gap analysis

| Area | Status | Class |
|---|---|---|
| Agent independence | Separate containers, aliases, memory paths. Not independent cloud identities or permissions | Architectural limitation |
| Distributed execution | `agentcore://` transport exists; untested; streaming not bridged | Architectural limitation |
| Agent communication | Structured, validated, observable by request ID; **unauthenticated** | Security issue (known) |
| Memory | Append-only, never read, ephemeral on Stage | Architectural limitation |
| Identity and authorization | Enforced in the application, not in the services | Architectural limitation |
| Observability | Request IDs, structured logs, live events; no traces, no token or cost metrics | Future enhancement |
| Scalability | Sequential execution, one stream thread per request, 8-stream cap, single process | Architectural limitation |
| Reliability | Typed failures and deadline; cancellation cooperative; F1 (no terminal on a contract bug) | Reliability issue |
| Untrusted output | Validated by contract; Fact Check citations verified by the application | Pass |
| Tests | Docker test profile broken; `.env` sensitivity; a UI test lacks its Node guard (below) | Reliability / documentation issue |

Defects found during the audit (documented, not fixed here, because this task is documentation-only):

1. **D1. The documented Docker test profile does not run the suite.** `Dockerfile.tests` copies only `pyproject.toml`, `README.md`, `src`, `tests` and `scripts`. `tests/test_dev_configuration.py` reads `/app/compose.yaml` and fails at collection; three shipped-config tests need `/app/.env.example`; three team-page tests need `/app/images/`. Node is also absent, so 48 UI tests skip. Pre-existing since PRs #9 and #10.
2. **D2. A UI test lacks its Node guard.** `test_the_fact_check_section_shows_the_cited_quote_and_whether_it_was_found` (`tests/test_ui_live.py:210`) lost its `@needs_node` decorator in the conflict resolution of merge `9fe12dd`, so it errors where Node is missing (it did in the Docker image).
3. **D3. A local `.env` changes test outcomes.** python-dotenv loads `.env` on import, flipping the orchestration flags, so 15 tests fail when a developer has the live settings in `.env`.
4. **D4. F1 and F2 above.**

## 13. Recommended next milestone

**Phase 6.6: test and baseline stabilization**, before any Phase 7 feature work. Rank: correctness, then security, then reliability.

1. *(correctness, small)* Fix D2 (one decorator) and D3 (load tests with a clean environment, for example a `conftest` that ignores `.env`).
2. *(correctness, small)* Fix D1 by copying `compose.yaml`, `.env.example`, `deploy/`, `images/` into the test image and installing Node, or by declaring the host suite authoritative and dropping the Docker profile from the docs.
3. *(reliability, small)* Make the stream guarantee a terminal event even on an emitter contract error (F1), and add a test; remove or use the `cancelled` code (F2).
4. *(validation, billable, needs approval)* A committed, repeatable Fact Check evaluation result and a live end-to-end run of all six agents; record real maturity evidence.
5. *(security, medium)* Authenticate agent-to-agent calls (shared secret or signed request) before any multi-tenant or public use.
6. *(architecture, larger)* Make agent memory retrievable and durable; only then consider distributed runtimes.

Dependencies: 1 and 2 first (they make every later check trustworthy); 3 is independent; 4 needs approval and a working alias set; 5 should precede 6 and any AgentCore deployment.

## 14. Evidence references

Code: `web_app.py:45,52,135,153,179,192-208,257-307`; `iterative_execution.py:61-68,164,205-355,425-460`; `orchestration_policy.py:36-198`; `orchestration_state.py:53-92,95,200`; `orchestration_planner.py:3,31,33,123,132-170`; `delegation_planner.py:28-65`; `deadline.py:21`; `live_events.py:23,43,106,342-373`; `live_stream.py:42-52,59,248-284,287,309-370`; `live_hooks.py:13-88`; `model_agent.py:9-30`; `litellm_client.py:71-97`; `memory.py:12`; `specialist_agent.py:57-75,123,186`; `fact_check_guard.py:15-80`; `agentcore_runtime.py:13`; `agentcore_client.py:49`; `static/index.html:780,816,842,962,1113,1164,1229,1308-1372`. Configuration: `compose.yaml`, `deploy/digitalocean/app.yaml`, `.env.example`, `execution_config/bindings.toml`, `agent_identities/*.toml`. Fixtures: `tests/fixtures/live_events/`. Tests: see development status.
