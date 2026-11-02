"""Sage in the live conversation: event schema, stream, final response, and the Enterprise
Autonomous AI Adoption scenario. Offline: scripted planner and fake transport, no model.

The scenario test shows the plumbing the acceptance scenario needs: Sage's analysis is built from
the security result, reaches synthesis, and a synthesis that uses it can change the answer. It does
not show that a real model does so; that is measured separately (docs/sage-service.md)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from orchestration_helpers import C, Script, delegate, finish
from pipeline_fixtures import RID, FakeSend, response
from viharbot import web_app
from viharbot.iterative_execution import run_iterative_pipeline
from viharbot.live_events import EventCapability, Service, check_stream, parse_event
from viharbot.live_stream import EventEmitter, drive
from viharbot.live_hooks import CancelToken
from viharbot.orchestration_view import build_orchestration_meta
from viharbot.schemas import SageAnalysis
import typing

QUESTION = "Evaluate Enterprise Autonomous AI Adoption"
SPECIALISTS = {"fact_check": "u-f", "source_quality": "u-s", "privacy_review": "u-p",
               "synthesis": "u-y", "sage": "u-sage"}
R, S, SG, Y = C.RESEARCH, C.SECURITY_ASSESSMENT, C.PHILOSOPHICAL_ANALYSIS, C.SYNTHESIS

# The security result recommends something with human consequences, as in the acceptance scenario.
MONITORING = "Monitor all employee interactions with autonomous agents."


class ScenarioSend(FakeSend):
    """Security recommends blanket monitoring. Sage objects to it on proportionality. Synthesis
    answers from whatever context it was actually given, so its answer depends on Sage being there."""

    def __init__(self):
        super().__init__({
            "security": self._security, "synthesis": self._synthesis})

    @staticmethod
    def _security(raw):
        return {**raw, "assessment": {**raw["assessment"], "recommendations": [MONITORING]}}

    @staticmethod
    def _synthesis(raw):
        return raw  # replaced in __call__, which can see the payload

    def __call__(self, url, payload, request_id, destination):
        reply = super().__call__(url, payload, request_id, destination)
        if destination == "synthesis":
            context = payload["context"]
            recommends = context["security"]["assessment"]["recommendations"] if "assessment" in context[
                "security"] else context["security"]["recommendations"]
            sage = context.get("sage")
            answer = (f"Adopt, with proportional safeguards: {sage['recommendations_for_charlie'][0]}"
                      if sage else f"Adopt, and {recommends[0].lower()}")
            reply["result"] = {**reply["result"], "answer": answer}
        return reply


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setenv("WEB_AUTH_ENABLED", "false")
    monkeypatch.setenv("CHARLIE_PLANNER_MODEL", "planner/alias")
    monkeypatch.setenv("LITELLM_API_KEY", "test-key")
    monkeypatch.setenv("SAGE_URL", "http://sage:8000/process")
    holder = SimpleNamespace(actions=[delegate(R), delegate(S), delegate(SG), delegate(Y), finish()],
                             send=None)
    real = web_app.run_iterative_pipeline

    def wrapper(question, request_id, **kwargs):
        holder.send = ScenarioSend()
        return real(question, request_id,
                    **{**kwargs, "send": holder.send, "decide": Script(*holder.actions)})

    monkeypatch.setattr(web_app, "run_iterative_pipeline", wrapper)
    return TestClient(web_app.app), holder


def stream(client, message=QUESTION):
    reply = client.post("/api/chat/stream", json={"message": message})
    assert reply.status_code == 200
    return [json.loads(line[6:]) for block in reply.text.split("\n\n") for line in block.splitlines()
            if line.startswith("data: ")]


# ---- the event contract knows Sage -----------------------------------------------------------------
def test_the_event_schema_names_sage_and_its_capability():
    assert "sage" in typing.get_args(Service)
    assert "philosophical_analysis" in typing.get_args(typing.get_args(EventCapability)[0])


def test_sage_events_validate_and_wrong_ones_do_not():
    events = stream_events()
    sage_events = [e for e in events if e.event_type in {"delegation_authorized", "agent_started"}
                   and e.data.capability == "philosophical_analysis"]
    assert [e.event_type for e in sage_events] == ["delegation_authorized", "agent_started"]
    assert sage_events[0].data.service == "sage" and sage_events[0].data.target_identity == "sage"
    assert sage_events[1].source.identity_id == "sage" and sage_events[1].source.display_name == "Sage"
    raw = json.loads(sage_events[0].model_dump_json())
    for edit in ({"service": "atlas"}, {"target_identity": "ghost"}):
        bad = {**raw, "data": {**raw["data"], **edit}}
        with pytest.raises(ValidationError):
            parse_event(bad)


def stream_events():
    events = []
    send = ScenarioSend()

    def work(emitter, cancel):
        iterative = run_iterative_pipeline(
            QUESTION, RID, research_url="u-r", security_url="u-sec", specialist_urls=SPECIALISTS,
            send=send, decide=Script(*[delegate(c) for c in (R, S, SG, Y)], finish()),
            emit=emitter, cancel=cancel)
        response_ = web_app.build_chat_response(
            iterative.to_legacy_dict(), RID, build_orchestration_meta(iterative))
        state = iterative.orchestration
        return response_, {
            "status": state.status, "completion_kind": state.completion_kind,
            "completion_reason": state.completion_reason, "decisions": state.decision_number,
            "executions": len(state.completed_steps)}

    emitter = EventEmitter(RID, events.append)
    drive(emitter, work, CancelToken())
    return events


def test_the_whole_stream_with_sage_obeys_the_ordering_and_terminal_rules():
    events = stream_events()
    check_stream(events)
    kinds = [e.event_type for e in events]
    assert kinds[-2:] == ["final_response", "orchestration_completed"]
    completed = [e for e in events if e.event_type == "agent_completed"]
    assert [e.data.capability for e in completed] == [
        "research", "security_assessment", "philosophical_analysis", "synthesis"]
    sage_done = completed[2]
    assert sage_done.source.identity_id == "sage" and sage_done.data.result_version == 1


def test_the_dependencies_sage_read_are_recorded_as_required_or_optional():
    events = stream_events()
    recorded = [e for e in events if e.event_type == "dependency_recorded"
                and e.data.consumer.capability == "philosophical_analysis"]
    assert len(recorded) == 1
    deps = {d.capability: d.dependency_type for d in recorded[0].data.dependencies}
    assert deps == {"research": "required", "security_assessment": "optional"}
    synthesis = next(e for e in events if e.event_type == "dependency_recorded"
                     and e.data.consumer.capability == "synthesis")
    assert {d.capability: d.dependency_type for d in synthesis.data.dependencies}[
        "philosophical_analysis"] == "optional"


def test_a_stale_sage_run_is_reported_when_new_evidence_supersedes_it():
    events = []
    send = ScenarioSend()

    def work(emitter, cancel):
        iterative = run_iterative_pipeline(
            QUESTION, RID, research_url="u-r", security_url="u-sec", specialist_urls=SPECIALISTS,
            send=send, decide=Script(*[delegate(c) for c in (R, SG, S)], finish()),
            emit=emitter, cancel=cancel)
        return web_app.build_chat_response(
            iterative.to_legacy_dict(), RID, build_orchestration_meta(iterative)), {
            "status": iterative.orchestration.status,
            "completion_kind": iterative.orchestration.completion_kind,
            "completion_reason": iterative.orchestration.completion_reason,
            "decisions": iterative.orchestration.decision_number,
            "executions": len(iterative.orchestration.completed_steps)}

    drive(EventEmitter(RID, events.append), work, CancelToken())
    final = next(e for e in events if e.event_type == "final_response")
    assert final.data.response.sage is None  # Sage never saw security, so it is not shown
    meta = final.data.response.orchestration
    sage_step = next(step for step in meta.steps if step.capability == "philosophical_analysis")
    assert sage_step.in_final_result is False


# ---- the public response and the stream's final response ------------------------------------------
def test_the_final_response_carries_sage_when_it_ran_and_null_when_it_did_not(live):
    client, holder = live
    final = next(e for e in stream(client) if e["event_type"] == "final_response")
    sage = final["data"]["response"]["sage"]
    assert SageAnalysis.model_validate(sage).executive_summary == "Monitoring staff changes trust."
    holder.actions = [delegate(R), delegate(S), delegate(Y), finish()]
    final = next(e for e in stream(client) if e["event_type"] == "final_response")
    assert final["data"]["response"]["sage"] is None


def test_the_ordinary_chat_endpoint_returns_sage_too(live):
    client, _ = live
    body = client.post("/api/chat", json={"message": QUESTION}).json()
    assert body["sage"]["recommendations_for_charlie"] == ["Weigh monitoring against proportionality."]
    assert body["synthesis"]["answer"].startswith("Adopt, with proportional safeguards")


def test_the_sage_field_is_optional_so_older_responses_still_validate():
    from viharbot.chat_models import ChatResponse

    field = ChatResponse.model_fields["sage"]
    assert field.default is None and not field.is_required()


# ---- the acceptance scenario's plumbing ---------------------------------------------------------
def test_sage_changes_what_synthesis_is_given_and_so_can_change_the_answer(live):
    client, holder = live
    with_sage = client.post("/api/chat", json={"message": QUESTION}).json()["synthesis"]["answer"]
    holder.actions = [delegate(R), delegate(S), delegate(Y), finish()]
    without = client.post("/api/chat", json={"message": QUESTION}).json()["synthesis"]["answer"]
    assert without == f"Adopt, and {MONITORING.lower()}"  # security's recommendation, unqualified
    assert with_sage != without and "proportional safeguards" in with_sage


def test_sage_was_given_the_security_recommendation_it_is_asked_to_weigh(live):
    client, holder = live
    client.post("/api/chat", json={"message": QUESTION})
    sage_request = holder.send.payload("sage")
    assert sage_request["context"]["security"]["recommendations"] == [MONITORING]
    assert sage_request["question"] == QUESTION


def test_a_deployment_without_sage_runs_the_same_scenario_without_it(live, monkeypatch):
    client, holder = live
    monkeypatch.delenv("SAGE_URL")
    reply = client.post("/api/chat", json={"message": QUESTION})
    assert reply.status_code == 200 and reply.json()["sage"] is None
    assert "sage" not in holder.send.order  # the delegation was refused, never sent
    assert reply.json()["synthesis"]["answer"].startswith("Adopt, and")


# ---- the page --------------------------------------------------------------------------------------
from test_ui_orchestration import (  # noqa: E402
    HOSTILE, all_text, chat_body, elements, needs_node, respond, with_class)


@needs_node
def test_the_page_shows_sage_only_when_a_sage_result_is_returned():
    assert "Sage" not in all_text(respond(chat_body())["final"]["tree"])
    sage = response("sage")["result"]
    sage["risks_and_tradeoffs"] = [{"statement": "Monitoring erodes trust.", "claim_type": "reasoned_interpretation",
                                    "evidence_refs": ["security"], "competing_values": ["safety", "privacy"]}]
    sage["ethical_considerations"][0]["claim_type"] = "evidence_supported"
    sage["ethical_considerations"][0]["evidence_refs"] = ["security"]
    tree = respond(chat_body(sage=sage))["final"]["tree"]
    text = all_text(tree)
    assert "Sage · human and organizational perspective" in text
    assert "Reasoning over the team’s results, not new research." in text
    assert "Monitoring staff changes trust." in text
    assert "[Interpretation; from security] Monitoring erodes trust. Between: safety vs privacy." in text
    assert "[Supported by supplied results; from security] Proportionality matters." in text
    assert "For Charlie" in text and "Weigh monitoring against proportionality." in text
    assert with_class(tree, "sage-section")


@needs_node
def test_hostile_text_in_a_sage_result_is_shown_as_text_never_as_markup():
    sage = response("sage")["result"]
    sage["executive_summary"] = HOSTILE
    sage["recommendations_for_charlie"] = [HOSTILE]
    tree = respond(chat_body(sage=sage))["final"]["tree"]
    assert HOSTILE in all_text(tree)
    assert not [n for n in elements(tree) if n["tag"] in {"script", "img", "iframe"}]


@needs_node
def test_sage_has_a_portrait_a_name_and_a_label_in_the_live_page():
    html = (__import__("test_ui_orchestration").INDEX).read_text()
    assert 'sage: "Sage"' in html and 'philosophical_analysis: "Perspective"' in html
    assert 'sage: "/images/Sage_headshot.png"' in html
