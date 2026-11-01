import sys

import pytest

pytest.importorskip("pptx")

from pptx import Presentation  # noqa: E402

from pixel_core_helpers import all_specs, longest_payload, payload_for  # noqa: E402
from viharbot.pixel import render  # noqa: E402
from viharbot.pixel.models import SlidePayload  # noqa: E402
from viharbot.pixel.registry import design_tokens, get_layout_spec  # noqa: E402

SPECS = all_specs()
ROADMAP = get_layout_spec("LAYOUT_20_MULTI_YEAR_ROADMAP")
BADGE_SPEC = next(s for s in SPECS if s.badge_tokens)


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    path = tmp_path_factory.mktemp("tpl") / "template.pptx"
    assert render.build_template(path) == 24
    return path


def shape_texts(slide):
    return [s.text_frame.text for s in slide.shapes if s.has_text_frame]


def indexed_payloads(texts=None):
    out = []
    for i, s in enumerate(SPECS):
        out.append(payload_for(s, slide_id=f"s{i}"))
    return out


def test_template_has_one_slide_per_layout(template):
    prs = Presentation(str(template))
    assert len(prs.slides) == 24
    ids = [next(s.text_frame.text for s in sl.shapes if s.name == "LAYOUT_ID") for sl in prs.slides]
    assert ids == [s.layout_id for s in SPECS]


def test_populate_all_layouts(template, tmp_path):
    out = tmp_path / "out.pptx"
    assert render.populate(template, indexed_payloads(), out) == 24
    prs = Presentation(str(out))
    assert len(prs.slides) == 24
    for slide in prs.slides:
        assert all("{{" not in t for t in shape_texts(slide))
        assert not any(s.name == "LAYOUT_ID" for s in slide.shapes)


def test_populate_accepts_dicts(template, tmp_path):
    p = payload_for(BADGE_SPEC).model_dump()
    assert render.populate(template, [p], tmp_path / "o.pptx") == 1


def test_badges_recoloured_and_labelled(template, tmp_path):
    statuses = design_tokens()["status"]
    badges = BADGE_SPEC.badge_tokens
    overrides = {b: ("NOT_ASSESSED" if i == 0 else "HIGH") for i, b in enumerate(badges)}
    p = payload_for(BADGE_SPEC, status_overrides=overrides)
    out = tmp_path / "o.pptx"
    render.populate(template, [p], out)
    slide = Presentation(str(out)).slides[0]
    for badge, status in overrides.items():
        shape = next(s for s in slide.shapes if s.name == f"BADGE_{badge}")
        assert str(shape.fill.fore_color.rgb) == statuses[status]["fill"]
        assert shape.text_frame.text == statuses[status]["label"]
    assert statuses["NOT_ASSESSED"]["label"] == "NOT ASSESSED"


def test_unfilled_tokens_raise_and_write_nothing(template, tmp_path):
    p = payload_for(BADGE_SPEC)
    tokens = dict(p.tokens)
    tokens.pop("SLIDE_TITLE")
    out = tmp_path / "o.pptx"
    with pytest.raises(ValueError, match="unfilled tokens"):
        render.populate(template, [p.model_copy(update={"tokens": tokens})], out)
    assert not out.exists()


def test_unknown_status_override_raises(template, tmp_path):
    p = payload_for(BADGE_SPEC)
    bad = SlidePayload.model_construct(**{**p.__dict__, "status_overrides": {BADGE_SPEC.badge_tokens[0]: "WAT"}})
    out = tmp_path / "o.pptx"
    with pytest.raises(ValueError, match="unknown status"):
        render.populate(template, [bad], out)
    assert not out.exists()


def test_override_for_missing_badge_shape_raises(template, tmp_path):
    p = payload_for(BADGE_SPEC)
    bad = SlidePayload.model_construct(**{**p.__dict__, "status_overrides": {"NO_SUCH": "LOW"}})
    with pytest.raises(ValueError, match="no badge shape"):
        render.populate(template, [bad], tmp_path / "o.pptx")


def test_unknown_layout_fails(template, tmp_path):
    p = payload_for(BADGE_SPEC).model_copy(update={"layout_id": "LAYOUT_99"})
    with pytest.raises(KeyError):
        render.populate(template, [p], tmp_path / "o.pptx")


def test_find_template_missing_layout(template):
    prs = Presentation(str(template))
    with pytest.raises(KeyError, match="no template slide"):
        render._find_template(prs, "LAYOUT_99")


def test_longest_text_fits_or_is_at_minimum_font(template, tmp_path):
    payloads = [longest_payload(s).model_copy(update={"slide_id": f"s{i}"}) for i, s in enumerate(SPECS)]
    out = tmp_path / "long.pptx"
    render.populate(template, payloads, out)
    prs = Presentation(str(out))
    cells = 0
    for slide in prs.slides:
        for shape in slide.shapes:
            if not shape.name.startswith("CELL_"):
                continue
            cells += 1
            frame = shape.text_frame
            available = (shape.height - frame.margin_top - frame.margin_bottom) / 914400
            sizes = [r.font.size.pt for p in frame.paragraphs for r in p.runs if r.font.size]
            fits = render._text_height_in(shape) <= available
            assert fits or max(sizes) <= render.MIN_FONT_PT, shape.name
    assert cells >= 24


def test_fit_shrinks_overfull_cell_and_counts_unfit(template, tmp_path):
    P = render._pptx()
    prs = Presentation(str(template))
    slide = prs.slides[0]
    cell = next(s for s in slide.shapes if s.name.startswith("CELL_"))
    start = max(r.font.size.pt for p in cell.text_frame.paragraphs for r in p.runs)
    cell.text_frame.paragraphs[0].runs[0].text = "word " * 400  # more than any cell can hold
    unfit = render._fit_cells(P, slide)
    assert unfit >= 1
    assert max(r.font.size.pt for p in cell.text_frame.paragraphs for r in p.runs) == render.MIN_FONT_PT
    assert start > render.MIN_FONT_PT


def test_header_band_placement():
    spec = ROADMAP
    placed = render._placements(spec)
    assert len(placed) == len(spec.cells)
    band, rest = placed[: spec.header_cells], placed[spec.header_cells :]
    assert spec.header_cells == 2
    assert all(h == render.BAND_H_IN for *_, h in band)
    assert len({top for _, _, top, _, _ in band}) == 1
    band_bottom = band[0][2] + render.BAND_H_IN
    assert all(top >= band_bottom for _, _, top, _, _ in rest)
    for i, (_, l1, t1, w1, h1) in enumerate(placed):
        for _, l2, t2, w2, h2 in placed[i + 1 :]:
            assert l1 + w1 <= l2 + 1e-9 or l2 + w2 <= l1 + 1e-9 or t1 + h1 <= t2 + 1e-9 or t2 + h2 <= t1 + 1e-9


def test_badges_beside_short_cells_and_above_tall_cells(template):
    prs = Presentation(str(template))
    margins = {}
    for spec, slide in zip(SPECS, prs.slides):
        placed = {c.id: h for c, *_, h in render._placements(spec)}
        for cell in spec.cells:
            if not cell.badges:
                continue
            shape = next(s for s in slide.shapes if s.name == f"CELL_{cell.id}")
            f = shape.text_frame
            kind = "short" if placed[cell.id] < render.SHORT_CELL_IN else "tall"
            margins.setdefault(kind, []).append((f.margin_left, f.margin_right, f.margin_top))
    assert margins.get("short") and margins.get("tall")
    assert all(r > l for l, r, _ in margins["short"])
    assert all(r == l and t > 0.3 * 914400 for l, r, t in margins["tall"])


def test_pptx_import_failure(monkeypatch):
    monkeypatch.setitem(sys.modules, "pptx", None)
    with pytest.raises(RuntimeError, match="python-pptx"):
        render._pptx()


def test_text_height_counts_newlines_and_scales_with_text(template):
    prs = Presentation(str(template))
    shape = next(s for s in prs.slides[0].shapes if s.name.startswith("CELL_"))
    short = render._text_height_in(shape)
    shape.text_frame.paragraphs[0].runs[0].text = "a\nb\nc"
    assert render._text_height_in(shape) > short


# --- HTML ---------------------------------------------------------------------------------

def html_for(payloads, **kw):
    return render.render_html(payloads, download_url=kw.pop("download_url", "/d/x.pptx"), **kw)


def test_html_escapes_hostile_text():
    hostile = "<script>alert(1)</script>"
    p = payload_for(BADGE_SPEC)
    p = p.model_copy(update={"tokens": {**p.tokens, "SLIDE_TITLE": hostile}})
    page = html_for([p], warnings=[hostile], title=hostile, download_url='/x"><script>')
    assert "<script" not in page
    assert page.count("&lt;script&gt;alert(1)&lt;/script&gt;") >= 3
    assert '/x&quot;&gt;&lt;script&gt;' in page


def test_html_footer_after_main_with_link_and_warnings():
    page = html_for([payload_for(BADGE_SPEC)], warnings=["careful & slow"])
    main_end, footer = page.index("</main>"), page.index('<footer class="dl">')
    assert footer > main_end
    assert page.index('href="/d/x.pptx"') > footer
    assert "<li>careful &amp; slow</li>" in page
    assert "<ul" not in html_for([payload_for(BADGE_SPEC)])


def test_html_uses_design_tokens_and_badge_labels():
    tokens = design_tokens()
    p = payload_for(BADGE_SPEC, status_overrides={b: "NOT_ASSESSED" for b in BADGE_SPEC.badge_tokens})
    page = html_for([p])
    for theme in tokens["themes"].values():
        assert theme["background"] in page and theme["card"] in page
    for style in tokens["status"].values():
        assert f"#{style['fill']}" in page
    assert '<span class="badge NOT_ASSESSED">NOT ASSESSED</span>' in page


def test_html_header_band_for_header_layouts():
    page = html_for([payload_for(ROADMAP)])
    assert 'class="grid band"' in page
    assert "repeat(2,minmax(0,1fr))" in page
    plain = next(s for s in SPECS if not s.header_cells)
    assert 'class="grid band"' not in html_for([payload_for(plain)]).split("</style>")[1]


def test_html_all_layouts_no_script():
    page = html_for(indexed_payloads())
    assert page.count('<section class="slide') == 24
    assert "<script" not in page
    assert "slide(s)" in page and "24 slide(s)" in page
