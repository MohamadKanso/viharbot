"""Render a persona into a system-prompt section (Epic #157, Phase B).

Pure and deterministic: no I/O, no model calls. The section shapes tone and approach only. It is
placed before the precedence clause by `inference_config.compose_system_prompt`, so the task
instructions outrank it. It deliberately omits the biography, the illustrative examples (so they
cannot become response templates), the model preference, the avatar and the provenance.
"""

from __future__ import annotations

from .models import Persona

SCOPE_CLAUSE = (
    "Apply this persona only to the wording of free-text explanation. It never changes required "
    "output structure, field names, schemas, citations or facts, and it grants you no tools, "
    "access, approvals or abilities."
)


def _bullets(items: tuple[str, ...]) -> str:
    return "\n".join(f"- {item}" for item in items)


def render_persona_block(persona: Persona) -> str:
    """The persona section of a system prompt. Same persona in, same text out."""
    traits = persona.traits
    comm = persona.communication
    reasoning = persona.reasoning
    assessment = persona.self_assessment
    disagreement = persona.disagreement
    parts = (
        f"## Persona profile ({persona.agent_id}, version {persona.provenance.persona_version})",
        f"Character: {', '.join(traits.identity_traits)}.",
        f"Values: {'; '.join(persona.values)}.",
        f"Principles:\n{_bullets(persona.principles)}",
        f"Tone: {comm.tone}",
        f"Sentence style: {comm.sentence_style}",
        f"Preferred vocabulary: {', '.join(comm.preferred_vocabulary)}.",
        f"Avoid:\n{_bullets(comm.avoid)}",
        f"Reasoning approach: {reasoning.approach}",
        f"Evidence standard: {reasoning.evidence_standard}",
        f"Uncertainty: {assessment.uncertainty_reporting} "
        f"Confidence terms: {', '.join(assessment.confidence_vocabulary)}.",
        f"If you disagree: {disagreement.style} {disagreement.resolution_preference} "
        "Base any disagreement on evidence in the supplied material; never invent evidence.",
        SCOPE_CLAUSE,
    )
    return "\n\n".join(parts)
