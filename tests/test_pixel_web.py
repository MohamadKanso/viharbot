"""Pixel as the web service sees it: the deck step, deck downloads, the team-models page data,
the chat page with inlined design tokens, the live stream, and each agent's /model route."""

from __future__ import annotations

import importlib
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from pipeline_fixtures import ASSESSMENT, FINDINGS
from viharbot import pixel_execution, research_agent, security_agent, web_app
from viharbot.chat_models import DeckInfo
from viharbot.pixel.flag import PPT_FLAG
from viharbot.pixel.state import deck_dir  # noqa: F401  (keeps the import path honest)
from viharbot.schemas import DeckResult

tm = importlib.import_module("viharbot.team_models")

PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
CSP = "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"
DECK_ID = "deck-0123456789ab"
DECK_RESULT = DeckResult(deck_id=DECK_ID, slide_count=3, rejected_slides=["S9"], warnings=["w"], model_calls=2)


def envelope(request_id: str, result: dict | None = None, agent: str = "pixel") -> dict:
    return {"request_id": request_id, "agent": agent, "requested_model_alias": "pixel/alias",
            "result": result if result is not None else DECK_RESULT.model_dump()}


class Send:
    """A fake transport that records calls and returns a prepared reply."""

    def __init__(self, reply=None, error: Exception | None = None):
        self.reply, self.error, self.calls = reply, error, []

    def __call__(self, url, payload, request_id, destination, **kwargs):
        self.calls.append({"url": url, "payload": payload, "request_id": request_id, "destination": destination})
        if self.error:
            raise self.error
        return self.reply(request_id) if callable(self.reply) else (self.reply or envelope(request_id))


class Reporter:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return lambda **kw: self.events.append((name, kw))

    @property
    def names(self):
        return [n for n, _ in self.events]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMORY_DIR", str(tmp_path))
    monkeypatch.setenv("WEB_AUTH_ENABLED", "false")
    monkeypatch.setenv(PPT_FLAG, "true")
    monkeypatch.setenv("PIXEL_URL", "http://pixel:8000/process")
    for name in ("CHARLIE_PLANNER_MODEL", "LITELLM_API_KEY", "LITELLM_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setitem(tm._cache, "at", 0.0)
    monkeypatch.setitem(tm._cache, "value", None)


def pipeline_result():
    return {"request_id": "r", "agent_activity": [],
            "research": {"findings": FINDINGS}, "security": {"assessment": ASSESSMENT},
            "synthesis": {"result": {"answer": "a", "key_points": ["k"], "confidence": "high", "caveats": []}}}


# ---- wants_deck / pixel_authorized -------------------------------------------------------------
@pytest.mark.parametrize("message", [
    "give me a slide deck", "Make SLIDES for the board", "export to PowerPoint", "a power point please",
    "save as pptx", "build a .ppt", "I need a presentation on passkeys"])
def test_wants_deck_positive(message):
    assert pixel_execution.wants_deck(message)


@pytest.mark.parametrize("message", [
    "explain passkeys", "what is a slider control", "the deck is stacked against us", "pptxx is not a word",
    "slip and slide"])
def test_wants_deck_negative(message):
    assert not pixel_execution.wants_deck(message)


def bindings(binding):
    return lambda: SimpleNamespace(lookup=lambda capability: binding)


@pytest.mark.parametrize("binding, expected", [
    (SimpleNamespace(enabled=True, identity_id="pixel"), True),
    (SimpleNamespace(enabled=False, identity_id="pixel"), False),
    (SimpleNamespace(enabled=True, identity_id="nobody"), False),
    (None, False),
])
def test_pixel_authorized_needs_an_enabled_binding_for_a_registered_identity(monkeypatch, binding, expected):
    monkeypatch.setattr(pixel_execution, "default_bindings", bindings(binding))
    assert pixel_execution.pixel_authorized() is expected


def test_the_shipped_bindings_authorize_pixel():
    assert pixel_execution.pixel_authorized()


# ---- run_pixel_stage ---------------------------------------------------------------------------
def test_success_returns_public_deck_info_and_an_activity_row():
    send = Send()
    deck, activity = pixel_execution.run_pixel_stage("make slides", pipeline_result(), "req-1", send=send)
    assert deck == DeckInfo(
        status="complete", deck_id=DECK_ID, slide_count=3, download_url=f"/api/decks/{DECK_ID}",
        view_url=f"/api/decks/{DECK_ID}/view", rejected_slides=["S9"], warnings=["w"])
    assert activity["agent"] == "pixel" and activity["status"] == "complete"
    call = send.calls[0]
    assert call["destination"] == "pixel" and call["url"] == "http://pixel:8000/process"
    assert call["payload"]["question"] == "make slides"
    assert call["payload"]["context"]["research"] == FINDINGS
    assert call["payload"]["context"]["synthesis"]["answer"] == "a"


def test_failure_is_reported_in_deck_info_and_a_failed_activity():
    deck, activity = pixel_execution.run_pixel_stage(
        "make slides", pipeline_result(), "req-1", send=Send(error=RuntimeError("PRIVATE")))
    assert deck.status == "failed" and deck.error == "The slide deck could not be generated"
    assert "PRIVATE" not in deck.model_dump_json()
    assert activity["status"] == "failed" and activity["agent"] == "pixel"


@pytest.mark.parametrize("reply", [
    lambda rid: envelope("other-request"),
    lambda rid: envelope(rid, agent="research"),
    lambda rid: envelope(rid, result={"nope": 1}),
])
def test_a_reply_that_does_not_match_the_request_fails_the_step(reply):
    deck, _ = pixel_execution.run_pixel_stage("s", pipeline_result(), "req-1", send=Send(reply=reply))
    assert deck.status == "failed"


def test_an_unauthorized_step_fails_without_calling_the_service(monkeypatch):
    monkeypatch.setattr(pixel_execution, "default_bindings", bindings(None))
    send = Send()
    deck, activity = pixel_execution.run_pixel_stage("s", pipeline_result(), "r", send=send)
    assert deck.status == "failed" and "not authorized" in deck.error
    assert activity is None and send.calls == []


LIVE = dict(decision_number=2, execution_id="exec-3")
DEPS = [{"identity_id": "nova", "capability": "research", "result_version": 1, "dependency_type": "optional"}]


def test_a_live_reporter_sees_the_team_events_in_order():
    reporter = Reporter()
    pixel_execution.run_pixel_stage("s", pipeline_result(), "r", send=Send(), reporter=reporter,
                                    dependencies=DEPS, **LIVE)
    assert reporter.names == ["delegation_authorized", "agent_started", "agent_completed", "dependency_recorded"]
    authorized, started, completed, recorded = (kw for _, kw in reporter.events)
    assert authorized["target_identity"] == "pixel" and authorized["capability"] == "slide_generation"
    assert started["identity_id"] == "pixel" and completed["model_alias"] == "pixel/alias"
    assert recorded["consumer"]["identity_id"] == "pixel" and recorded["dependencies"] == DEPS
    assert all(kw["decision_number"] == 2 and kw["execution_id"] == "exec-3" for kw in (authorized, started))


def test_no_dependency_event_without_dependencies():
    reporter = Reporter()
    pixel_execution.run_pixel_stage("s", pipeline_result(), "r", send=Send(), reporter=reporter, **LIVE)
    assert reporter.names == ["delegation_authorized", "agent_started", "agent_completed"]


def test_a_live_failure_reports_agent_failed():
    reporter = Reporter()
    pixel_execution.run_pixel_stage("s", pipeline_result(), "r", send=Send(error=OSError("x")),
                                    reporter=reporter, **LIVE)
    assert reporter.names == ["delegation_authorized", "agent_started", "agent_failed"]
    assert reporter.events[-1][1]["error_code"] == "agent_execution_failed"


def test_a_reporter_without_a_decision_context_is_not_used():
    reporter = Reporter()
    pixel_execution.run_pixel_stage("s", pipeline_result(), "r", send=Send(), reporter=reporter)
    assert reporter.events == []


def test_the_default_transport_goes_through_post_json(monkeypatch):
    seen = {}

    def fake_post_json(url, payload, request_id, destination, *, timeout_cap=90, **kw):
        seen.update(url=url, timeout_cap=timeout_cap)
        return envelope(request_id)

    monkeypatch.setattr(pixel_execution, "post_json", fake_post_json)
    deck, _ = pixel_execution.run_pixel_stage("s", pipeline_result(), "r")
    assert deck.status == "complete" and seen["timeout_cap"] == pixel_execution.PIXEL_TIMEOUT_SECONDS


# ---- fetch_deck --------------------------------------------------------------------------------
class Body:
    def __init__(self, data: bytes):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n=-1):
        return self.data if n < 0 else self.data[:n]


def test_fetch_deck_rejects_bad_ids_and_kinds_without_a_request(monkeypatch):
    def never(*a, **k):
        raise AssertionError("no request expected")

    monkeypatch.setattr(pixel_execution, "no_redirect_urlopen", never)
    for deck_id, kind in [("deck-short", "pptx"), ("../etc/passwd", "pptx"), (DECK_ID + "x", "pptx"),
                          (DECK_ID, "exe"), (DECK_ID.upper(), "html")]:
        assert pixel_execution.fetch_deck(deck_id, kind) is None


def test_fetch_deck_requests_the_pixel_service_with_the_issued_id(monkeypatch):
    seen = {}

    def fake(request, timeout):
        seen["url"] = request.full_url
        return Body(b"PK-bytes")

    monkeypatch.setattr(pixel_execution, "no_redirect_urlopen", fake)
    assert pixel_execution.fetch_deck(DECK_ID) == b"PK-bytes"
    assert seen["url"] == f"http://pixel:8000/decks/{DECK_ID}.pptx"
    pixel_execution.fetch_deck(DECK_ID, "html")
    assert seen["url"].endswith(f"/decks/{DECK_ID}.html")


@pytest.mark.parametrize("data", [b"", b"x" * (pixel_execution.MAX_DECK_BYTES + 1)])
def test_fetch_deck_refuses_empty_and_oversized_bodies(monkeypatch, data):
    monkeypatch.setattr(pixel_execution, "no_redirect_urlopen", lambda r, t: Body(data))
    assert pixel_execution.fetch_deck(DECK_ID) is None


@pytest.mark.parametrize("error", [OSError("x"), TimeoutError(), pixel_execution.URLError("x")])
def test_fetch_deck_is_none_when_the_service_cannot_be_reached(monkeypatch, error):
    def boom(request, timeout):
        raise error

    monkeypatch.setattr(pixel_execution, "no_redirect_urlopen", boom)
    assert pixel_execution.fetch_deck(DECK_ID) is None


# ---- team_models -------------------------------------------------------------------------------
def test_alias_reads_a_valid_alias(monkeypatch):
    seen = {}

    def fake(request, timeout):
        seen["url"] = request.full_url
        return Body(b'{"agent": "research", "model_alias": "gpt/x"}')

    monkeypatch.setattr(tm, "no_redirect_urlopen", fake)
    assert tm._alias("http://research:8000/research") == "gpt/x"
    assert seen["url"] == "http://research:8000/model"


@pytest.mark.parametrize("body", [b'{"model_alias": null}', b'{"model_alias": "  "}', b'{"model_alias": 3}',
                                  b'{}', b"not json"])
def test_alias_is_none_for_missing_blank_or_bad_replies(monkeypatch, body):
    monkeypatch.setattr(tm, "no_redirect_urlopen", lambda r, t: Body(body))
    assert tm._alias("http://x:8000/p") is None


def test_alias_is_none_for_agentcore_empty_urls_and_errors(monkeypatch):
    def boom(request, timeout):
        raise OSError("down")

    monkeypatch.setattr(tm, "no_redirect_urlopen", boom)
    assert tm._alias("agentcore://arn") is None and tm._alias("") is None
    assert tm._alias("http://x:8000/p") is None


SERVICES = {name: f"http://{name}:8000/p" for name in
            ("research", "security", "fact-check", "source-quality", "privacy-review", "synthesis", "pixel")}


def test_collect_groups_the_approved_bindings_by_identity(monkeypatch):
    monkeypatch.setattr(tm, "_alias", lambda url: None if "synthesis" in url else f"alias-for-{url.split('//')[1].split(':')[0]}")
    grouped = tm.collect(SERVICES)
    assert grouped["pixel"] == [{"capability": "slide_generation", "service": "pixel", "model": "alias-for-pixel"}]
    flat = [entry for entries in grouped.values() for entry in entries]
    assert {e["service"] for e in flat} == set(SERVICES)
    assert next(e for e in flat if e["service"] == "synthesis")["model"] is None
    assert all(e["capability"] != "planning" for e in grouped.get("charlie", []))  # no planner configured


def test_collect_adds_the_planner_when_configured(monkeypatch):
    monkeypatch.setattr(tm, "_alias", lambda url: None)
    monkeypatch.setenv("CHARLIE_PLANNER_MODEL", " planner/alias ")
    assert {"capability": "planning", "service": "planner", "model": "planner/alias"} in tm.collect(SERVICES)["charlie"]


def test_team_models_is_cached_within_the_cache_window(monkeypatch):
    calls = []
    monkeypatch.setattr(tm, "collect", lambda urls: calls.append(1) or {"calls": len(calls)})
    assert tm.team_models(SERVICES) == {"calls": 1}
    assert tm.team_models(SERVICES) == {"calls": 1} and len(calls) == 1
    monkeypatch.setitem(tm._cache, "at", tm._cache["at"] - tm.CACHE_SECONDS - 1)
    assert tm.team_models(SERVICES) == {"calls": 2}


# ---- web routes --------------------------------------------------------------------------------
@pytest.fixture
def client():
    return TestClient(web_app.app)


def test_team_models_route_passes_every_service_url(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(web_app, "team_models", lambda services: seen.update(services) or {"pixel": []})
    reply = client.get("/api/team-models")
    assert reply.status_code == 200 and reply.json() == {"identities": {"pixel": []}}
    assert set(seen) == set(SERVICES) and seen["pixel"] == "http://pixel:8000/process"


def test_deck_download_proxies_the_pptx(client, monkeypatch):
    monkeypatch.setattr(web_app, "fetch_deck", lambda deck_id, kind="pptx": b"PPTX" if kind == "pptx" else None)
    reply = client.get(f"/api/decks/{DECK_ID}")
    assert reply.status_code == 200 and reply.content == b"PPTX"
    assert reply.headers["content-type"] == PPTX
    assert reply.headers["content-disposition"] == f'attachment; filename="{DECK_ID}.pptx"'


def test_deck_view_proxies_the_html_with_a_strict_policy(client, monkeypatch):
    monkeypatch.setattr(web_app, "fetch_deck", lambda deck_id, kind="pptx": b"<html>deck</html>" if kind == "html" else None)
    reply = client.get(f"/api/decks/{DECK_ID}/view")
    assert reply.status_code == 200 and reply.text == "<html>deck</html>"
    assert reply.headers["content-type"] == "text/html; charset=utf-8"
    assert reply.headers["content-security-policy"] == CSP
    assert reply.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("path", [f"/api/decks/{DECK_ID}", f"/api/decks/{DECK_ID}/view", "/api/decks/not-a-deck"])
def test_unknown_decks_are_404(client, monkeypatch, path):
    monkeypatch.setattr(web_app, "fetch_deck", lambda deck_id, kind="pptx": None)
    assert client.get(path).status_code == 404


def test_deck_routes_and_team_models_need_authentication_when_enabled(client, monkeypatch):
    monkeypatch.setenv("WEB_AUTH_ENABLED", "true")
    monkeypatch.setenv("WEB_USERNAME", "u")
    monkeypatch.setenv("WEB_PASSWORD", "p")
    monkeypatch.setattr(web_app, "fetch_deck", lambda deck_id, kind="pptx": b"x")
    monkeypatch.setattr(web_app, "team_models", lambda services: {})
    for path in (f"/api/decks/{DECK_ID}", f"/api/decks/{DECK_ID}/view", "/api/team-models", "/"):
        assert client.get(path).status_code == 401
        assert client.get(path, auth=("u", "wrong")).status_code == 401
        assert client.get(path, auth=("u", "p")).status_code == 200


def test_home_serves_the_page_with_design_tokens_inlined(client):
    reply = client.get("/")
    assert reply.status_code == 200 and "text/html" in reply.headers["content-type"]
    assert "--px-dark-background" in reply.text
    assert web_app.TOKENS_MARKER not in reply.text
    assert 'rel="stylesheet"' not in reply.text


# ---- add_deck_step and /api/chat ---------------------------------------------------------------
def test_add_deck_step_is_none_when_the_flag_is_off_or_no_deck_is_asked_for(monkeypatch):
    monkeypatch.setattr(web_app, "run_pixel_stage", lambda *a, **k: pytest.fail("must not run"))
    result = pipeline_result()
    assert web_app.add_deck_step("explain passkeys", result, "r") is None
    monkeypatch.setenv(PPT_FLAG, "false")
    assert web_app.add_deck_step("give me a slide deck", result, "r") is None
    assert result["agent_activity"] == []


def test_add_deck_step_appends_the_activity_row(monkeypatch):
    seen = {}

    def fake(message, result, request_id, **live):
        seen.update(message=message, live=live)
        return DeckInfo(status="complete", deck_id=DECK_ID), {"agent": "pixel"}

    monkeypatch.setattr(web_app, "run_pixel_stage", fake)
    result = pipeline_result()
    deck = web_app.add_deck_step("  give me a slide deck ", result, "r", decision_number=1)
    assert deck.status == "complete" and result["agent_activity"] == [{"agent": "pixel"}]
    assert seen == {"message": "give me a slide deck", "live": {"decision_number": 1}}


def test_add_deck_step_without_an_activity_row_adds_nothing(monkeypatch):
    monkeypatch.setattr(web_app, "run_pixel_stage",
                        lambda *a, **k: (DeckInfo(status="failed", error="no"), None))
    result = pipeline_result()
    assert web_app.add_deck_step("a deck please? slides", result, "r").status == "failed"
    assert result["agent_activity"] == []


def chat(client, message):
    return client.post("/api/chat", json={"message": message})


def test_chat_returns_a_deck_field_only_when_a_deck_is_asked_for(client, monkeypatch):
    monkeypatch.setattr(web_app, "run_pipeline", lambda *a, **k: pipeline_result())
    monkeypatch.setattr(pixel_execution, "post_json",
                        lambda url, payload, request_id, destination, **kw: envelope(request_id))
    with_deck = chat(client, "Plan passkeys and give me a slide deck")
    assert with_deck.status_code == 200
    body = with_deck.json()
    assert body["deck"]["status"] == "complete" and body["deck"]["view_url"] == f"/api/decks/{DECK_ID}/view"
    assert body["agent_activity"][-1]["agent"] == "pixel"
    assert "slide_text" not in json.dumps(body["deck"])
    assert "deck" not in chat(client, "Plan passkeys").json()


def test_chat_survives_a_pixel_failure(client, monkeypatch):
    monkeypatch.setattr(web_app, "run_pipeline", lambda *a, **k: pipeline_result())

    def down(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(pixel_execution, "post_json", down)
    reply = chat(client, "make a presentation")
    assert reply.status_code == 200 and reply.json()["deck"]["status"] == "failed"
    assert reply.json()["agent_activity"][-1]["status"] == "failed"


# ---- live stream -------------------------------------------------------------------------------
def fake_iterative(question, request_id, **kwargs):
    state = SimpleNamespace(status="completed", completion_kind="charlie_approved",
                            completion_reason="charlie_finished", decision_number=1, completed_steps=[],
                            iteration=6, latest=lambda capability: None)
    return SimpleNamespace(to_legacy_dict=pipeline_result, orchestration=state)


@pytest.fixture
def live(client, monkeypatch):
    monkeypatch.setenv("CHARLIE_PLANNER_MODEL", "planner/alias")
    monkeypatch.setenv("LITELLM_API_KEY", "test-key")
    monkeypatch.setattr(web_app, "run_iterative_pipeline", fake_iterative)
    monkeypatch.setattr(web_app, "build_orchestration_meta", lambda iterative: None)
    monkeypatch.setattr(pixel_execution, "post_json",
                        lambda url, payload, request_id, destination, **kw: envelope(request_id))
    return client


def stream(client, message):
    reply = client.post("/api/chat/stream", json={"message": message})
    assert reply.status_code == 200
    events = []
    for block in reply.text.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


def pixel_events(events):
    return [e for e in events if e["event_type"] in {
        "delegation_authorized", "agent_started", "agent_completed", "agent_failed", "dependency_recorded"}]


def test_the_stream_carries_pixel_events_and_the_deck(live):
    events = stream(live, "Plan passkeys and give me a slide deck")
    types = [e["event_type"] for e in events]
    assert types[-2:] == ["final_response", "orchestration_completed"]
    final = next(e for e in events if e["event_type"] == "final_response")
    deck = final["data"]["response"]["deck"]
    assert deck["status"] == "complete" and deck["slide_count"] == 3
    ours = pixel_events(events)
    assert [e["event_type"] for e in ours] == ["delegation_authorized", "agent_started", "agent_completed"]
    assert all(e["data"]["capability"] == "slide_generation" for e in ours)
    assert ours[0]["data"]["target_identity"] == "pixel"
    assert all(e["source"]["identity_id"] == "pixel" for e in ours[1:])


def test_the_stream_has_no_pixel_events_without_a_deck_request(live):
    events = stream(live, "Plan passkeys")
    assert pixel_events(events) == []
    final = next(e for e in events if e["event_type"] == "final_response")
    assert "deck" not in final["data"]["response"]
    assert events[-1]["event_type"] == "orchestration_completed"


def test_a_failing_pixel_call_is_reported_in_deck_and_the_stream_completes(live, monkeypatch):
    def down(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(pixel_execution, "post_json", down)
    events = stream(live, "make a slide deck")
    final = next(e for e in events if e["event_type"] == "final_response")
    assert final["data"]["response"]["deck"]["status"] == "failed"
    assert [e["event_type"] for e in pixel_events(events)] == [
        "delegation_authorized", "agent_started", "agent_failed"]
    assert events[-1]["event_type"] == "orchestration_completed"


def test_the_stream_records_the_results_pixel_used(live, monkeypatch):
    step = SimpleNamespace(identity_id="nova", capability=SimpleNamespace(value="research"), version=1)

    def iterative(question, request_id, **kwargs):
        out = fake_iterative(question, request_id)
        out.orchestration.latest = lambda capability: step if capability.value == "research" else None
        return out

    monkeypatch.setattr(web_app, "run_iterative_pipeline", iterative)
    events = stream(live, "give me slides")
    recorded = [e for e in pixel_events(events) if e["event_type"] == "dependency_recorded"]
    assert recorded and recorded[0]["data"]["dependencies"][0]["identity_id"] == "nova"


# ---- /model routes -----------------------------------------------------------------------------
def test_research_and_security_model_routes(monkeypatch):
    monkeypatch.setenv("RESEARCH_MODEL", " r/alias ")
    monkeypatch.delenv("SECURITY_MODEL", raising=False)
    assert TestClient(research_agent.app).get("/model").json() == {"agent": "research", "model_alias": "r/alias"}
    assert TestClient(security_agent.app).get("/model").json() == {"agent": "security", "model_alias": None}
    monkeypatch.setenv("SECURITY_MODEL", "s/alias")
    assert TestClient(security_agent.app).get("/model").json()["model_alias"] == "s/alias"


def test_specialist_model_route(monkeypatch):
    monkeypatch.setenv("AGENT_KIND", "fact-check")
    monkeypatch.setenv("FACT_CHECK_MODEL", "fc/alias")
    module = importlib.reload(importlib.import_module("viharbot.specialist_agent"))
    assert TestClient(module.app).get("/model").json() == {"agent": "fact-check", "model_alias": "fc/alias"}
    monkeypatch.delenv("FACT_CHECK_MODEL")
    assert TestClient(module.app).get("/model").json()["model_alias"] is None
