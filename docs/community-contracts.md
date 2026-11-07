# Community interaction contracts

**Status: Phase C1 of Epic #157 (issue #209). Data contracts only.** Nothing executes, schedules, sends or authorizes these messages, and no runtime module imports them (tested). C2 (#210) will have the orchestrator use them on policy-authorized edges. See the [persona and community specification](persona-community-specification.md).

## What they are for

Constitution [5.3 to 5.6](governance/agent-constitution-v1.md) require structured agent communication, a delegation contract, evidence-based disagreement with five named parts, and disclosure of unresolved disagreement. These models make a message that breaks those rules **unrepresentable**, so a later orchestrator can validate instead of trusting free text.

## Messages

All share an envelope: `message_id`, `engagement_id`, `task_id` (the parent task), `request_id` (the existing per-request id used in logs), `sender`, `recipient`, optional `in_reply_to`, and a `message_type` that selects the model.

| Type | Purpose | Key rules |
|---|---|---|
| `review_request` | Ask a reviewer to independently review an author's work | `author` is named and must differ from the reviewer (`recipient`); one or more `artifact_refs`; carries `objective`, `scope`, `completion_criteria` and `escalation_conditions` (delegation contract, 5.3) |
| `review_report` | The reviewer's answer | Must reply to a request. `issues_found` needs findings; `no_issues_found` and `unable_to_review` allow none; `unable_to_review` needs a reason; `limitations` are always required (5.8). Every finding must cite evidence |
| `challenge` | Constructive disagreement | All five parts of 5.5 are required: disputed conclusion, reason, evidence (at least one reference), potential consequence, proposed resolution. Only an agent can challenge, not the application |
| `challenge_resolution` | The outcome | Must reply to a challenge. A decided outcome (`accepted`, `partly_accepted`, `rejected`) must cite evidence. `unresolved` + `material` requires an `escalation` (5.6) |

`sender` is a registered identity, or `application` for a policy-authorized review request. `recipient` and `author` must be registered identities, and sender and recipient must differ.

`validate_reply(original, reply)` checks that a reply really answers its original: right reply type, `in_reply_to` equals the original's `message_id`, same engagement, task and request id, sent by the original recipient back to the original sender. This blocks forged or crossed replies.

## What a message cannot do

* **Grant authority.** Every model rejects unknown fields, so a message cannot carry tools, permissions, scopes, model aliases, approvals, trust scores or seniority. Tests cover each, including inside nested objects.
* **Set a budget.** Execution budgets belong to application policy. A message cannot request or raise one.
* **Prove a claim.** An evidence reference is a structural citation of material supplied in the engagement. Requiring one stops an unsupported challenge from being expressed; it does not make the cited claim true. Whether a challenge is sound is a judgment for the reviewer, the orchestrator and, if material, the user.
* **Instruct its reader.** Message contents are untrusted data to whoever receives them.

## Parsing

`parse_message(dict)` and `parse_message_json(str | bytes)` return the right model or raise `ContractError`. JSON over 32 KiB is rejected before decoding. Errors name the field path and message and never echo the rejected value. Models are frozen.

## Deliberately not here

Authorizing or executing review edges and budget handling (C2); relationship records and collaboration history (C3); storage of messages (Phase D, ADR-009); SSE events for community activity (E1); validation that evidence is relevant or true (a behavioral concern for evaluation, not a schema). Contracts for planned or unconnected agents (Atlas, Orbit) describe intent: they have no running service to send or receive them yet.
