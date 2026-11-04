# ViharBot

[![CI](https://github.com/vchokshi/viharbot/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/vchokshi/viharbot/actions/workflows/ci.yml?query=branch%3Amain)
[![Tests](https://img.shields.io/github/check-runs/vchokshi/viharbot/main?nameFilter=Python%20tests%20%283.12%29&label=tests)](https://github.com/vchokshi/viharbot/actions/workflows/ci.yml?query=branch%3Amain)
[![Coverage](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2Fvchokshi%2Fviharbot%2Fbadges%2Fcoverage.json)](https://github.com/vchokshi/viharbot/actions/workflows/ci.yml?query=branch%3Amain)
[![Lint](https://img.shields.io/github/check-runs/vchokshi/viharbot/main?nameFilter=Lint%20%28Ruff%29&label=lint)](https://github.com/vchokshi/viharbot/actions/workflows/ci.yml?query=branch%3Amain)
[![Docker Build](https://img.shields.io/github/check-runs/vchokshi/viharbot/main?nameFilter=Docker%20build%20and%20smoke%20test&label=docker%20build)](https://github.com/vchokshi/viharbot/actions/workflows/ci.yml?query=branch%3Amain)

[![CodeQL](https://github.com/vchokshi/viharbot/actions/workflows/codeql.yml/badge.svg?branch=main)](https://github.com/vchokshi/viharbot/security/code-scanning)
[![Dependency Review](https://github.com/vchokshi/viharbot/actions/workflows/dependency-review.yml/badge.svg?branch=main)](https://github.com/vchokshi/viharbot/actions/workflows/dependency-review.yml)
[![Security Scan](https://github.com/vchokshi/viharbot/actions/workflows/security.yml/badge.svg?branch=main)](https://github.com/vchokshi/viharbot/actions/workflows/security.yml?query=branch%3Amain)

ViharBot is a browser-based, multi-agent research and security assistant. A FastAPI web container coordinates six independently deployed FastAPI agents over private JSON REST. A named coordinator, Charlie Chatwin, can plan the work one step at a time, but the application, not the model, decides what may run. All model inference goes through Strands Agents to an externally managed [LiteLLM](docs/litellm-integration.md) proxy; there are no direct provider calls.

This README describes what the code on `main` does today. Planned work is labelled as planned. See [development status](docs/development-status.md) for what is implemented, tested, integrated, deployed and operationally validated, and the [Phase 6.5 audit](docs/audits/phase-6.5-architecture-audit.md) for the evidence behind these statements.

## Architecture

```mermaid
flowchart LR
  B[Browser chat] -->|Basic Auth when enabled| W[Web container]
  W -. "planner only (opt-in key)" .-> L[(External LiteLLM)]
  W -->|private JSON REST + request ID| R[research]
  W -->|private JSON REST| F[fact-check]
  W -->|private JSON REST| Q[source-quality]
  W -->|private JSON REST| S[security]
  W -->|private JSON REST| P[privacy-review]
  W -->|private JSON REST| Y[synthesis]
  R --> L
  F --> L
  Q --> L
  S --> L
  P --> L
  Y --> L
```

The web container serves the UI and runs the orchestration. It is model-backed only for Charlie's planner, and only when iterative orchestration is enabled and a planner alias and key are supplied. Locally, Compose publishes only the web port, on `127.0.0.1:8080`; the six agents stay on the private Compose network. See [architecture](docs/architecture.md) for the full picture.

## Agents and capabilities

Eight agent identities are defined; four are connected to running services. Logical identities and physical services are different things: Scout and Cipher each own two services, and Charlie owns the Synthesis service and the planner.

| Identity | Status | Capability | Service | Default model alias |
|---|---|---|---|---|
| Nova | live | research | `research` | `gpt-6.1-sol` |
| Scout | live | fact check | `fact-check` | `gemini/gemini-3.5-flash` |
| Scout | live | source quality | `source-quality` | `gemini/gemini-3.5-flash` |
| Cipher | live | security assessment | `security` | `openai/gpt-5.4` |
| Cipher | live | privacy review | `privacy-review` | `gpt-5.4-mini` |
| Charlie | live | synthesis | `synthesis` | `gpt-5.4-mini` |
| Charlie | live (opt-in) | planning | in the web container | `gpt-6-luna` (`CHARLIE_PLANNER_MODEL`) |
| Atlas | planned | none connected | none | none |
| Sage | live in the dev stack; Charlie may delegate it ([Sage service](docs/sage-service.md)) | philosophical analysis | `sage`, bound | `gpt-6.1-sol` in Compose |
| Pixel | live, on request (flag) | slide generation | `pixel` | `gpt-5.4-mini` |
| Orbit | support role, not connected | none connected | none | none |

Fact Check reviews only the evidence Research supplied; it does not browse. Each non-trivial verdict must quote that evidence, and the application checks the quote. Source Quality inspects reference strings without fetching them.

## Skills registry (proposed architecture)

A declarative registry of 98 skills across eleven identities (the original eight, the education agents Archie and Ada, see [Education agents](docs/education-agents.md), and the proposed coding agent Kepler, see [Kepler](docs/kepler.md), which has no skills yet) lives in `src/viharbot/skills/`. It is **metadata only**: every skill is `proposed`, none can execute, and nothing in the running application reads it. An identity says who an agent is; a skill says what professional work it could do; a capability says what it is suited to do and whether a service exists; an execution binding says which service runs it. Only bindings run anything. See [Skills registry](docs/skills-registry.md) for how to add and validate a skill and how skills would later be authorized, and the governance documents: [Founding Charter v2](docs/governance/founding-charter-v2.md), [Agent Constitution v1](docs/governance/agent-constitution-v1.md), [Agent Architecture v1.1](docs/governance/agent-architecture-v1.1.md) and [Skills Registry v1](docs/governance/agent-skills-registry-v1.md). These documents are proposed, not implemented, except where the repository says otherwise.

## Execution modes

`POST /api/chat` picks the first mode whose flags allow it (`web_app.py`):

| Mode | Flags | Behavior |
|---|---|---|
| Slide decks (Pixel) | `VIHARBOT_PPT_GENERATION_ENABLED` (true in local Compose while testing; set it false when the feature is done) | After the pipeline, if the message asks for a deck, the pixel service plans and builds a `.pptx`; see [Pixel slide service](docs/pixel-slide-service.md) |
| Live team conversation (iterative orchestration) | **No flag.** It runs when Charlie's planner is configured: a planner alias (`gpt-6-luna` by default) and `WEB_LITELLM_API_KEY` passed to the web container. Without the key the web service runs the fixed pipeline | Charlie recommends one step at a time; policy authorizes it; the loop runs it, and Pixel joins when you ask for a deck |
| Fixed pipeline | none (the default) | Research, then the specialists in a fixed order |

All flags default to `false`. `POST /api/chat/stream` (live events) works only in iterative mode and returns `409` otherwise. Details: [iterative orchestration](docs/iterative-orchestration.md), [controlled delegation](docs/controlled-delegation.md), [identity execution](docs/identity-execution-integration.md).

## Charlie's orchestration

Each decision is a fresh, stateless planner call that can only propose `delegate <capability>` or `finish`. The application enforces dependencies (Research first; Security needs Research; Privacy Review and Synthesis need Research and Security), freshness of versioned results, budgets (8 decisions, 6 executions, 2 repeats, 240 seconds, 2 refused actions by default) and an absolute request deadline. A recommendation is never an authorization.

## Live observability

With iterative mode on, the page streams real events as they happen: Charlie's planning, delegations, each agent starting and finishing, dependencies, superseded evidence and the final answer. It shows them as a chat between Charlie and the specialists, with the model for each speaker when the service reported it (requires `VIHARBOT_DEV_DIAGNOSTICS_ENABLED`). The chat wording is templated from recorded events, not generated by the agents' models. The protocol is documented in [live observability](docs/live-observability.md).

## Models and credentials

Every model call goes through `build_agent()` (`src/viharbot/model_agent.py`), which builds a Strands `LiteLLMModel` pointed at LiteLLM. Each agent container gets its own alias and `LITELLM_API_KEY`. The web container receives a key only if you opt in with `WEB_LITELLM_API_KEY` (needed for Charlie's planner). `LITELLM_BASE_URL` defaults to `https://litellm.viharchokshi.com`. A configured alias does not prove it works: confirm each alias is available to your LiteLLM account first (see [LiteLLM integration](docs/litellm-integration.md)). ViharBot does not deploy or modify LiteLLM. Never commit `.env`.

## Run locally

```bash
cp .env.example .env     # set LITELLM_API_KEY and aliases your account can use
docker compose up --build -d
```

Open [http://127.0.0.1:8080](http://127.0.0.1:8080). Local Basic Auth is off by default because the listener is bound to loopback; set `WEB_AUTH_ENABLED=1`, `WEB_USERNAME` and `WEB_PASSWORD` to try it. To turn on live orchestration, add the three flags, `CHARLIE_PLANNER_MODEL` and `WEB_LITELLM_API_KEY` to `.env` (this makes billable calls). A CLI-style run: `docker compose --profile demo run --build --rm orchestrator`. Stop with `docker compose down`.

## Tests

The default suite mocks all model and LiteLLM calls and is the authoritative one. Run it on the host with Node installed (the UI tests run the page's real script in a fake DOM):

```bash
python -m pytest -q
```

Two environment notes. A local `.env` is loaded by python-dotenv and can flip the feature flags on, which makes about 15 tests fail on the host; move `.env` aside when running tests there. The Docker profile runs the same offline suite in a container:

```bash
docker compose --profile tests run --build --rm tests
```

The test image (`Dockerfile.tests`) adds Node 24 and `git`, and its build context is an allowlist (`Dockerfile.tests.dockerignore`), so a local `.env`, credentials and `.git` never enter it. The `tests` service takes no values from your shell or `.env`, keeps the opt-in live checks off and has no network at run time, so it cannot make a model call. The image is for tests only; the production images contain neither pytest nor Node. A skipped test is not a pass: without Node the UI tests skip, and CI fails on any skip other than the opt-in live checks. Opt-in live checks (`RUN_LITELLM_INTEGRATION`, `RUN_LITELLM_INFERENCE`) and `scripts/fact_check_eval/run.py` make billable calls and are off by default.

## Development Wiki

The top bar's **Development Wiki** (`#wiki`) shows epic progress, a timeline and the repository's own documentation, read from GitHub. It is read-only, makes no model calls and needs `DEV_WIKI_GITHUB_TOKEN` (empty means "unavailable"). Percentages are derived from linked child issues and shown with their method; where nothing is linked they read *unknown*. See [Development Wiki](docs/development-wiki.md).

## Quality checks

Every pull request and every push to `main` runs [GitHub Actions](https://github.com/vchokshi/viharbot/actions): Python tests on 3.12 and 3.13 with coverage, frontend tests on Node 24, Ruff, and a Docker build with a health smoke test (on pull requests only when a container definition or `src/` changes; always on `main`). CodeQL, dependency review and four security scanners run separately. The badges above come from those runs; none is hand-maintained. A green badge says the check ran and passed against the latest run on `main`. It does not prove the code has no bugs or vulnerabilities, and a badge can show an older successful run until the next one finishes.

Test results, coverage (XML and HTML) and scan reports are uploaded as artifacts on each run. Code scanning findings are under [Security](https://github.com/vchokshi/viharbot/security/code-scanning). The same checks locally:

```bash
python -m pip install -e ".[test]" ruff==0.17.3
python -m pytest --cov              # also needs Node for the UI tests; move any .env aside
ruff check src tests scripts .github/scripts
```

Details, thresholds, required repository settings and known limits: [continuous integration](docs/continuous-integration.md).

## Deployment

Pushing to `main` updates source only; nothing deploys automatically.

| Target | State |
|---|---|
| Local Docker Compose | The development environment (seven services plus `orchestrator` and `tests` profiles) |
| DigitalOcean App Platform | A template exists in [`deploy/digitalocean/app.yaml`](deploy/digitalocean/app.yaml) (seven components, `deploy_on_push: false`). The committed spec keeps every optional mode off, so a Stage built from it runs the fixed pipeline and `/api/chat/stream` answers `409` |
| AWS / Bedrock AgentCore | Compatibility code and notes only ([AgentCore notes](deploy/agentcore/README.md)); nothing is deployed and the streaming route is not bridged |

## Security boundaries

* All web routes except `/health` use the same shared Basic Auth when enabled (`/`, `/images/*`, `/api/chat`, `/api/chat/stream`, `/api/development/*`). It is not per-user identity.
* The six agents have **no authentication**; they rely on the private network. Do not publish their ports.
* The browser never receives an agent key. Logs hold agent, request ID, destination, alias, duration and error type, never prompts, findings or keys.
* Model output is untrusted: the page renders it with `textContent`, and Markdown answers produce no links or HTML.

## Known limitations

* Agent memory is append-only JSONL, written but never read back, and ephemeral on App Platform.
* Agent-to-agent REST is unauthenticated; identities do not have separate cloud permissions.
* Calls run one at a time, and cancelling a browser request does not stop work already in flight.
* Planned, not built: per-user accounts, durable sessions and memory, authenticated inter-agent calls, distributed runtimes, Atlas, Sage and Orbit. Pixel's slide decks are built only in local Compose and are unverified against a live model; see [Pixel slide service](docs/pixel-slide-service.md).

## Contributing and licensing

ViharBot is personally owned and intended to remain proprietary. Public visibility is not a licence, and no open-source licence has been granted. Anyone can fork and propose changes; merges are decided by the founder, and external code is not merged until a contribution agreement is approved (none exists yet). See [CONTRIBUTING.md](CONTRIBUTING.md) for both contributor paths, onboarding, AI use and architectural approval.

## Documentation

[Contributing](CONTRIBUTING.md) · [Architecture](docs/architecture.md) · [Continuous integration](docs/continuous-integration.md) · [Development status](docs/development-status.md) · [Development Wiki](docs/development-wiki.md) · [Phase 6.5 audit](docs/audits/phase-6.5-architecture-audit.md) · [Live observability protocol](docs/live-observability.md) · [Iterative orchestration](docs/iterative-orchestration.md) · [Controlled delegation](docs/controlled-delegation.md) · [Identity execution](docs/identity-execution-integration.md) · [Personality-aware inference](docs/personality-aware-inference.md) · [Agent identities](docs/agent-identity-architecture.md) · [Skills registry](docs/skills-registry.md) · [Governance](docs/governance/agent-architecture-v1.1.md) · [Agent communication](docs/agent-communication.md) · [LiteLLM](docs/litellm-integration.md) · [Deployment](docs/deployment-strategy.md) · [Identity and security](docs/identity-and-security.md) · [Memory](docs/memory-architecture.md) · [Observability](docs/observability.md) · [AgentCore](deploy/agentcore/README.md)
