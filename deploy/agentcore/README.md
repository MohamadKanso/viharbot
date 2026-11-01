# Amazon Bedrock AgentCore deployment

This is an optional deployment target. A push to `main` does not build images, publish to ECR, or create or update AgentCore resources. AgentCore deployment is a separate user-selected operation.

## Verified runtime requirements

AgentCore Runtime accepts containerized agents that serve `GET /ping` and `POST /invocations` on port 8080. Container images must target `linux/arm64`. ViharBot keeps its existing route shapes and adds a small adapter for those two AgentCore paths. The adapter accepts `{ "request_id": "…", "input": { ... } }` and returns the existing response JSON. See the [AWS HTTP protocol contract](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-http-protocol-contract.html) and [container getting-started guide](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-instances-getting-started.html).

The six model agents and the web coordinator can each run as separate AgentCore Runtime resources. The coordinator can invoke those runtimes through the authenticated `InvokeAgentRuntime` HTTPS API. Give its execution role permission to invoke only the six agent runtimes. Each model agent needs outbound HTTPS access to the configured external LiteLLM endpoint and its own `LITELLM_API_KEY` and model alias.

Set the coordinator's `RESEARCH_AGENTCORE_ARN`, `SECURITY_AGENTCORE_ARN`, `FACT_CHECK_AGENTCORE_ARN`, `SOURCE_QUALITY_AGENTCORE_ARN`, `PRIVACY_REVIEW_AGENTCORE_ARN`, and `SYNTHESIS_AGENTCORE_ARN` variables to the matching runtime ARNs. When a runtime ARN is configured, it takes precedence over the local/REST URL variable for that agent.

## Browser interface

AgentCore Runtime is an agent invocation endpoint, not a static website host. The current HTML page calls `/api/chat` and needs ordinary browser asset delivery. For an AWS hosted browser app, publish the static HTML and image separately (for example, S3 with CloudFront) and provide an authenticated HTTPS API bridge that invokes the web coordinator runtime. Keep this bridge and the site deployment explicit and separate from the seven AgentCore runtimes. The browser must not receive AWS credentials or the LiteLLM key.

## Deployment prerequisites and limits

- An AWS account with AgentCore Runtime access, a selected region, and fresh credentials.
- An execution role for each runtime, with only the permissions that runtime needs. The coordinator role needs `bedrock-agentcore:InvokeAgentRuntime` for the six agent ARNs.
- ECR repositories for ARM64 images. Build images with `docker buildx build --platform linux/arm64` and use port 8080 (the Dockerfiles accept `--build-arg APP_PORT=8080` for agent images).
- Supply `LITELLM_API_KEY` through the AWS deployment configuration; never add it to source or print it in deployment logs.
- Current AgentCore microVM sessions and local `/data` files do not provide the project's durable per-agent memory. The current JSONL memory remains ephemeral unless a concrete storage need is designed separately.

**Slide decks.** In production set `VIHARBOT_PPT_GENERATION_ENABLED=true` on the web coordinator and the pixel runtime (it is false on Stage). That also needs a seventh agent runtime for the `pixel` service, which uses `Dockerfile.pixel`, and a Pixel model alias that the gateway routes. See [Pixel slide service](../../docs/pixel-slide-service.md); none of this is provisioned or tested.

No AWS resources are created by this guide. AgentCore Runtime container images are limited to 2 GB, and the service has region-specific quotas and charges. Check current [quotas](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/bedrock-agentcore-limits.html) and [pricing](https://aws.amazon.com/bedrock/agentcore/pricing/) before provisioning seven runtimes and the browser hosting bridge.
