"""Small builders shared by the Pixel core tests. No model, network or real keys."""

from __future__ import annotations

from viharbot.pixel.models import LayoutSpec, SlidePayload
from viharbot.pixel.registry import list_layouts


def all_specs() -> list[LayoutSpec]:
    return list_layouts()


def payload_for(spec: LayoutSpec, text: str = "placeholder", status: str = "HIGH", **extra) -> SlidePayload:
    fields = {
        "slide_id": spec.layout_id[7:9] if spec.layout_id[7:9].isdigit() else "s1",
        "layout_id": spec.layout_id,
        "tokens": {t: text for t in spec.tokens},
        "status_overrides": {b: status for b in spec.badge_tokens},
        "claim_status": "proposed",
    }
    fields.update(extra)
    return SlidePayload(**fields)


def longest_payload(spec: LayoutSpec) -> SlidePayload:
    """Every field filled with the maximum number of words its layout allows."""
    tokens = {}
    for token in spec.tokens:
        limit = spec.word_limits.get(token, 15)
        tokens[token] = " ".join(["Segmentation"] * limit)
    return payload_for(spec, tokens=tokens)
