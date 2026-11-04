from __future__ import annotations

import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.error import URLError

import pytest

from deadline_helpers import (
    REMAINING_CASES, REST_TIMEOUT_CAP, FakeAgentCore, FakeHTTPResponse, deadline_with,
    install_agentcore, install_rest)
from orchestration_helpers import Clock
from pipeline_fixtures import RID, FakeSend, response
from viharbot import agentcore_client, demo
from viharbot.agentcore_client import invoke_agentcore
from viharbot.deadline import (
    DeadlineExceeded, RequestDeadline, bounded_timeout, is_timeout_error)
from viharbot.demo import AgentPipelineError, invoke_agent, post_json
from viharbot.pipeline_state import AgentActivity


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("a real network call was attempted")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)


# the deadline abstraction
def test_remaining_counts_down_on_an_injected_monotonic_clock():
    deadline, clock = deadline_with(10.0)
    assert deadline.remaining() == pytest.approx(10.0)
    clock.now += 3.5
    assert deadline.remaining() == pytest.approx(6.5)
    assert deadline.require_remaining() == pytest.approx(6.5)
    assert not deadline.expired()


@pytest.mark.parametrize("advance", [10.0, 10.5, 99.0])
def test_a_deadline_expires_at_zero_remaining_or_less(advance):
    deadline, clock = deadline_with(10.0)
    clock.now += advance
    assert deadline.expired() and deadline.remaining() <= 0
    with pytest.raises(DeadlineExceeded):
        deadline.require_remaining()


def test_deadline_exceeded_is_a_distinct_timeout_error():
    assert issubclass(DeadlineExceeded, TimeoutError)
    assert not issubclass(DeadlineExceeded, AgentPipelineError)


def test_after_builds_an_absolute_deadline_once_and_never_resets():
    clock = Clock()
    clock.now = 100.0
    deadline = RequestDeadline.after(30.0, clock)
    assert deadline.expires_at == 130.0
    clock.now = 120.0
    assert deadline.remaining() == pytest.approx(10.0)  # not 30: nothing was reset
    with pytest.raises(AttributeError):
        deadline.expires_at = 500.0  # frozen


def test_the_default_clock_is_monotonic_not_wall_time():
    import time

    assert RequestDeadline(expires_at=0.0).clock is time.monotonic


@pytest.mark.parametrize("remaining,cap,expected", [(30.0, 90.0, 30.0), (200.0, 90.0, 90.0),
                                                    (0.2, 90.0, 0.2)])
def test_bounded_timeout_has_no_minimum(remaining, cap, expected):
    deadline, _ = deadline_with(remaining)
    assert bounded_timeout(cap, deadline) == pytest.approx(expected)
    assert bounded_timeout(cap, None) == cap


def test_timeout_shaped_errors_are_recognized():
    assert is_timeout_error(TimeoutError())
    assert is_timeout_error(URLError(TimeoutError("timed out")))

    class ReadTimeoutError(Exception):
        pass

    assert is_timeout_error(ReadTimeoutError())
    assert not is_timeout_error(ValueError("bad"))


# the shared remaining-time matrix: REST
@pytest.mark.parametrize("remaining,planner,rest,agentcore", REMAINING_CASES)
def test_rest_timeout_is_min_of_cap_and_remaining(monkeypatch, remaining, planner, rest, agentcore):
    deadline, _ = deadline_with(remaining)
    calls = install_rest(monkeypatch)
    if rest is None:
        with pytest.raises(DeadlineExceeded):
            post_json("http://svc/x", {}, RID, "research", deadline=deadline)
        assert calls == []  # no HTTP request
    else:
        assert post_json("http://svc/x", {}, RID, "research", deadline=deadline)["agent"] == "research"
        assert calls == [pytest.approx(rest)]
        assert calls[0] <= REST_TIMEOUT_CAP


def test_rest_without_a_deadline_is_unchanged(monkeypatch):
    calls = install_rest(monkeypatch)
    assert post_json("http://svc/x", {}, RID, "research")["agent"] == "research"  # four args
    assert calls == [90]


def test_rest_timeout_never_exceeds_the_cap(monkeypatch):
    deadline, _ = deadline_with(1000.0)
    calls = install_rest(monkeypatch)
    post_json("http://svc/x", {}, RID, "research", deadline=deadline)
    assert calls == [90]


def test_rest_deadline_expiring_while_the_response_opens_rejects_the_result(monkeypatch):
    deadline, clock = deadline_with(5.0)
    install_rest(monkeypatch, on_open=lambda: setattr(clock, "now", clock.now + 6))
    with pytest.raises(DeadlineExceeded):
        post_json("http://svc/x", {}, RID, "research", deadline=deadline)


def test_rest_deadline_expiring_during_the_body_read_rejects_the_result(monkeypatch):
    deadline, clock = deadline_with(5.0)
    reads = []

    def slow_read():
        reads.append(1)
        clock.now += 3  # each chunk takes three seconds

    install_rest(monkeypatch, on_read=slow_read)
    with pytest.raises(DeadlineExceeded):
        post_json("http://svc/x", {}, RID, "research", deadline=deadline)
    assert len(reads) <= 3  # it stopped reading instead of draining the body


def test_rest_socket_timeout_after_expiry_becomes_deadline_exceeded(monkeypatch):
    deadline, clock = deadline_with(5.0)

    def timing_out(request, timeout):
        clock.now += 6
        raise URLError(TimeoutError("timed out"))

    monkeypatch.setattr(demo, "urlopen", timing_out)
    with pytest.raises(DeadlineExceeded):
        post_json("http://svc/x", {}, RID, "research", deadline=deadline)


def test_rest_socket_timeout_with_time_left_stays_an_ordinary_failure(monkeypatch):
    deadline, _ = deadline_with(50.0)

    def timing_out(request, timeout):
        raise URLError(TimeoutError("timed out"))

    monkeypatch.setattr(demo, "urlopen", timing_out)
    with pytest.raises(URLError):
        post_json("http://svc/x", {}, RID, "research", deadline=deadline)


# the shared remaining-time matrix: AgentCore
@pytest.mark.parametrize("remaining,planner,rest,agentcore", REMAINING_CASES)
def test_agentcore_timeouts_are_bounded_by_remaining_time(monkeypatch, remaining, planner, rest, agentcore):
    deadline, _ = deadline_with(remaining)
    fake = install_agentcore(monkeypatch)
    if agentcore is None:
        with pytest.raises(DeadlineExceeded):
            invoke_agentcore("arn:x", {}, RID, deadline=deadline)
        assert fake.sessions_created == 0 and fake.invocations == []  # no AgentCore call
    else:
        assert invoke_agentcore("arn:x", {}, RID, deadline=deadline)["agent"] == "research"
        config = fake.configs[0]
        assert config.connect_timeout == pytest.approx(agentcore)
        assert config.read_timeout == pytest.approx(agentcore)
        assert config.retries == {"total_max_attempts": 1}  # the SDK does not retry
        assert fake.invocations[0]["runtimeSessionId"] != RID  # derived; see test_nova_contract


def test_agentcore_without_a_deadline_uses_the_shared_default_client(monkeypatch):
    created = []

    class Client:
        def invoke_agent_runtime(self, **kw):
            return {"statusCode": 200, "response": FakeHTTPResponse(
                b'{"request_id": "req-1", "agent": "research"}')}

    import boto3

    monkeypatch.setattr(agentcore_client, "_runtime_client", None)
    monkeypatch.setattr(boto3, "client", lambda *a, **k: created.append(k) or Client())
    assert invoke_agentcore("arn:x", {}, RID)["agent"] == "research"
    assert created and "config" not in created[0]  # default botocore behavior, as before


def test_agentcore_setup_time_counts_against_the_deadline(monkeypatch):
    deadline, clock = deadline_with(1.0)
    fake = install_agentcore(monkeypatch, on_client=lambda: setattr(clock, "now", clock.now + 1.5))
    with pytest.raises(DeadlineExceeded):
        invoke_agentcore("arn:x", {}, RID, deadline=deadline)
    assert fake.invocations == []  # the budget was gone before the invocation


def test_agentcore_deadline_expiring_during_the_invocation_rejects_the_result(monkeypatch):
    deadline, clock = deadline_with(5.0)
    install_agentcore(monkeypatch, on_invoke=lambda: setattr(clock, "now", clock.now + 6))
    with pytest.raises(DeadlineExceeded):
        invoke_agentcore("arn:x", {}, RID, deadline=deadline)


def test_agentcore_does_not_read_the_body_once_the_invocation_overran_the_deadline(monkeypatch):
    deadline, clock = deadline_with(5.0)
    reads = []
    install_agentcore(monkeypatch, on_invoke=lambda: setattr(clock, "now", clock.now + 6),
                      on_read=lambda: reads.append(1))
    with pytest.raises(DeadlineExceeded):
        invoke_agentcore("arn:x", {}, RID, deadline=deadline)
    assert reads == []  # the late response body was never read


def test_agentcore_deadline_expiring_during_the_body_read_rejects_the_result(monkeypatch):
    deadline, clock = deadline_with(5.0)
    install_agentcore(monkeypatch, on_read=lambda: setattr(clock, "now", clock.now + 6))
    with pytest.raises(DeadlineExceeded):
        invoke_agentcore("arn:x", {}, RID, deadline=deadline)


def test_agentcore_timeout_errors_after_expiry_become_deadline_exceeded(monkeypatch):
    deadline, clock = deadline_with(5.0)

    class ReadTimeoutError(Exception):
        pass

    def boom():
        clock.now += 6
        raise ReadTimeoutError("read timed out")

    install_agentcore(monkeypatch, on_invoke=boom)
    with pytest.raises(DeadlineExceeded):
        invoke_agentcore("arn:x", {}, RID, deadline=deadline)


def test_agentcore_errors_with_time_left_are_not_reclassified(monkeypatch):
    deadline, _ = deadline_with(50.0)

    def boom():
        raise ValueError("bad payload")

    install_agentcore(monkeypatch, on_invoke=boom)
    with pytest.raises(ValueError):
        invoke_agentcore("arn:x", {}, RID, deadline=deadline)


def test_post_json_forwards_the_deadline_to_agentcore(monkeypatch):
    deadline, _ = deadline_with(7.0)
    fake = install_agentcore(monkeypatch)
    assert post_json("agentcore://arn:x", {}, RID, "research", deadline=deadline)["agent"] == "research"
    assert fake.configs[0].read_timeout == pytest.approx(7.0)


def test_concurrent_requests_get_independent_agentcore_clients_and_timeouts(monkeypatch):
    fake = install_agentcore(monkeypatch)
    barrier = threading.Barrier(2)
    seen = {}

    def call(remaining):
        deadline, _ = deadline_with(remaining)
        barrier.wait(timeout=5)
        invoke_agentcore("arn:x", {}, RID, deadline=deadline)
        seen[remaining] = True

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(call, [4.0, 9.0]))
    assert sorted(c.read_timeout for c in fake.configs) == [pytest.approx(4.0), pytest.approx(9.0)]
    assert sorted(i["config"].read_timeout for i in fake.invocations) == [
        pytest.approx(4.0), pytest.approx(9.0)]  # each call used its own client
    assert fake.sessions_created == 1  # one shared session, no shared clients


# invoke_agent: late results, compatibility and exception categories
def test_invoke_agent_rejects_results_that_arrive_late_and_records_it():
    deadline, clock = deadline_with(5.0)
    activity: list[AgentActivity] = []

    def slow(url, payload, request_id, destination):
        clock.now += 6
        return response(destination)

    with pytest.raises(DeadlineExceeded):
        invoke_agent(slow, "u", {}, RID, "research", activity, deadline=deadline)
    assert [(a.agent, a.status, a.error) for a in activity] == [
        ("research", "failed", "DeadlineExceeded")]


def test_invoke_agent_does_not_start_after_expiry():
    deadline, clock = deadline_with(5.0)
    clock.now += 5
    send = FakeSend()
    with pytest.raises(DeadlineExceeded):
        invoke_agent(send, "u", {}, RID, "research", [], deadline=deadline)
    assert send.calls == []


def test_deadline_exceeded_is_not_wrapped_in_agent_pipeline_error():
    deadline, _ = deadline_with(50.0)

    def expiring(url, payload, request_id, destination):
        raise DeadlineExceeded("request deadline exceeded")

    with pytest.raises(DeadlineExceeded) as caught:
        invoke_agent(expiring, "u", {}, RID, "research", [], deadline=deadline)
    assert not isinstance(caught.value, AgentPipelineError)


def test_ordinary_failures_are_still_agent_pipeline_errors():
    deadline, _ = deadline_with(50.0)

    def failing(url, payload, request_id, destination):
        raise TimeoutError("socket timed out with time to spare")

    with pytest.raises(AgentPipelineError) as caught:
        invoke_agent(failing, "u", {}, RID, "research", [], deadline=deadline)
    assert caught.value.activity[-1]["error"] == "TimeoutError"


def test_four_argument_send_callbacks_keep_working_with_a_deadline():
    deadline, _ = deadline_with(50.0)
    send = FakeSend()
    envelope, _ = invoke_agent(send, "u", {"question": "q"}, RID, "research", [], deadline=deadline)
    assert envelope.request_id == RID and send.order == ["research"]


def test_deadline_aware_senders_receive_the_same_deadline_object():
    deadline, _ = deadline_with(50.0)
    seen = []

    def aware(url, payload, request_id, destination, *, deadline=None):
        seen.append(deadline)
        return response(destination)

    invoke_agent(aware, "u", {}, RID, "research", [], deadline=deadline)
    assert seen == [deadline] and seen[0] is deadline


def test_without_a_deadline_invoke_agent_behaves_as_before():
    activity: list[AgentActivity] = []
    send = FakeSend()
    invoke_agent(send, "u", {}, RID, "research", activity)
    assert [(a.agent, a.status) for a in activity] == [("research", "complete")]
