# Phase 6.6 release readiness (DRAFT)

**Status: OPEN / NOT RELEASE-READY.** Draft for founder review. It records what has been verified, on which commit, and what has not. Nothing here approves a release, a deployment or a billable test.

* Release tracker: issue #23. Draft written 2026-11-06 against `main` at `57b64c7`.
* Evidence is kept in three separate categories, as the tracker asks: **implemented** (code and docs merged), **tested** (offline tests and CI), **deployed** (a running environment). Only the first two are claimed below. **Nothing from Phase 6.6 has been deployed or run against live inference.**

## 1. Verdict

The offline baseline is in place and green on one commit: the blocking defects (#19, #20, #21, #22) and the streaming-contract conformance work (#53) are merged, evidenced and closed, and the offline suite, Docker suite, security scans and CodeQL all pass on `main`. The release gate is **not** met, because three things remain that only the owner can decide or approve:

1. The **live six-service, browser SSE smoke test** has not been run (billable; needs explicit approval and a cost limit).
2. The **latency, timeout, error and inference-cost baseline** depends on item 1, so it does not exist.
3. The **tracker administration** is incomplete: no Phase 6.6 issue is assigned to the open GitHub milestone `6.6`, and the milestone has not been closed.

## 2. Candidate commit and evidence

Candidate: `57b64c785bf22f993477afb86951392aab5750c7` (`main` after PR #208). All four workflows for this exact commit completed successfully.

| Check | Result | Evidence |
|---|---|---|
| CI (Python 3.12, Python 3.13, Frontend Node 24, Lint, Docker build and smoke test, `CI result` gate) | success | https://github.com/vchokshi/viharbot/actions/runs/42284376429 |
| Offline suite | **2849 passed, 3 skipped** on 3.12, on 3.13, and inside the Docker image (0 failed, 0 errors) | same run, job logs |
| Docker profile (`docker compose --profile tests run`) | executed in CI with no network and no secrets; only the opt-in live checks skipped | same run, "Offline suite in Docker" |
| Security Scan (Gitleaks, Bandit, pip-audit, Trivy) | success | https://github.com/vchokshi/viharbot/actions/runs/42284376383 |
| CodeQL | success | https://github.com/vchokshi/viharbot/actions/runs/42284376499 |
| OpenSSF Scorecard | success | https://github.com/vchokshi/viharbot/actions/runs/42284376474 |
| Coverage badge data | published by the `main` run | same CI run |

The 3 skipped tests are the opt-in live LiteLLM checks (`RUN_LITELLM_INTEGRATION`, `RUN_LITELLM_INFERENCE`). They are off by design because they make billable calls.

**Branch protection.** The active `protect-main` ruleset forbids deletion and non-fast-forward updates, requires a pull request, and requires the checks `CI result`, `Contributor registry`, `Dependency review` and `CLA acceptance`. Checked from the ruleset definition; badges were not separately verified beyond the coverage-badge job succeeding.

## 3. Tracker gate, item by item

| Gate item (from #23) | Status | Notes |
|---|---|---|
| Fix #19 to #22 (Docker image, Node guard, `.env` isolation, SSE terminal outcome) | **Done** | Section 4 |
| Canonical SSE contract fixtures and conformance (#53) | **Done** | Section 4 |
| Clean checkout tests on Python 3.12/3.13 and Node 24 | **Done on CI** | Run above. Local runs used Python 3.13 only |
| Docker offline suite executed, not only built | **Done** | CI executes it; no network, no secrets |
| CI, security scans, required checks | **Done** for the candidate commit | Section 2 |
| Badges | Partially verified | Coverage badge data job succeeded; badge rendering not checked |
| SSE fault-injection traces | **Done** as tests | `tests/test_live_terminal_faults.py`; no live traces captured |
| Browser distinguishes completed, failed, interrupted | **Done** in the Node fake DOM | **No real browser has been run** |
| `POST /api/chat` backward compatibility | **Done** | Unchanged; covered by the suite |
| Live six-service workflow and browser SSE timeline (opt-in, billable) | **Not done** | Needs explicit approval |
| Latency, timeout, error and inference-cost baseline | **Not done** | Depends on the live run |
| Record tested commit, limitations, rollback | This document | Needs owner review |
| Milestone `6.6` assignment and closure | **Not done** | 0 issues assigned; milestone open |
| Staging verification | **Not applicable** | Nothing was deployed. `deploy_on_push` stays disabled |

## 4. Child issues

| Issue | Pull request | Merge commit | Post-merge CI | State |
|---|---|---|---|---|
| #19 Docker test image | #192 | `65d0cd0` | https://github.com/vchokshi/viharbot/actions/runs/41936449622 | closed |
| #20 Node guard | #192 | `65d0cd0` | same | closed |
| #21 `.env` isolation | #194 | `75909e0` | https://github.com/vchokshi/viharbot/actions/runs/41938530530 | closed |
| #22 SSE terminal outcome | #196 | `c771137` | https://github.com/vchokshi/viharbot/actions/runs/41941592119 | closed |
| #53 SSE contract fixtures | #199 | `950d968` | https://github.com/vchokshi/viharbot/actions/runs/42112279136 | closed |

What each changed, in one line: #19 the test image now runs the whole offline suite (Node 24, `git`, an allowlist build context, no secrets, no runtime network); #20 restored `@needs_node` and added a regression check for every Node-reaching test; #21 stops LiteLLM's import-time `.env` load during tests; #22 makes "answer sent but completion could not be" an explicit, logged, tested protocol-failure outcome and reserves `cancelled`; #53 regenerates and compares every documented stream against the real loop, and adds a complete full-team fixture. No production Dockerfile, schema or wire format changed in any of them.

## 5. Known limitations

* **Fixtures are not live captures.** The documented streams are regenerated from the real orchestration loop with mocked inference. They show what the code emits, not what a live deployment with live models emits.
* **The browser path is tested in a Node fake DOM, not a real browser.**
* **Default execution budget (6) cannot cover all seven specialists.** A run that delegates to every specialist ends under `policy_budget` with the last delegation rejected. Whether six is the right default is an open product decision.
* **No `403`.** Authentication is HTTP Basic: `401` for bad or missing credentials, `503` when web authentication is not configured.
* **Disconnects do not guarantee terminal delivery.** A cancelled or disconnected run emits no terminal event, and in-flight remote inference is not interrupted. After the answer is sent, a failure to send the completion ends the stream without a terminal event (the page shows it as interrupted and the server logs it at error level). The page ignores frames after the first valid terminal event.
* **The `cancelled` failure code is declared but reserved**, and the emitter refuses to send it. Deleting it would change the wire contract and belongs with contract work.
* **Test-image size.** The Docker test image grew about 314 MB (measured on arm64; CI is amd64). It is never deployed.
* **Docker job scope.** On a pull request the Docker job runs only when a container definition or `src/` changes, so a tests-only change gets its Docker run on the next push to `main`.
* **Test image runs as root** (a known Trivy `DS-0002` finding, documented earlier and not part of this phase).
* **The `.env` fix is test-only.** In Compose, the `web` service still takes `.env` values by design. That is a production-facing question, not addressed.
* **Test fragility.** Four tests in `tests/test_pipeline_state.py` failed under full-suite ordering when a real `.env` leaked in. The leak is closed, but the order sensitivity itself was not investigated.
* **PR #208 and other merges after #199 are in the candidate commit** and are outside Phase 6.6's scope; this draft makes no claim about them beyond CI passing.

## 6. Items needing the owner

1. **Possible past billable call (verify).** Before #21 was fixed, host test runs on a machine with a local `.env` loaded a real planner key, and the live planner path logged `orchestration planner call failed`. It could not be determined whether a request actually reached the gateway. Checking gateway usage for the affected period would settle it.
2. **Live smoke test approval.** If approved, it needs: a cost ceiling, the model aliases to use, how many runs, and who watches the browser SSE timeline. It would also supply the missing latency, timeout, failure and cost baseline.
3. **Milestone.** Assign #19 to #23 and #53 to milestone `6.6` and close it when the gate is met.
4. **Exceptions.** None are approved. If the live smoke test is deferred past this release, that deferral should be recorded here as an explicit, owner-approved exception.
5. **Decision on the budget default** (finding above) and on whether to retire the reserved `cancelled` code.

## 7. Rollback

Every Phase 6.6 change is a merged pull request and can be reverted independently with `git revert -m 1 <merge-commit>`: `65d0cd0`, `75909e0`, `c771137`, `950d968`. They touch only tests, fixtures, docs, CI configuration and, for #22 only, the error handling and logging in `src/viharbot/live_stream.py` (no schema, no page, no wire change). Nothing was deployed, so rollback needs no redeploy, no data step and no infrastructure change. A push to `main` does not deploy.

## 8. Not verified

A live run of any kind; a real browser; a proxy in front of the stream; DigitalOcean staging or AWS (neither was deployed or touched); Python 3.12 on a developer machine (covered by CI only); badge rendering; the effect of the merges after #199.

*Sources: pull requests #192, #194, #196, #199 and their descriptions; the CI runs linked above; `docs/live-observability.md`; `docs/continuous-integration.md`; `docs/development-status.md`.*
