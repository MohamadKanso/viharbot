# LiteLLM integration

LiteLLM is an external project already running at https://litellm.viharchokshi.com. ViharBot does not deploy or modify it.

## Environment

- LITELLM_BASE_URL: defaults to the supplied HTTPS endpoint.
- LITELLM_API_KEY: proxy key injected into each model-using agent container.
- RESEARCH_MODEL: gpt-6.1-sol. PRIVACY_MODEL, SYNTHESIS_MODEL, PIXEL_MODEL: gpt-5.4-mini (the gateway lists it without the `openai/` prefix; aliases must match its `/v1/models` list).
- SECURITY_MODEL: openai/gpt-5.4.
- FACT_CHECK_MODEL, SOURCE_QUALITY_MODEL: gemini/gemini-3.5-flash (since PR #13). Other Gemini aliases were tested on 2026-10-27 and are not usable on this account today: gemini-2.5-flash and gemini-2.5-flash-lite return 404 (retired for new users), gemini-3.7-flash and gemini-3.8-flash returned 503 for high demand, and the Pro models returned 429 quota errors.
- CHARLIE_PLANNER_MODEL: gpt-6-luna in local Compose (never inferred from another alias). Used by the planner in the web container, which runs the live team only when WEB_LITELLM_API_KEY is passed (there is no flag). The Stage spec does not set it.

These are configured defaults. A configured alias does not prove a live request succeeds, and the alias list reflects one account at one time; confirm availability with GET /v1/models and a small real call before relying on an alias.

Aliases are passed to Strands LiteLLMModel with use_litellm_proxy enabled. The API base URL is normalized to end in /v1. No provider model IDs or provider credentials are configured here.

## Connectivity and inference

scripts/check_litellm.py calls the OpenAI-compatible GET /v1/models endpoint without generating text. Tests mock this call by default. The opt-in connectivity test makes this non-inference request when RUN_LITELLM_INTEGRATION=1 is set. Catalog presence does not verify model usability. The live SecurityAgent inference test uses openai/gpt-5.4 and requires an HTTP 200 structured result. Run it before using SecurityAgent in the demo. It is gated by RUN_LITELLM_INFERENCE=1; leave it off for the default mock-only suite.

Run the standalone connectivity check inside the test image with: docker compose --profile tests run --build --rm tests python scripts/check_litellm.py

Agent inference uses Strands structured output on LiteLLM's OpenAI-compatible chat completions API. Each agent uses its own alias variable; Charlie's planner uses CHARLIE_PLANNER_MODEL. Errors fail the HTTP request safely; there is no provider fallback.

Keys are sent in Authorization headers by the SDK and never logged or passed to another agent. The web container receives a key only through the opt-in WEB_LITELLM_API_KEY. All containers trust the Cloudflare gateway CA in certs/; a host Python process does not, so run live scripts such as scripts/fact_check_eval/run.py inside a container. Keep .env out of version control and rotate proxy credentials through the LiteLLM operator.
