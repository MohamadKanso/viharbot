"""Parse one lesson from Markdown with TOML front matter. Never executes or follows its text.

Format (a documented difference from the onboarding repos, whose lessons have no front matter):

    +++
    lesson_id = "git-fundamentals"
    ... scalar fields, [[instructor_references]], [hands_on_exercise], [knowledge_assessment]
    +++
    ## Learning objectives      (numbered or bulleted list)
    ## Core concepts            (list)
    ## Technical explanation    (prose)
    ## Practical example        (prose)
    ## Expected outcomes        (list)

TOML is parsed with the standard library, so no YAML dependency is added.
"""

from __future__ import annotations

import re
import tomllib

from pydantic import ValidationError

from .models import Lesson

MAX_BYTES = 200_000
FENCE = "+++"
LIST_SECTIONS = {"learning objectives": "learning_objectives", "core concepts": "core_concepts",
                 "expected outcomes": "expected_outcomes"}
PROSE_SECTIONS = {"technical explanation": "technical_explanation",
                  "practical example": "practical_example"}
ITEM = re.compile(r"^\s*(?:[-*]|\d+[.)])\s+(.*\S)\s*$")
H2 = re.compile(r"^##\s+(.+?)\s*$")


class LessonFormatError(ValueError):
    """The text is not a valid lesson. The message names the problem, never echoes the text."""


def _split(text: str) -> tuple[str, str]:
    lines = text.replace("\r\n", "\n").split("\n")
    if not lines or lines[0].strip() != FENCE:
        raise LessonFormatError("missing opening front matter fence '+++'")
    for i in range(1, len(lines)):
        if lines[i].strip() == FENCE:
            return "\n".join(lines[1:i]), "\n".join(lines[i + 1:])
    raise LessonFormatError("missing closing front matter fence '+++'")


def _sections(body: str) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    current: list[str] | None = None
    fenced = False
    for line in body.split("\n"):
        if line.strip().startswith("```"):
            fenced = not fenced
        m = None if fenced else H2.match(line)
        if m:
            key = m[1].strip().lower()
            if key in found:
                raise LessonFormatError(f"duplicate section '{key}'")
            current = found[key] = []
        elif current is not None:
            current.append(line)
    return found


def parse_lesson(text: str) -> Lesson:
    if len(text.encode()) > MAX_BYTES:
        raise LessonFormatError("lesson exceeds the size limit")
    head, body = _split(text)
    try:
        data = tomllib.loads(head)
    except tomllib.TOMLDecodeError as exc:
        raise LessonFormatError("front matter is not valid TOML") from exc
    sections = _sections(body)
    for heading, field in LIST_SECTIONS.items():
        if heading not in sections:
            raise LessonFormatError(f"missing section '{heading}'")
        data[field] = [m[1] for ln in sections[heading] if (m := ITEM.match(ln))]
    for heading, field in PROSE_SECTIONS.items():
        if heading not in sections:
            raise LessonFormatError(f"missing section '{heading}'")
        data[field] = "\n".join(sections[heading]).strip()
    try:
        return Lesson.model_validate(data)
    except ValidationError as exc:
        # Field locations and messages only; input values are not echoed.
        problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        raise LessonFormatError(f"invalid lesson: {problems}") from exc
