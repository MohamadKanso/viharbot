# Sage service (Phase 7.1, PR A)

> **Status: Charlie can delegate to Sage (PR B).** `philosophical_analysis` is implemented and bound to this service, and the live loop treats it as an optional capability. It is not in the fixed pipeline, not in the public `/api/chat` response, and the live event stream and UI do not show it yet (PR C). Sage's identity is unchanged: Director of Philosophy & Human Perspective, with the same personality, expertise and prompt.

Sage is a specialist. It does not plan, delegate or call other agents, and it has no tools. Given a question and a fixed set of other agents' results, it returns perspective on the ethical, human, organizational and governance dimensions as a validated `SageAnalysis`.

## What it does

`POST /process` takes a `SageRequest` and returns the standard specialist envelope (`request_id`, `agent: "sage"`, `requested_model_alias`, `result`). `GET /health` and `GET /model` (the configured alias, no secret) follow the other agents. All inference goes through the externally managed LiteLLM gateway with the existing `build_agent()`; there is no provider SDK in the service.

```
SageRequest -> build_sage_prompt -> build_agent (LiteLLM) -> SageAnalysis -> guard_analysis -> envelope
```

## The request: only what Sage is authorized to read

`question` (1 to 6000 characters, not blank) and `context`, which may carry only `research`, `fact_check`, `source_quality`, `security` and `privacy_review`, each at most 20,000 characters of JSON. Any other field or section is refused with a 422 before any model call, so a caller cannot widen what Sage sees. The orchestrator will decide which sections a given request actually carries; Sage does not need security results unless the question depends on them.

The prompt names the sections that were supplied, tells the model they are the only evidence it may cite, and marks the question and every result as untrusted data. Results are embedded as one escaped JSON line, so text inside a result cannot pose as an instruction.

## The response: findings are told apart from judgments

`SageAnalysis` has an executive summary, stakeholders, ethical considerations, human and organizational impacts, risks and trade-offs, recommended mitigations, assumptions and uncertainties, and recommendations for Charlie. Every consideration, impact and trade-off carries a `claim_type`:

| claim_type | meaning |
|---|---|
| `evidence_supported` | a supplied context section states it; the section is named in `evidence_refs` |
| `reasoned_interpretation` | an inference from the evidence or the question |
| `normative_judgment` | a value judgment, resting on a stated framework or assumption |
| `recommendation` | a proposed action |

All lists and strings are length-bounded.

**The guard.** After the model answers, `guard_analysis` runs deterministic code, in the same spirit as the fact-check guard. A claim may cite only a section that was actually supplied; references to anything else are dropped, and an `evidence_supported` claim left with no reference is downgraded to `reasoned_interpretation`. The downgrade is disclosed as the first item of `assumptions_and_uncertainties`; nothing is corrected silently. Sage cannot cite evidence it was not given.

**Prompt composition.** The system prompt is Sage's existing identity prompt, then the application's precedence clause, then the task rules. Sage is new and has no earlier behavior to preserve, so its identity always applies and does not depend on the personality-inference flag the older services use.

## Model alias decision

Sage prefers a Claude Opus model, but that is a preference, not a configured alias. On 2026-11-01 the gateway's `/v1/models` listed `gemini/gemini-3.5-flash`, `gpt-6-luna`, `gpt-6.1-sol`, `openai/gpt-5.4` and `openal/gpt-5.4-mini`: **no Claude or Opus alias**. The default is therefore **`gpt-6.1-sol`**, verified with a one-word completion that returned 200. It is the strongest reasoning model available at $2.00 input and $10.00 output per million tokens, against $2.50 and $15.00 for `openai/gpt-5.4`; `gpt-6-luna` ($0.10 and $0.50) is far cheaper but nothing here shows it reasons well enough about normative questions. That is a judgment call, not a measured result, and a quality comparison across models is tracked separately (issue #73). Constitution Section 9.2 asks for a task-based justification for a pricier model: ethical analysis with a structured, bounded output is the most reasoning-heavy task a specialist here has. Change it with `SAGE_MODEL`. If a Claude alias is added to the gateway later, set it there; no code changes.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `LITELLM_BASE_URL`, `LITELLM_API_KEY` | as the other agents | the gateway; the key is never in source and never logged |
| `SAGE_MODEL` | `gpt-6.1-sol` in Compose; none in code | Sage's own alias |
| `SAGE_MODEL_TIMEOUT_SECONDS` | 80 (clamped to 5 to 180) | the model call's timeout, with no client retries (LiteLLM's default two retries would multiply it) |
| `MEMORY_DIR` | `/data` in Compose | Sage's own append-only memory, like the other agents |

The timeout default is below the orchestrator's 90 s read cap on a REST call, so Sage fails with its own error before the caller gives up. `compose.yaml` adds a `sage` service with a healthcheck and no published port; nothing depends on it yet. It is not in the DigitalOcean spec, which is a separate cost decision.

## Observability

One structured log line per request with the request ID, model alias, duration and error type, never the question, the results or the output. Token counts are logged only when the client reports them; none are estimated.

## Validation

Offline tests (no model, no network, no key) cover Sage's identity and prompt composition, the contracts and their bounds, the request's refusal of anything outside the contract, the prompt, the guard, routing through `build_agent` with the real gateway client settings, failure handling (sanitized 502 that leaks no message, key or question), timeouts and packaging. Beyond them, one real call to the gateway with the acceptance scenario's question and a synthetic context (a fixture standing in for a security result that recommends monitoring employee interactions with agents) returned a valid analysis in about 40 s using about 1,500 tokens in and 2,100 out (about $0.024 at the gateway's prices), engaged with the monitoring recommendation's privacy, trust and proportionality consequences, and cited only supplied sections. That run also found a defect that unit tests could not: the model wrote claim-type tags such as `[reasoned_interpretation]` into the summary and recommendations because the rules asked for labels on fields that have no label. The rules now keep labels out of prose, a test pins that, and a second real run had no tags in any of 42 text fields. This is two real calls on one scenario, not an evaluation.

## How Charlie uses Sage (PR B)

- **Authorization.** `philosophical_analysis` has one binding, `sage` to the `sage` service. It needs research and nothing else. It never gates completion: Charlie can finish without it, and the application completes by policy without it if a limit is reached.
- **Availability.** The web service offers Sage only where `SAGE_URL` (or `SAGE_AGENTCORE_ARN`) is set. There is no built-in address, so a deployment without a Sage service, such as the current Stage spec, never offers it and Charlie cannot delegate to it. Compose sets `SAGE_URL` and the web service waits for Sage's health check.
- **Inputs and freshness.** Required: research. Optional, read when fresh: fact check, source quality, security and privacy review. A Sage result is stale if a newer result of any of those exists that it did not read, so it is never shown beside findings it did not see. Synthesis treats Sage as an optional input in the same way, so a Sage result that arrives after synthesis makes the synthesis stale and Charlie must run it again. The planner is told to request Sage after the results it should weigh and before synthesis.
- **What Sage receives.** The question plus bare results (not response envelopes) for at most the five authorized sections. A section over the 20,000-character request limit is left out whole, never truncated, and Sage is told which sections it received.
- **Synthesis.** The synthesis context gets a `sage` key only when a fresh Sage result exists. The synthesis service's system prompt is unchanged (a test pins it); instead its message carries an instruction, only when Sage ran, to weigh the analysis against the other results, say where it changes the answer, and not append it as a separate section.
- **Output.** The Sage analysis reaches the user through the synthesis answer and the agent activity. It is deliberately not a new field on the public chat response; that is PR C's decision with the UI.
- **Timeouts.** The orchestrator's read timeout for any agent is the smaller of 90 s and the time left on the one request deadline, which is never reset or extended. Sage's own model timeout (80 s) sits below the 90 s cap. A Sage result that arrives after the deadline is discarded and the run completes by policy if research and security are present.
- **Failure.** A Sage failure is handled like any other agent's: the request fails with a sanitized error rather than silently omitting the analysis. Making optional capabilities degrade gracefully is a separate decision and would change every optional agent.

## What is not done

- **Live events and the UI (PR C).** The event schema's `Service` literal and capability type need `sage` and `philosophical_analysis`; the live conversation, UI display of the analysis, the end-to-end acceptance scenario and latency measurements follow.
- **Stage and AWS.** The Stage spec has no Sage component and nothing here deploys one; Stage behavior is unchanged.
- **Time.** The loop is sequential. Sage takes about 40 s with `gpt-6.1-sol`, on top of Research (about 35 s), security, any other steps, synthesis and the planner's own calls. Whether a full run fits in 240 s is not measured yet; PR C measures it. The budget is not raised for Sage.
- No automated quality evaluation of Sage's analysis exists, and no real end-to-end run through Charlie has been made yet.
