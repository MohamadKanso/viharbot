#!/usr/bin/env python3
"""Populate a PowerPoint template from a Pixel slide-payload JSON file.

Proposed utility (see docs/pixel-slide-generation-spec.md). Requires python-pptx,
which is intentionally not in requirements.txt:  pip install python-pptx

Template convention:
  * Each template slide has a shape named LAYOUT_ID whose text is the layout id.
  * Badge shapes are named BADGE_<TOKEN>; status_overrides[<TOKEN>] recolors them.
  * Text tokens look like {{TOKEN_NAME}} and are replaced from payload["tokens"].

Usage:
  python scripts/pptx_populate.py --make-sample-template template.pptx
  python scripts/pptx_populate.py template.pptx payloads.json out.pptx
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import sys

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.util import Inches

RAG = {
    "CRITICAL": "DC2626",
    "HIGH": "EA580C",
    "MEDIUM": "F59E0B",
    "LOW": "10B981",
}
TOKEN_RE = re.compile(r"\{\{([A-Z0-9_]+)\}\}")
MAX_WORDS = 15


def _runs(shape):
    if shape.has_text_frame:
        for para in shape.text_frame.paragraphs:
            for run in para.runs:
                yield run
    if getattr(shape, "has_table", False) and shape.has_table:
        for row in shape.table.rows:
            for cell in row.cells:
                for para in cell.text_frame.paragraphs:
                    yield from para.runs


def _replace(shape, tokens: dict[str, str], missing: set[str]) -> None:
    for run in _runs(shape):
        def sub(match: re.Match) -> str:
            name = match.group(1)
            if name not in tokens:
                missing.add(name)
                return match.group(0)
            return tokens[name]
        run.text = TOKEN_RE.sub(sub, run.text)


def _find_template(prs: Presentation, layout_id: str):
    for slide in prs.slides:
        for shape in slide.shapes:
            if shape.name == "LAYOUT_ID" and shape.has_text_frame \
                    and shape.text_frame.text.strip() == layout_id:
                return slide
    raise KeyError(f"no template slide for {layout_id}")


def _clone(prs: Presentation, template):
    # Shapes only: pictures and charts need relationship copying and are not supported.
    new = prs.slides.add_slide(prs.slide_layouts[6])
    for shape in list(new.shapes):
        shape._element.getparent().remove(shape._element)
    for shape in template.shapes:
        new.shapes._spTree.insert_element_before(copy.deepcopy(shape._element), "p:extLst")
    return new


def _recolor(slide, overrides: dict[str, str]) -> None:
    for key, status in overrides.items():
        if status not in RAG:
            raise ValueError(f"unknown status {status!r} for {key}")
        hits = [s for s in slide.shapes if s.name == f"BADGE_{key}"]
        if not hits:
            raise ValueError(f"no badge shape BADGE_{key}")
        for shape in hits:
            shape.fill.solid()
            shape.fill.fore_color.rgb = RGBColor.from_string(RAG[status])
            if shape.has_text_frame:  # color is never the only signal: label the badge
                shape.text_frame.text = status


def _check(payload: dict) -> list[str]:
    problems = []
    for name, text in payload.get("tokens", {}).items():
        if len(text.split()) > MAX_WORDS and not name.startswith("RULE"):
            problems.append(f"{payload['slide_id']}:{name} exceeds {MAX_WORDS} words")
    if payload.get("claim_status") not in ("implemented", "proposed"):
        problems.append(f"{payload['slide_id']}: claim_status missing")
    return problems


def build(template_path: str, payload_path: str, out_path: str) -> None:
    with open(payload_path, encoding="utf-8") as fh:
        payloads = json.load(fh)
    if isinstance(payloads, dict):
        payloads = payloads.get("slides", [payloads])

    prs = Presentation(template_path)
    template_count = len(prs.slides)
    errors: list[str] = []
    for payload in payloads:
        errors += _check(payload)
        slide = _clone(prs, _find_template(prs, payload["layout_id"]))
        missing: set[str] = set()
        tokens = payload.get("tokens", {})
        for shape in slide.shapes:
            if shape.name != "LAYOUT_ID":
                _replace(shape, tokens, missing)
        _recolor(slide, payload.get("status_overrides", {}))
        for shape in list(slide.shapes):  # drop the layout tag from output
            if shape.name == "LAYOUT_ID":
                shape._element.getparent().remove(shape._element)
        if missing:
            errors.append(f"{payload['slide_id']}: unfilled tokens {sorted(missing)}")
    if errors:
        sys.exit("build failed:\n  " + "\n  ".join(errors))

    # Remove the original template slides, keeping only generated ones.
    ids = prs.slides._sldIdLst
    for sld_id in list(ids)[:template_count]:
        prs.part.drop_rel(sld_id.rId)
        ids.remove(sld_id)
    prs.save(out_path)
    print(f"wrote {out_path} ({len(payloads)} slides)")


def make_sample_template(path: str) -> None:
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    tag = slide.shapes.add_textbox(Inches(0), Inches(0), Inches(3), Inches(0.3))
    tag.name = "LAYOUT_ID"
    tag.text_frame.text = "LAYOUT_14_OWASP_LLM_TOP10"
    for i, key in enumerate(["LLM01", "LLM02", "LLM06", "LLM07"]):
        top = Inches(1 + i * 1.4)
        note = slide.shapes.add_textbox(Inches(2), top, Inches(10), Inches(1))
        note.text_frame.text = "{{%s_NOTE}}" % key
        badge = slide.shapes.add_shape(1, Inches(0.5), top, Inches(1.3), Inches(0.6))
        badge.name = f"BADGE_{key}_BADGE"
        badge.text_frame.text = key
    prs.save(path)
    print(f"wrote sample template {path}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--make-sample-template", metavar="PATH")
    ap.add_argument("template", nargs="?")
    ap.add_argument("payloads", nargs="?")
    ap.add_argument("out", nargs="?")
    args = ap.parse_args()
    if args.make_sample_template:
        make_sample_template(args.make_sample_template)
    elif args.template and args.payloads and args.out:
        build(args.template, args.payloads, args.out)
    else:
        ap.error("give --make-sample-template PATH, or template payloads out")
