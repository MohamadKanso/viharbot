"""Epic #157 Phase C1: community interaction contracts. Offline; nothing executes or sends."""

from __future__ import annotations

import copy
import re
from pathlib import Path

import pytest

from viharbot.community import (
    APPLICATION_SENDER,
    MAX_MESSAGE_BYTES,
    Challenge,
    ChallengeResolution,
    ContractError,
    ReviewReport,
    ReviewRequest,
    parse_message,
    parse_message_json,
    validate_reply,
)
from viharbot.live_events import APPLICATION_IDENTITY

SRC = Path(__file__).resolve().parent.parent / "src" / "viharbot"
ENVELOPE = {"engagement_id": "eng-1", "task_id": "task-1", "request_id": "req-1"}
EVIDENCE = [{"artifact_ref": "art-nova-1", "locator": "section 2", "excerpt": "two sources agree"}]


def review_request(**over):
    base = {
        **ENVELOPE, "message_type": "review_request", "message_id": "m1",
        "sender": "charlie", "recipient": "scout", "author": "nova",
        "artifact_refs": ["art-nova-1"], "objective": "Independently validate Nova's findings.",
        "scope": "Sources and claims in the research findings only.",
        "completion_criteria": "Each claim marked corroborated, single-source or contradicted.",
        "escalation_conditions": "Report to Charlie if a source cannot be checked.",
    }
    return {**base, **over}


def review_report(**over):
    base = {
        **ENVELOPE, "message_type": "review_report", "message_id": "m2", "in_reply_to": "m1",
        "sender": "scout", "recipient": "charlie", "outcome": "issues_found",
        "findings": [{
            "severity": "medium", "summary": "Claim 3 contradicts its source",
            "detail": "The cited source states the opposite of finding 3.",
            "evidence": EVIDENCE, "suggested_resolution": "Re-check against the primary source.",
        }],
        "limitations": ["Paywalled sources were not examined"],
    }
    return {**base, **over}


def challenge(**over):
    base = {
        **ENVELOPE, "message_type": "challenge", "message_id": "m3",
        "sender": "cipher", "recipient": "orbit", "artifact_ref": "art-orbit-design",
        "disputed_conclusion": "The service boundary needs no authentication.",
        "reason": "The design exposes the endpoint beyond the private network.",
        "evidence": [{"artifact_ref": "art-orbit-design", "locator": "diagram 2"}],
        "potential_consequence": "Any caller could invoke privileged automation.",
        "proposed_resolution": "Add service authentication and test that unauthenticated calls fail.",
    }
    return {**base, **over}


def resolution(**over):
    base = {
        **ENVELOPE, "message_type": "challenge_resolution", "message_id": "m4", "in_reply_to": "m3",
        "sender": "orbit", "recipient": "cipher", "outcome": "accepted",
        "rationale": "The endpoint is reachable; authentication will be added.",
        "evidence": [{"artifact_ref": "art-orbit-design", "locator": "diagram 2"}],
    }
    return {**base, **over}


def rejects(payload, match=None):
    with pytest.raises(ContractError, match=match):
        parse_message(payload)


# --- Valid messages --------------------------------------------------------------------------


@pytest.mark.parametrize(("factory", "cls"), [
    (review_request, ReviewRequest), (review_report, ReviewReport),
    (challenge, Challenge), (resolution, ChallengeResolution),
])
def test_valid_messages_parse_and_round_trip(factory, cls):
    message = parse_message(factory())
    assert isinstance(message, cls)
    assert parse_message(message.model_dump(mode="json")) == message
    assert parse_message_json(message.model_dump_json()) == message


def test_application_constant_matches_the_event_system_identity():
    assert APPLICATION_SENDER == APPLICATION_IDENTITY


def test_application_may_send_a_review_request_on_an_authorized_edge():
    assert parse_message(review_request(sender="application")).sender == "application"


def test_no_issues_and_unable_to_review_reports_are_valid():
    assert parse_message(review_report(outcome="no_issues_found", findings=[]))
    assert parse_message(review_report(outcome="unable_to_review", findings=[],
                                       reason_unable="Source archive was unavailable."))


# --- Envelope (Constitution 5.4) -------------------------------------------------------------


@pytest.mark.parametrize("missing", ["message_id", "engagement_id", "task_id", "request_id",
                                     "sender", "recipient"])
def test_envelope_fields_are_required(missing):
    payload = review_request()
    del payload[missing]
    rejects(payload, missing)


@pytest.mark.parametrize("field", ["sender", "recipient"])
def test_unregistered_identities_are_rejected(field):
    rejects(review_request(**{field: "ghost"}), "registered identity")


def test_sender_and_recipient_must_differ():
    rejects(review_request(sender="scout", recipient="scout"), "must differ")


def test_the_application_is_not_a_valid_recipient():
    rejects(review_request(recipient="application"), "registered identity")


def test_a_message_cannot_reply_to_itself():
    rejects(review_request(in_reply_to="m1"), "reply to itself")


@pytest.mark.parametrize("bad", ["", " ", "has space", "../x", "a" * 65, "-lead"])
def test_ids_must_be_simple_references(bad):
    rejects(review_request(message_id=bad))


def test_unknown_message_type_is_rejected():
    rejects(review_request(message_type="delegate"))


# --- Review ----------------------------------------------------------------------------------


def test_reviewer_must_not_be_the_author():
    rejects(review_request(recipient="nova", author="nova"), "must not be the author")
    rejects(review_request(author="ghost"), "registered identity")


def test_review_request_needs_artifacts_and_unique_refs():
    rejects(review_request(artifact_refs=[]))
    rejects(review_request(artifact_refs=["a", "a"]), "must not repeat")


def test_review_request_carries_the_delegation_contract_fields():
    for field in ("objective", "scope", "completion_criteria", "escalation_conditions"):
        payload = review_request()
        del payload[field]
        rejects(payload, field)


def test_report_must_reply_and_match_its_outcome():
    payload = review_report()
    del payload["in_reply_to"]
    rejects(payload, "must reply")
    rejects(review_report(findings=[]), "requires at least one finding")
    rejects(review_report(outcome="no_issues_found"), "only allowed with issues_found")
    rejects(review_report(outcome="unable_to_review", findings=[]), "requires a reason")
    rejects(review_report(reason_unable="because"), "only allowed with unable_to_review")


def test_report_must_state_limitations():
    rejects(review_report(limitations=[]))


def test_a_finding_must_cite_evidence():
    payload = review_report()
    payload["findings"][0]["evidence"] = []
    rejects(payload, "evidence")
    del payload["findings"][0]["evidence"]
    rejects(payload, "evidence")


# --- Constructive disagreement (Constitution 5.5) --------------------------------------------


@pytest.mark.parametrize("part", ["disputed_conclusion", "reason", "evidence",
                                  "potential_consequence", "proposed_resolution"])
def test_a_challenge_needs_all_five_parts(part):
    payload = challenge()
    del payload[part]
    rejects(payload, part)


def test_a_challenge_cannot_be_made_without_evidence():
    rejects(challenge(evidence=[]), "evidence")


def test_only_an_agent_can_challenge():
    rejects(challenge(sender="application"), "only an agent identity")


def test_decided_resolution_needs_evidence_and_unresolved_material_needs_escalation():
    rejects(resolution(evidence=[]), "must cite evidence")
    rejects(resolution(outcome="unresolved", evidence=[], material=True), "requires an escalation")
    assert parse_message(resolution(outcome="unresolved", evidence=[], material=True,
                                    escalation="Disclose to Charlie and the user."))
    assert parse_message(resolution(outcome="unresolved", evidence=[], material=False))
    rejects(resolution(escalation="escalate"), "only for an unresolved")
    payload = resolution()
    del payload["in_reply_to"]
    rejects(payload, "must reply")


# --- A message grants no authority -----------------------------------------------------------


@pytest.mark.parametrize("factory", [review_request, review_report, challenge, resolution])
@pytest.mark.parametrize("extra", [
    {"tools": ["shell"]}, {"permissions": ["deploy"]}, {"scopes": ["admin"]},
    {"model_alias": "x/y"}, {"budget": {"executions": 99}}, {"approved": True},
    {"trust_score": 0.99}, {"seniority": "senior"}, {"authorized_resources": ["db"]},
])
def test_messages_cannot_carry_authority_or_budget(factory, extra):
    rejects({**factory(), **extra}, "Extra inputs are not permitted")


def test_nested_objects_also_forbid_unknown_fields():
    payload = challenge()
    payload["evidence"][0]["permissions"] = ["read"]
    rejects(payload, "Extra inputs are not permitted")


def test_messages_are_immutable():
    message = parse_message(challenge())
    with pytest.raises(Exception):
        message.reason = "changed"  # type: ignore[misc]


# --- Parsing hygiene -------------------------------------------------------------------------


def test_oversized_and_malformed_json_are_rejected_as_contract_errors():
    with pytest.raises(ContractError, match="size limit"):
        parse_message_json("x" * (MAX_MESSAGE_BYTES + 1))
    with pytest.raises(ContractError):
        parse_message_json("{not json")
    with pytest.raises(ContractError):
        parse_message_json(b"[]")


def test_errors_do_not_echo_rejected_values():
    secret = "sk-test-SECRET-VALUE-123"
    with pytest.raises(ContractError) as raised:
        parse_message(review_request(sender=secret))
    assert secret not in str(raised.value)
    with pytest.raises(ContractError) as raised:
        parse_message({**review_request(), "api_key": secret})
    assert secret not in str(raised.value)


def test_text_fields_are_bounded():
    rejects(challenge(reason="x" * 1001))
    rejects(challenge(evidence=[{"artifact_ref": "a", "excerpt": "x" * 501}]))
    rejects(challenge(evidence=[{"artifact_ref": "a"}] * 11))


# --- Replies must match what they answer ------------------------------------------------------


def test_matching_replies_validate():
    validate_reply(parse_message(review_request()), parse_message(review_report()))
    validate_reply(parse_message(challenge()), parse_message(resolution()))


@pytest.mark.parametrize(("make", "over", "match"), [
    (review_report, {"in_reply_to": "other"}, "does not reference"),
    (review_report, {"sender": "nova"}, "from the original recipient"),
    (review_report, {"recipient": "nova"}, "from the original recipient"),
    (review_report, {"task_id": "task-2"}, "same engagement"),
    (review_report, {"engagement_id": "eng-2"}, "same engagement"),
    (review_report, {"request_id": "req-2"}, "same engagement"),
])
def test_forged_or_mismatched_replies_are_rejected(make, over, match):
    original = parse_message(review_request())
    with pytest.raises(ContractError, match=match):
        validate_reply(original, parse_message(make(**over)))


def test_reply_type_must_match_the_original():
    with pytest.raises(ContractError, match="reply type"):
        validate_reply(parse_message(review_request()), parse_message(resolution(
            in_reply_to="m1", sender="scout", recipient="charlie")))
    with pytest.raises(ContractError, match="reply type"):
        validate_reply(parse_message(challenge()), parse_message(review_report(
            in_reply_to="m3", sender="orbit", recipient="cipher")))


def test_payloads_are_not_mutated_by_parsing():
    payload = review_report()
    before = copy.deepcopy(payload)
    parse_message(payload)
    assert payload == before


# --- Phase C1 changes no runtime behavior ----------------------------------------------------


def test_no_runtime_module_imports_the_community_package():
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        if "community" in path.relative_to(SRC).parts:
            continue
        if re.search(r"^\s*(from|import)\s+[.\w]*community\b", path.read_text(encoding="utf-8"), re.M):
            offenders.append(str(path.relative_to(SRC)))
    assert offenders == []
