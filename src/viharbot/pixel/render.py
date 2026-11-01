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
import math
import re
from pathlib import Path
from typing import Any, Iterable

from .models import LayoutSpec, SlidePayload
from .registry import design_tokens, get_layout_spec, list_layouts

TOKEN_RE = re.compile(r"\{\{([A-Z0-9_]+)\}\}")
SLIDE_W_IN, SLIDE_H_IN = 13.333, 7.5


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

    n, cols = len(spec.cells), max(1, spec.grid_cols)
    rows = math.ceil(n / cols)
    x0, y0, width, height, gap = 0.6, 1.7, 12.13, 5.3, 0.2
    cell_w = (width - gap * (cols - 1)) / cols
    cell_h = (height - gap * (rows - 1)) / rows
    for index, cell in enumerate(spec.cells):
        left = x0 + (index % cols) * (cell_w + gap)
        top = y0 + (index // cols) * (cell_h + gap)
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
        frame.margin_left = frame.margin_right = Inches(0.14)
        frame.margin_top = Inches(0.42 if cell.badges else 0.14)
        for i, token in enumerate(cell.tokens):
            paragraph = frame.paragraphs[0] if i == 0 else frame.add_paragraph()
            paragraph.alignment = P["PP_ALIGN"].LEFT
            _add_run(P, paragraph, "{{%s}}" % token, 12 if i == 0 else 10.5, i == 0, theme["text"])
        for b, badge_token in enumerate(cell.badges):
            badge_w = 1.25
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
