#!/usr/bin/env python3
"""Build a Pixel template deck, or populate one from slide payloads.

Thin command line over src/viharbot/pixel/render.py (see docs/pixel-slide-generation-spec.md).
Needs python-pptx:  pip install '.[pixel]'

Usage:
  python scripts/pptx_populate.py --make-template template.pptx
  python scripts/pptx_populate.py template.pptx payloads.json out.pptx

payloads.json is a list of SlidePayload objects, or {"slides": [...]}. Payloads are validated
against the layout registry first; any error stops the build and nothing is written.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from viharbot.pixel.models import SlidePayload  # noqa: E402
from viharbot.pixel.render import build_template, populate  # noqa: E402
from viharbot.pixel.validate import errors, validate_payload  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--make-template", metavar="PATH")
    ap.add_argument("template", nargs="?")
    ap.add_argument("payloads", nargs="?")
    ap.add_argument("out", nargs="?")
    args = ap.parse_args()
    if args.make_template:
        print(f"wrote {args.make_template} ({build_template(args.make_template)} layouts)")
        return 0
    if not (args.template and args.payloads and args.out):
        ap.error("give --make-template PATH, or template payloads out")
    data = json.loads(Path(args.payloads).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("slides", [data])
    payloads = [SlidePayload.model_validate(item) for item in data]
    found = [i for p in payloads for i in validate_payload(p)]
    for issue in found:
        print(f"{issue.severity}: {issue.slide_id}: {issue.message}", file=sys.stderr)
    if errors(found):
        return 1
    print(f"wrote {args.out} ({populate(args.template, payloads, args.out)} slides)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
