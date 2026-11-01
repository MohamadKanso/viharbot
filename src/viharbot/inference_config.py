"""Resolve the effective inference configuration for an operational service.

Legacy behavior is the default. Personality-aware inference activates only inside a service
whose own environment enables its flag, and only for that service's approved binding. No
identity ever arrives from a request, and resolution makes no network or model calls.
"""

from __future__ import annotations

import os

from pydantic import BaseModel, ConfigDict, ValidationError

from .execution_bindings import (
    OPERATIONAL_SERVICES,
    ExecutionBindingSet,
    default_bindings,
    load_bindings,
)
from .identity import AgentId, Capability, CapabilityStatus, ModelAlias, NonEmpty, capability_status
from .identity_registry import AgentRegistry, default_registry

PERSONALITY_FLAG = "VIHARBOT_PERSONALITY_INFERENCE_ENABLED"

SERVICE_CAPABILITY: dict[str, Capability] = {
    service: capability for capability, service in OPERATIONAL_SERVICES.items()
}

PRECEDENCE_CLAUSE = (
    "The task instructions below define what you must do and what you must not claim. "
    "If your persona above conflicts with them, the task instructions take precedence."
)


class InferenceConfigError(RuntimeError):
    """Personality-aware inference was requested but could not be configured safely.

    Messages are fixed strings; they never include prompts, aliases or request data.
    """


class ResolvedInferenceConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    identity_id: AgentId
    capability: Capability
    service_name: NonEmpty
    model_alias: ModelAlias
    system_prompt: NonEmpty


class EffectiveInference(BaseModel):
    """What a service actually uses for one call."""

    model_config = ConfigDict(frozen=True)

    model_alias: str
    system_prompt: str
    identity_id: str | None = None
    capability: str | None = None
    personality_mode: bool = False

    def log_extra(self) -> dict[str, object]:
        """Log fields for this call. Legacy calls add nothing beyond the alias."""
        extra: dict[str, object] = {"model_alias": self.model_alias or None}
        if self.personality_mode:
            extra.update(
                identity_id=self.identity_id,
                capability=self.capability,
                personality_mode=True,
            )
        return extra


def _flag(name: str) -> bool:
    return os.getenv(name, "false").strip().lower() in {"1", "true", "yes"}


def personality_inference_active() -> bool:
    """The flag must be set in this service's own environment."""
    return _flag(PERSONALITY_FLAG)


def compose_system_prompt(identity_prompt: str, capability_instructions: str) -> str:
    """Identity prompt, then precedence clause, then the service's own instructions verbatim."""
    return "\n\n".join(
        (identity_prompt.strip(), PRECEDENCE_CLAUSE, capability_instructions.strip())
    )


def resolve_inference_config(
    service_name: str,
    capability_instructions: str,
    service_alias: str,
    registry: AgentRegistry | None = None,
    bindings: ExecutionBindingSet | None = None,
) -> ResolvedInferenceConfig:
    """Resolve identity, prompt and alias for a service from trusted configuration only."""
    registry = registry or default_registry()
    if bindings is None:
        bindings = default_bindings() if registry is default_registry() else load_bindings(registry)

    capability = SERVICE_CAPABILITY.get(service_name)
    if capability is None:
        raise InferenceConfigError("unknown service")
    if capability_status(capability) is not CapabilityStatus.IMPLEMENTED:
        raise InferenceConfigError("capability is not implemented")
    binding = bindings.lookup(capability)
    if binding is None:
        raise InferenceConfigError("no approved binding for this service")
    if not binding.enabled:
        raise InferenceConfigError("binding is disabled")
    if binding.service_name != service_name:
        raise InferenceConfigError("binding does not match this service")
    if binding.identity_id not in registry:
        raise InferenceConfigError("bound identity is not registered")
    identity = registry.get(binding.identity_id)
    if capability not in identity.capabilities:
        raise InferenceConfigError("identity does not declare this capability")
    if not capability_instructions.strip():
        raise InferenceConfigError("capability instructions are empty")

    # Precedence: the identity's own approved alias, then the service's existing alias,
    # otherwise fail closed. preferred_model_family is a description, never an alias.
    alias = identity.model_alias or service_alias.strip()
    if not alias:
        raise InferenceConfigError("no model alias is configured")
    try:
        return ResolvedInferenceConfig(
            identity_id=identity.id,
            capability=capability,
            service_name=service_name,
            model_alias=alias,
            system_prompt=compose_system_prompt(identity.system_prompt, capability_instructions),
        )
    except ValidationError:
        raise InferenceConfigError("model alias is invalid") from None


def legacy_inference(alias: str, prompt: str) -> EffectiveInference:
    return EffectiveInference(model_alias=alias, system_prompt=prompt)


def effective_inference(
    service_name: str,
    *,
    legacy_alias: str,
    legacy_prompt: str,
    registry: AgentRegistry | None = None,
    bindings: ExecutionBindingSet | None = None,
) -> EffectiveInference:
    """The service's existing behavior, unless the flag is on in its environment."""
    if not personality_inference_active():
        return legacy_inference(legacy_alias, legacy_prompt)
    resolved = resolve_inference_config(
        service_name, legacy_prompt, legacy_alias, registry, bindings
    )
    return EffectiveInference(
        model_alias=resolved.model_alias,
        system_prompt=resolved.system_prompt,
        identity_id=resolved.identity_id,
        capability=resolved.capability.value,
        personality_mode=True,
    )
