import pytest
from pydantic import ValidationError

from viharbot.pixel import registry
from viharbot.pixel.models import Cell, DeckManifest, EngagementProfile, LayoutSpec, SlideStub
from viharbot.pixel.theme import tokens_css


@pytest.fixture
def clean_registry_caches():
    """Let a test swap registry._load, then restore the real cached data afterwards."""
    registry._layouts.cache_clear()
    yield
    registry._layouts.cache_clear()


def test_layouts_are_24_in_order_with_unique_ids():
    specs = registry.list_layouts()
    ids = [s.layout_id for s in specs]
    assert len(specs) == 24
    assert len(set(ids)) == 24
    assert [i[7:9] for i in ids] == [f"{n:02d}" for n in range(1, 25)]


@pytest.mark.parametrize("spec", registry.list_layouts(), ids=lambda s: s.layout_id)
def test_every_layout_has_header_tokens_and_unique_tokens(spec):
    assert spec.tokens[:2] == ("SLIDE_TAG", "SLIDE_TITLE")
    assert len(set(spec.tokens)) == len(spec.tokens)
    assert len(set(spec.badge_tokens)) == len(spec.badge_tokens)
    assert not set(spec.badge_tokens) & set(spec.tokens)


def test_header_cells_and_word_limits():
    header = {s.layout_id[7:9]: s.header_cells for s in registry.list_layouts() if s.header_cells}
    assert header == {"07": 2, "13": 2, "20": 2, "21": 2}
    layout23 = next(s for s in registry.list_layouts() if s.layout_id.startswith("LAYOUT_23"))
    assert layout23.word_limits == {"RULE": 30}


def test_layout_tokens_and_badge_tokens_follow_cells():
    spec = LayoutSpec(
        layout_id="L", theme="dark", purpose="p",
        cells=(Cell(id="a", tokens=("A1", "A2"), badges=("B1",)), Cell(id="b", tokens=("C",), badges=("B2",))),
    )
    assert spec.tokens == ("SLIDE_TAG", "SLIDE_TITLE", "A1", "A2", "C")
    assert spec.badge_tokens == ("B1", "B2")


def test_lookup_and_unknown_errors():
    spec = registry.list_layouts()[0]
    assert registry.get_layout_spec(spec.layout_id) is spec
    with pytest.raises(registry.UnknownLayout):
        registry.get_layout_spec("LAYOUT_99_NOPE")
    with pytest.raises(registry.UnknownFramework):
        registry.get_framework("nope")


def test_frameworks():
    keys = registry.framework_keys()
    assert keys and "nist_csf_2" in keys
    entry = registry.get_framework("nist_csf_2")
    assert entry["key"] == "nist_csf_2"
    assert len(entry) > 1


def test_layout_prompt_view_merges_default_word_limit():
    spec = next(s for s in registry.list_layouts() if s.layout_id.startswith("LAYOUT_23"))
    view = registry.layout_prompt_view(spec)
    assert view["layout_id"] == spec.layout_id
    assert view["tokens"] == list(spec.tokens)
    assert view["badge_tokens"] == list(spec.badge_tokens)
    assert view["word_limits"] == {"default": 15, "RULE": 30}


def test_design_tokens_have_both_themes_and_all_statuses():
    tokens = registry.design_tokens()
    assert set(tokens["themes"]) == {"dark", "light"}
    assert set(tokens["status"]) == {"CRITICAL", "HIGH", "MEDIUM", "LOW", "NOT_ASSESSED"}
    for style in tokens["status"].values():
        assert {"fill", "text", "label"} <= set(style)


def test_tokens_css_defines_every_colour():
    css = tokens_css()
    tokens = registry.design_tokens()
    assert css.startswith(":root{") and css.endswith("}\n")
    for theme, colors in tokens["themes"].items():
        for key, value in colors.items():
            assert f"--px-{theme}-{key.replace('_', '-')}:#{value};" in css
    for key, value in tokens["accents"].items():
        assert f"--px-accent-{key.replace('_', '-')}:#{value};" in css
    for key, style in tokens["status"].items():
        name = key.lower().replace("_", "-")
        assert f"--px-{name}:#{style['fill']};" in css
        assert f"--px-{name}-text:#{style['text']};" in css
    assert "--px-not-assessed" in css


def _layouts_json(*layouts):
    return {"layouts": list(layouts)}


def _layout(layout_id, tokens=("A",), badges=()):
    return {"layout_id": layout_id, "theme": "dark", "purpose": "p",
            "cells": [{"id": "c", "tokens": list(tokens), "badges": list(badges)}]}


def test_duplicate_layout_ids_rejected(monkeypatch, clean_registry_caches):
    monkeypatch.setattr(registry, "_load", lambda name: _layouts_json(_layout("X"), _layout("X")))
    with pytest.raises(ValueError, match="duplicate layout_id"):
        registry._layouts()


def test_duplicate_tokens_rejected(monkeypatch, clean_registry_caches):
    monkeypatch.setattr(registry, "_load", lambda name: _layouts_json(_layout("X", tokens=("A", "A"))))
    with pytest.raises(ValueError, match="duplicate token in X"):
        registry._layouts()


def test_token_reused_as_badge_rejected(monkeypatch, clean_registry_caches):
    monkeypatch.setattr(registry, "_load", lambda name: _layouts_json(_layout("X", tokens=("A",), badges=("A",))))
    with pytest.raises(ValueError, match="duplicate token"):
        registry._layouts()


@pytest.mark.parametrize("bad", ["", "-x", "has space", "a" * 33, "a/b"])
def test_slide_id_pattern_rejected(bad):
    with pytest.raises(ValidationError):
        SlideStub(slide_id=bad, layout_id="L", title="t", purpose="p")


def test_slide_id_pattern_accepted():
    assert SlideStub(slide_id="S1.a_b-c", layout_id="L", title="t", purpose="p").slide_id == "S1.a_b-c"


@pytest.mark.parametrize("bad", ["", "../x", "Upper", "a" * 65, "-a"])
def test_deck_id_pattern_rejected(bad):
    with pytest.raises(ValidationError):
        DeckManifest(deck_id=bad, sections=[])


def test_engagement_profile_forbids_extras_and_caps():
    assert EngagementProfile(scope="s").audience == "CISO and board"
    with pytest.raises(ValidationError):
        EngagementProfile(scope="s", extra="x")
    with pytest.raises(ValidationError):
        EngagementProfile(scope="x" * 501)
    with pytest.raises(ValidationError):
        EngagementProfile(scope="s", mandates=["m"] * 13)
    with pytest.raises(ValidationError):
        EngagementProfile(scope="s", mandates=["m" * 121])


def test_manifest_stubs_flatten_sections():
    stub = lambda i: SlideStub(slide_id=i, layout_id="L", title="t", purpose="p")  # noqa: E731
    manifest = DeckManifest(deck_id="d", sections=[
        {"section_id": "a", "title": "A", "theme": "dark", "slides": [stub("1"), stub("2")]},
        {"section_id": "b", "title": "B", "theme": "light", "slides": [stub("3")]},
    ])
    assert [s.slide_id for s in manifest.stubs()] == ["1", "2", "3"]
