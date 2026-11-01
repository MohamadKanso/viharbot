"""The Pixel model-comparison tool: prices, evidence mapping, the coverage plan, the comparison
loop and the command line. A scripted fake replaces the model; nothing is spent."""

from __future__ import annotations

import json

import pytest

from pixel_fakes import ScriptedAgent, manifest_of, stub
from viharbot.pixel import compare
from viharbot.pixel.flag import PPT_FLAG
from viharbot.pixel.models import SlideBatch
from viharbot.pixel.registry import list_layouts
from viharbot.pixel.runner import PixelError
from viharbot.pixel.validate import errors, validate_manifest

EVIDENCE = {"synthesis": "Use passkeys."}
INFO = {"data": [
    {"model_name": "openai/gpt-x", "model_info": {"input_cost_per_token": 0.001, "output_cost_per_token": 0.002}},
    {"model_name": "bare", "model_info": {"input_cost_per_token": 0.5, "output_cost_per_token": 1.0}},
    {"model_name": "unpriced", "model_info": {"input_cost_per_token": None, "output_cost_per_token": 1}},
    {"model_name": "nometa"},
]}


def test_price_for_matches_exact_alias_and_alias_without_provider_prefix():
    assert compare.price_for("openai/gpt-x", INFO) == (0.001, 0.002)
    assert compare.price_for("proxy/bare", INFO) == (0.5, 1.0)


@pytest.mark.parametrize("alias", ["unpriced", "nometa", "unknown/alias"])
def test_price_for_is_none_when_no_usable_price_exists(alias):
    assert compare.price_for(alias, INFO) is None


def test_fetch_prices_keeps_only_priced_aliases(monkeypatch):
    monkeypatch.setattr("viharbot.litellm_client.model_info", lambda: INFO)
    assert compare.fetch_prices(["openai/gpt-x", "unpriced"]) == {"openai/gpt-x": (0.001, 0.002)}


def test_fetch_prices_is_empty_when_litellm_cannot_be_reached(monkeypatch):
    def boom():
        raise OSError("down")

    monkeypatch.setattr("viharbot.litellm_client.model_info", boom)
    assert compare.fetch_prices(["a"]) == {}


def test_evidence_from_chat_maps_each_team_result_by_name():
    chat = {"findings": {"a": 1}, "assessment": {"b": 2}, "fact_check": {"c": 3}, "source_quality": {"d": 4},
            "privacy_review": {"e": 5}, "synthesis": {"f": 6}, "ignored": {"x": 1}}
    evidence = compare.evidence_from_chat(chat)
    assert set(evidence) == {"research", "security", "fact_check", "source_quality",
                             "privacy_review", "synthesis"}
    assert json.loads(evidence["research"]) == {"a": 1}


def test_evidence_from_chat_skips_empty_results_and_fails_when_none_remain():
    assert set(compare.evidence_from_chat({"findings": {"a": 1}, "synthesis": None})) == {"research"}
    with pytest.raises(PixelError, match="no team results"):
        compare.evidence_from_chat({"findings": {}, "synthesis": None})


def test_coverage_manifest_has_one_valid_stub_per_layout_in_four_sections():
    manifest = compare.coverage_manifest(EVIDENCE)
    stubs = manifest.stubs()
    assert len(stubs) == 24 == len(list_layouts())
    assert len(manifest.sections) == 4 and all(len(s.slides) == 6 for s in manifest.sections)
    assert [s.layout_id for s in stubs] == [spec.layout_id for spec in list_layouts()]
    assert errors(validate_manifest(manifest, set(EVIDENCE))) == []


def test_coverage_manifest_cites_the_first_evidence_when_there_is_no_synthesis():
    manifest = compare.coverage_manifest({"research": "r"})
    assert {r for s in manifest.stubs() for r in s.evidence_refs} == {"research"}


def test_deck_stats_counts_text_blocks_and_gaps(tmp_path):
    gapped = ScriptedAgent(manifest_of(stub("A1")), text="[to be supplied]")
    filled = ScriptedAgent(manifest_of(stub("A1")), text="Lorem ipsum")
    compare.compare(["m/gap"], EVIDENCE, "q", tmp_path, factory_for=lambda alias: gapped.factory())
    compare.compare(["m/full"], EVIDENCE, "q", tmp_path, factory_for=lambda alias: filled.factory())
    blocks, gaps = compare.deck_stats(tmp_path / "deck-m_gap-r1.pptx")
    assert blocks > 0 and gaps >= blocks
    full_blocks, full_gaps = compare.deck_stats(tmp_path / "deck-m_full-r1.pptx")
    assert full_blocks == blocks and full_gaps == 0


def fake_factory(agents):
    return lambda alias: agents[alias].factory()


def test_one_failing_alias_does_not_stop_the_others(tmp_path):
    agents = {"m/good": ScriptedAgent(manifest_of(stub("A1"))),
              "m/down": ScriptedAgent(None, fail_with=RuntimeError("PRIVATE detail"))}
    rows = compare.compare(["m/down", "m/good"], EVIDENCE, "q", tmp_path, factory_for=fake_factory(agents))
    by = {r["alias"]: r for r in rows}
    assert by["m/down"]["status"] == "failed" and by["m/down"]["notes"] == "RuntimeError"  # no message leaks
    assert by["m/good"]["status"] == "ok"


def test_a_pixel_error_shows_its_safe_message_in_the_notes(tmp_path):
    agents = {"m/a": ScriptedAgent(manifest_of(stub("A1")))}
    rows = compare.compare(["m/a"], EVIDENCE, "q", tmp_path, max_model_calls=1, factory_for=fake_factory(agents))
    assert rows[0]["status"] == "failed" and rows[0]["notes"] == "model call budget exhausted"


def test_successful_run_reports_counts_tokens_and_files(tmp_path):
    agent = ScriptedAgent(manifest_of(stub("A1"), stub("A2")))
    rows = compare.compare(["m/a"], EVIDENCE, "q", tmp_path / "out", factory_for=lambda alias: agent.factory())
    row = rows[0]
    assert row["status"] == "ok" and row["slides"] == 2 and row["left_out"] == 0
    assert (row["tokens_in"], row["tokens_out"], row["calls"]) == (200, 80, 2)
    assert row["cost"] is None  # no price supplied
    assert row["file"].endswith("deck-m_a-r1.pptx") and (tmp_path / "out" / "deck-m_a-r1.pptx").is_file()


def test_cost_is_tokens_times_price_and_none_without_a_price(tmp_path):
    agents = {"m/priced": ScriptedAgent(manifest_of(stub("A1"))),
              "m/free": ScriptedAgent(manifest_of(stub("A1")))}
    rows = compare.compare(["m/priced", "m/free"], EVIDENCE, "q", tmp_path,
                           prices={"m/priced": (0.01, 0.1)}, factory_for=fake_factory(agents))
    assert rows[0]["cost"] == pytest.approx(200 * 0.01 + 80 * 0.1)
    assert rows[1]["cost"] is None


def test_repeat_writes_distinct_deck_files(tmp_path):
    agent = ScriptedAgent(manifest_of(stub("A1")))
    rows = compare.compare(["m/a"], EVIDENCE, "q", tmp_path, repeat=2, factory_for=lambda a: agent.factory())
    assert [r["run"] for r in rows] == [1, 2]
    assert rows[0]["file"] != rows[1]["file"]
    assert all((tmp_path / f).is_file() for f in ("deck-m_a-r1.pptx", "deck-m_a-r2.pptx"))


def filling_factory(alias):
    """A fake that fills every token of every stub it is asked for (a coverage run)."""
    agent = ScriptedAgent(None, text="Lorem ipsum")
    return agent.factory()


def test_all_layouts_yields_24_slides_with_no_gaps(tmp_path):
    rows = compare.compare(["m/a"], EVIDENCE, "q", tmp_path, all_layouts=True, max_model_calls=16,
                           factory_for=filling_factory)
    assert rows[0]["status"] == "ok", rows[0]
    assert rows[0]["slides"] == 24 and rows[0]["gaps"] == 0 and rows[0]["calls"] == 4


def test_creative_comparison_uses_only_the_brief(tmp_path):
    agent = ScriptedAgent(manifest_of(stub("A1", refs=("brief",))))
    rows = compare.compare(["m/a"], {"ignored": "x"}, "the brief text", tmp_path, creative=True,
                           factory_for=lambda a: agent.factory())
    assert rows[0]["status"] == "ok" and "Illustrative content" in rows[0]["notes"]
    assert all("ignored" not in p for p in agent.prompts)


def test_default_factory_for_builds_agents_through_build_agent(monkeypatch):
    seen = {}
    monkeypatch.setattr("viharbot.model_agent.build_agent",
                        lambda *, model_alias, system_prompt: seen.update(alias=model_alias) or "agent")
    factory = compare.agent_factory_for("m/a", creative=True)
    assert factory() == "agent" and seen["alias"] == "m/a"


def rows_fixture():
    return [
        {"alias": "m/a", "run": 1, "status": "ok", "slides": 4, "left_out": 1, "blocks": 10, "gaps": 2,
         "calls": 3, "over_limit": 0, "tokens_in": 100, "tokens_out": 50, "cost": 0.5, "seconds": 2.0,
         "notes": "note-one"},
        {"alias": "m/a", "run": 2, "status": "ok", "slides": 2, "left_out": 0, "blocks": 5, "gaps": 0,
         "calls": 3, "over_limit": 1, "tokens_in": 1, "tokens_out": 1, "cost": None, "seconds": 4.0},
        {"alias": "m/b", "run": 1, "status": "failed", "seconds": 0.1, "notes": "boom"},
    ]


def test_format_table_has_a_row_per_run_averages_and_notes():
    table = compare.format_table(rows_fixture())
    assert "m/a | 1 | ok | 4 | 1 | 2/10 | 0 | 3 | 100/50 | $0.5000 | $0.1250 | 2.0" in table
    assert "m/a | 2 | ok | 2 | 0 | 0/5 | 1 | 3 | 1/1 | n/a | n/a | 4.0" in table
    assert "m/b | 1 | failed | - | - | - | - | - | - | n/a | n/a | 0.1" in table
    assert "averages over successful runs:" in table
    assert "m/a: 2 runs | slides 3.0 | left out 0.5 | sec 3.0 | cost $0.5000" in table
    assert "m/b: " not in table.split("averages over successful runs:")[1].split("\n\n")[0]
    assert "m/a run 1: note-one" in table and "m/b run 1: boom" in table


def test_format_table_has_no_averages_for_single_runs():
    table = compare.format_table(rows_fixture()[:1])
    assert "averages" not in table


# ---- command line ---------------------------------------------------------------------------------
def test_main_refuses_when_the_flag_is_not_true(monkeypatch, capsys):
    monkeypatch.setenv(PPT_FLAG, "false")
    assert compare.main(["--creative", "m/a"]) == 2
    assert "refusing to spend model calls" in capsys.readouterr().err


def test_main_requires_evidence_unless_creative(monkeypatch):
    monkeypatch.setenv(PPT_FLAG, "true")
    with pytest.raises(SystemExit) as exc:
        compare.main(["m/a"])
    assert exc.value.code == 2


def test_main_creative_run_prints_the_table_and_returns_zero(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv(PPT_FLAG, "true")
    agent = ScriptedAgent(manifest_of(stub("A1", refs=("brief",))))
    monkeypatch.setattr(compare, "agent_factory_for", lambda alias, creative=False: agent.factory())
    monkeypatch.setattr(compare, "fetch_prices", lambda aliases: {})
    code = compare.main(["--creative", "--question", "q", "--out", str(tmp_path), "m/a"])
    captured = capsys.readouterr()
    assert code == 0
    assert "m/a | 1 | ok" in captured.out
    assert "no LiteLLM price found for m/a" in captured.err


def test_main_returns_one_when_a_model_fails_and_reads_the_evidence_file(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv(PPT_FLAG, "true")
    chat = tmp_path / "chat.json"
    chat.write_text(json.dumps({"synthesis": {"answer": "a"}}), encoding="utf-8")
    agent = ScriptedAgent(None, fail_with=RuntimeError("x"))
    monkeypatch.setattr(compare, "agent_factory_for", lambda alias, creative=False: agent.factory())
    monkeypatch.setattr(compare, "fetch_prices", lambda aliases: {a: (1.0, 1.0) for a in aliases})
    code = compare.main(["--evidence", str(chat), "--out", str(tmp_path / "o"), "--repeat", "99", "m/a"])
    out = capsys.readouterr().out
    assert code == 1 and out.count("m/a | ") == 10  # --repeat is clamped to 10
