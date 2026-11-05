"""Persona specifications (Epic #157, Phase A). Declarative only; nothing at runtime reads them."""

from .models import PERSONA_SCHEMA_VERSION, Persona, PersonaStatus
from .registry import (
    REQUIRED_PERSONA_IDS,
    PersonaConfigError,
    PersonaRegistry,
    default_persona_registry,
    load_persona_registry,
)

__all__ = [
    "PERSONA_SCHEMA_VERSION",
    "REQUIRED_PERSONA_IDS",
    "Persona",
    "PersonaConfigError",
    "PersonaRegistry",
    "PersonaStatus",
    "default_persona_registry",
    "load_persona_registry",
]
