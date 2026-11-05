"""The documented streams are what the application actually emits (issue #53).

Fixtures are regenerated from the real orchestration loop (mocked inference, production emitter) and
compared, so a fixture that drifts from the code fails here instead of going stale. No network, no
model call, no credentials.
"""

from __future__ import annotations

import pytest

from live_conformance_helpers import GENERATED, SCENARIOS, fixture, regenerate, render, stable
from live_helpers import FIXTURES
from orchestration_helpers import C, Script, delegate, finish
from test_live_stream import frames_of, live, post  # noqa: F401  (live is a pytest fixture)
from viharbot.chat_models import ChatResponse
from viharbot.live_events import EVENT_TYPES, check_stream, parse_event

SUCCESS = [n for n in SCENARIOS if not n.startswith(("D_", "E_"))]
# deck is sent only when a deck was requested, so no ordinary stream carries it.
ALWAYS_PRESENT = set(ChatResponse.model_fields) - {"deck"}
OPTIONAL_SECTIONS = ("fact_check", "source_quality", "privacy_review", "sage", "synthesis")


def final_response_of(events):
    return next(e for e in events if e["event_type"] == "final_response")["data"]["response"]


# ---- the fixtures are what the code emits ----------------------------------------------------
def test_the_scenario_list_is_the_fixture_directory():
    assert sorted(SCENARIOS) == sorted(p.stem for p in FIXTURES.glob("*.json"))


@pytest.mark.parametrize("name", SCENARIOS)
def test_every_documented_stream_matches_what_the_real_loop_emits(name):
    """Timestamps and durations are ignored; every id, source, correlation, status, code and body
    (including the whole nested ChatResponse) must match."""
    assert stable(regenerate(name)) == stable(fixture(name)["events"])


@pytest.mark.parametrize("name", sorted(GENERATED))
def test_generated_fixtures_match_the_code_exactly_and_byte_for_byte(name):
    assert regenerate(name) == fixture(name)["events"]
    assert render(name) == (FIXTURES / f"{name}.json").read_text()


def test_the_scenarios_together_cover_all_fourteen_event_types():
    seen = {e["event_type"] for name in SCENARIOS for e in fixture(name)["events"]}
    assert seen == set(EVENT_TYPES) and len(EVENT_TYPES) == 14


@pytest.mark.parametrize("name", SCENARIOS)
def test_every_scenario_ends_with_exactly_one_terminal_and_nothing_after(name):
    events = [parse_event(raw) for raw in fixture(name)["events"]]
    terminal = check_stream(events)
    assert terminal == ("orchestration_failed" if name.startswith(("D_", "E_")) else "orchestration_completed")
    assert [e.event_type for e in events].count(terminal) == 1 and events[-1].event_type == terminal
    # on success the answer immediately precedes the completion; on failure there is no answer
    kinds = [e.event_type for e in events]
    if terminal == "orchestration_completed":
        assert kinds[-2:] == ["final_response", "orchestration_completed"]
    else:
        assert "final_response" not in kinds


# ---- the nested ChatResponse is complete, never a placeholder ----------------------------------
@pytest.mark.parametrize("name", SUCCESS)
def test_every_success_fixture_carries_every_chat_response_field(name):
    """Adding a ChatResponse field makes the stale fixtures fail here until they are updated."""
    response = final_response_of(fixture(name)["events"])
    assert set(response) == ALWAYS_PRESENT
    parsed = ChatResponse.model_validate(response)
    assert parsed.request_id == fixture(name)["events"][0]["request_id"]


def test_the_full_team_response_has_every_optional_section_populated():
    response = final_response_of(fixture("F_full_team")["events"])
    for section in OPTIONAL_SECTIONS:
        assert response[section], f"{section} is empty in the full-team ChatResponse"
    parsed = ChatResponse.model_validate(response)
    assert parsed.sage is not None and parsed.synthesis is not None
    assert {a.agent for a in parsed.agent_activity} >= {
        "research", "security", "fact-check", "source-quality", "privacy-review", "synthesis", "sage"}


def test_the_full_team_run_delegates_to_all_seven_specialists_in_order():
    events = fixture("F_full_team")["events"]
    delegated = [e["data"]["capability"] for e in events if e["event_type"] == "delegation_authorized"]
    assert delegated == ["research", "fact_check", "source_quality", "security_assessment",
                         "privacy_review", "philosophical_analysis", "synthesis"]
    assert events[-1]["data"]["completion_kind"] == "charlie_approved"


# ---- over HTTP: framing, headers, correlation and the same complete response ------------------
def test_the_full_team_streams_over_http_exactly_as_documented(live):  # noqa: F811
    client, state, monkeypatch = live
    monkeypatch.setenv("SAGE_URL", "http://sage:8000/process")  # Sage is offered only when configured
    monkeypatch.setenv("VIHARBOT_ORCH_MAX_EXECUTIONS", "7")  # seven specialists, default is six
    state.decide = lambda question: Script(
        delegate(C.RESEARCH), delegate(C.FACT_CHECK), delegate(C.SOURCE_QUALITY),
        delegate(C.SECURITY_ASSESSMENT), delegate(C.PRIVACY_REVIEW),
        delegate(C.PHILOSOPHICAL_ANALYSIS), delegate(C.SYNTHESIS), finish())
    reply = post(client)
    assert reply.status_code == 200
    assert reply.headers["content-type"].startswith("text/event-stream")
    assert reply.headers["cache-control"] == "no-cache" and reply.headers["x-accel-buffering"] == "no"
    request_id = reply.headers["x-request-id"]

    frames = frames_of(reply.content)  # asserts id/event/data framing, single-line JSON, blank-line ends
    events = [parse_event(f["data"]) for f in frames]
    assert [f["id"] for f in frames] == [e.event_id for e in events]
    assert [f["event"] for f in frames] == [e.event_type for e in events]
    assert check_stream(events) == "orchestration_completed"
    assert {e.request_id for e in events} == {request_id}
    assert [e.sequence for e in events] == list(range(1, len(events) + 1))

    response = next(e for e in frames if e["event"] == "final_response")["data"]["data"]["response"]
    assert response["request_id"] == request_id and set(response) == ALWAYS_PRESENT
    for section in OPTIONAL_SECTIONS:
        assert response[section], section
    assert ChatResponse.model_validate(response).sage is not None
