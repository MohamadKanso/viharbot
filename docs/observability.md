# Observability

Each agent service and the web coordinator emits structured JSON logs. Iterative mode adds live, per-request events over `POST /api/chat/stream`, specified in [live observability](live-observability.md); each stream also logs one `live stream ended` record with its terminal event name (or `no_terminal_event`). Records include agent identity, request ID, destination agent when relevant, model alias, duration, and error type. The web service forwards one request ID across the full pipeline so a browser request can be traced across all containers.

The `/api/chat` response includes an `agent_activity` array in call order. Each entry reports the agent, model alias returned by that agent, status, and round-trip duration measured by the web coordinator. Failed calls include their elapsed time so the UI can show partial progress. Calls are sequential and share one LiteLLM service; latency includes network time and the inference wait, and is expected to vary.

Prompts, chat history, findings, API keys, and passwords are excluded from application logs. OpenTelemetry and hosted trace collection are deferred. DigitalOcean App Platform provides component logs; no export destination is configured by this repository.
