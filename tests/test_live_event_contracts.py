"""The live event contract (schema 1.0): fixtures, per-event validation and stream invariants."""

from __future__ import annotations

import copy
import json

import pytest
from pydantic import ValidationError

from live_helpers import FIXTURES, collect, live_work, types
from orchestration_helpers import C, Script, delegate, finish
from viharbot.live_events import (
    EVENT_TYPES,
    TERMINAL_EVENT_TYPES,
    StreamInvariantError,
    check_stream,
    parse_event,
    serialize_event,
)

SCENARIOS = sorted(p.stem for p in FIXTURES.glob("*.json"))
DOCS = {name: json.loads((FIXTURES / f"{name}.json").read_text()) for name in SCENARIOS}
FAILURE_SCENARIOS = {"D_planner_failure", "E_agent_failure"}


def one_of_each() -> dict[str, dict]:
    found: dict[str, dict] = {}
    for doc in DOCS.values():
        for event in doc["events"]:
            found.setdefault(event["event_type"], event)
    return found


BASE = one_of_each()


def with_(event: dict, **path_values) -> dict:
    """A deep copy with dotted-path edits applied, e.g. with_(e, **{"data.status": "x"})."""
    copy_ = copy.deepcopy(event)
    for path, value in path_values.items():
        node = copy_
        *parents, leaf = path.split(".")
        for key in parents:
            node = node[key]
        if value is DELETE:
            del node[leaf]
        else:
            node[leaf] = value
    return copy_


DELETE = object()


# ---- the documented fixtures ------------------------------------------------------------------
def test_the_scenarios_are_the_five_the_diagnostics_variant_and_the_full_team():
    assert SCENARIOS == ["A_successful_request", "A_successful_request_with_diagnostics",
                         "B_policy_rejection", "C_evidence_supersession", "D_planner_failure",
                         "E_agent_failure", "F_full_team"]


def test_all_fourteen_event_types_have_a_complete_validated_fixture():
    assert set(BASE) == set(EVENT_TYPES) and len(EVENT_TYPES) == 14
    for event_type, raw in BASE.items():
        event = parse_event(raw)
        assert event.event_type == event_type
        assert event.schema_version == "1.0" and event.request_id.startswith("req-")


@pytest.mark.parametrize("name", SCENARIOS)
def test_every_scenario_is_a_valid_complete_stream(name):
    events = [parse_event(raw) for raw in DOCS[name]["events"]]
    terminal = check_stream(events)
    assert terminal == ("orchestration_failed" if name in FAILURE_SCENARIOS else "orchestration_completed")
    assert (events[-1].event_type in TERMINAL_EVENT_TYPES) and DOCS[name]["description"]


@pytest.mark.parametrize("name", SCENARIOS)
def test_fixtures_round_trip_exactly_and_are_single_line_json(name):
    for raw in DOCS[name]["events"]:
        line = serialize_event(parse_event(raw))
        assert "\n" not in line and json.loads(line) == raw


def test_failure_scenarios_never_contain_a_final_response():
    for name in FAILURE_SCENARIOS:
        assert "final_response" not in {e["event_type"] for e in DOCS[name]["events"]}


def test_the_final_response_fixture_is_a_complete_chat_response():
    final = BASE["final_response"]["data"]["response"]
    assert {"request_id", "findings", "assessment", "agent_activity"} <= set(final)
    assert final["request_id"] == BASE["final_response"]["request_id"]
    assert final["orchestration"]["mode"] == "iterative"


def test_diagnostics_fields_exist_only_in_the_diagnostics_variant():
    plain = json.dumps(DOCS["A_successful_request"]["events"])
    rich = DOCS["A_successful_request_with_diagnostics"]["events"]
    assert "rationale" not in plain and '"model_alias"' not in json.dumps(
        [e for e in DOCS["A_successful_request"]["events"] if e["event_type"] != "final_response"])
    planner = next(e for e in rich if e["event_type"] == "planner_completed")["data"]
    agent = next(e for e in rich if e["event_type"] == "agent_completed")["data"]
    assert planner["rationale"] and agent["model_alias"]


def test_documented_scenarios_match_what_the_real_loop_does():
    """A fixture that drifts from the application's real behavior fails here."""
    actual = collect(live_work(Script(delegate(C.RESEARCH), delegate(C.SECURITY_ASSESSMENT), finish())))
    assert types(actual) == [e["event_type"] for e in DOCS["A_successful_request"]["events"]]
    actual = collect(live_work(Script(delegate(C.SECURITY_ASSESSMENT), delegate(C.RESEARCH),
                                      delegate(C.SECURITY_ASSESSMENT), finish())))
    assert types(actual) == [e["event_type"] for e in DOCS["B_policy_rejection"]["events"]]
    actual = collect(live_work(Script(delegate(C.RESEARCH), delegate(C.SECURITY_ASSESSMENT),
                                      delegate(C.RESEARCH), delegate(C.SECURITY_ASSESSMENT), finish())))
    assert types(actual) == [e["event_type"] for e in DOCS["C_evidence_supersession"]["events"]]


# ---- every event rejects what it should --------------------------------------------------------
@pytest.mark.parametrize("event_type", EVENT_TYPES)
def test_unknown_fields_are_rejected_at_every_level(event_type):
    raw = BASE[event_type]
    for edit in ({"surprise": 1}, {"data.surprise": 1}, {"source.surprise": 1}, {"correlation.surprise": 1}):
        with pytest.raises(ValidationError):
            parse_event(with_(raw, **edit))


@pytest.mark.parametrize("event_type", EVENT_TYPES)
@pytest.mark.parametrize("field", ["schema_version", "event_id", "request_id", "sequence", "timestamp",
                                   "event_type", "source", "correlation", "data"])
def test_every_envelope_field_is_required(event_type, field):
    with pytest.raises(ValidationError):
        parse_event(with_(BASE[event_type], **{field: DELETE}))


@pytest.mark.parametrize("event_type", [t for t in EVENT_TYPES if t != "final_response"])
def test_every_required_data_field_is_required(event_type):
    required = {k for k in BASE[event_type]["data"] if k not in ("rationale", "model_alias")}
    assert required
    for key in required:
        with pytest.raises(ValidationError):
            parse_event(with_(BASE[event_type], **{f"data.{key}": DELETE}))


def test_event_type_and_data_must_agree():
    mismatched = with_(BASE["planner_started"], event_type="agent_completed")
    with pytest.raises(ValidationError):
        parse_event(mismatched)
    with pytest.raises(ValidationError):
        parse_event(with_(BASE["planner_started"], event_type="not_an_event"))


@pytest.mark.parametrize("edit", [
    {"schema_version": "2.0"}, {"event_id": "evt-1"}, {"event_id": "evt-000099"},
    {"sequence": 0}, {"sequence": -1}, {"timestamp": "yesterday"},
    {"timestamp": "2026-10-27T19:00:00+00:00"}, {"timestamp": "2026-13-45T25:61:61Z"},
    {"request_id": ""}, {"source.role": "wizard"}, {"correlation.decision_number": 0},
    {"correlation.execution_id": "run-1"}, {"correlation.parent_event_id": "evt-000900"},
])
def test_the_common_envelope_is_validated(edit):
    with pytest.raises(ValidationError):
        parse_event(with_(BASE["planner_started"], **edit))


@pytest.mark.parametrize("event_type,edit", [
    ("planner_started", {"data.status": "done"}),
    ("planner_started", {"data.remaining_budget_ms": -1}),
    ("planner_completed", {"data.action": "explore"}),
    ("planner_completed", {"data.capability": "shell"}),
    ("planner_completed", {"data.capability": "logistics_planning"}),  # planned-only, not implemented
    ("planner_completed", {"data.duration_ms": -0.5}),
    ("planner_completed", {"data.rationale": "x" * 301}),
    ("planner_failed", {"data.error_code": "boom: traceback at line 3"}),
    ("delegation_requested", {"data.target_identity": "orbit-prime"}),
    ("delegation_authorized", {"data.service": "shell"}),
    ("delegation_authorized", {"data.result_version": 0}),
    ("delegation_rejected", {"data.rejection_code": "because I said so"}),
    ("agent_started", {"data.capability": "creative_design"}),
    ("agent_completed", {"data.validated": False}),
    ("agent_completed", {"data.duration_ms": -1}),
    ("agent_failed", {"data.error_code": "KeyError: 'secret'"}),
    ("orchestration_completed", {"data.completion_kind": "model_decided"}),
    ("orchestration_completed", {"data.completion_reason": "because"}),
    ("orchestration_completed", {"data.completion_kind": "policy_budget"}),  # disagrees with status and reason
    ("orchestration_completed", {"data.status": "budget_exhausted"}),
    ("orchestration_completed", {"data.decisions": -1}),
    ("orchestration_failed", {"data.error_code": "oops"}),
    ("orchestration_failed", {"data.message": "m" * 201}),
    ("orchestration_failed", {"data.status": "completed"}),
])
def test_event_specific_values_are_validated(event_type, edit):
    with pytest.raises(ValidationError):
        parse_event(with_(BASE[event_type], **edit))


def test_a_finish_has_no_capability_and_a_delegation_has_one():
    finish_event = next(e for e in DOCS["A_successful_request"]["events"]
                        if e["event_type"] == "planner_completed" and e["data"]["action"] == "finish")
    assert finish_event["data"]["capability"] is None
    with pytest.raises(ValidationError):
        parse_event(with_(finish_event, **{"data.capability": "research"}))
    with pytest.raises(ValidationError):
        parse_event(with_(BASE["planner_completed"], **{"data.capability": None}))  # delegate needs one


def test_a_refused_finish_is_the_only_rejection_without_a_capability():
    refused = with_(BASE["delegation_rejected"], **{"data.capability": None, "data.rejection_code": "mandatory_missing"})
    assert parse_event(refused).data.capability is None
    with pytest.raises(ValidationError):
        parse_event(with_(BASE["delegation_rejected"], **{"data.capability": "planning"}))


# ---- identities, versions and dependencies -----------------------------------------------------
@pytest.mark.parametrize("edit", [
    {"source.identity_id": "ghost", "source.display_name": "Ghost"},
    {"source.display_name": "Wrong Name"},
    {"source.identity_id": "application"},  # the system identity needs the application role
])
def test_sources_must_be_registered_identities_or_the_documented_system_identity(edit):
    with pytest.raises(ValidationError):
        parse_event(with_(BASE["agent_started"], **edit))


def test_only_charlie_orchestrates_and_only_the_application_uses_the_system_identity():
    with pytest.raises(ValidationError):
        parse_event(with_(BASE["planner_started"], **{"source.identity_id": "nova", "source.display_name": "Nova"}))
    with pytest.raises(ValidationError):
        parse_event(with_(BASE["delegation_authorized"], **{"source.display_name": "Someone"}))
    assert parse_event(BASE["delegation_authorized"]).source.identity_id == "application"


def test_dependency_references_are_validated_and_never_empty():
    event = BASE["dependency_recorded"]
    assert event["data"]["dependencies"] and event["data"]["consumer"]["result_version"] >= 1
    for edit in ({"data.dependencies": []},
                 {"data.dependencies": [{**event["data"]["dependencies"][0], "dependency_type": "magic"}]},
                 {"data.dependencies": [{**event["data"]["dependencies"][0], "identity_id": "ghost"}]},
                 {"data.dependencies": [{**event["data"]["dependencies"][0], "result_version": 0}]},
                 {"data.consumer": {**event["data"]["consumer"], "capability": "shell"}}):
        with pytest.raises(ValidationError):
            parse_event(with_(event, **edit))


def test_a_supersession_needs_a_newer_version_and_real_affected_results():
    event = BASE["evidence_superseded"]
    assert event["data"]["replacement_version"] == event["data"]["superseded_version"] + 1
    for edit in ({"data.replacement_version": event["data"]["superseded_version"]},
                 {"data.replacement_version": 1}, {"data.affected_results": []},
                 {"data.affected_results": [{"identity_id": "ghost", "capability": "research", "result_version": 1}]}):
        with pytest.raises(ValidationError):
            parse_event(with_(event, **edit))


def test_the_final_response_must_be_a_valid_chat_response():
    for edit in ({"data.response.findings": DELETE}, {"data.response.agent_activity": "none"},
                 {"data.response.extra_field": 1}, {"data.response": {}}):
        with pytest.raises(ValidationError):
            parse_event(with_(BASE["final_response"], **edit))


def test_an_event_may_only_name_a_parent_that_came_before_it():
    with pytest.raises(ValidationError):
        parse_event(with_(BASE["planner_completed"], **{"correlation.parent_event_id": "evt-000009"}))


# ---- stream invariants ---------------------------------------------------------------------------
def stream(name="A_successful_request"):
    return [parse_event(raw) for raw in DOCS[name]["events"]]


def rebuilt(raw_events):
    return [parse_event(raw) for raw in raw_events]


def test_a_valid_stream_passes_and_a_truncated_one_is_incomplete():
    assert check_stream(stream()) == "orchestration_completed"
    with pytest.raises(StreamInvariantError, match="terminal"):
        check_stream(stream()[:-2])
    assert check_stream(stream()[:-2], require_terminal=False) is None


@pytest.mark.parametrize("mutation,message", [
    (lambda e: e[:3] + e[4:], "sequence"),                                   # a gap
    (lambda e: [e[0]] + e,  "sequence"),                                     # a duplicate
    (lambda e: list(reversed(e)), "sequence"),                               # out of order
])
def test_sequence_must_run_one_to_n(mutation, message):
    with pytest.raises(StreamInvariantError, match=message):
        check_stream(mutation(stream()))


def test_events_after_a_terminal_event_are_rejected():
    raw = copy.deepcopy(DOCS["A_successful_request"]["events"])
    extra = with_(raw[0], **{"sequence": len(raw) + 1, "event_id": f"evt-{len(raw) + 1:06d}",
                             "correlation.parent_event_id": None})
    with pytest.raises(StreamInvariantError, match="terminal"):
        check_stream(rebuilt(raw + [extra]))


def test_two_terminal_events_are_rejected():
    raw = copy.deepcopy(DOCS["D_planner_failure"]["events"])
    again = with_(raw[-1], **{"sequence": len(raw) + 1, "event_id": f"evt-{len(raw) + 1:06d}"})
    with pytest.raises(StreamInvariantError):
        check_stream(rebuilt(raw + [again]))


def test_completion_must_immediately_follow_the_final_response():
    events = stream()
    assert [e.event_type for e in events[-2:]] == ["final_response", "orchestration_completed"]
    raw = copy.deepcopy(DOCS["A_successful_request"]["events"])
    swapped = [raw[i] for i in range(len(raw) - 2)] + [raw[-1]]
    swapped[-1] = with_(swapped[-1], **{"sequence": len(swapped), "event_id": f"evt-{len(swapped):06d}",
                                        "correlation.parent_event_id": swapped[-2]["event_id"]})
    with pytest.raises(StreamInvariantError):
        check_stream(rebuilt(swapped))


def test_a_failed_run_cannot_have_a_final_response():
    success = copy.deepcopy(DOCS["A_successful_request"]["events"])
    failure = copy.deepcopy(DOCS["D_planner_failure"]["events"])
    mixed = success[:16] + [with_(failure[-1], **{"sequence": 17, "event_id": "evt-000017",
                                                  "request_id": success[0]["request_id"],
                                                  "correlation.parent_event_id": "evt-000016"})]
    with pytest.raises(StreamInvariantError):
        check_stream(rebuilt(mixed))


def test_a_stream_belongs_to_exactly_one_request():
    raw = copy.deepcopy(DOCS["A_successful_request"]["events"])
    raw[3] = with_(raw[3], request_id="req-other")
    with pytest.raises(StreamInvariantError, match="one request"):
        check_stream(rebuilt(raw))


def test_two_runs_never_share_ids_or_sequence_state():
    first = collect(live_work(Script(delegate(C.RESEARCH), delegate(C.SECURITY_ASSESSMENT), finish()),
                              request_id="req-one"), request_id="req-one")
    second = collect(live_work(Script(delegate(C.RESEARCH), delegate(C.SECURITY_ASSESSMENT), finish()),
                               request_id="req-two"), request_id="req-two")
    assert check_stream(first) and check_stream(second)
    assert {e.request_id for e in first} == {"req-one"} and {e.request_id for e in second} == {"req-two"}
    assert [e.sequence for e in first] == [e.sequence for e in second] == list(range(1, len(first) + 1))
