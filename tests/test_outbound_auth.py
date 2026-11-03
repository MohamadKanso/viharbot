"""Outbound service assertions (ADR-006 A2b). Offline: throwaway keys, a fake transport, no network."""

from __future__ import annotations

import io
import logging
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from test_service_auth import load_specialist, raw_seed, world  # noqa: F401  (world is a fixture)
from viharbot import demo
from viharbot import outbound_auth as oa
from viharbot import service_auth as sa

REAL_TIME = time.time
TARGETS = ("fact-check", "source-quality", "privacy-review", "synthesis")


@pytest.fixture
def on(world, monkeypatch):
    """Outbound minting on, key read from a file, as the verifier's registry expects."""
    monkeypatch.setenv(oa.ENABLE_ENV, "on")
    monkeypatch.setenv(oa.KID_ENV, "web-1")
    monkeypatch.setenv(oa.KEY_FILE_ENV, world.key_file)
    monkeypatch.setattr(sa.time, "time", world.clock)  # tokens are issued at the injected clock
    return world


def verifier(world, audience):
    return sa.ServiceAuthenticator(audience=audience, registry=world.registry, clock=world.clock)


def parts(url):
    split = urlsplit(url)
    path = split.path or "/"
    return path.encode(), (split.query.encode() if split.query else None)


def mint(url, destination="fact-check", body=b"", method="POST"):
    headers = oa.outbound_headers(url, destination=destination, method=method, body=body)
    assert list(headers) == [sa.ASSERTION_HEADER]  # never Authorization
    return headers[sa.ASSERTION_HEADER]


def verify(world, url, destination="fact-check", body=b"", **kwargs):
    raw_path, query = parts(url)
    return verifier(world, destination).verify(
        mint(url, destination, body), method="POST", raw_path=raw_path, query=query, body=body,
        required_capability="specialist.process", **kwargs,
    )


@pytest.mark.parametrize("name", TARGETS)
def test_minted_assertion_verifies_for_each_wired_target(on, name):
    caller = verify(on, f"http://{name}:8000/process", name, b'{"q":1}')
    assert (caller.sub, caller.kid, caller.capability) == ("web", "web-1", "specialist.process")


@pytest.mark.parametrize(
    "url",
    [
        "http://fact-check:8000/process?b=2&a=1",
        "http://fact-check:8000/process?a=1&a=2&a=1",
        "http://fact-check:8000/process?q=caf%C3%A9&r=%2B+x",
        "http://fact-check:8000/process?flag",
        "http://fact-check:8000",  # no path: signed as "/"
    ],
)
def test_request_binding_matches_query_and_empty_body(on, url):
    assert verify(on, url).sub == "web"


def test_lower_case_percent_hex_is_canonicalized_like_the_verifier(on):
    token = mint("http://fact-check:8000/process?q=%c3%a9")
    claims = sa._json_object(token.split(".")[1])
    assert claims["tgt"] == "/process?q=%C3%A9"
    assert verify(on, "http://fact-check:8000/process?q=%c3%a9").sub == "web"


def test_query_order_body_and_path_are_bound(on):
    token = mint("http://fact-check:8000/process?a=1&b=2")
    for kwargs in (
        dict(raw_path=b"/process", query=b"b=2&a=1", body=b""),  # reordered
        dict(raw_path=b"/process", query=b"a=1&b=2", body=b"x"),  # body differs from the empty one
        dict(raw_path=b"/other", query=b"a=1&b=2", body=b""),
    ):
        with pytest.raises(sa.AuthError) as raised:
            verifier(on, "fact-check").verify(token, method="POST", **kwargs)
        assert raised.value.reason == "binding_mismatch"


def test_wrong_audience_and_capability_are_refused(on):
    token = mint("http://fact-check:8000/process")
    with pytest.raises(sa.AuthError) as raised:
        verifier(on, "synthesis").verify(token, method="POST", raw_path=b"/process", query=None, body=b"")
    assert raised.value.reason == "wrong_audience"
    with pytest.raises(sa.AuthError) as raised:
        verifier(on, "fact-check").verify(
            token, method="POST", raw_path=b"/process", query=None, body=b"",
            required_capability="something.else",
        )
    assert raised.value.reason == "capability_not_permitted"


def test_lifetime_is_short_and_claims_name_the_target(on):
    claims = sa._json_object(mint("http://synthesis:8000/process", "synthesis").split(".")[1])
    assert claims["exp"] - claims["iat"] == oa.OUTBOUND_LIFETIME_SECONDS <= 60
    assert claims["aud"] == "synthesis" and claims["cap"] == "specialist.process"


class Transport:
    """Stands in for urlopen: records what would be sent, returns a fixed JSON body."""

    def __init__(self, body=b'{"ok": true}'):
        self.body, self.requests = body, []

    def __call__(self, request, timeout):
        self.requests.append(request)
        return Response(self.body)


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.mark.parametrize("env", [{}, {oa.ENABLE_ENV: "off"}, {oa.ENABLE_ENV: "0"}])
def test_off_by_default_sends_no_header(monkeypatch, env):
    for name in (oa.ENABLE_ENV, oa.KID_ENV, oa.KEY_FILE_ENV):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert oa.outbound_headers("http://fact-check:8000/process", destination="fact-check",
                               method="POST", body=b"{}") == {}
    transport = Transport()
    monkeypatch.setattr(demo, "urlopen", transport)
    assert demo.post_json("http://fact-check:8000/process", {"a": 1}, "rid", "fact-check") == {"ok": True}
    (request,) = transport.requests
    assert dict(request.header_items()) == {"Content-type": "application/json", "X-request-id": "rid"}


def test_destinations_without_a_verifying_route_get_no_token(on):
    for name in ("research", "security", "pixel"):
        assert oa.outbound_headers(f"http://{name}:8000/x", destination=name, method="POST", body=b"") == {}


def test_unreadable_key_fails_closed_and_nothing_is_sent(on, monkeypatch, tmp_path):
    monkeypatch.setenv(oa.KEY_FILE_ENV, str(tmp_path / "missing.key"))
    transport = Transport()
    monkeypatch.setattr(demo, "urlopen", transport)
    with pytest.raises(oa.OutboundAuthError):
        demo.post_json("http://fact-check:8000/process", {"a": 1}, "rid", "fact-check")
    assert transport.requests == []


@pytest.mark.parametrize("case", ["garbage_key", "no_kid", "bad_flag", "bad_target"])
def test_other_failures_are_closed(on, monkeypatch, tmp_path, case):
    url = "http://fact-check:8000/process"
    if case == "garbage_key":
        bad = tmp_path / "bad.key"
        bad.write_text("not a key")
        monkeypatch.setenv(oa.KEY_FILE_ENV, str(bad))
    elif case == "no_kid":
        monkeypatch.delenv(oa.KID_ENV)
    elif case == "bad_flag":
        monkeypatch.setenv(oa.ENABLE_ENV, "maybe")
    else:
        url += "/../x"
    with pytest.raises(oa.OutboundAuthError):
        oa.outbound_headers(url, destination="fact-check", method="POST", body=b"")


def test_key_from_environment_value_and_the_issuer_seam(on, monkeypatch):
    monkeypatch.delenv(oa.KEY_FILE_ENV)
    monkeypatch.setenv(sa.SIGNING_KEY_ENV, raw_seed(on.key))
    assert verify(on, "http://fact-check:8000/process").sub == "web"

    class Signer:  # the seam: a separate signer (option B) would implement this
        def issue(self, **kwargs):
            return "tok"

    headers = oa.outbound_headers("http://fact-check:8000/p", destination="fact-check",
                                  method="POST", body=b"", issuer=Signer())
    assert headers == {sa.ASSERTION_HEADER: "tok"}

    class Broken:
        def issue(self, **kwargs):
            raise RuntimeError("signer down")

    with pytest.raises(oa.OutboundAuthError):
        oa.outbound_headers("http://fact-check:8000/p", destination="fact-check",
                            method="POST", body=b"", issuer=Broken())


def test_logs_never_contain_key_or_token(on, monkeypatch, caplog, tmp_path):
    seed = raw_seed(on.key)
    with caplog.at_level(logging.DEBUG):
        headers = oa.outbound_headers("http://fact-check:8000/process", destination="fact-check",
                                      method="POST", body=b"secret-body", request_id="r1")
        token = headers[sa.ASSERTION_HEADER]
        monkeypatch.setenv(oa.KEY_FILE_ENV, str(tmp_path / "nope"))
        with pytest.raises(oa.OutboundAuthError):
            oa.outbound_headers("http://fact-check:8000/process", destination="fact-check",
                                method="POST", body=b"secret-body", request_id="r2")
        with pytest.raises(oa.OutboundAuthError):
            demo.post_json("http://fact-check:8000/process", {"a": "secret-body"}, "r3", "fact-check")
    text = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert "outbound assertion" in text
    for secret in (token, token.split(".")[2], seed, "secret-body", on.key_file):
        assert secret not in text


def test_end_to_end_post_json_to_specialist_with_auth_enabled(on, monkeypatch, tmp_path):
    module = load_specialist(
        monkeypatch, tmp_path, **{sa.ENABLE_ENV: "on", sa.REGISTRY_ENV: str(on.path)}
    )
    try:
        monkeypatch.setattr(sa.time, "time", REAL_TIME)  # the callee runs on the real clock
        client = TestClient(module.app)

        def transport(request, timeout):
            response = client.post(
                request.selector, content=request.data, headers=dict(request.header_items())
            )
            if response.status_code >= 400:
                raise HTTPError(request.full_url, response.status_code, "err", {}, io.BytesIO(b""))
            return Response(response.content)

        monkeypatch.setattr(demo, "urlopen", transport)

        def call(destination):
            with pytest.raises(HTTPError) as raised:
                demo.post_json("http://testserver/process", {"question": "q"}, "rid", destination)
            return raised.value.code

        # Signed: the callee authenticates it and the request reaches the handler, which fails at
        # the stubbed model (502). That proves the verifier accepted the outbound token.
        assert call("fact-check") == 502
        assert call("synthesis") == 401  # signed for another audience: refused
        monkeypatch.setenv(oa.ENABLE_ENV, "off")
        assert call("fact-check") == 401  # unsigned: refused by the same callee
    finally:
        load_specialist(monkeypatch, tmp_path)
