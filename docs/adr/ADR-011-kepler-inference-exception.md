# ADR-011: Kepler may use Bedrock-hosted non-Anthropic models (scoped exception to LiteLLM-only inference)

**Status: Accepted (2026-11-05, founder).**. The founder confirms the numbering and accepts the decision. Tracking issue: [#198](https://github.com/vchokshi/viharbot/issues/198).

## Context

`AGENTS.md` requires all foundation-model inference to go through the external LiteLLM gateway and forbids provider-native inference. That rule fits the six FastAPI agents. Kepler is different: it is the first agent on AWS Bedrock AgentCore ([kepler-steward-aws.md](../kepler-steward-aws.md)), it has no model today, and its AWS roles already carry an explicit deny on `bedrock:InvokeModel` and `bedrock:InvokeModelWithResponseStream` (runtime policy, boundary policy, and the simulation and negative tests). Requiring a LiteLLM key, a public gateway hop and a Secrets Manager secret for an agent that lives inside AWS is a cost and a dependency that the platform can avoid. #198 also prohibits Claude/Anthropic models, direct or indirect, until licensing and organizational authorization are confirmed.

## Decision

Relax the LiteLLM-only invariant **for Kepler only**, in this way:

1. Kepler may run inference through **Amazon Bedrock**, authorized by its IAM runtime role, with no API key.
2. Only **non-Anthropic** models may be used. The permitted set is an explicit allowlist of model IDs in the Kepler runtime configuration, and the code **fails closed** on any ID not on it. Claude/Anthropic models stay prohibited, on Bedrock or anywhere else, until the founder records licensing and organizational approval in this ADR.
3. LiteLLM stays permitted for Kepler and remains the default for every other agent. Nothing about the six existing agents, `build_agent()`, or the gateway changes. ViharBot still does not deploy or modify LiteLLM.
4. The exception does not extend to any other agent, to other providers' native APIs, or to Kepler holding any provider API key.
5. Logging is unchanged: structured JSON with agent, request ID, model ID, duration and errors; no full prompts or private memory.

## What this changes in AWS (not done by this ADR)

The live IAM objects deny `bedrock:InvokeModel*`, and the boundary caps every Kepler role. Enabling Bedrock needs, in order, an administrator action in the lab account:

1. A new boundary version that replaces the blanket deny with an allow scoped to the allowlisted non-Anthropic foundation-model ARNs (and, if used, inference profiles), keeping an explicit deny on `arn:aws:bedrock:*::foundation-model/anthropic.*` and Anthropic inference profiles.
2. The same change in `runtime-policy.json`, then Access Analyzer validation, then `policy-simulation.sh` and `deployer-negative-test.sh` updated so "invoke a foundation model" is allowed for an allowlisted model and denied for an Anthropic one.
3. Bedrock model access enabled for the chosen models in the account, which is a console or account-level step. Which models are available and their price are **not verified here**; check current Bedrock availability and cost before choosing.

This PR edits none of those files and runs no AWS command. Until the administrator change is made, the live deny stays in force, so the exception is documented but inert.

## Consequences

- Positive: no gateway key or public egress for Kepler inference; identity-based access; the Anthropic prohibition becomes enforced by IAM and by code, not only by policy text.
- Negative: a second inference path to govern, audit and cost-track; per-token Bedrock spend appears on the AWS bill (a material cost change to report before any deployment); the "all inference through one gateway" audit story no longer covers Kepler.
- The no-Claude rule is now enforced in two places (allowlist in code, IAM deny) that must stay in sync.

## Allowlist chosen

Initial allowlist, chosen by the founder as Meta Llama: `meta.llama3-1-8b-instruct-v1:0` only (on-demand, no inference profile; listed active in `us-west-2` on 2026-11-05 by a read-only catalog call, which does not prove the account has access or show price). Adding a model means editing the allowlist in the policy files and, once it exists, in code, and a new boundary version.

## Not decided

Whether Kepler uses a model at all for its first build (it does not today); the founder's approval to include Claude later; changes to the governance architecture document, which describes LiteLLM-only inference and is founder-owned (`docs/governance/` is not edited here).
