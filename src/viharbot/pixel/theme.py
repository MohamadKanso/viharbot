"""Pixel's design tokens as CSS custom properties, for any page Pixel is responsible for.

One source: data/design_tokens.json, which also draws the .pptx slides and the deck HTML page.
The chat page links the stylesheet this builds, so its output uses the same colours.
No python-pptx, model or network access is needed here.
"""

from __future__ import annotations

from .registry import design_tokens


def _name(key: str) -> str:
    return key.lower().replace("_", "-")


def tokens_css() -> str:
    """`:root{--px-...}` for every theme colour, accent and status colour in the token file."""
    tokens = design_tokens()
    lines: list[str] = []
    for theme, colors in tokens["themes"].items():
        for key, value in colors.items():
            lines.append(f"--px-{theme}-{_name(key)}:#{value};")
    for key, value in tokens["accents"].items():
        lines.append(f"--px-accent-{_name(key)}:#{value};")
    for key, style in tokens["status"].items():
        lines.append(f"--px-{_name(key)}:#{style['fill']};")
        lines.append(f"--px-{_name(key)}-text:#{style['text']};")
    return ":root{" + "".join(lines) + "}\n"
