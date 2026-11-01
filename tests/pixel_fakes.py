"""A scripted stand-in for the model that Pixel's tests share. No network, no model, no keys.

`ScriptedAgent` is what `agent_factory()` returns: called as `agent(prompt, structured_output_model=M)`
it answers a DeckManifest request with a fixed plan and a SlideBatch request with valid payloads
for exactly the stubs named in the prompt. `broken` makes chosen slides invalid for their first N
appearances, so retry behaviour can be exercised without a real model.
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace

from viharbot.pixel.models import (
    DeckManifest, Section, SlideBatch, SlidePayload, SlideStub)
from viharbot.pixel.registry import get_layout_spec

LAYOUT = "LAYOUT_01_BOARD_BLUF"
USAGE = {"inputTokens": 100, "outputTokens": 40}
_STUBS = re.compile(r"Stubs:\n(\[.*\])\n")


def stub(slide_id: str, layout_id: str = LAYOUT, refs: tuple[str, ...] = ("synthesis",)) -> SlideStub:
    return SlideStub(slide_id=slide_id, layout_id=layout_id, title=f"Title {slide_id}",
                     purpose="Purpose", evidence_refs=list(refs))


def manifest_of(*stubs: SlideStub, per_section: int | None = None, deck_id: str = "x") -> DeckManifest:
    size = per_section or len(stubs) or 1
    sections = [Section(section_id=f"S{i // size + 1}", title="Section", theme="dark",
                        slides=list(stubs[i:i + size])) for i in range(0, len(stubs), size)]
    return DeckManifest(deck_id=deck_id, sections=sections)


def payload_for(s: SlideStub, text: str = "Lorem ipsum", **changes) -> SlidePayload:
    spec = get_layout_spec(s.layout_id)
    data = dict(
        slide_id=s.slide_id, layout_id=s.layout_id,
        tokens={name: text for name in spec.tokens},
        status_overrides={b: "LOW" for b in spec.badge_tokens},
        claim_status="proposed", evidence_refs=list(s.evidence_refs))
    data.update(changes)
    return SlidePayload(**data)


def invalid_payload_for(s: SlideStub) -> SlidePayload:
    """Fails validation: a declared token is missing."""
    p = payload_for(s)
    tokens = dict(p.tokens)
    tokens.pop("SLIDE_TITLE")
    return p.model_copy(update={"tokens": tokens})


class ScriptedAgent:
    def __init__(self, manifest: DeckManifest | None = None, *, broken: dict[str, int] | None = None,
                 usage: dict | None = USAGE, claim_status: str = "proposed", text: str = "Lorem ipsum",
                 fail_with: Exception | None = None, manifests: list[DeckManifest] | None = None):
        self.manifests = list(manifests) if manifests else [manifest]
        self.broken = dict(broken or {})
        self.usage = usage
        self.claim_status = claim_status
        self.text = text
        self.fail_with = fail_with
        self.prompts: list[str] = []
        self.models: list[type] = []

    def __call__(self, prompt, *, structured_output_model):
        self.prompts.append(prompt)
        self.models.append(structured_output_model)
        if self.fail_with is not None:
            raise self.fail_with
        metrics = SimpleNamespace(accumulated_usage=self.usage) if self.usage is not None else None
        if structured_output_model is DeckManifest:
            value = self.manifests.pop(0) if len(self.manifests) > 1 else self.manifests[0]
        else:
            stubs = [SlideStub(**d) for d in json.loads(_STUBS.search(prompt).group(1))]
            slides = []
            for s in stubs:
                if self.broken.get(s.slide_id, 0) > 0:
                    self.broken[s.slide_id] -= 1
                    slides.append(invalid_payload_for(s))
                else:
                    slides.append(payload_for(s, self.text, claim_status=self.claim_status))
            value = SlideBatch(slides=slides)
        return SimpleNamespace(structured_output=value, metrics=metrics)

    def factory(self):
        return lambda: self
