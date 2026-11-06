# Repository guidance

## Project intent

ViharBot implements a browser-based chat app and six independently deployed FastAPI agents: Research, Fact Check, Source Quality, Security, Privacy Review, and Synthesis. The web container coordinates them over private JSON REST. Keep proposals distinct from implemented features.

## Invariants

- All foundation model inference goes through the external LiteLLM project using its OpenAI-compatible API.
- Configure each model-using agent with `LITELLM_BASE_URL`, `LITELLM_API_KEY`, and its own model alias. Never deploy or modify LiteLLM, add provider-native inference, or put credentials in source.
- **Exception, Kepler only (accepted in ADR-011; effective):** Kepler may use Amazon Bedrock with its IAM role instead of LiteLLM, restricted to an explicit allowlist of non-Anthropic models that fails closed. No Claude/Anthropic model, direct or indirect, until the founder records licensing and organizational approval. Every other agent stays LiteLLM-only. Live IAM (change record row 12, 2026-11-06) allows `bedrock:InvokeModel*` only on `meta.llama3-1-8b-instruct-v1:0` and denies every other model and all Anthropic models. The code-side allowlist is not built yet, so no Kepler code may call a model until it is; Bedrock model access and price are unverified.
- Do not send real keys or sensitive prompts over public HTTP. Permit credentials over HTTPS or HTTP to a literal loopback/private IP only (for secure local tunnels/private networking).
- Use plain JSON REST for agent communication. Do not add A2A, brokers, service meshes, or orchestration frameworks unless the user changes this direction.
- Keep each agent independently deployable with independent configuration and memory. Start with simple isolated storage; do not add databases without a concrete need.
- Begin with structured JSON logs, including agent, request ID, destination agent, model alias, duration, and errors. Avoid full prompts, secrets, and private memory in logs.
- Do not provision AWS resources unless explicitly requested. AgentCore capabilities must be verified before depending on them.

## Deployment choices

- A push to `main` updates source only. It must not deploy to DigitalOcean, AWS, or another provider.
- Keep DigitalOcean App Platform `deploy_on_push` disabled. A user may choose a DigitalOcean deployment explicitly after reviewing the change.
- Treat local Docker Compose as dev and DigitalOcean App Platform as Stage. Before deploying to Stage, finish local implementation and verification, summarize the commit, verification status, and any material cost change, then ask: **“Do you want to promote this dev build to Stage?”** Wait for an affirmative answer for that specific build.
- AWS deployments are a separate, explicit provider choice. Do not create AWS resources unless the user explicitly asks. Verify current AgentCore capabilities before depending on them.
- After an approved deployment, report whether it ran, what happened, whether it succeeded, and verify the provider's deployment status and health endpoint where available. Approval alone does not establish success.

## Implementation style

Keep the learning example minimal and document limitations, especially local unauthenticated interfaces and placeholder behavior. Prefer standard library HTTP clients where practical. Do not add or run tests unless requested; never call a real inference endpoint with user credentials as an implicit check.
