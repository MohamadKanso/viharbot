"""Deterministic .pptx rendering: build a template deck from the layout registry, then clone
template slides and fill them from validated payloads.

Needs python-pptx (`pip install '.[pixel]'`), imported lazily so the rest of the package does
not depend on it. Shapes only: no pictures or charts. A token must sit inside one run, which
this module guarantees for templates it builds itself.

Template conventions (also true of hand-made templates):
  * a shape named LAYOUT_ID whose text is the layout id (removed from the output)
  * text tokens written {{TOKEN_NAME}}
  * badge shapes named BADGE_<TOKEN>, recolored and relabeled from status_overrides
"""

from __future__ import annotations

import copy
import html as _html
import logging
import math
import re
from pathlib import Path
from typing import Any, Iterable

from .models import LayoutSpec, SlidePayload
from .registry import design_tokens, get_layout_spec, list_layouts

logger = logging.getLogger("pixel")

TOKEN_RE = re.compile(r"\{\{([A-Z0-9_]+)\}\}")
SLIDE_W_IN, SLIDE_H_IN = 13.333, 7.5
BADGE_W_IN = 1.25
SHORT_CELL_IN = 1.6  # cells shorter than this put their badges beside the text, not above it
MIN_FONT_PT = 8.0
AVG_CHAR_EM = 0.55  # average glyph width in ems; deliberately a little wide so the estimate errs safe
LINE_HEIGHT = 1.2


def _pptx() -> dict[str, Any]:
    try:
        from pptx import Presentation
        from pptx.dml.color import RGBColor
        from pptx.enum.shapes import MSO_SHAPE
        from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
        from pptx.util import Inches, Pt
    except ImportError:
        raise RuntimeError("python-pptx is required: pip install '.[pixel]'") from None
    return {
        "Presentation": Presentation, "RGBColor": RGBColor, "MSO_SHAPE": MSO_SHAPE,
        "MSO_ANCHOR": MSO_ANCHOR, "PP_ALIGN": PP_ALIGN, "Inches": Inches, "Pt": Pt,
    }


def _rgb(P: dict[str, Any], hex_value: str):
    return P["RGBColor"].from_string(hex_value)


def _add_run(P, paragraph, text: str, size: float, bold: bool, color: str) -> None:
    run = paragraph.add_run()
    run.text = text
    run.font.size = P["Pt"](size)
    run.font.bold = bold
    run.font.color.rgb = _rgb(P, color)


BAND_H_IN = 0.9


def _placements(spec: LayoutSpec) -> list[tuple]:
    """(cell, left, top, width, height) in inches: an optional header band, then the grid."""
    x0, y0, width, height, gap = 0.6, 1.7, 12.13, 5.3, 0.2
    out: list[tuple] = []
    band = spec.cells[: spec.header_cells]
    if band:
        w = (width - gap * (len(band) - 1)) / len(band)
        out += [(c, x0 + i * (w + gap), y0, w, BAND_H_IN) for i, c in enumerate(band)]
        y0, height = y0 + BAND_H_IN + gap, height - BAND_H_IN - gap
    rest = spec.cells[spec.header_cells :]
    cols = max(1, spec.grid_cols)
    rows = math.ceil(len(rest) / cols) or 1
    cell_w = (width - gap * (cols - 1)) / cols
    cell_h = (height - gap * (rows - 1)) / rows
    out += [
        (c, x0 + (i % cols) * (cell_w + gap), y0 + (i // cols) * (cell_h + gap), cell_w, cell_h)
        for i, c in enumerate(rest)
    ]
    return out


def _draw_layout(P: dict[str, Any], prs, spec: LayoutSpec) -> None:
    tokens = design_tokens()
    theme = tokens["themes"][spec.theme]
    accent = tokens["accents"]["zero_trust" if spec.theme == "dark" else "ai"]
    Inches = P["Inches"]
    slide = prs.slides.add_slide(prs.slide_layouts[6])

    bg = slide.shapes.add_shape(P["MSO_SHAPE"].RECTANGLE, 0, 0, prs.slide_width, prs.slide_height)
    bg.name = "BG"
    bg.fill.solid()
    bg.fill.fore_color.rgb = _rgb(P, theme["background"])
    bg.line.fill.background()

    tag = slide.shapes.add_textbox(0, 0, Inches(3.5), Inches(0.2))
    tag.name = "LAYOUT_ID"
    tag.text_frame.text = spec.layout_id

    header = slide.shapes.add_textbox(Inches(0.6), Inches(0.35), Inches(12.1), Inches(1.1))
    header.name = "HEADER"
    frame = header.text_frame
    frame.word_wrap = True
    _add_run(P, frame.paragraphs[0], "{{SLIDE_TAG}}", 10, True, accent)
    _add_run(P, frame.add_paragraph(), "{{SLIDE_TITLE}}", 22, True, theme["text"])

    for cell, left, top, cell_w, cell_h in _placements(spec):
        box = slide.shapes.add_shape(
            P["MSO_SHAPE"].ROUNDED_RECTANGLE, Inches(left), Inches(top), Inches(cell_w), Inches(cell_h)
        )
        box.name = f"CELL_{cell.id}"
        box.adjustments[0] = 0.04
        box.fill.solid()
        box.fill.fore_color.rgb = _rgb(P, theme["card"])
        box.line.color.rgb = _rgb(P, theme["border"])
        frame = box.text_frame
        frame.word_wrap = True
        frame.vertical_anchor = P["MSO_ANCHOR"].TOP
        short = cell_h < SHORT_CELL_IN
        badge_room = len(cell.badges) * (BADGE_W_IN + 0.05) if cell.badges else 0
        frame.margin_left = Inches(0.14)
        frame.margin_right = Inches(0.14 + (badge_room if short else 0))
        frame.margin_top = Inches(0.42 if cell.badges and not short else 0.12)
        frame.margin_bottom = Inches(0.06)
        sizes = (11.0, 9.5) if short else (12.0, 10.5)
        for i, token in enumerate(cell.tokens):
            paragraph = frame.paragraphs[0] if i == 0 else frame.add_paragraph()
            paragraph.alignment = P["PP_ALIGN"].LEFT
            _add_run(P, paragraph, "{{%s}}" % token, sizes[0] if i == 0 else sizes[1], i == 0, theme["text"])
        for b, badge_token in enumerate(cell.badges):
            badge_w = BADGE_W_IN
            badge_left = left + cell_w - 0.12 - (len(cell.badges) - b) * (badge_w + 0.05) + 0.05
            badge = slide.shapes.add_shape(
                P["MSO_SHAPE"].ROUNDED_RECTANGLE,
                Inches(badge_left), Inches(top + 0.1), Inches(badge_w), Inches(0.26),
            )
            badge.name = f"BADGE_{badge_token}"
            badge.fill.solid()
            badge.fill.fore_color.rgb = _rgb(P, tokens["status"]["NOT_ASSESSED"]["fill"])
            badge.line.fill.background()
            badge.text_frame.margin_top = badge.text_frame.margin_bottom = 0
            paragraph = badge.text_frame.paragraphs[0]
            paragraph.alignment = P["PP_ALIGN"].CENTER
            _add_run(P, paragraph, "STATUS", 8, True, tokens["status"]["NOT_ASSESSED"]["text"])


def build_template(path: str | Path) -> int:
    """Write a template deck with one slide per registered layout; returns the slide count."""
    P = _pptx()
    prs = P["Presentation"]()
    prs.slide_width, prs.slide_height = P["Inches"](SLIDE_W_IN), P["Inches"](SLIDE_H_IN)
    layouts = list_layouts()
    for spec in layouts:
        _draw_layout(P, prs, spec)
    prs.save(str(path))
    return len(layouts)


def _runs(shape) -> Iterable:
    if shape.has_text_frame:
        for paragraph in shape.text_frame.paragraphs:
            yield from paragraph.runs
    if getattr(shape, "has_table", False) and shape.has_table:
        for row in shape.table.rows:
            for cell in row.cells:
                for paragraph in cell.text_frame.paragraphs:
                    yield from paragraph.runs


def _fill_tokens(shape, tokens: dict[str, str], missing: set[str]) -> None:
    for run in _runs(shape):
        def sub(match: re.Match) -> str:
            name = match.group(1)
            if name not in tokens:
                missing.add(name)
                return match.group(0)
            return tokens[name]

        run.text = TOKEN_RE.sub(sub, run.text)


def _recolor(P, slide, overrides: dict[str, str]) -> None:
    statuses = design_tokens()["status"]
    for key, status in overrides.items():
        style = statuses.get(status)
        if style is None:
            raise ValueError(f"unknown status {status!r} for {key}")
        hits = [s for s in slide.shapes if s.name == f"BADGE_{key}"]
        if not hits:
            raise ValueError(f"no badge shape BADGE_{key}")
        for shape in hits:
            shape.fill.solid()
            shape.fill.fore_color.rgb = _rgb(P, style["fill"])
            if shape.has_text_frame:  # colour is never the only signal: label the badge
                paragraph = shape.text_frame.paragraphs[0]
                if not paragraph.runs:
                    paragraph.add_run()
                for extra in paragraph.runs[1:]:
                    extra.text = ""
                run = paragraph.runs[0]
                run.text = style["label"]
                run.font.bold = True
                run.font.color.rgb = _rgb(P, style["text"])


def _find_template(prs, layout_id: str):
    for slide in prs.slides:
        for shape in slide.shapes:
            if (
                shape.name == "LAYOUT_ID"
                and shape.has_text_frame
                and shape.text_frame.text.strip() == layout_id
            ):
                return slide
    raise KeyError(f"no template slide for {layout_id}")


def _clone(prs, template):
    new = prs.slides.add_slide(prs.slide_layouts[6])
    for shape in list(new.shapes):
        shape._element.getparent().remove(shape._element)
    for shape in template.shapes:
        new.shapes._spTree.insert_element_before(copy.deepcopy(shape._element), "p:extLst")
    return new


def _text_height_in(shape) -> float:
    """Estimated height of a shape's text in inches, from its width, margins and font sizes."""
    frame = shape.text_frame
    width = (shape.width - frame.margin_left - frame.margin_right) / 914400
    total = 0.0
    for paragraph in frame.paragraphs:
        size = max((run.font.size.pt for run in paragraph.runs if run.font.size), default=10.5)
        per_line = max(1, int(width * 72 / (size * AVG_CHAR_EM)))
        lines = sum(max(1, math.ceil(len(part) / per_line)) for part in paragraph.text.split("\n"))
        total += lines * size * LINE_HEIGHT / 72
    return total


def _fit_cells(P, slide) -> int:
    """Lower each overfull cell's font size, to MIN_FONT_PT, until its text should fit.

    Deterministic and independent of the model. It works from an estimate, not a real layout, so
    a cell may still overflow at the minimum size; returns how many cells could not be made to fit.
    """
    unfit = 0
    for shape in slide.shapes:
        if not (shape.name.startswith("CELL_") and shape.has_text_frame):
            continue
        frame = shape.text_frame
        available = (shape.height - frame.margin_top - frame.margin_bottom) / 914400
        while _text_height_in(shape) > available:
            runs = [r for p in frame.paragraphs for r in p.runs if r.font.size]
            if not runs or max(r.font.size.pt for r in runs) <= MIN_FONT_PT:
                unfit += 1
                break
            for run in runs:
                run.font.size = P["Pt"](max(MIN_FONT_PT, run.font.size.pt - 0.5))
    return unfit


def populate(
    template_path: str | Path,
    payloads: Iterable[SlidePayload | dict[str, Any]],
    out_path: str | Path,
) -> int:
    """Fill cloned template slides from payloads and save; returns the slide count.

    Raises ValueError listing every problem, and writes nothing, if any token is unfilled.
    """
    P = _pptx()
    items = [p if isinstance(p, SlidePayload) else SlidePayload.model_validate(p) for p in payloads]
    prs = P["Presentation"](str(template_path))
    template_count = len(prs.slides)
    problems: list[str] = []
    for payload in items:
        get_layout_spec(payload.layout_id)  # unknown layouts fail before any drawing
        slide = _clone(prs, _find_template(prs, payload.layout_id))
        missing: set[str] = set()
        for shape in slide.shapes:
            if shape.name != "LAYOUT_ID":
                _fill_tokens(shape, payload.tokens, missing)
        try:
            _recolor(P, slide, dict(payload.status_overrides))
        except ValueError as exc:
            problems.append(f"{payload.slide_id}: {exc}")
        unfit = _fit_cells(P, slide)
        if unfit:
            logger.warning("pixel text may overflow", extra={"agent": "pixel", "error": f"{unfit} cells at minimum font size"})
        for shape in list(slide.shapes):
            if shape.name == "LAYOUT_ID":
                shape._element.getparent().remove(shape._element)
        if missing:
            problems.append(f"{payload.slide_id}: unfilled tokens {sorted(missing)}")
    if problems:
        raise ValueError("build failed: " + "; ".join(problems))

    ids = prs.slides._sldIdLst  # drop the template slides, keep only generated ones
    for sld_id in list(ids)[:template_count]:
        prs.part.drop_rel(sld_id.rId)
        ids.remove(sld_id)
    prs.save(str(out_path))
    return len(items)


# ---------------------------------------------------------------------------------------------
# HTML output page. Pixel renders it from the same design tokens and layout registry as the
# .pptx, so colours, card structure and badge labels are identical across the two formats.
# Every value is escaped; the page is self-contained (inline CSS only, no scripts or assets).
# ---------------------------------------------------------------------------------------------

def _css() -> str:
    tokens = design_tokens()
    themes, accents, status = tokens["themes"], tokens["accents"], tokens["status"]
    rules = [
        "*{box-sizing:border-box}",
        "body{margin:0;background:#" + themes["light"]["background"] + ";color:#" + themes["light"]["text"]
        + ";font:16px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}",
        "main{max-width:1180px;margin:0 auto;padding:24px 16px 96px}",
        ".slide{border-radius:14px;padding:28px 28px 24px;margin:0 0 22px;border:1px solid transparent}",
        ".slide h2{margin:2px 0 18px;font-size:1.45rem;line-height:1.25}",
        ".tag{font-size:.72rem;font-weight:700;letter-spacing:.08em;text-transform:uppercase}",
        ".grid{display:grid;gap:14px}.grid.band{margin-bottom:14px}",
        ".cell{position:relative;border-radius:12px;padding:14px 16px;border:1px solid transparent}",
        ".cell p{margin:0 0 6px;font-size:.92rem}.cell p:first-of-type{font-weight:700;font-size:.98rem}",
        ".badges{display:flex;flex-wrap:wrap;gap:6px;justify-content:flex-end;margin:0 0 8px}",
        ".badge{display:inline-block;padding:2px 10px;border-radius:999px;font-size:.7rem;font-weight:700;"
        "letter-spacing:.04em}",
        ".dl{position:sticky;bottom:0;background:#" + themes["dark"]["background"] + ";color:#"
        + themes["dark"]["text"] + ";padding:14px 20px;display:flex;flex-wrap:wrap;gap:16px;align-items:center;"
        "justify-content:space-between;border-top:3px solid #" + accents["zero_trust"] + "}",
        ".dl a{color:#" + accents["zero_trust"] + ";font-weight:700}",
        ".dl .notes{flex-basis:100%;margin:0;padding-left:18px;font-size:.8rem;color:#"
        + themes["dark"]["muted"] + "}",
        "@media(max-width:800px){.grid{grid-template-columns:1fr!important}.slide{padding:20px 16px}}",
    ]
    for name, theme in themes.items():
        accent = accents["zero_trust" if name == "dark" else "ai"]
        rules.append(f".slide.{name}{{background:#{theme['background']};color:#{theme['text']}}}")
        rules.append(f".slide.{name} .cell{{background:#{theme['card']};border-color:#{theme['border']}}}")
        rules.append(f".slide.{name} .tag{{color:#{accent}}}")
    for name, style in status.items():
        rules.append(f".badge.{name}{{background:#{style['fill']};color:#{style['text']}}}")
    return "".join(rules)


def render_html(
    payloads: Iterable[SlidePayload | dict[str, Any]],
    *,
    download_url: str,
    warnings: Iterable[str] = (),
    title: str = "Pixel slide deck",
) -> str:
    """A self-contained HTML page of the deck, with the download link in a bar at the bottom."""
    esc = lambda value: _html.escape(str(value), quote=True)  # noqa: E731
    statuses = design_tokens()["status"]
    items = [p if isinstance(p, SlidePayload) else SlidePayload.model_validate(p) for p in payloads]
    sections: list[str] = []
    for payload in items:
        spec = get_layout_spec(payload.layout_id)
        cells: list[str] = []
        band_cells: list[str] = []
        for position, cell in enumerate(spec.cells):
            badges = "".join(
                f'<span class="badge {esc(status)}">{esc(statuses[status]["label"])}</span>'
                for status in (payload.status_overrides.get(b) for b in cell.badges)
                if status in statuses
            )
            texts = "".join(f"<p>{esc(payload.tokens.get(t, ''))}</p>" for t in cell.tokens)
            html_cell = f'<div class="cell">{f"<div class=badges>{badges}</div>" if badges else ""}{texts}</div>'
            (band_cells if position < spec.header_cells else cells).append(html_cell)
        cols = max(1, spec.grid_cols)
        band = (
            f'<div class="grid band" style="grid-template-columns:repeat({len(band_cells)},minmax(0,1fr))">'
            f'{"".join(band_cells)}</div>'
            if band_cells
            else ""
        )
        sections.append(
            f'<section class="slide {esc(spec.theme)}" data-layout="{esc(spec.layout_id)}">'
            f'<div class="tag">{esc(payload.tokens.get("SLIDE_TAG", ""))}</div>'
            f'<h2>{esc(payload.tokens.get("SLIDE_TITLE", ""))}</h2>'
            f'{band}'
            f'<div class="grid" style="grid-template-columns:repeat({cols},minmax(0,1fr))">{"".join(cells)}</div>'
            "</section>"
        )
    notes = "".join(f"<li>{esc(w)}</li>" for w in warnings)
    footer = (
        f'<footer class="dl"><span>{len(items)} slide(s)</span>'
        f'<a href="{esc(download_url)}" download>Download the .pptx</a>'
        f'{f"<ul class=notes>{notes}</ul>" if notes else ""}</footer>'
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{esc(title)}</title><style>{_css()}</style></head>"
        f"<body><main>{''.join(sections)}</main>{footer}</body></html>"
    )
