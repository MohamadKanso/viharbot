"""Compare Pixel across models on frozen evidence. A test tool, not part of the service.

Every model gets the same evidence, the same prompts, the same layout and token schemas, the same
validators and the same renderer, so the branding kit (design_tokens.json, layouts.json, render.py)
is shared and the decks differ only in what the model wrote. The model is chosen here, from the
aliases on the command line, never from a request.

Usage (inside the pixel container, which has the dependencies and the LiteLLM settings):
  python -m viharbot.pixel.compare --evidence chat.json --out /data/compare ALIAS [ALIAS ...]
  add --all-layouts to force one slide per layout (24), planned by code, with the model filling them
  add --creative (and --question "a brief") for a test where the model gets only the brief
  add --repeat N to run each alias N times; token counts come from the runs and prices from
  LiteLLM's read-only /model/info (the same key the agents use), so cost needs no extra setup

`chat.json` is a saved /api/chat response, so the six upstream agents are not run again. Each
alias costs a few real model calls through LiteLLM (at most --max-calls). It refuses to run
unless VIHARBOT_PPT_GENERATION_ENABLED is true.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Callable

from .flag import PPT_FLAG, ppt_generation_enabled
from .instructions import CREATIVE_INSTRUCTIONS, PIXEL_INSTRUCTIONS
from .models import DeckManifest, EngagementProfile, Section, SlideStub
from .registry import list_layouts
from .runner import AgentFactory, PixelError, build_deck_in_memory

# Evidence name -> key in a saved /api/chat response.
CHAT_KEYS = {
    "research": "findings",
    "security": "assessment",
    "fact_check": "fact_check",
    "source_quality": "source_quality",
    "privacy_review": "privacy_review",
    "synthesis": "synthesis",
}


def evidence_from_chat(data: dict) -> dict[str, str]:
    evidence = {
        name: json.dumps(data[key], ensure_ascii=False)
        for name, key in CHAT_KEYS.items()
        if data.get(key)
    }
    if not evidence:
        raise PixelError("no team results found in the evidence file")
    return evidence


COVERAGE_NOTE = (
    "This is a layout-coverage test and the evidence is placeholder Lorem Ipsum text. Fill every "
    "token with Lorem-Ipsum-style words of 8 to 12 words instead of '[to be supplied]', so each "
    "layout's text fit can be judged. Still never exceed a token's word limit."
)


def coverage_manifest(evidence: dict[str, str], per_section: int = 6) -> DeckManifest:
    """One slide for every registered layout, in registry order, planned by code, not a model."""
    ref = "synthesis" if "synthesis" in evidence else next(iter(evidence))
    stubs = [
        SlideStub(
            slide_id=f"L{n:02d}", layout_id=spec.layout_id, title=spec.layout_id,
            purpose=spec.purpose[:200], evidence_refs=[ref],
        )
        for n, spec in enumerate(list_layouts(), 1)
    ]
    sections = [
        Section(section_id=f"G{i // per_section + 1}", title="Layout coverage",
                theme="dark", slides=stubs[i : i + per_section])
        for i in range(0, len(stubs), per_section)
    ]
    return DeckManifest(deck_id="coverage", sections=sections)


def agent_factory_for(alias: str, creative: bool = False) -> AgentFactory:
    """The same prompt composition the pixel service uses, with the alias under test."""
    from ..inference_config import effective_inference
    from ..model_agent import build_agent

    inference = effective_inference(
        "pixel", legacy_alias=alias,
        legacy_prompt=CREATIVE_INSTRUCTIONS if creative else PIXEL_INSTRUCTIONS,
    )
    return lambda: build_agent(
        model_alias=inference.model_alias, system_prompt=inference.system_prompt
    )


def deck_stats(path: Path) -> tuple[int, int]:
    """(text blocks, '[to be supplied]' gaps) in a finished deck."""
    from pptx import Presentation

    blocks = gaps = 0
    for slide in Presentation(str(path)).slides:
        for shape in slide.shapes:
            if shape.has_text_frame and not shape.name.startswith("BADGE"):
                text = shape.text_frame.text
                if text.strip():
                    blocks += 1
                    gaps += text.count("[to be supplied]")
    return blocks, gaps


def price_for(alias: str, info: dict) -> tuple[float, float] | None:
    """(input, output) dollars per token from LiteLLM model info, matching the alias with or
    without its provider prefix (the proxy may list `openai/gpt-x` as `gpt-x`)."""
    names = {alias, alias.split("/", 1)[-1]}
    for entry in info.get("data", []):
        if entry.get("model_name") in names:
            meta = entry.get("model_info") or {}
            if meta.get("input_cost_per_token") is not None and meta.get("output_cost_per_token") is not None:
                return float(meta["input_cost_per_token"]), float(meta["output_cost_per_token"])
    return None


def fetch_prices(aliases: list[str]) -> dict[str, tuple[float, float]]:
    """One read-only metadata call to LiteLLM (GET model/info); no inference, no extra key."""
    from ..litellm_client import model_info

    try:
        info = model_info()
    except Exception:
        return {}
    return {a: p for a in aliases if (p := price_for(a, info)) is not None}


def compare(
    aliases: list[str],
    evidence: dict[str, str],
    question: str,
    out_dir: Path,
    *,
    target_slides: int = 6,
    max_model_calls: int = 8,
    all_layouts: bool = False,
    creative: bool = False,
    repeat: int = 1,
    prices: dict[str, tuple[float, float]] | None = None,
    factory_for: Callable[[str], AgentFactory] | None = None,
) -> list[dict]:
    """With creative=True the only input is the brief (`question`); evidence is ignored.

    `prices` maps alias to (input, output) dollars per token. Cost needs both prices and token
    counts, and shows as n/a when either is missing.
    """
    factory_for = factory_for or (lambda alias: agent_factory_for(alias, creative))
    prices = prices or {}
    if creative:
        evidence = {"brief": question}
    out_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    for index, alias in enumerate(aliases):
        for run in range(1, repeat + 1):
            row: dict = {"alias": alias, "run": run, "status": "failed"}
            safe = re.sub(r"[^A-Za-z0-9._-]+", "_", alias)
            path = out_dir / f"deck-{safe}-r{run}.pptx"
            started = time.perf_counter()
            try:
                built = build_deck_in_memory(
                    f"compare-{index}-{run}",
                    EngagementProfile(scope=question[:500]),
                    evidence,
                    len(list_layouts()) if all_layouts else target_slides,
                    path,
                    agent_factory=factory_for(alias),
                    max_model_calls=max_model_calls,
                    manifest=coverage_manifest(evidence) if all_layouts else None,
                    batch_note=COVERAGE_NOTE if all_layouts and not creative else "",
                    creative=creative,
                )
                blocks, gaps = deck_stats(path)
                price = prices.get(alias)
                tokens = built.input_tokens + built.output_tokens
                cost = (
                    built.input_tokens * price[0] + built.output_tokens * price[1]
                    if price and tokens
                    else None
                )
                row.update(
                    status="ok", slides=built.slide_count, left_out=len(built.rejected),
                    blocks=blocks, gaps=gaps, calls=built.model_calls, file=str(path),
                    over_limit=sum(w.count("exceeds") for w in built.warnings),
                    tokens_in=built.input_tokens, tokens_out=built.output_tokens, cost=cost,
                    notes=" | ".join(built.warnings)[:300],
                )
            except Exception as exc:  # one model failing must not stop the others
                row["notes"] = str(exc) if isinstance(exc, PixelError) else type(exc).__name__
            row["seconds"] = round(time.perf_counter() - started, 1)
            rows.append(row)
    return rows


def _usd(value: float | None) -> str:
    return "n/a" if value is None else f"${value:.4f}"


def format_table(rows: list[dict]) -> str:
    header = ["model", "run", "status", "slides", "left out", "gaps/blocks", "over limit", "calls",
              "tokens in/out", "cost", "cost/slide", "sec"]
    lines = [" | ".join(header), " | ".join("---" for _ in header)]
    for r in rows:
        per_slide = r["cost"] / r["slides"] if r.get("cost") is not None and r.get("slides") else None
        lines.append(" | ".join(str(x) for x in (
            r["alias"], r.get("run", 1), r["status"], r.get("slides", "-"), r.get("left_out", "-"),
            f"{r['gaps']}/{r['blocks']}" if "gaps" in r else "-", r.get("over_limit", "-"),
            r.get("calls", "-"),
            f"{r['tokens_in']}/{r['tokens_out']}" if "tokens_in" in r else "-",
            _usd(r.get("cost")), _usd(per_slide), r["seconds"])))
    aliases = list(dict.fromkeys(r["alias"] for r in rows))
    if any(r.get("run", 1) > 1 for r in rows):
        lines.append("\naverages over successful runs:")
        for alias in aliases:
            ok = [r for r in rows if r["alias"] == alias and r["status"] == "ok"]
            if not ok:
                continue
            costs = [r["cost"] for r in ok if r.get("cost") is not None]
            lines.append(
                f"{alias}: {len(ok)} runs | slides {sum(r['slides'] for r in ok) / len(ok):.1f} | "
                f"left out {sum(r['left_out'] for r in ok) / len(ok):.1f} | "
                f"sec {sum(r['seconds'] for r in ok) / len(ok):.1f} | "
                f"cost {_usd(sum(costs) / len(costs) if costs else None)}")
    for r in rows:
        if r.get("notes"):
            lines.append(f"\n{r['alias']} run {r.get('run', 1)}: {r['notes']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("aliases", nargs="+", help="LiteLLM model aliases to compare")
    ap.add_argument("--evidence", help="a saved /api/chat response (JSON); not used with --creative")
    ap.add_argument("--creative", action="store_true",
                    help="test mode: the model gets only --question as a brief and writes freely; "
                         "output is labelled illustrative and forced to 'proposed'")
    ap.add_argument("--question", default="Compare slide output across models", help="deck scope text")
    ap.add_argument("--out", default="compare", help="directory for the decks")
    ap.add_argument("--slides", type=int, default=6)
    ap.add_argument("--max-calls", type=int, default=None,
                    help="model-call cap per alias (default 8, or 16 with --all-layouts)")
    ap.add_argument("--repeat", type=int, default=1, help="runs per alias (1-10); each costs real calls")
    ap.add_argument("--all-layouts", action="store_true",
                    help="one slide per registered layout, planned by code; the model only fills them")
    args = ap.parse_args(argv)
    if not ppt_generation_enabled():
        print(f"{PPT_FLAG} is not true; refusing to spend model calls", file=sys.stderr)
        return 2
    if not args.creative and not args.evidence:
        ap.error("--evidence is required unless --creative is set")
    evidence = {}
    if not args.creative:
        evidence = evidence_from_chat(json.loads(Path(args.evidence).read_text(encoding="utf-8")))
    prices = fetch_prices(args.aliases)
    missing = [a for a in args.aliases if a not in prices]
    if missing:
        print(f"note: no LiteLLM price found for {', '.join(missing)}; cost will show n/a", file=sys.stderr)
    rows = compare(args.aliases, evidence, args.question, Path(args.out),
                   target_slides=args.slides, all_layouts=args.all_layouts, creative=args.creative,
                   max_model_calls=args.max_calls or (16 if args.all_layouts else 8),
                   repeat=max(1, min(10, args.repeat)), prices=prices)
    print(format_table(rows))
    return 0 if all(r["status"] == "ok" for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
