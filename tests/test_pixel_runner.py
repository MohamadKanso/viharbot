"""Pixel runner: in-memory build, retries, budget, creative mode, evidence loading and the
disk-backed plan/generate/render path. The model is a scripted fake; nothing leaves the process."""

from __future__ import annotations

import pytest
from pptx import Presentation

from pixel_fakes import (
    LAYOUT, ScriptedAgent, invalid_payload_for, manifest_of, payload_for, stub)
from viharbot.pixel import runner
from viharbot.pixel.models import (
    DeckManifest, EngagementProfile, Issue, SlideBatch)
from viharbot.pixel.runner import (
    CREATIVE_WARNING, MAX_EVIDENCE_CHARS, ModelBudget, PixelError, build_deck_in_memory)

ENGAGEMENT = EngagementProfile(scope="Brief about passkeys")
EVIDENCE = {"synthesis": "Use passkeys for the workforce."}


def build(agent, tmp_path, evidence=EVIDENCE, target=2, **kwargs):
    return build_deck_in_memory("deck-t", ENGAGEMENT, evidence, target, tmp_path / "out.pptx",
                                agent_factory=agent.factory(), **kwargs)


def test_happy_path_plans_fills_and_writes_an_openable_pptx(tmp_path):
    agent = ScriptedAgent(manifest_of(stub("A1"), stub("A2")))
    built = build(agent, tmp_path)
    assert agent.models == [DeckManifest, SlideBatch]
    assert built.model_calls == 2 and built.slide_count == 2 and built.rejected == []
    assert (built.input_tokens, built.output_tokens) == (200, 80)
    assert [p.slide_id for p in built.payloads] == ["A1", "A2"]
    assert len(Presentation(str(tmp_path / "out.pptx")).slides) == 2


def test_slides_follow_the_plan_order_across_sections_and_batches(tmp_path):
    agent = ScriptedAgent(manifest_of(stub("A1"), stub("A2"), stub("A3"), per_section=2))
    built = build(agent, tmp_path, batch_size=1, max_model_calls=8, target=3)
    assert [p.slide_id for p in built.payloads] == ["A1", "A2", "A3"]
    assert built.model_calls == 4  # one plan + one per batch of one


def test_a_failing_slide_is_retried_once_and_then_accepted(tmp_path):
    agent = ScriptedAgent(manifest_of(stub("A1"), stub("A2")), broken={"A2": 1})
    built = build(agent, tmp_path)
    assert len(agent.prompts) == 3 and built.model_calls == 3
    assert "These slides were rejected" in agent.prompts[2]
    assert [p.slide_id for p in built.payloads] == ["A1", "A2"] and built.rejected == []


def test_a_slide_that_keeps_failing_is_left_out_with_a_content_free_reason(tmp_path):
    agent = ScriptedAgent(manifest_of(stub("A1"), stub("A2")), broken={"A2": 5}, text="SECRET-SLIDE-TEXT")
    built = build(agent, tmp_path)
    assert built.rejected == ["A2"] and [p.slide_id for p in built.payloads] == ["A1"]
    assert len(agent.prompts) == 3
    assert any("A2 left out" in w for w in built.warnings)
    assert all("SECRET-SLIDE-TEXT" not in w for w in built.warnings)


def test_a_missing_slide_in_the_batch_counts_as_failed_and_is_redone(tmp_path):
    class Dropper(ScriptedAgent):
        dropped = False

        def __call__(self, prompt, *, structured_output_model):
            out = super().__call__(prompt, structured_output_model=structured_output_model)
            if structured_output_model is SlideBatch and not self.dropped:
                self.dropped = True
                out.structured_output = SlideBatch(slides=out.structured_output.slides[:1])
            return out

    agent = Dropper(manifest_of(stub("A1"), stub("A2")))
    built = build(agent, tmp_path)
    assert [p.slide_id for p in built.payloads] == ["A1", "A2"] and built.model_calls == 3


def test_when_nothing_passes_validation_the_build_fails(tmp_path):
    agent = ScriptedAgent(manifest_of(stub("A1")), broken={"A1": 5})
    with pytest.raises(PixelError, match="no slide passed validation"):
        build(agent, tmp_path)


def test_the_call_budget_is_a_hard_cap(tmp_path):
    agent = ScriptedAgent(manifest_of(stub("A1")))
    with pytest.raises(PixelError, match="model call budget exhausted"):
        build(agent, tmp_path, max_model_calls=1)
    assert len(agent.prompts) == 1  # the second call was never made


def test_an_invalid_manifest_is_retried_once_then_rejected(tmp_path):
    bad = manifest_of(stub("A1", refs=("not-supplied",)))
    agent = ScriptedAgent(bad)
    with pytest.raises(PixelError, match="manifest failed validation"):
        build(agent, tmp_path)
    assert len(agent.prompts) == 2 and "previous manifest was rejected" in agent.prompts[1]


def test_an_invalid_manifest_that_is_fixed_on_retry_proceeds(tmp_path):
    agent = ScriptedAgent(manifests=[manifest_of(stub("A1", refs=("nope",))), manifest_of(stub("A1"))])
    assert build(agent, tmp_path).slide_count == 1


def test_a_manifest_planning_more_than_twice_the_target_is_refused(tmp_path):
    agent = ScriptedAgent(manifest_of(*[stub(f"A{i}") for i in range(1, 4)]))
    with pytest.raises(PixelError, match="too many slides"):
        build(agent, tmp_path, target=1)


@pytest.mark.parametrize("kwargs, evidence, message", [
    ({}, {}, "no evidence supplied"),
    ({"target": 0}, EVIDENCE, "target_slides"),
    ({"target": 31}, EVIDENCE, "target_slides"),
    ({"batch_size": 0}, EVIDENCE, "batch_size"),
    ({"batch_size": 11}, EVIDENCE, "batch_size"),
])
def test_bad_inputs_are_rejected_before_any_model_call(tmp_path, kwargs, evidence, message):
    agent = ScriptedAgent(manifest_of(stub("A1")))
    with pytest.raises(PixelError, match=message):
        build(agent, tmp_path, evidence=evidence, **kwargs)
    assert agent.prompts == []


def test_creative_mode_prompts_carry_only_the_brief_and_force_proposed(tmp_path):
    agent = ScriptedAgent(manifest_of(stub("A1", refs=("brief",))), claim_status="implemented")
    built = build(agent, tmp_path, evidence={"brief": "Brief: sell passkeys"}, creative=True)
    for prompt in agent.prompts:
        assert "Evidence documents" not in prompt and "Framework keys" not in prompt
        assert "Frameworks:" not in prompt
        assert ENGAGEMENT.scope in prompt
    assert [p.claim_status for p in built.payloads] == ["proposed"]
    assert built.warnings[0] == CREATIVE_WARNING


def test_non_creative_prompts_include_evidence_and_frameworks(tmp_path):
    agent = ScriptedAgent(manifest_of(stub("A1")), claim_status="implemented")
    built = build(agent, tmp_path)
    assert all("Evidence documents" in p for p in agent.prompts)
    assert "Framework keys" in agent.prompts[0]
    assert [p.claim_status for p in built.payloads] == ["implemented"]  # not forced outside creative


def test_a_supplied_manifest_skips_the_planner_call(tmp_path):
    agent = ScriptedAgent(None)
    built = build(agent, tmp_path, manifest=manifest_of(stub("A1")))
    assert agent.models == [SlideBatch] and built.model_calls == 1


def test_a_supplied_manifest_that_does_not_validate_is_refused(tmp_path):
    agent = ScriptedAgent(None)
    with pytest.raises(PixelError, match="manifest failed validation"):
        build(agent, tmp_path, manifest=manifest_of(stub("A1", refs=("missing",))))
    assert agent.prompts == []


def test_batch_note_reaches_the_prompt(tmp_path):
    agent = ScriptedAgent(manifest_of(stub("A1")))
    build(agent, tmp_path, batch_note="LOREM-NOTE")
    assert "Test note: LOREM-NOTE" in agent.prompts[1]


def test_payload_warnings_are_surfaced_and_truncated(tmp_path):
    class Warner(ScriptedAgent):
        def __call__(self, prompt, *, structured_output_model):
            out = super().__call__(prompt, structured_output_model=structured_output_model)
            if structured_output_model is SlideBatch:
                out.structured_output = SlideBatch(slides=[
                    p.model_copy(update={"warnings": ["w" * 500]}) for p in out.structured_output.slides])
            return out

    built = build(Warner(manifest_of(stub("A1"))), tmp_path)
    assert built.warnings == ["w" * 200]


def test_unexpected_structured_output_is_an_error():
    class Wrong:
        def __call__(self, prompt, *, structured_output_model):
            return type("O", (), {"structured_output": None})()

    with pytest.raises(PixelError, match="unexpected structured result"):
        runner._call(lambda: Wrong(), "p", DeckManifest)


def test_usage_without_metrics_is_ignored(tmp_path):
    built = build(ScriptedAgent(manifest_of(stub("A1")), usage=None), tmp_path)
    assert (built.input_tokens, built.output_tokens) == (0, 0)


def test_model_budget_ignores_non_dict_usage_and_none_counts():
    budget = ModelBudget(2)
    budget.add_usage("nope")
    budget.add_usage({"inputTokens": None, "outputTokens": 3})
    assert (budget.input_tokens, budget.output_tokens) == (0, 3)
    budget.spend(), budget.spend()
    with pytest.raises(PixelError):
        budget.spend()


# ---- helpers ------------------------------------------------------------------------------------
def test_batches_split_in_order():
    stubs = [stub(f"A{i}") for i in range(5)]
    assert [[s.slide_id for s in b] for b in runner._batches(stubs, 2)] == [
        ["A0", "A1"], ["A2", "A3"], ["A4"]]


def test_force_proposed_only_changes_creative_batches():
    batch = SlideBatch(slides=[payload_for(stub("A1"), claim_status="implemented")])
    assert runner._force_proposed(batch, False) is batch
    assert runner._force_proposed(batch, True).slides[0].claim_status == "proposed"


def test_failed_ids_covers_errors_missing_slides_and_batch_wide_errors():
    stubs = [stub("A1"), stub("A2"), stub("A3")]
    batch = SlideBatch(slides=[payload_for(stubs[0]), payload_for(stubs[1])])
    issues = [Issue(slide_id="A1", severity="error", message="m"),
              Issue(slide_id="A2", severity="warning", message="m"),
              Issue(slide_id="other", severity="error", message="m")]
    assert runner._failed_ids(issues, stubs, batch) == {"A1", "A3"}
    batch_error = [Issue(slide_id="batch", severity="error", message="duplicate")]
    full = SlideBatch(slides=[payload_for(s) for s in stubs])
    assert runner._failed_ids(batch_error, stubs, full) == {"A1", "A2", "A3"}


# ---- evidence loading ----------------------------------------------------------------------------
@pytest.fixture
def evidence_root(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setenv("PIXEL_EVIDENCE_ROOT", str(root))
    return root


def test_load_evidence_reads_files_under_the_root(evidence_root):
    (evidence_root / "sub").mkdir()
    (evidence_root / "sub" / "a.md").write_text("alpha", encoding="utf-8")
    assert runner.load_evidence(["sub/a.md"]) == {"sub/a.md": "alpha"}


def test_load_evidence_truncates_long_files(evidence_root):
    (evidence_root / "big.txt").write_text("x" * (MAX_EVIDENCE_CHARS + 50), encoding="utf-8")
    assert len(runner.load_evidence(["big.txt"])["big.txt"]) == MAX_EVIDENCE_CHARS


def test_load_evidence_refuses_paths_outside_the_root_or_missing(evidence_root, tmp_path):
    (tmp_path / "outside.md").write_text("secret", encoding="utf-8")
    for path in ("../outside.md", "nope.md", str(tmp_path / "outside.md")):
        with pytest.raises(PixelError, match="outside the evidence root or missing"):
            runner.load_evidence([path])


def test_load_evidence_refuses_disallowed_suffixes_and_too_many_files(evidence_root):
    (evidence_root / "a.py").write_text("print(1)", encoding="utf-8")
    with pytest.raises(PixelError, match="not allowed"):
        runner.load_evidence(["a.py"])
    with pytest.raises(PixelError, match="at most"):
        runner.load_evidence(["a.md"] * (runner.MAX_EVIDENCE_FILES + 1))


# ---- default factory -----------------------------------------------------------------------------
def test_default_agent_factory_requires_pixel_model(monkeypatch):
    monkeypatch.delenv("PIXEL_MODEL", raising=False)
    with pytest.raises(PixelError, match="PIXEL_MODEL is not set"):
        runner.default_agent_factory()


def test_default_agent_factory_builds_through_build_agent(monkeypatch):
    seen = {}

    def fake_build_agent(*, model_alias, system_prompt):
        seen.update(alias=model_alias, prompt=system_prompt)
        return "agent"

    monkeypatch.setenv("PIXEL_MODEL", "pixel/alias")
    monkeypatch.setattr("viharbot.model_agent.build_agent", fake_build_agent)
    assert runner.default_agent_factory() == "agent"
    assert seen["alias"] == "pixel/alias" and seen["prompt"]


# ---- disk-backed path ----------------------------------------------------------------------------
@pytest.fixture
def disk(tmp_path, monkeypatch, evidence_root):
    monkeypatch.setenv("MEMORY_DIR", str(tmp_path / "mem"))
    (evidence_root / "ev.md").write_text("Passkeys reduce phishing.", encoding="utf-8")
    return evidence_root


def plan(agent, deck_id="deck-disk", target=4):
    return runner.plan_ciso_deck(ENGAGEMENT, ["ev.md"], target, deck_id=deck_id,
                                 agent_factory=agent.factory())


def test_plan_generate_and_render_round_trip_and_resume(disk, tmp_path):
    stubs = [stub("A1", refs=("ev.md",)), stub("A2", refs=("ev.md",)), stub("A3", refs=("ev.md",))]
    agent = ScriptedAgent(manifest_of(*stubs, per_section=3))
    manifest = plan(agent)
    assert manifest.deck_id == "deck-disk" and agent.models == [DeckManifest]

    results = runner.generate_deck("deck-disk", batch_size=2, agent_factory=agent.factory())
    assert [r.accepted for r in results] == [["A1", "A2"], ["A3"]] and agent.models.count(SlideBatch) == 2

    again = ScriptedAgent(None)
    assert runner.generate_deck("deck-disk", batch_size=2, agent_factory=again.factory()) == []
    assert again.prompts == []  # every batch was already saved

    out = tmp_path / "deck.pptx"
    assert runner.render_deck("deck-disk", out) == 3
    assert len(Presentation(str(out)).slides) == 3


def test_generate_deck_retries_a_rejected_batch_once(disk):
    agent = ScriptedAgent(manifest_of(stub("A1", refs=("ev.md",))), broken={"A1": 1})
    plan(agent)
    results = runner.generate_deck("deck-disk", agent_factory=agent.factory())
    assert results[0].accepted == ["A1"] and len(agent.prompts) == 3


def test_generate_deck_leaves_a_twice_rejected_batch_unsaved(disk):
    agent = ScriptedAgent(manifest_of(stub("A1", refs=("ev.md",))), broken={"A1": 5})
    plan(agent)
    results = runner.generate_deck("deck-disk", agent_factory=agent.factory())
    assert results[0].rejected == ["A1"]
    with pytest.raises(PixelError, match="no accepted payload"):
        runner.render_deck("deck-disk", disk / "never.pptx")


def test_render_deck_allow_partial_renders_the_accepted_slides(disk, tmp_path):
    stubs = [stub("A1", refs=("ev.md",)), stub("A2", refs=("ev.md",))]
    agent = ScriptedAgent(manifest_of(*stubs))
    plan(agent)
    batch = SlideBatch(slides=[payload_for(stubs[0], evidence_refs=["ev.md"])])
    assert runner.render_deck_batch("deck-disk", batch).accepted == ["A1"]
    with pytest.raises(PixelError, match="1 planned slides"):
        runner.render_deck("deck-disk", tmp_path / "p.pptx")
    assert runner.render_deck("deck-disk", tmp_path / "p.pptx", allow_partial=True) == 1


def test_render_deck_batch_rejects_invalid_and_unplanned_slides(disk):
    s = stub("A1", refs=("ev.md",))
    plan(ScriptedAgent(manifest_of(s)))
    bad = runner.render_deck_batch("deck-disk", SlideBatch(slides=[invalid_payload_for(s)]))
    assert bad.accepted == [] and bad.rejected == ["A1"]
    extra = runner.render_deck_batch(
        "deck-disk", SlideBatch(slides=[payload_for(stub("ZZ", refs=("ev.md",)))]))
    assert extra.rejected == ["ZZ"] and any("unplanned" in i.message for i in extra.issues)


def test_plan_ciso_deck_validates_target_and_manifest(disk):
    with pytest.raises(PixelError, match="target_slides"):
        plan(ScriptedAgent(manifest_of(stub("A1"))), target=0)
    bad = ScriptedAgent(manifest_of(stub("A1", refs=("unknown.md",))))
    with pytest.raises(PixelError, match="manifest failed validation"):
        plan(bad)
    assert len(bad.prompts) == 2


def test_plan_ciso_deck_generates_an_id_when_none_is_given(disk):
    agent = ScriptedAgent(manifest_of(stub("A1", refs=("ev.md",))))
    manifest = runner.plan_ciso_deck(ENGAGEMENT, ["ev.md"], 2, agent_factory=agent.factory())
    assert manifest.deck_id.startswith("deck-")


def test_generate_deck_validates_batch_size(disk):
    with pytest.raises(PixelError, match="batch_size"):
        runner.generate_deck("deck-disk", batch_size=0)
