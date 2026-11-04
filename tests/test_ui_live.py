"""The live panel, tabs and Markdown, run by the page's own script in Node (skipped without Node).

Events come from the real-run fixtures, so the page is exercised against what the server emits.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from test_ui_orchestration import INDEX, NODE, needs_node, text_of, with_class, elements, walk

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "live_events"
HARNESS = Path(__file__).resolve().parent / "js" / "run_stream.mjs"


def load(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())["events"]


def frame(event):
    return f"id: {event['event_id']}\nevent: {event['event_type']}\ndata: {json.dumps(event)}\n\n"


def stream(scenario):
    done = subprocess.run([NODE, str(HARNESS), str(INDEX)], input=json.dumps(scenario),
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def run(name, **extra):
    events = load(name)
    return events, stream({"chunks": [frame(e) for e in events],
                           "headers": {"X-Request-ID": events[0]["request_id"]}, **extra})


@needs_node
def test_rows_appear_before_the_stream_finishes():
    events, result = run("A_successful_request")
    first_final = next(i for i, e in enumerate(events) if e["event_type"] == "final_response")
    before = result["frames"][first_final - 1]
    assert with_class(before, "live-panel") and not with_class(before, "response-card")
    counts = [len(with_class(f, "live-row")) for f in result["frames"][:first_final]]
    assert counts == sorted(counts) and counts[0] >= 1 and counts[-1] > counts[0]


@needs_node
def test_a_successful_stream_ends_with_the_answer_and_three_tabs():
    _, result = run("A_successful_request")
    tree = result["final"]
    assert not with_class(tree, "live-panel") and len(with_class(tree, "response-card")) == 1
    assert [text_of(b) for b in with_class(tree, "tab-button")] == [
        "Execution Timeline", "Agent Interactions", "Developer Metrics"]
    assert result["sendDisabled"] is False and result["intervals"]["cleared"] == 1
    assert [c["url"] for c in result["fetchCalls"]] == ["/api/chat/stream"]


@needs_node
def test_charlie_and_the_specialists_are_named_with_portraits():
    _, result = run("A_successful_request")
    shown = text_of(result["final"])
    for phrase in ("Charlie", "Nova", "Cipher"):
        assert phrase in shown
    sources = {n["attrs"].get("src") for n in elements(result["final"]) if n["tag"] == "img"}
    assert "/images/Nova_headshot.png" in sources and "/images/Charlie_Chatwin_headshot.png" in sources


@needs_node
def test_tabs_switch_and_expose_metrics_without_invented_numbers():
    _, result = run("A_successful_request", clickTabs=["metrics"])
    tree = result["clicks"][0]
    panels = with_class(tree, "tab-panel")
    assert [p["hidden"] for p in panels] == [True, True, False]
    metrics = text_of(panels[2])
    assert "not reported by the services" in metrics and "Request ID" in metrics


@needs_node
def test_a_policy_rejection_is_shown_as_the_applications_decision():
    _, result = run("B_policy_rejection")
    assert "The application declined" in text_of(result["final"])


@needs_node
def test_superseded_evidence_is_explained():
    _, result = run("C_evidence_supersession")
    assert "replaced v" in text_of(result["final"])


@needs_node
@pytest.mark.parametrize("name", ["D_planner_failure", "E_agent_failure"])
def test_failures_end_with_an_error_and_no_answer(name):
    _, result = run(name)
    tree = result["final"]
    assert with_class(tree, "error-card") and not with_class(tree, "answer")
    assert not [n for n in with_class(tree, "response-card") if "error-card" not in n["class"]]
    assert result["sendDisabled"] is False


@needs_node
def test_a_dropped_connection_is_reported_as_unverified_not_as_success():
    events = load("A_successful_request")
    result = stream({"chunks": [frame(e) for e in events[:4]], "error": "reset",
                     "headers": {"X-Request-ID": events[0]["request_id"]}})
    tree = result["final"]
    assert "could not be verified" in text_of(tree)
    assert not [n for n in with_class(tree, "response-card") if "error-card" not in n["class"]]


@needs_node
def test_events_for_another_request_are_refused():
    events = load("A_successful_request")
    result = stream({"chunks": [frame(e) for e in events], "headers": {"X-Request-ID": "someone-else"}})
    assert "could not be verified" in text_of(result["final"])


@needs_node
def test_a_frame_split_across_chunks_is_reassembled():
    events = load("A_successful_request")
    raw = "".join(frame(e) for e in events)
    pieces = [raw[i:i + 37] for i in range(0, len(raw), 37)]
    result = stream({"chunks": pieces, "headers": {"X-Request-ID": events[0]["request_id"]}})
    assert with_class(result["final"], "tab-button")


@needs_node
def test_keepalive_comments_are_ignored():
    events = load("A_successful_request")
    chunks = [": keep-alive\n\n"] + [frame(e) for e in events]
    result = stream({"chunks": chunks, "headers": {"X-Request-ID": events[0]["request_id"]}})
    assert with_class(result["final"], "tab-button")


@needs_node
def test_409_falls_back_to_the_ordinary_endpoint():
    from test_ui_orchestration import chat_body
    result = stream({"status": 409, "ordinaryBody": chat_body(), "chunks": []})
    assert [c["url"] for c in result["fetchCalls"]] == ["/api/chat/stream", "/api/chat"]
    assert with_class(result["final"], "response-card")


@needs_node
def test_hostile_text_in_events_is_never_made_markup():
    events = load("A_successful_request")
    hostile = '<img src=x onerror="alert(1)"><script>alert(2)</script>'
    for event in events:
        if event["event_type"] == "final_response":
            event["data"]["response"]["synthesis"] = {
                "answer": f"# Title\n\n{hostile}\n\n[click](javascript:alert(1))\n\n- one\n- two",
                "key_points": [hostile], "caveats": []}
    result = stream({"chunks": [frame(e) for e in events], "headers": {"X-Request-ID": events[0]["request_id"]}})
    tags = {n["tag"] for n in elements(result["final"])}
    assert "script" not in tags and "a" not in tags
    assert hostile in text_of(result["final"])  # shown literally, as text
    assert [n for n in elements(result["final"]) if n["tag"] == "h4" and text_of(n) == "Title"]


def test_page_has_no_html_injection_or_new_hosts():
    script = re.search(r"<script>\n(.*?)</script>", INDEX.read_text(), re.S).group(1)
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert banned not in script
    assert "https://" not in script
    assert script.count("http://") == 1 and "http://www.w3.org/2000/svg" in script  # the SVG namespace


def test_reduced_motion_and_mobile_rules_exist():
    css = re.search(r'<style id="live-styles">(.*?)</style>', INDEX.read_text(), re.S).group(1)
    assert "prefers-reduced-motion: reduce" in css and "max-width: 640px" in css


@needs_node
def test_the_team_talks_as_a_conversation_with_charlie_left_and_specialists_right():
    events, result = run("A_successful_request")
    first_final = next(i for i, e in enumerate(events) if e["event_type"] == "final_response")
    rows = with_class(result["frames"][first_final - 1], "live-row")
    side = lambda row: next(c for c in row["class"].split() if c.startswith("side-"))
    said = [(side(r), text_of(r)) for r in rows]
    assert any(s == "side-left" and "Nova, can you please take it?" in t for s, t in said)
    assert any(s == "side-right" and t.startswith("Nova") and "I'm on it" in t for s, t in said)
    assert any(s == "side-right" and "Done:" in t for s, t in said)
    assert any(s == "side-center" and "Approved." in t for s, t in said)
    assert "not generated by the agents' models" in text_of(result["frames"][first_final - 1])


@needs_node
def test_each_speaker_names_the_model_the_service_reported():
    events = load("A_successful_request_with_diagnostics")
    for event in events:
        if event["event_type"] == "planner_completed":
            event["data"]["model_alias"] = "planner-m"
    result = stream({"chunks": [frame(e) for e in events], "headers": {"X-Request-ID": events[0]["request_id"]}})
    names = [text_of(n) for n in with_class(result["final"], "live-name")]
    assert "Charlie · planner-m" in names
    assert "Nova · m-r" in names and "Cipher · m-sec" in names
    # the "on it" line also names the model once the service has reported it
    assert names.count("Nova · m-r") == 2


@needs_node
def test_no_model_is_claimed_when_none_was_reported():
    _, result = run("A_successful_request")
    assert not [n for n in with_class(result["final"], "live-name") if "·" in text_of(n)]


@needs_node
def test_the_fact_check_section_shows_the_cited_quote_and_whether_it_was_found():
    from test_ui_orchestration import chat_body
    claims = [
        {"claim": "A", "status": "supported_by_supplied_evidence", "rationale": "r", "follow_up": "f",
         "evidence_quote": "the audit found 4%", "citation_verified": True},
        {"claim": "B", "status": "insufficient_evidence", "rationale": "r", "follow_up": "f",
         "evidence_quote": "an invented line", "citation_verified": False},
        {"claim": "C", "status": "insufficient_evidence", "rationale": "r", "follow_up": "f"},
    ]
    body = chat_body(fact_check={"summary": "s", "claims": claims, "limitations": []})
    result = stream({"status": 409, "ordinaryBody": body, "chunks": []})
    shown = text_of(result["final"])
    assert "“the audit found 4%” (found in the supplied evidence)" in shown
    assert "“an invented line” (not found in the supplied evidence)" in shown
    assert shown.count("Evidence cited") == 2  # none is claimed for a claim with no quote
