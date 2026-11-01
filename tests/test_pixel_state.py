import pytest

from viharbot.pixel import state
from viharbot.pixel.models import DeckManifest, EngagementProfile, SlideBatch, SlidePayload, SlideStub


@pytest.fixture(autouse=True)
def memory(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_DIR", str(tmp_path))
    return tmp_path


def make_manifest(deck_id="deck-1"):
    stub = SlideStub(slide_id="s1", layout_id="L", title="t", purpose="p")
    return DeckManifest(deck_id=deck_id, sections=[
        {"section_id": "a", "title": "A", "theme": "dark", "slides": [stub]}])


def make_batch():
    return SlideBatch(slides=[SlidePayload(slide_id="s1", layout_id="L", tokens={"A": "b"}, claim_status="proposed")])


def plan(deck_id="deck-1"):
    state.save_plan(deck_id, EngagementProfile(scope="scope"), make_manifest(deck_id), ["e.md"])


def test_plan_round_trip(memory):
    plan()
    saved = state.load_state("deck-1")
    assert saved["next_batch"] == 0 and saved["digest"] == [] and saved["evidence_paths"] == ["e.md"]
    assert state.load_manifest("deck-1") == make_manifest()
    assert state.load_engagement("deck-1").scope == "scope"
    assert (memory / "pixel" / "deck-1" / "state.json").is_file()


def test_missing_plan_raises():
    with pytest.raises(FileNotFoundError):
        state.load_state("nothing")


@pytest.mark.parametrize("bad", ["../x", "Upper", "a" * 65, "", "a/b", "-a"])
def test_invalid_deck_ids_rejected(bad):
    with pytest.raises(ValueError, match="invalid deck_id"):
        state.deck_dir(bad)


def test_memory_dir_default(monkeypatch):
    monkeypatch.delenv("MEMORY_DIR")
    assert str(state.deck_dir("d")) == "data/pixel/d"


def test_batches_save_load_and_next_batch():
    plan()
    assert state.load_batches("deck-1") == []
    assert not state.batch_saved("deck-1", 0)
    state.save_batch("deck-1", 0, make_batch(), ["one"])
    assert state.batch_saved("deck-1", 0) and not state.batch_saved("deck-1", 1)
    assert state.load_state("deck-1")["next_batch"] == 1
    state.save_batch("deck-1", 1, make_batch(), ["two"])
    assert state.load_state("deck-1")["next_batch"] == 2
    state.save_batch("deck-1", 0, make_batch(), [])  # re-saving an earlier batch never rewinds
    assert state.load_state("deck-1")["next_batch"] == 2
    assert state.load_batches("deck-1") == [make_batch(), make_batch()]


def test_digest_is_capped_at_40_keeping_latest():
    plan()
    state.save_batch("deck-1", 0, make_batch(), [f"line {i}" for i in range(30)])
    state.save_batch("deck-1", 1, make_batch(), [f"line {i}" for i in range(30, 60)])
    digest = state.load_state("deck-1")["digest"]
    assert len(digest) == 40
    assert digest[0] == "line 20" and digest[-1] == "line 59"


def test_atomic_write_leaves_no_tmp_file(memory):
    plan()
    state.save_batch("deck-1", 0, make_batch(), ["x"])
    leftovers = [p for p in memory.rglob("*") if p.name.endswith(".tmp")]
    assert leftovers == []
