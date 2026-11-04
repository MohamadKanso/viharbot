"""Education activity events: declared and validated, never terminal, never emitted yet."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from viharbot.live_events import (
    EDUCATION_EVENT_TYPES, EVENT_TYPES, TERMINAL_EVENT_TYPES, check_stream, parse_event)

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "live_events"
EVENTS = {e["event_type"]: e for e in json.loads((FIXTURES / "education" / "education_events.json").read_text())["events"]}
FORBIDDEN = re.compile(r"prompt|body|text|email|name|secret|token|key|url", re.I)


def test_the_chat_registry_is_unchanged_and_the_education_registry_is_separate():
    assert len(EVENT_TYPES) == 14 and len(EDUCATION_EVENT_TYPES) == 11
    assert not set(EVENT_TYPES) & set(EDUCATION_EVENT_TYPES)
    assert set(EVENTS) == set(EDUCATION_EVENT_TYPES)


@pytest.mark.parametrize("event_type", EDUCATION_EVENT_TYPES)
def test_each_education_event_validates_and_is_not_terminal(event_type):
    event = parse_event(EVENTS[event_type])
    assert event.event_type == event_type and event_type not in TERMINAL_EVENT_TYPES


@pytest.mark.parametrize("event_type", EDUCATION_EVENT_TYPES)
def test_education_events_reject_unknown_fields_and_missing_data(event_type):
    raw = EVENTS[event_type]
    top = copy.deepcopy(raw)
    top["surprise"] = 1
    inner = copy.deepcopy(raw)
    inner["data"]["surprise"] = 1
    for bad in (top, inner):
        with pytest.raises(ValidationError):
            parse_event(bad)
    for key in raw["data"]:
        bad = copy.deepcopy(raw)
        del bad["data"][key]
        with pytest.raises(ValidationError):
            parse_event(bad)


@pytest.mark.parametrize("event_type", EDUCATION_EVENT_TYPES)
def test_education_data_carries_no_free_text_fields(event_type):
    assert not [k for k in EVENTS[event_type]["data"] if FORBIDDEN.search(k)]


@pytest.mark.parametrize("value", ["Jane Doe", "jane@example.com", "x" * 65, "UPPER", ""])
def test_opaque_ids_reject_free_text_and_personal_identifiers(value):
    bad = copy.deepcopy(EVENTS["lesson.started"])
    bad["data"]["learner_ref"] = value
    with pytest.raises(ValidationError):
        parse_event(bad)


def test_education_events_never_end_a_stream():
    events = [parse_event(e) for e in EVENTS.values()]
    assert check_stream(events, require_terminal=False) is None


def test_every_existing_fixture_still_parses():
    for path in FIXTURES.glob("*.json"):
        for raw in json.loads(path.read_text())["events"]:
            assert raw["event_type"] in EVENT_TYPES and parse_event(raw)


def test_the_docs_list_exactly_the_education_events_as_non_terminal():
    text = (ROOT / "docs" / "live-observability.md").read_text()
    rows = re.findall(r"^\| `([a-z_.]+)` \|.*\| no \|$", text, re.M)
    assert [r for r in rows if "." in r] == list(EDUCATION_EVENT_TYPES)
    assert "not yet emitted" in (ROOT / "docs" / "education-agents.md").read_text()
