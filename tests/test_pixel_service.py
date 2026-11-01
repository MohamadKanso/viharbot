"""The pixel service: /process, the retained decks and the download routes. A scripted fake
replaces the model, so no LiteLLM call or key is involved."""

from __future__ import annotations

import os
import time

import pytest
from fastapi.testclient import TestClient

from pixel_fakes import ScriptedAgent, manifest_of, stub
from viharbot import pixel_agent
from viharbot.pixel.flag import PPT_FLAG
from viharbot.schemas import DeckResult

PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
CONTEXT = {"synthesis": {"answer": "Use passkeys."}, "research": {"summary": "s"}}


@pytest.fixture
def svc(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_DIR", str(tmp_path))
    monkeypatch.setenv("PIXEL_MODEL", "pixel/alias")
    monkeypatch.setenv(PPT_FLAG, "true")
    for name in ("PIXEL_TARGET_SLIDES", "PIXEL_MAX_MODEL_CALLS", "PIXEL_MAX_DECKS"):
        monkeypatch.delenv(name, raising=False)
    agent = ScriptedAgent(manifest_of(stub("A1"), stub("A2")))
    monkeypatch.setattr(pixel_agent, "build_agent", lambda **kw: agent)
    return TestClient(pixel_agent.app), agent, tmp_path / "pixel" / "decks"


def process(client, context=CONTEXT, **kwargs):
    return client.post("/process", json={"question": "Make a deck on passkeys", "context": context}, **kwargs)


def test_health_and_model(svc, monkeypatch):
    client, _, _ = svc
    assert client.get("/health").json() == {"status": "ok", "agent": "pixel"}
    assert client.get("/model").json() == {"agent": "pixel", "model_alias": "pixel/alias"}
    monkeypatch.delenv("PIXEL_MODEL")
    assert client.get("/model").json()["model_alias"] is None


def test_process_builds_a_deck_and_stores_pptx_and_html(svc):
    client, agent, folder = svc
    reply = process(client, headers={"X-Request-ID": "req-1"})
    assert reply.status_code == 200 and reply.headers["x-request-id"] == "req-1"
    body = reply.json()
    assert body["agent"] == "pixel" and body["request_id"] == "req-1"
    assert body["requested_model_alias"] == "pixel/alias"
    result = DeckResult.model_validate(body["result"])
    assert result.slide_count == 2 and result.model_calls == 2 and result.rejected_slides == []
    assert (folder / f"{result.deck_id}.pptx").is_file()
    assert "<html" in (folder / f"{result.deck_id}.html").read_text(encoding="utf-8").lower()
    assert result.deck_id.startswith("deck-") and len(result.deck_id) == len("deck-") + 12


def test_process_generates_a_request_id_when_none_is_sent(svc):
    client, _, _ = svc
    assert process(client).headers["x-request-id"]


def test_only_the_known_evidence_keys_of_the_context_are_used(svc):
    client, agent, _ = svc
    context = {**CONTEXT, "unrelated": "LEAKED-CONTEXT-VALUE", "synthesis_extra": "LEAKED-TWO"}
    assert process(client, context).status_code == 200
    joined = "\n".join(agent.prompts)
    assert "Use passkeys." in joined and "LEAKED" not in joined
    assert "--- research ---" in joined and "--- unrelated ---" not in joined


def test_process_is_503_when_the_flag_is_off_and_no_model_runs(svc, monkeypatch):
    client, agent, folder = svc
    monkeypatch.setenv(PPT_FLAG, "false")
    reply = process(client, headers={"X-Request-ID": "req-off"})
    assert reply.status_code == 503 and reply.headers["x-request-id"] == "req-off"
    assert agent.prompts == [] and not folder.exists()


def test_process_with_no_evidence_is_a_sanitized_502(svc):
    client, _, _ = svc
    reply = process(client, context={"unknown": "x"})
    assert reply.status_code == 502 and reply.json() == {"detail": "pixel processing failed"}


def test_a_model_failure_is_a_sanitized_502(svc, monkeypatch):
    client, _, _ = svc
    broken = ScriptedAgent(None, fail_with=RuntimeError("PRIVATE-PROVIDER-DETAIL sk-secret"))
    monkeypatch.setattr(pixel_agent, "build_agent", lambda **kw: broken)
    reply = process(client)
    assert reply.status_code == 502
    assert reply.json() == {"detail": "pixel processing failed"} and "PRIVATE" not in reply.text


def test_an_exhausted_call_budget_is_a_502(svc, monkeypatch):
    client, agent, _ = svc
    monkeypatch.setenv("PIXEL_MAX_MODEL_CALLS", "2")
    agent.broken = {"A1": 9}  # the retry needs a third call
    reply = process(client)
    assert reply.status_code == 502 and reply.json() == {"detail": "pixel processing failed"}


def test_process_validates_the_request_body(svc):
    client, _, _ = svc
    assert client.post("/process", json={"question": ""}).status_code == 422


# ---- retention ---------------------------------------------------------------------------------
def make_decks(folder, count):
    folder.mkdir(parents=True)
    now = time.time()
    for i in range(count):
        for ext in ("pptx", "html"):
            path = folder / f"deck-{i:012x}.{ext}"
            path.write_text("x")
            os.utime(path, (now - 1000 + i, now - 1000 + i))  # higher i is newer


def test_prune_decks_keeps_the_newest_and_removes_matching_html(svc):
    _, _, folder = svc
    make_decks(folder, 4)
    pixel_agent.prune_decks(2)
    assert sorted(p.name for p in folder.iterdir()) == [
        "deck-000000000002.html", "deck-000000000002.pptx",
        "deck-000000000003.html", "deck-000000000003.pptx"]


def test_process_prunes_to_pixel_max_decks(svc, monkeypatch):
    client, _, folder = svc
    monkeypatch.setenv("PIXEL_MAX_DECKS", "1")
    ids = [process(client).json()["result"]["deck_id"] for _ in range(3)]
    left = {p.stem for p in folder.glob("*.pptx")}
    assert len(left) == 1 and left <= set(ids)
    assert {p.stem for p in folder.glob("*.html")} == left


@pytest.mark.parametrize("raw, expected", [
    (None, 5), ("7", 7), ("0", 2), ("-3", 2), ("999", 40), ("many", 5), ("", 5)])
def test_int_env_bounds_and_bad_values(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("PIXEL_TEST_INT", raising=False)
    else:
        monkeypatch.setenv("PIXEL_TEST_INT", raw)
    assert pixel_agent._int_env("PIXEL_TEST_INT", 5, 2, 40) == expected


# ---- downloads ---------------------------------------------------------------------------------
def test_an_issued_deck_id_downloads_as_pptx_and_html(svc):
    """Regression: every valid id once 404'd."""
    client, _, folder = svc
    deck_id = process(client).json()["result"]["deck_id"]

    pptx = client.get(f"/decks/{deck_id}.pptx")
    assert pptx.status_code == 200 and pptx.headers["content-type"] == PPTX
    assert pptx.content == (folder / f"{deck_id}.pptx").read_bytes()
    assert deck_id in pptx.headers["content-disposition"]

    html = client.get(f"/decks/{deck_id}.html")
    assert html.status_code == 200 and html.headers["content-type"].startswith("text/html")
    csp = html.headers["content-security-policy"]
    assert csp == "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"
    assert html.headers["x-content-type-options"] == "nosniff"
    assert "<html" in html.text.lower()


@pytest.mark.parametrize("path", [
    "/decks/deck-abc.pptx",                       # malformed
    "/decks/deck-0123456789ABCD.pptx",            # wrong length and case
    "/decks/deck-0123456789ab.html.pptx",
    "/decks/..%2f..%2fetc%2fpasswd.pptx",         # traversal
    "/decks/%2e%2e%2fsecret.html",
    "/decks/deck-0123456789ab.pptx",              # well-formed but never issued
    "/decks/deck-0123456789ab.html",
])
def test_malformed_traversal_and_unknown_ids_are_404(svc, path):
    client, _, _ = svc
    assert client.get(path).status_code == 404


def test_a_deck_with_only_one_file_404s_for_the_other_kind(svc):
    client, _, folder = svc
    deck_id = process(client).json()["result"]["deck_id"]
    (folder / f"{deck_id}.html").unlink()
    assert client.get(f"/decks/{deck_id}.html").status_code == 404
    assert client.get(f"/decks/{deck_id}.pptx").status_code == 200


def test_a_directory_with_a_deck_name_is_not_served(svc):
    client, _, folder = svc
    (folder / "deck-0123456789ab.pptx").mkdir(parents=True)
    assert client.get("/decks/deck-0123456789ab.pptx").status_code == 404
