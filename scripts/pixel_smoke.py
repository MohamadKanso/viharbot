#!/usr/bin/env python3
"""Deterministic smoke build of a CISO validation deck. No model call, no network.

Builds the template for all registered layouts, fills one placeholder slide per layout (every
value "[to be supplied]", every badge NOT_ASSESSED, so nothing is invented) plus the repo-grounded
samples in scripts/pixel_smoke_payloads.json, then reopens the .pptx and checks it.

Usage:  python scripts/pixel_smoke.py OUT.pptx      (needs: pip install '.[pixel]')
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from viharbot.pixel.models import GAP, SlidePayload  # noqa: E402
from viharbot.pixel.registry import design_tokens, list_layouts  # noqa: E402
from viharbot.pixel.render import build_template, populate  # noqa: E402
from viharbot.pixel.validate import errors, validate_payload  # noqa: E402

FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    if not condition:
        FAILURES.append(message)


def placeholder(spec) -> SlidePayload:
    tokens = {t: GAP for t in spec.tokens}
    tokens["SLIDE_TAG"], tokens["SLIDE_TITLE"] = "Placeholder", spec.layout_id
    return SlidePayload(
        slide_id=f"P-{spec.layout_id[7:9]}", layout_id=spec.layout_id, tokens=tokens,
        status_overrides={b: "NOT_ASSESSED" for b in spec.badge_tokens}, claim_status="proposed",
    )


def negative_cases(base: SlidePayload) -> None:
    def broken(**change) -> SlidePayload:
        data = base.model_dump()
        data.update(change)
        return SlidePayload.model_validate(data)

    check(errors(validate_payload(broken(tokens={**base.tokens, "NOPE": "x"}))), "undeclared token accepted")
    first = next(iter(base.tokens))
    check(errors(validate_payload(broken(tokens={k: v for k, v in base.tokens.items() if k != first}))),
          "missing token accepted")
    check(errors(validate_payload(broken(tokens={**base.tokens, "SLIDE_TITLE": "word " * 40}))),
          "word limit not enforced")
    check(errors(validate_payload(broken(tokens={**base.tokens, "SLIDE_TITLE": "The platform is compliant"}))),
          "compliance claim accepted")
    check(errors(validate_payload(broken(claim_status="implemented"))), "implemented without evidence accepted")
    if base.status_overrides:
        check(errors(validate_payload(broken(status_overrides={}))), "missing badge override accepted")
    roadmap = next(s for s in list_layouts() if s.layout_id == "LAYOUT_20_MULTI_YEAR_ROADMAP")
    tokens = {t: "$1M" for t in roadmap.tokens}
    tokens["TOTAL_INVESTMENT"] = "$9M"
    mismatch = validate_payload(SlidePayload(
        slide_id="T-20", layout_id=roadmap.layout_id, tokens=tokens, claim_status="proposed"))
    check(any(i.severity == "warning" and "does not equal" in i.message for i in mismatch),
          "roadmap total mismatch not reported")
    check(not errors(mismatch), "total mismatch must warn, not block")


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    out = Path(sys.argv[1])
    template = out.with_suffix(".template.pptx")
    layouts = list_layouts()
    check(len(layouts) == 24, f"expected 24 layouts, found {len(layouts)}")

    samples = [SlidePayload.model_validate(p) for p in json.loads(
        (Path(__file__).with_name("pixel_smoke_payloads.json")).read_text(encoding="utf-8"))]
    payloads = [placeholder(s) for s in layouts] + samples
    for payload in payloads:
        for issue in errors(validate_payload(payload)):
            check(False, f"{issue.slide_id}: {issue.message}")
    negative_cases(placeholder(layouts[0]))
    if FAILURES:
        print("\n".join(f"FAIL {f}" for f in FAILURES))
        return 1

    check(build_template(template) == len(layouts), "template slide count differs from layouts")
    check(populate(template, payloads, out) == len(payloads), "populated slide count differs")

    from pptx import Presentation

    deck = Presentation(str(out))
    check(len(deck.slides) == len(payloads), "saved deck slide count differs")
    leftovers = []
    for number, slide in enumerate(deck.slides, 1):
        for shape in slide.shapes:
            check(shape.name != "LAYOUT_ID", f"slide {number} kept its LAYOUT_ID tag")
            if shape.has_text_frame and "{{" in shape.text_frame.text:
                leftovers.append(f"slide {number} {shape.name}")
    check(not leftovers, f"unfilled tokens remain: {leftovers[:5]}")

    expected = design_tokens()["status"]["LOW"]["fill"]
    badge = next((s for s in deck.slides[len(layouts) + 2].shapes if s.name == "BADGE_LLM06_BADGE"), None)
    check(badge is not None, "LLM06 badge missing")
    if badge is not None:
        check(str(badge.fill.fore_color.rgb) == expected, "LOW badge fill is not the LOW colour")
        check(badge.text_frame.text == "LOW", "badge label is not the status text")

    if FAILURES:
        print("\n".join(f"FAIL {f}" for f in FAILURES))
        return 1
    print(f"ok: {len(payloads)} slides ({len(layouts)} placeholder + {len(samples)} sample) -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
