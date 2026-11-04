"""Body size cap before service authentication (ADR-006 A2, issue #101). Offline, no inference."""

from __future__ import annotations

import asyncio
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from test_service_auth import load_specialist, raw_seed, registry_text
from viharbot import body_limit as bl
from viharbot import service_auth as sa

LIMIT = 2048


def drive(*, method="POST", path="/process", headers=(), chunks=()):
    """Run one ASGI request. Returns (status, body, receive_calls, app_called)."""
    state = {"calls": 0, "app": False}
    sent: list[dict] = []
    queue = list(chunks)

    async def inner(scope, receive, send):
        state["app"] = True
        while True:
            message = await receive()
            if not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def receive():
        state["calls"] += 1
        if queue:
            body = queue.pop(0)
            return {"type": "http.request", "body": body, "more_body": bool(queue)}
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    middleware = bl.BodySizeLimitMiddleware(
        inner, max_bytes=LIMIT, paths=frozenset({"/process"}), agent="t"
    )
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": [(k.encode(), v.encode()) for k, v in headers],
    }
    asyncio.run(middleware(scope, receive, send))
    status = sent[0]["status"]
    return status, sent[-1]["body"], state["calls"], state["app"]


def test_content_length_over_limit_is_refused_before_the_app_and_without_reading():
    status, body, reads, app_called = drive(
        headers=[("content-length", str(LIMIT + 1))], chunks=[b"x" * 10]
    )
    assert status == 413 and reads == 0 and not app_called
    assert json.loads(body) == {"detail": "request body too large"}


def test_chunked_stream_over_limit_stops_reading_as_soon_as_it_crosses():
    chunks = [b"x" * 1024] * 10
    status, _, reads, _ = drive(chunks=chunks)
    assert status == 413
    assert reads == 3  # 1024, 2048 (at the limit), 3072 (over): the other seven are never read


def test_understated_content_length_is_still_cut_off_by_the_stream_count():
    status, _, reads, _ = drive(headers=[("content-length", "10")], chunks=[b"x" * 3000])
    assert status == 413 and reads == 1


def test_body_exactly_at_the_limit_passes_through():
    status, body, _, app_called = drive(
        headers=[("content-length", str(LIMIT))], chunks=[b"x" * 1024, b"x" * 1024]
    )
    assert (status, body, app_called) == (200, b"ok", True)


def test_other_paths_and_methods_and_scopes_are_not_capped():
    big = [b"x" * (LIMIT * 4)]
    assert drive(path="/health", chunks=big)[0] == 200
    assert drive(method="GET", chunks=big)[0] == 200

    seen = []

    async def inner(scope, receive, send):
        seen.append(scope["type"])

    middleware = bl.BodySizeLimitMiddleware(inner, max_bytes=1, paths=frozenset(), agent="t")
    asyncio.run(middleware({"type": "lifespan"}, None, None))
    assert seen == ["lifespan"]


def test_absurdly_long_content_length_is_refused_without_conversion_error():
    status, _, reads, app_called = drive(
        headers=[("content-length", "9" * 5000)], chunks=[b"x"]
    )
    assert status == 413 and reads == 0 and not app_called


def test_invalid_content_length_falls_back_to_the_stream_count():
    assert drive(headers=[("content-length", "abc")], chunks=[b"x" * 10])[0] == 200


def test_refusal_after_the_response_started_is_not_swallowed():
    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await receive()

    async def receive():
        return {"type": "http.request", "body": b"x" * (LIMIT + 1), "more_body": False}

    async def send(_message):
        return None

    middleware = bl.BodySizeLimitMiddleware(
        inner, max_bytes=LIMIT, paths=frozenset({"/process"}), agent="t"
    )
    scope = {"type": "http", "method": "POST", "path": "/process", "headers": []}
    with pytest.raises(bl._TooLarge):
        asyncio.run(middleware(scope, receive, send))


def test_limit_configuration():
    assert bl.max_body_bytes_from_environment({}) == bl.DEFAULT_MAX_BODY_BYTES
    assert bl.max_body_bytes_from_environment({bl.LIMIT_ENV: " 5000 "}) == 5000
    for bad in ("abc", "0", "1023", str(16 * 1024 * 1024 + 1), "-5"):
        with pytest.raises(bl.BodyLimitConfigError):
            bl.max_body_bytes_from_environment({bl.LIMIT_ENV: bad})


def test_default_limit_budget_sanity_check():
    # question cap 6000 chars (up to 4 bytes each) plus ample room for structured results.
    assert bl.DEFAULT_MAX_BODY_BYTES >= 6000 * 4 + 200_000


# --- specialist wiring --------------------------------------------------------------------------


def auth_specialist(monkeypatch, tmp_path, **extra):
    key = Ed25519PrivateKey.generate()
    registry = tmp_path / "registry.toml"
    registry.write_text(registry_text(("web-1", "web", key, ["specialist.process"])))
    (tmp_path / "k").write_text(raw_seed(key))
    module = load_specialist(
        monkeypatch,
        tmp_path,
        **{sa.ENABLE_ENV: "on", sa.REGISTRY_ENV: str(registry), bl.LIMIT_ENV: str(LIMIT), **extra},
    )
    calls = {"verify": 0}
    real = module._authenticator.verify

    def spy(*args, **kwargs):
        calls["verify"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(module._authenticator, "verify", spy)
    return module, calls


@pytest.mark.parametrize("path", ["/process", "/invocations"])
def test_over_limit_unauthenticated_request_never_reaches_verification_or_handler(
    monkeypatch, tmp_path, path
):
    module, calls = auth_specialist(monkeypatch, tmp_path)
    handled = []
    monkeypatch.setattr(module, "build_agent", lambda **_k: handled.append(1))
    client = TestClient(module.app)
    response = client.post(path, content=b"x" * (LIMIT + 1))
    assert response.status_code == 413
    assert calls["verify"] == 0 and handled == []
    # Chunked (no Content-Length) is refused as well.
    chunked = client.post(path, content=(b"x" * 512 for _ in range(8)))
    assert chunked.status_code == 413 and calls["verify"] == 0 and handled == []


def test_in_limit_requests_still_get_the_normal_authentication_result(monkeypatch, tmp_path):
    module, calls = auth_specialist(monkeypatch, tmp_path)
    client = TestClient(module.app)
    assert client.post("/process", json={}).status_code == 401  # no assertion: verified, refused
    assert calls["verify"] == 1
    assert client.get("/health").status_code == 200
    assert client.get("/model").status_code == 200
    # /health is not capped even with a body over the limit.
    assert client.request("GET", "/health", content=b"x" * (LIMIT * 2)).status_code == 200


def test_default_limit_applies_when_the_variable_is_unset(monkeypatch, tmp_path):
    module, calls = auth_specialist(monkeypatch, tmp_path, **{bl.LIMIT_ENV: ""})
    client = TestClient(module.app)
    assert client.post("/process", content=b"x" * 10_000).status_code == 401
    over = client.post("/process", content=b"x" * (bl.DEFAULT_MAX_BODY_BYTES + 1))
    assert over.status_code == 413 and calls["verify"] == 1


def test_cap_is_not_installed_when_authentication_is_off(monkeypatch, tmp_path):
    module = load_specialist(monkeypatch, tmp_path, **{bl.LIMIT_ENV: str(LIMIT)})
    assert module._authenticator is None
    assert all(m.cls is not bl.BodySizeLimitMiddleware for m in module.app.user_middleware)
    client = TestClient(module.app)
    response = client.post("/process", content=b"x" * (LIMIT * 4))
    assert response.status_code not in (401, 403, 413)
    load_specialist(monkeypatch, tmp_path)
