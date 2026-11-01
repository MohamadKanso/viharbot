"""Approved bindings from an identity's capability to an existing operational service.

A binding names responsibility, not behavior: the service keeps its own prompt, model alias,
permissions and HTTP contract. A binding never grants access to anything.
"""

from __future__ import annotations

import tomllib
from collections.abc import Iterable
from functools import cache
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError

from .identity import AgentId, Capability, CapabilityStatus, NonEmpty, capability_status
from .identity_registry import AgentRegistry, default_registry

BINDINGS_FILE = Path(__file__).with_name("execution_config") / "bindings.toml"

# The one existing service that fulfills each implemented capability.
OPERATIONAL_SERVICES: dict[Capability, str] = {
    Capability.RESEARCH: "research",
    Capability.FACT_CHECK: "fact-check",
    Capability.SOURCE_QUALITY: "source-quality",
    Capability.SECURITY_ASSESSMENT: "security",
    Capability.PRIVACY_REVIEW: "privacy-review",
    Capability.SYNTHESIS: "synthesis",
    Capability.SLIDE_GENERATION: "pixel",
}


class ExecutionConfigError(ValueError):
    """The execution bindings were invalid."""


class ExecutionBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    identity_id: AgentId
    capability: Capability
    service_name: NonEmpty
    enabled: bool = True


class ExecutionBindingSet:
    """Bindings validated against the identity registry and capability definitions."""

    def __init__(self, bindings: Iterable[ExecutionBinding], registry: AgentRegistry) -> None:
        validated: list[ExecutionBinding] = []
        pairs: set[tuple[str, Capability]] = set()
        enabled_capabilities: set[Capability] = set()
        enabled_services: set[str] = set()
        for index, binding in enumerate(bindings):
            where = f"binding[{index}]"
            if binding.identity_id not in registry:
                raise ExecutionConfigError(f"{where}: unknown identity")
            if binding.capability not in registry.get(binding.identity_id).capabilities:
                raise ExecutionConfigError(f"{where}: identity does not declare this capability")
            if capability_status(binding.capability) is not CapabilityStatus.IMPLEMENTED:
                raise ExecutionConfigError(f"{where}: capability is planned and cannot execute")
            expected = OPERATIONAL_SERVICES[binding.capability]
            if binding.service_name not in OPERATIONAL_SERVICES.values():
                raise ExecutionConfigError(f"{where}: unknown service")
            if binding.service_name != expected:
                raise ExecutionConfigError(f"{where}: service does not fulfill this capability")
            pair = (binding.identity_id, binding.capability)
            if pair in pairs:
                raise ExecutionConfigError(f"{where}: duplicate binding")
            pairs.add(pair)
            if binding.enabled:
                if binding.capability in enabled_capabilities:
                    raise ExecutionConfigError(f"{where}: ambiguous, capability already bound")
                if binding.service_name in enabled_services:
                    raise ExecutionConfigError(f"{where}: ambiguous, service already bound")
                enabled_capabilities.add(binding.capability)
                enabled_services.add(binding.service_name)
            validated.append(binding)
        self._bindings = tuple(validated)

    @property
    def bindings(self) -> tuple[ExecutionBinding, ...]:
        return self._bindings

    def lookup(self, capability: Capability) -> ExecutionBinding | None:
        """The enabled binding for a capability, else a disabled one, else None."""
        candidates = [b for b in self._bindings if b.capability is capability]
        return next((b for b in candidates if b.enabled), candidates[0] if candidates else None)

    def __len__(self) -> int:
        return len(self._bindings)


def load_bindings(
    registry: AgentRegistry | None = None, path: Path | None = None
) -> ExecutionBindingSet:
    """Load bindings from repository-controlled configuration.

    `path` exists for tests and is never derived from request data.
    """
    source = path or BINDINGS_FILE
    try:
        data = tomllib.loads(source.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ExecutionConfigError(f"{source.name}: invalid TOML ({exc})") from None
    unknown = set(data) - {"binding"}
    if unknown:
        raise ExecutionConfigError(f"{source.name}: unexpected top-level keys")
    entries = data.get("binding", [])
    if not isinstance(entries, list) or not entries:
        raise ExecutionConfigError(f"{source.name}: no bindings defined")
    parsed: list[ExecutionBinding] = []
    for index, entry in enumerate(entries):
        try:
            parsed.append(ExecutionBinding.model_validate(entry))
        except ValidationError as exc:
            detail = "; ".join(
                f"{'.'.join(str(p) for p in item['loc']) or 'binding'}: {item['msg']}"
                for item in exc.errors(include_input=False, include_url=False)
            )
            raise ExecutionConfigError(f"binding[{index}]: {detail}") from None
    return ExecutionBindingSet(parsed, registry or default_registry())


@cache
def default_bindings() -> ExecutionBindingSet:
    return load_bindings()
