from __future__ import annotations

import socket
import subprocess
import sys
from pathlib import Path

import pytest

from viharbot import identity, identity_execution, inference_config
from viharbot.execution_bindings import (
    OPERATIONAL_SERVICES,
    ExecutionBinding,
    ExecutionBindingSet,
    default_bindings,
)
from viharbot.identity import AgentIdentity, Capability, CapabilityStatus
from viharbot.identity_registry import AgentRegistry, default_registry, load_registry
from viharbot.inference_config import (
    IDENTITY_ROUTING_FLAG,
    PERSONALITY_FLAG,
    PRECEDENCE_CLAUSE,
    InferenceConfigError,
    compose_system_prompt,
    effective_inference,
    personality_inference_active,
    resolve_inference_config,
)

INSTRUCTIONS = "SERVICE-INSTRUCTIONS: do not invent sources."
EXPECTED_IDENTITY = {
    "research": "nova", "fact-check": "scout", "source-quality": "scout",
    "security": "cipher", "privacy-review": "cipher", "synthesis": "charlie", "pixel": "pixel",
}


def resolve(service="research", alias="svc/alias", **kwargs):
    return resolve_inference_config(service, INSTRUCTIONS, alias, **kwargs)


def with_alias(registry, identity_id, alias):
    changed = [i.model_copy(update={"model_alias": alias}) if i.id == identity_id else i
               for i in registry.list()]
    return AgentRegistry(changed)


# 1, 6, 7. approved identities resolve with their prompt and the capability instructions
@pytest.mark.parametrize("service,identity_id", sorted(EXPECTED_IDENTITY.items()))
def test_approved_services_resolve_to_their_bound_identity(service, identity_id):
    resolved = resolve(service)
    assert resolved.identity_id == identity_id
    assert resolved.service_name == service
    assert resolved.capability is next(c for c, v in OPERATIONAL_SERVICES.items() if v == service)
    assert default_registry().get(identity_id).system_prompt in resolved.system_prompt
    assert INSTRUCTIONS in resolved.system_prompt
    assert resolved.model_alias == "svc/alias"


def test_every_implemented_service_is_covered():
    assert set(EXPECTED_IDENTITY) == set(OPERATIONAL_SERVICES.values())


# 8. deterministic composition, identity first, instructions last and verbatim
def test_composition_is_deterministic_and_ordered():
    first, second = resolve().system_prompt, resolve().system_prompt
    assert first == second
    identity_prompt = default_registry().get("nova").system_prompt
    assert first == compose_system_prompt(identity_prompt, INSTRUCTIONS)
    assert first.index(identity_prompt) < first.index(PRECEDENCE_CLAUSE) < first.index(INSTRUCTIONS)
    assert first.endswith(INSTRUCTIONS)


def test_capability_instructions_are_preserved_verbatim_for_every_service():
    for service in EXPECTED_IDENTITY:
        text = "line one\n  line two with  spacing."
        resolved = resolve_inference_config(service, text, "a/b")
        assert resolved.system_prompt.endswith(text)


# 9. nothing user-controlled reaches the system prompt
def test_resolver_accepts_no_request_data():
    import inspect

    params = set(inspect.signature(resolve_inference_config).parameters)
    assert params == {"service_name", "capability_instructions", "service_alias", "registry",
                      "bindings"}


# 2, 3, 4, 5. rejections
@pytest.mark.parametrize("service", ["", "orbit-runner", "automation", "Research", None])
def test_unknown_services_are_rejected(service):
    with pytest.raises(InferenceConfigError, match="unknown service"):
        resolve(service)


def test_unregistered_identity_is_rejected():
    without_nova = AgentRegistry([i for i in default_registry().list() if i.id != "nova"])
    with pytest.raises(InferenceConfigError, match="not registered"):
        resolve("research", registry=without_nova, bindings=default_bindings())


def test_identity_that_does_not_declare_the_capability_is_rejected():
    stripped = [
        i.model_copy(update={"capabilities": (Capability.SYNTHESIS,)}) if i.id == "nova" else i
        for i in default_registry().list()
    ]
    with pytest.raises(InferenceConfigError, match="does not declare"):
        resolve("research", registry=AgentRegistry(stripped), bindings=default_bindings())


def test_disabled_and_missing_bindings_cannot_activate_inference():
    disabled = ExecutionBindingSet(
        [ExecutionBinding(identity_id=b.identity_id, capability=b.capability,
                          service_name=b.service_name, enabled=b.service_name != "research")
         for b in default_bindings().bindings], default_registry())
    with pytest.raises(InferenceConfigError, match="binding is disabled"):
        resolve("research", bindings=disabled)
    with pytest.raises(InferenceConfigError, match="no approved binding"):
        resolve("research", bindings=ExecutionBindingSet([], default_registry()))
    assert resolve("security", bindings=disabled).identity_id == "cipher"


def test_planned_capability_cannot_activate_inference(monkeypatch):
    monkeypatch.setitem(identity.CAPABILITY_STATUS, Capability.RESEARCH, CapabilityStatus.PLANNED)
    with pytest.raises(InferenceConfigError, match="not implemented"):
        resolve("research")


def test_identities_without_bindings_can_never_be_resolved():
    # atlas, sage and orbit declare only planned capabilities and have no bindings.
    bound = {resolve(s).identity_id for s in EXPECTED_IDENTITY}
    assert bound == {"nova", "scout", "cipher", "charlie", "pixel"}


def test_empty_capability_instructions_are_rejected():
    with pytest.raises(InferenceConfigError, match="instructions are empty"):
        resolve_inference_config("research", "  ", "a/b")


# 10, 11, 12. alias policy
def test_service_alias_is_used_when_no_identity_alias_is_configured():
    assert all(i.model_alias is None for i in default_registry().list())
    assert resolve("security", alias="  openai/gpt-5.4  ").model_alias == "openai/gpt-5.4"


def test_identity_alias_takes_precedence_when_explicitly_configured():
    registry = with_alias(default_registry(), "nova", "approved/nova-alias")
    resolved = resolve("research", alias="svc/alias", registry=registry)
    assert resolved.model_alias == "approved/nova-alias"
    # Other services still use their own aliases.
    assert resolve("security", alias="svc/alias", registry=registry).model_alias == "svc/alias"


@pytest.mark.parametrize("alias", ["", "   "])
def test_missing_alias_fails_closed(alias):
    with pytest.raises(InferenceConfigError, match="no model alias is configured"):
        resolve("research", alias=alias)


def test_invalid_alias_is_rejected_without_echoing_it():
    with pytest.raises(InferenceConfigError, match="model alias is invalid") as caught:
        resolve("research", alias="has space")
    assert "has space" not in str(caught.value)


def test_preferred_model_families_are_never_used_as_aliases():
    for item in default_registry().list():
        assert item.model_alias is None
    for service in EXPECTED_IDENTITY:
        families = {i.preferred_model_family for i in default_registry().list()}
        with pytest.raises(InferenceConfigError, match="no model alias"):
            resolve(service, alias="")
        assert resolve(service, alias="svc/x").model_alias not in families


# 13, 15. flags
def test_flags_default_to_disabled(monkeypatch):
    monkeypatch.delenv(PERSONALITY_FLAG, raising=False)
    monkeypatch.delenv(IDENTITY_ROUTING_FLAG, raising=False)
    assert personality_inference_active() is False


@pytest.mark.parametrize("personality,routing,expected", [
    ("true", "true", True), ("1", "yes", True), ("true", "false", False),
    ("false", "true", False), ("true", None, False), (None, "true", False), ("", "", False),
])
def test_personality_requires_both_flags(monkeypatch, personality, routing, expected):
    for name, value in ((PERSONALITY_FLAG, personality), (IDENTITY_ROUTING_FLAG, routing)):
        monkeypatch.delenv(name, raising=False)
        if value is not None:
            monkeypatch.setenv(name, value)
    assert personality_inference_active() is expected


def test_routing_flag_name_matches_the_orchestrator():
    assert IDENTITY_ROUTING_FLAG == identity_execution.IDENTITY_ROUTING_FLAG


def test_effective_inference_is_legacy_unless_both_flags_are_on(monkeypatch):
    monkeypatch.setenv(PERSONALITY_FLAG, "true")
    monkeypatch.delenv(IDENTITY_ROUTING_FLAG, raising=False)
    legacy = effective_inference("research", legacy_alias="a/b", legacy_prompt="LEGACY")
    assert (legacy.model_alias, legacy.system_prompt, legacy.personality_mode) == ("a/b", "LEGACY", False)
    assert legacy.identity_id is None
    monkeypatch.setenv(IDENTITY_ROUTING_FLAG, "true")
    active = effective_inference("research", legacy_alias="a/b", legacy_prompt="LEGACY")
    assert active.personality_mode and active.identity_id == "nova" and active.capability == "research"
    assert active.system_prompt.endswith("LEGACY") and active.system_prompt != "LEGACY"


def test_log_fields_distinguish_personality_mode():
    from viharbot.inference_config import EffectiveInference

    assert EffectiveInference(model_alias="a/b", system_prompt="p").log_extra() == {"model_alias": "a/b"}
    assert EffectiveInference(model_alias="", system_prompt="p").log_extra() == {"model_alias": None}
    active = EffectiveInference(model_alias="a/b", system_prompt="p", identity_id="nova",
                                capability="research", personality_mode=True)
    assert active.log_extra() == {"model_alias": "a/b", "identity_id": "nova",
                                  "capability": "research", "personality_mode": True}


# 22, 23. no network, no provider SDKs
def test_resolution_makes_no_network_calls(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    assert resolve("synthesis").identity_id == "charlie"


def test_resolver_does_not_import_inference_clients():
    code = (
        "import sys\n"
        "from viharbot.inference_config import resolve_inference_config\n"
        "resolve_inference_config('research', 'x', 'a/b')\n"
        "bad = [m for m in ('strands', 'litellm', 'boto3', 'openai', 'anthropic', "
        "'google.generativeai') if m in sys.modules]\n"
        "assert not bad, bad\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_no_provider_native_inference_is_introduced():
    src = Path(inference_config.__file__).read_text()
    for marker in ("import openai", "import anthropic", "generativeai", "api.openai.com",
                   "api.anthropic.com", "urlopen", "requests", "httpx"):
        assert marker not in src
