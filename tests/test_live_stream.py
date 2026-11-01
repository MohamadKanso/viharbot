"""POST /api/chat/stream: SSE framing, auth, validation, isolation, backpressure and disconnects."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from live_helpers import C, R, S, Script, VersionedSend, delegate, finish
from pipeline_fixtures import RID
from viharbot import live_stream, web_app
from viharbot.delegation import PlannerUnavailableError
from viharbot.live_events import TERMINAL_EVENT_TYPES, check_stream, parse_event
from viharbot.live_hooks import CancelToken, OrchestrationCancelled
from viharbot.live_stream import (
    DIAGNOSTICS_FLAG, MAX_CONCURRENT_STREAMS, StreamPermit, event_stream, format_sse)

QUESTION = "SENSITIVE-QUESTION-TEXT about autonomous agents"
FRAME = re.compile(r"^id: (evt-\d{6})\nevent: ([a-z_]+)\ndata: (\{.*\})$")


def standard_script():
    return Script(delegate(R, "Research first."), delegate(S), finish())


def frames_of(body: bytes) -> list[dict]:
    text = body.decode("utf-8")
    assert "\r" not in text
    found = []
    for block in text.split("\n\n"):
        if not block.strip() or block.startswith(":"):
            continue
        match = FRAME.match(block)
        assert match, f"malformed SSE frame: {block[:80]!r}"
        found.append({"id": match.group(1), "event": match.group(2), "data": json.loads(match.group(3))})
    return found


def events_of(body: bytes):
    return [parse_event(frame["data"]) for frame in frames_of(body)]


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setenv("WEB_AUTH_ENABLED", "false")
    monkeypatch.setenv("CHARLIE_PLANNER_MODEL", "planner/alias")
    monkeypatch.setenv("LITELLM_API_KEY", "test-key")
    monkeypatch.delenv(DIAGNOSTICS_FLAG, raising=False)
    state = SimpleNamespace(decide=lambda question: standard_script(), sends=[], decisions=0)
    real = web_app.run_iterative_pipeline

    def wrapper(question, request_id, **kwargs):
        send = VersionedSend()
        state.sends.append(send)
        return real(question, request_id, **{**kwargs, "send": send, "decide": state.decide(question)})

    monkeypatch.setattr(web_app, "run_iterative_pipeline", wrapper)
    return TestClient(web_app.app), state, monkeypatch


def post(client, message=QUESTION, **kwargs):
    return client.post("/api/chat/stream", json={"message": message}, **kwargs)


def permits_available() -> int:
    held = []
    while (permit := StreamPermit.acquire()) is not None:
        held.append(permit)
    for permit in held:
        permit.release()
    return len(held)


# ---- response contract ------------------------------------------------------------------------
def test_a_successful_stream_has_the_documented_status_headers_and_framing(live):
    client, state, _ = live
    reply = post(client, headers={"X-Request-ID": "req-framing"})
    assert reply.status_code == 200
    assert reply.headers["content-type"] == "text/event-stream; charset=utf-8"
    assert reply.headers["cache-control"] == "no-cache"
    assert reply.headers["x-accel-buffering"] == "no"
    assert reply.headers["x-request-id"] == "req-framing"
    assert reply.content.endswith(b"\n\n") and b"\r" not in reply.content
    for frame in frames_of(reply.content):
        assert frame["event"] == frame["data"]["event_type"]  # the SSE name is the JSON event_type
        assert frame["id"] == frame["data"]["event_id"]
        assert frame["data"]["request_id"] == "req-framing"


def test_every_frame_validates_and_the_stream_obeys_the_terminal_rules(live):
    client, state, _ = live
    events = events_of(post(client).content)
    assert check_stream(events) == "orchestration_completed"
    assert [e.event_type for e in events[-2:]] == ["final_response", "orchestration_completed"]
    assert sum(e.event_type in TERMINAL_EVENT_TYPES for e in events) == 1
    assert [e.sequence for e in events] == list(range(1, len(events) + 1))


def test_a_generated_request_id_is_used_when_none_is_supplied(live):
    client, state, _ = live
    reply = post(client)
    request_id = reply.headers["x-request-id"]
    assert request_id and {e.request_id for e in events_of(reply.content)} == {request_id}


def test_the_final_response_matches_what_the_ordinary_endpoint_returns(live):
    client, state, _ = live

    def normalize(body):
        body = json.loads(json.dumps(body))
        for entry in body["agent_activity"]:
            entry["duration_ms"] = 0
        for step in body["orchestration"]["steps"]:
            step["duration_ms"] = 0
        body["orchestration"]["elapsed_ms"] = 0
        return body

    streamed = next(e for e in events_of(post(client).content) if e.event_type == "final_response")
    ordinary = client.post("/api/chat", json={"message": QUESTION}, headers={"X-Request-ID": "x"}).json()
    ordinary["request_id"] = streamed.data.response.request_id
    assert normalize(streamed.data.response.model_dump(mode="json", exclude_unset=True)) == normalize(ordinary)


# ---- authentication and validation happen before any stream or inference --------------------------
def test_authentication_is_the_same_as_the_ordinary_endpoint(live):
    client, state, patch = live
    patch.setenv("WEB_AUTH_ENABLED", "true")
    patch.setenv("WEB_USERNAME", "u")
    patch.setenv("WEB_PASSWORD", "p")
    for kwargs in ({}, {"auth": ("u", "wrong")}, {"auth": ("wrong", "p")}):
        unauthorized = post(client, **kwargs)
        assert unauthorized.status_code == 401 and "text/event-stream" not in unauthorized.headers["content-type"]
        assert "WWW-Authenticate" in unauthorized.headers
    ordinary = client.post("/api/chat", json={"message": "hi"})
    assert ordinary.status_code == 401
    assert state.sends == []  # nothing ran for any unauthenticated request
    assert post(client, auth=("u", "p")).status_code == 200


def test_authentication_is_enforced_when_credentials_are_not_configured(live):
    client, state, patch = live
    patch.setenv("WEB_AUTH_ENABLED", "true")
    patch.delenv("WEB_USERNAME", raising=False)
    patch.delenv("WEB_PASSWORD", raising=False)
    assert post(client, auth=("a", "b")).status_code == 503  # the same refusal as /api/chat
    assert state.sends == []


@pytest.mark.parametrize("payload", [
    {}, {"message": ""}, {"message": "x" * 3001}, {"message": "hi", "history": "no"},
    {"message": "hi", "history": [{"role": "system", "content": "x"}]},
    {"message": "hi", "history": [{"role": "user", "content": ""}]},
    {"message": "hi", "history": [{"role": "user", "content": "x" * 501}]},
    {"message": "hi", "history": [{"role": "user", "content": "x"}] * 5},
])
def test_invalid_requests_fail_with_422_before_any_stream_or_inference(live, payload):
    client, state, _ = live
    reply = client.post("/api/chat/stream", json=payload)
    assert reply.status_code == 422 and "text/event-stream" not in reply.headers["content-type"]
    assert state.sends == []


def test_the_request_contract_is_the_existing_one_with_nothing_new_required(live):
    client, state, _ = live
    ok = client.post("/api/chat/stream", json={"message": "hi", "history": [
        {"role": "user", "content": "earlier"}, {"role": "assistant", "content": "reply"}]})
    assert ok.status_code == 200
    schema = client.get("/openapi.json").json()
    body = schema["paths"]["/api/chat/stream"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert body == {"$ref": "#/components/schemas/ChatRequest"}


# ---- modes and limits --------------------------------------------------------------------------
@pytest.mark.parametrize("missing", ["CHARLIE_PLANNER_MODEL", "LITELLM_API_KEY"])
def test_streaming_needs_iterative_orchestration_and_says_so_before_streaming(live, missing):
    client, state, patch = live
    patch.delenv(missing)
    reply = post(client)
    assert reply.status_code == 409
    assert reply.json() == {"detail": "Live streaming requires iterative orchestration"}
    assert "text/event-stream" not in reply.headers["content-type"] and state.sends == []


def test_the_ordinary_endpoint_is_unaffected_when_streaming_is_unavailable(live):
    client, state, patch = live
    patch.delenv("LITELLM_API_KEY")
    patch.setattr(web_app, "run_pipeline", lambda q, rid, **kw: __import__("viharbot.demo", fromlist=["x"]).run_pipeline(
        q, rid, **{**kw, "send": VersionedSend()}))
    assert post(client).status_code == 409
    ordinary = client.post("/api/chat", json={"message": "hi"})
    assert ordinary.status_code == 200 and "orchestration" not in ordinary.json()


def test_too_many_live_streams_are_refused_with_429_and_slots_come_back(live):
    client, state, _ = live
    held = [StreamPermit.acquire() for _ in range(MAX_CONCURRENT_STREAMS)]
    try:
        refused = post(client)
        assert refused.status_code == 429 and state.sends == []
    finally:
        for permit in held:
            permit.release()
    assert post(client).status_code == 200
    assert permits_available() == MAX_CONCURRENT_STREAMS  # nothing leaked


def test_permits_return_after_failures_and_releasing_twice_is_harmless(live):
    client, state, _ = live
    state.decide = lambda question: (_ for _ in ()).throw(PlannerUnavailableError("no alias"))
    assert post(client).status_code == 200
    assert permits_available() == MAX_CONCURRENT_STREAMS
    permit = StreamPermit.acquire()
    permit.release()
    permit.release()
    assert permits_available() == MAX_CONCURRENT_STREAMS


# ---- failures arrive as events, not as a different status -----------------------------------------
def test_an_application_failure_after_the_stream_opens_is_a_terminal_event_not_an_http_error(live):
    client, state, _ = live

    def decide(question):
        def planner(state_, context):
            if state_.decision_number == 2:
                raise PlannerUnavailableError("no planner model alias is configured")
            return delegate(R)
        return planner

    state.decide = decide
    reply = post(client)
    assert reply.status_code == 200
    events = events_of(reply.content)
    assert check_stream(events) == "orchestration_failed"
    assert not [e for e in events if e.event_type == "final_response"]
    assert events[-1].data.error_code == "planner_unavailable"
    assert "no planner model alias" not in reply.text  # the raw exception text never reaches the stream
    assert events[-1].data.message == "Charlie's planning is not available."
    assert sum(e.event_type == "orchestration_failed" for e in events) == 1


# ---- privacy of the stream ----------------------------------------------------------------------
def test_the_stream_carries_no_prompt_question_or_credential_text(live):
    client, state, patch = live
    patch.setenv("LITELLM_API_KEY", "super-secret-key")
    body = post(client, QUESTION).text
    for forbidden in ("SENSITIVE-QUESTION-TEXT", "super-secret-key", "Identity and role", "Authorization",
                      "Bearer", "http://", "agentcore://", "Traceback"):
        assert forbidden not in body, forbidden
    assert "Research first." not in body  # the planner rationale is a diagnostics-only field


def test_developer_diagnostics_are_off_by_default_and_gated_by_a_flag(live):
    client, state, patch = live
    quiet = [e for e in events_of(post(client).content) if e.event_type == "planner_completed"][0]
    assert quiet.data.rationale is None and quiet.data.model_alias is None
    patch.setenv(DIAGNOSTICS_FLAG, "true")
    events = events_of(post(client).content)
    loud = [e for e in events if e.event_type == "planner_completed"][0]
    assert loud.data.rationale == "Research first."
    assert [e for e in events if e.event_type == "agent_completed"][0].data.model_alias == "m-r"


# ---- isolation between requests ----------------------------------------------------------------
def test_concurrent_streams_do_not_leak_events_or_share_sequences(live):
    client, state, _ = live
    state.decide = lambda question: (
        Script(delegate(R), delegate(S), finish()) if question.startswith("A") else
        Script(delegate(R), finish("too early?"), delegate(S), finish()))

    def run(index):
        message = ("A" if index % 2 == 0 else "B") + f" question {index}"
        reply = post(client, message, headers={"X-Request-ID": f"req-concurrent-{index}"})
        return index, events_of(reply.content)

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(run, range(6)))
    for index, events in results:
        assert {e.request_id for e in events} == {f"req-concurrent-{index}"}
        assert check_stream(events) == "orchestration_completed"
        assert [e.sequence for e in events] == list(range(1, len(events) + 1))
        final = next(e for e in events if e.event_type == "final_response")
        assert final.data.response.request_id == f"req-concurrent-{index}"
    assert permits_available() == MAX_CONCURRENT_STREAMS


# ---- the stream runner: backpressure, keep-alive and disconnects ----------------------------------
async def collect_all(agen):
    return [chunk async for chunk in agen]


def workers(request_id=None):
    """Live worker threads. With a request id, only that stream's worker (its thread is named
    `live-<first 8 characters of the request id>`), so a worker another test left behind, or one
    that is still exiting, cannot change the count."""
    name = None if request_id is None else f"live-{request_id[:8]}"
    return [t for t in threading.enumerate()
            if (t.name == name if name else t.name.startswith("live-"))]


def wait_until(condition, seconds=3.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.02)
    return False


def test_a_full_queue_closes_a_stream_nobody_is_reading_and_frees_everything():
    cancel, permit = CancelToken(), StreamPermit.acquire()
    emitted = []

    def work(emitter, token):
        for number in range(1, 40):
            emitter.planner_started(decision_number=number, remaining_ms=1000)
            emitted.append(number)
        raise AssertionError("an unread stream must stop the producer before this point")

    async def scenario():
        agen = event_stream(request_id="req-slow", work=work, permit=permit, cancel=cancel,
                            queue_size=2, sink_timeout=0.2)
        first = await agen.__anext__()  # read one frame, then stop reading
        assert first.startswith(b"id: evt-000001\n")
        await asyncio.sleep(0.8)
        await agen.aclose()

    asyncio.run(scenario())
    assert len(emitted) <= 4  # bounded: the queue held two, and the producer stopped soon after
    assert cancel.is_set() and wait_until(lambda: not workers("req-slow"))
    assert permits_available() == MAX_CONCURRENT_STREAMS


def test_idle_streams_send_keep_alive_comments_that_are_not_events():
    cancel, permit = CancelToken(), StreamPermit.acquire()
    gate = threading.Event()

    def work(emitter, token):
        gate.wait(5)
        raise RuntimeError("boom")

    async def scenario():
        agen = event_stream(request_id="req-idle", work=work, permit=permit, cancel=cancel, keepalive=0.05)
        chunks = [await agen.__anext__()]
        gate.set()
        async for chunk in agen:
            chunks.append(chunk)
        return chunks

    chunks = asyncio.run(scenario())
    assert chunks[0] == b": keep-alive\n\n"
    frames = frames_of(b"".join(chunks))
    assert [f["event"] for f in frames] == ["orchestration_failed"]  # comments are never counted as events
    assert frames[0]["data"]["data"]["error_code"] == "internal_error"


def test_a_disconnect_cancels_new_work_and_releases_the_slot_without_replaying_anything():
    gate, in_flight = threading.Event(), threading.Event()
    calls, cancel, permit = [], CancelToken(), StreamPermit.acquire()

    def work(emitter, token):
        emitter.planner_started(decision_number=1, remaining_ms=1000)
        calls.append("planner")
        in_flight.set()
        gate.wait(5)  # a model call that is still in flight when the client leaves
        emitter.planner_completed(decision_number=1, duration_ms=1, action="finish", capability=None,
                                  rationale=None, model_alias=None)
        calls.append("after-the-call")  # must not be reached
        raise AssertionError("the run continued after the client disconnected")

    async def scenario():
        agen = event_stream(request_id="req-gone", work=work, permit=permit, cancel=cancel)
        await agen.__anext__()  # the client reads one event...
        await agen.aclose()  # ...then disconnects

    try:
        asyncio.run(scenario())
        assert in_flight.wait(5)  # the worker has really started its call (it signals after the event)
        assert cancel.is_set() and permits_available() == MAX_CONCURRENT_STREAMS
        # Only this stream's worker is counted, so threads left by other tests cannot change it.
        assert calls == ["planner"] and len(workers("req-gone")) == 1  # the call is still running
    finally:
        gate.set()  # when it returns, the next event cannot be delivered and the run stops
    assert wait_until(lambda: not workers("req-gone"))
    assert calls == ["planner"]  # nothing further happened and nothing was retried


def test_sse_frames_cannot_be_broken_by_hostile_text_in_the_data():
    from viharbot.live_events import parse_event as parse
    event = parse({"schema_version": "1.0", "event_id": "evt-000001", "request_id": "req-1", "sequence": 1,
                   "timestamp": "2026-10-27T19:00:00.000Z", "event_type": "orchestration_failed",
                   "source": {"identity_id": "application", "display_name": "ViharBot", "role": "application"},
                   "correlation": {"decision_number": None, "execution_id": None, "parent_event_id": None},
                   "data": {"status": "failed", "error_code": "internal_error",
                            "message": "line one\nid: evt-999999\nevent: planner_started\r\n\r\n", "elapsed_ms": 1}})
    frame = format_sse(event).decode()
    assert frame.count("\n") == 4 and frame.endswith("\n\n")  # id, event, data, blank line only
    assert len([line for line in frame.split("\n") if line.startswith("event:")]) == 1


# ---- a real server over loopback: events arrive before the request finishes -----------------------
@contextlib.contextmanager
def loopback_server():
    import uvicorn

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    server = uvicorn.Server(uvicorn.Config(web_app.app, host="127.0.0.1", port=port,
                                           log_level="warning", lifespan="off"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    assert wait_until(lambda: server.started, 10)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(10)


def gated_planner(gate, calls):
    def decide(question):
        script = Script(delegate(R), delegate(S), finish())

        def planner(state, context):
            calls.append(state.decision_number)
            if state.decision_number == 2:
                gate.wait(10)  # a slow model call
            return script(state, context)

        return planner

    return decide


def read_events(lines, until):
    seen = []
    for line in lines:
        if line.startswith("data: "):
            seen.append(parse_event(json.loads(line[6:])))
            if until(seen):
                break
    return seen


def test_events_reach_the_client_before_the_orchestration_finishes(live):
    client, state, _ = live
    gate, calls = threading.Event(), []
    state.decide = gated_planner(gate, calls)
    with loopback_server() as base, httpx.Client(timeout=15) as http:
        with http.stream("POST", f"{base}/api/chat/stream", json={"message": QUESTION}) as response:
            assert response.headers["content-type"] == "text/event-stream; charset=utf-8"
            lines = response.iter_lines()
            early = read_events(lines, lambda e: e[-1].event_type == "planner_started"
                                and e[-1].correlation.decision_number == 2)
            # planner_started is sent just before the call begins, so wait until the call is in flight.
            assert wait_until(lambda: calls == [1, 2])
            # Decision 2's planner call is still blocked, yet its start and everything before it arrived.
            assert not gate.is_set()
            assert [e.event_type for e in early[:6]] == [
                "planner_started", "planner_completed", "delegation_requested", "delegation_authorized",
                "agent_started", "agent_completed"]
            assert not any(e.event_type in TERMINAL_EVENT_TYPES for e in early)
            gate.set()
            rest = read_events(lines, lambda e: e[-1].event_type in TERMINAL_EVENT_TYPES)
        assert check_stream(early + rest) == "orchestration_completed"


def test_a_real_disconnect_stops_further_delegation_and_never_replays_inference(live):
    client, state, _ = live
    gate, calls = threading.Event(), []
    state.decide = gated_planner(gate, calls)
    with loopback_server() as base, httpx.Client(timeout=15) as http:
        try:
            with http.stream("POST", f"{base}/api/chat/stream", json={"message": QUESTION}) as response:
                request_id = response.headers["x-request-id"]  # names this stream's worker thread
                read_events(response.iter_lines(), lambda e: e[-1].event_type == "planner_started"
                            and e[-1].correlation.decision_number == 2)
                assert wait_until(lambda: calls == [1, 2])  # decision 2's model call is now in flight
            # the client has left while that call is still running
            assert wait_until(lambda: permits_available() == MAX_CONCURRENT_STREAMS)
            sends_before = len(state.sends[0].calls)
        finally:
            gate.set()  # never leave the blocked call running if an assertion above fails
        assert wait_until(lambda: not workers(request_id))
        time.sleep(0.2)
        assert len(state.sends[0].calls) == sends_before == 1  # no second delegation after the disconnect
        assert calls == [1, 2]  # the planner was called once per decision and never replayed
