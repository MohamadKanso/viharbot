"""Load and validate the packaged persona definitions.

Loading is deterministic and side-effect free: it reads files shipped with the package and makes
no network, LiteLLM, AWS, Docker or Strands calls. Nothing in the application runtime reads this
registry in Phase A, so declaring a persona cannot change execution, prompts or permissions.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterable
from functools import cache
from pathlib import Path

from pydantic import ValidationError

from ..identity_registry import AgentRegistry, default_registry
from .models import Persona

PERSONAS_DIR = Path(__file__).with_name("definitions")
_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

# The eight established identities. Each must always have a persona. The two education identities
# (archie, ada) may have one later; they are not required in Phase A.
REQUIRED_PERSONA_IDS: frozenset[str] = frozenset(
    {"charlie", "nova", "atlas", "pixel", "sage", "cipher", "scout", "orbit"}
)


class PersonaConfigError(ValueError):
    """A persona file or the persona registry was invalid."""


class PersonaRegistry:
    """Personas validated against the identity registry, ordered by agent id."""

    def __init__(
        self,
        personas: Iterable[Persona],
        agents: AgentRegistry,
        required: frozenset[str] = REQUIRED_PERSONA_IDS,
    ) -> None:
        by_id: dict[str, Persona] = {}
        for persona in personas:
            if persona.agent_id in by_id:
                raise PersonaConfigError(f"{persona.agent_id}: duplicate persona")
            by_id[persona.agent_id] = persona
        for persona in by_id.values():
            self._check(persona, by_id, agents)
        missing = sorted(required - set(by_id))
        if missing:
            raise PersonaConfigError(f"missing personas for: {', '.join(missing)}")
        self._by_id = dict(sorted(by_id.items()))

    @staticmethod
    def _check(persona: Persona, by_id: dict[str, Persona], agents: AgentRegistry) -> None:
        where = persona.agent_id
        if where not in agents:
            raise PersonaConfigError(f"{where}: no such agent identity")
        identity = agents.get(where)
        personality = identity.personality.lower()
        for trait in persona.traits.identity_traits:
            if trait.lower() not in personality:
                raise PersonaConfigError(
                    f"{where}: identity trait is not in the approved identity personality"
                )
        if persona.model_preference.family != identity.preferred_model_family:
            raise PersonaConfigError(
                f"{where}: model family does not match the identity's preferred_model_family"
            )
        for peer in persona.collaboration:
            if peer.agent_id not in agents:
                raise PersonaConfigError(f"{where}: collaboration names an unknown agent")

    def get(self, agent_id: str) -> Persona:
        try:
            return self._by_id[agent_id]
        except KeyError:
            raise KeyError(f"no persona for agent id: {agent_id}") from None

    def list(self) -> tuple[Persona, ...]:
        return tuple(self._by_id.values())

    def __contains__(self, agent_id: object) -> bool:
        return agent_id in self._by_id

    def __len__(self) -> int:
        return len(self._by_id)


def _describe(error: ValidationError) -> str:
    # Field paths and messages only; never echo the rejected values.
    return "; ".join(
        f"{'.'.join(str(part) for part in item['loc']) or 'document'}: {item['msg']}"
        for item in error.errors(include_input=False, include_url=False)
    )


def load_persona_registry(
    directory: Path | None = None,
    agents: AgentRegistry | None = None,
    required: frozenset[str] | None = None,
) -> PersonaRegistry:
    """Load every `*.toml` persona in the packaged directory.

    `directory`, `agents` and `required` exist for tests. None is derived from request data.
    """
    root = directory or PERSONAS_DIR
    registry = agents or default_registry()
    personas: list[Persona] = []
    for path in sorted(root.glob("*.toml")):
        source = path.name
        if not _ID_PATTERN.fullmatch(path.stem):
            raise PersonaConfigError(f"{source}: file name is not a valid agent id")
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise PersonaConfigError(f"{source}: invalid TOML ({exc})") from None
        if data.get("agent_id") != path.stem:
            raise PersonaConfigError(f"{source}: agent_id must match the file name")
        try:
            personas.append(Persona.model_validate(data))
        except ValidationError as exc:
            raise PersonaConfigError(f"{source}: {_describe(exc)}") from None
    if not personas:
        raise PersonaConfigError("no personas found")
    return PersonaRegistry(
        personas, registry, REQUIRED_PERSONA_IDS if required is None else required
    )


@cache
def default_persona_registry() -> PersonaRegistry:
    return load_persona_registry()
