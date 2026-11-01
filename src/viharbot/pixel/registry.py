"""Static Pixel knowledge, loaded on demand from packaged JSON.

Layout specs, framework entries and design tokens live here rather than in Pixel's system
prompt. Code looks up only what a call needs and puts that into the call. Loading reads
packaged files and makes no network or model calls.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

from .models import LayoutSpec

DATA_DIR = Path(__file__).with_name("data")


class UnknownLayout(KeyError):
    pass


class UnknownFramework(KeyError):
    pass


def _load(name: str) -> dict[str, Any]:
    return json.loads((DATA_DIR / name).read_text(encoding="utf-8"))


@cache
def _layouts() -> dict[str, LayoutSpec]:
    specs = [LayoutSpec(**item) for item in _load("layouts.json")["layouts"]]
    by_id = {spec.layout_id: spec for spec in specs}
    if len(by_id) != len(specs):
        raise ValueError("duplicate layout_id in layouts.json")
    for spec in specs:
        seen = [t for c in spec.cells for t in c.tokens] + list(spec.badge_tokens)
        if len(set(seen)) != len(seen):
            raise ValueError(f"duplicate token in {spec.layout_id}")
    return by_id


@cache
def _frameworks() -> dict[str, dict[str, Any]]:
    return _load("frameworks.json")["entries"]


@cache
def design_tokens() -> dict[str, Any]:
    return _load("design_tokens.json")


def list_layouts() -> list[LayoutSpec]:
    return list(_layouts().values())


def get_layout_spec(layout_id: str) -> LayoutSpec:
    try:
        return _layouts()[layout_id]
    except KeyError:
        raise UnknownLayout(layout_id) from None


def framework_keys() -> list[str]:
    return list(_frameworks())


def get_framework(key: str) -> dict[str, Any]:
    try:
        return {"key": key, **_frameworks()[key]}
    except KeyError:
        raise UnknownFramework(key) from None


def layout_prompt_view(spec: LayoutSpec) -> dict[str, Any]:
    """The compact form of a layout that goes into a model prompt."""
    return {
        "layout_id": spec.layout_id,
        "purpose": spec.purpose,
        "tokens": list(spec.tokens),
        "badge_tokens": list(spec.badge_tokens),
        "word_limits": {"default": 15, **spec.word_limits},
    }
