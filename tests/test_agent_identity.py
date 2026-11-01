from __future__ import annotations

import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from viharbot import identity_registry
from viharbot.identity import (
    AgentIdentity,
    AgentRuntimeConfig,
    Capability,
    CapabilityStatus,
    capability_status,
)
from viharbot.identity_registry import (
    IDENTITY_DIR,
    AgentRegistry,
    IdentityConfigError,
    load_registry,
)

EXPECTED_IDS = {"charlie", "nova", "atlas", "pixel", "sage", "cipher", "scout", "orbit"}
EXPECTED_CAPABILITIES = {
    "charlie": {"synthesis", "research"},
    "nova": {"research", "evidence_discovery"},
    "atlas": {"logistics_planning"},
    "pixel": {"creative_design", "slide_generation"},
    "sage": {"philosophical_analysis"},
    "cipher": {"security_assessment", "privacy_review"},
    "scout": {"evidence_discovery", "source_quality", "fact_check"},
    "orbit": {"automation_engineering"},
}
EXPECTED_FAMILIES = {
    "charlie": "GPT-6 family",
    "nova": "Gemini Pro family",
    "atlas": "Claude Sonnet family",
    "pixel": "Gemini multimodal family",
    "sage": "Claude Opus family",
    "cipher": "GPT-6 family",
    "scout": "Gemini Pro family",
    "orbit": "Claude Sonnet family",
}


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """A writable copy of the packaged identities for building invalid configurations."""
    target = tmp_path / "agent_identities"
    shutil.copytree(IDENTITY_DIR, target)
    return target


def identity_kwargs(**overrides):
    base = dict(
        id="demo", name="Demo", title="Demo Agent", description="d", personality="p",
        expertise=("e",), capabilities=(Capability.RESEARCH,),
        preferred_model_family="Family", system_prompt="prompt",
    )
    return {**base, **overrides}


# 1, 2, 7, 8: loading, uniqueness, lookup, listing
def test_all_eight_identities_load_with_unique_ids():
    registry = load_registry()
    ids = [identity.id for identity in registry.list()]
    assert len(registry) == 8 and len(ids) == 8
    assert set(ids) == EXPECTED_IDS and len(set(ids)) == 8


def test_lookup_and_membership():
    registry = load_registry()
    assert registry.get("cipher").name == "Cipher"
    assert registry.get("charlie").title == "Chief Strategist & AI Orchestrator"
    assert "orbit" in registry and "nobody" not in registry
    with pytest.raises(KeyError):
        registry.get("nobody")


def test_default_registry_is_cached_and_equal():
    assert identity_registry.default_registry() is identity_registry.default_registry()


# 3: required fields
@pytest.mark.parametrize("identity", load_registry().list(), ids=lambda i: i.id)
def test_every_identity_has_all_required_fields(identity):
    for field in ("name", "title", "description", "personality", "preferred_model_family",
                  "system_prompt"):
        assert getattr(identity, field).strip()
    assert identity.expertise and identity.capabilities


def test_capabilities_and_model_families_match_the_specification():
    for identity in load_registry().list():
        assert {c.value for c in identity.capabilities} == EXPECTED_CAPABILITIES[identity.id]
        assert identity.preferred_model_family == EXPECTED_FAMILIES[identity.id]


# 4, 5, 6: invalid configurations
@pytest.mark.parametrize("override", [
    {"id": "Bad Id"}, {"id": ""}, {"name": "  "}, {"title": ""}, {"description": ""},
    {"personality": ""}, {"expertise": ()}, {"capabilities": ()},
    {"preferred_model_family": ""}, {"system_prompt": ""}, {"model_alias": "has space"},
    {"capabilities": (Capability.RESEARCH, Capability.RESEARCH)},
    {"capabilities": ("telepathy",)},
])
def test_invalid_identity_values_are_rejected(override):
    with pytest.raises(ValidationError):
        AgentIdentity(**identity_kwargs(**override))


def test_unknown_identity_fields_are_rejected():
    with pytest.raises(ValidationError):
        AgentIdentity(**identity_kwargs(api_key="x"))


def test_identity_is_immutable():
    identity = load_registry().get("nova")
    with pytest.raises(ValidationError):
        identity.name = "Other"


def test_duplicate_ids_are_rejected():
    one = AgentIdentity(**identity_kwargs())
    with pytest.raises(IdentityConfigError, match="duplicate agent id: demo"):
        AgentRegistry([one, one])


def test_unknown_capability_in_file_is_rejected_with_file_name(config_dir):
    path = config_dir / "nova.toml"
    path.write_text(path.read_text().replace('"evidence_discovery"', '"mind_reading"'))
    with pytest.raises(IdentityConfigError, match=r"nova\.toml.*capabilities"):
        load_registry(config_dir)


def test_error_messages_do_not_echo_rejected_values(config_dir):
    path = config_dir / "nova.toml"
    path.write_text(path.read_text().replace('"evidence_discovery"', '"SECRET-VALUE"'))
    with pytest.raises(IdentityConfigError) as caught:
        load_registry(config_dir)
    assert "SECRET-VALUE" not in str(caught.value)


@pytest.mark.parametrize("edit,message", [
    (lambda t: t.replace('id = "atlas"', 'id = "nova"'), "id must match"),
    (lambda t: t + 'unexpected = "x"\n', "unexpected"),
    (lambda t: t + 'system_prompt = "inline"\n', "system_prompt comes from"),
    (lambda t: t + "runtime_type = \"docker\"\n", "runtime_type"),
    (lambda t: t.replace('name = "Atlas"', "name = "), "invalid TOML"),
    (lambda t: t.replace('id = "atlas"\n', ""), "id must match"),
])
def test_bad_identity_files_report_clearly(config_dir, edit, message):
    path = config_dir / "atlas.toml"
    path.write_text(edit(path.read_text()))
    with pytest.raises(IdentityConfigError, match=message):
        load_registry(config_dir)


def test_bad_file_name_is_rejected(config_dir):
    (config_dir / "Bad Name.toml").write_text((config_dir / "atlas.toml").read_text())
    with pytest.raises(IdentityConfigError, match="file name"):
        load_registry(config_dir)


def test_empty_directory_is_rejected(tmp_path):
    with pytest.raises(IdentityConfigError, match="no agent identities"):
        load_registry(tmp_path)


# 9: prompts
@pytest.mark.parametrize("agent_id", sorted(EXPECTED_IDS))
def test_prompt_files_exist_and_cover_required_sections(agent_id):
    text = (IDENTITY_DIR / "prompts" / f"{agent_id}.md").read_text(encoding="utf-8")
    assert text.strip() == load_registry().get(agent_id).system_prompt
    for heading in ("Identity and role", "Personality and communication style",
                    "Areas of expertise", "Reasoning and evidence expectations",
                    "Security and privacy boundaries", "Limitations", "Collaboration"):
        assert f"## {heading}" in text


@pytest.mark.parametrize("breaker,message", [("missing", "is missing"), ("empty", "is empty")])
def test_missing_or_empty_prompt_is_rejected(config_dir, breaker, message):
    prompt = config_dir / "prompts" / "pixel.md"
    prompt.unlink() if breaker == "missing" else prompt.write_text("  \n")
    with pytest.raises(IdentityConfigError, match=rf"pixel\.toml.*{message}"):
        load_registry(config_dir)


def test_prompts_do_not_overclaim():
    registry = load_registry()
    assert "cannot execute commands or modify anything" in registry.get("orbit").system_prompt
    assert "cannot currently see, generate or edit images" in registry.get("pixel").system_prompt
    assert "do not execute anything yourself" in registry.get("charlie").system_prompt.lower()
    assert "do not scan, probe, exploit or access any system" in registry.get("cipher").system_prompt.lower()
    for identity in registry.list():
        assert "no tools beyond the text you are given" in identity.system_prompt


# 10: model preferences are not fabricated aliases
def test_no_litellm_alias_is_configured_for_any_identity():
    assert all(identity.model_alias is None for identity in load_registry().list())


def test_alias_field_accepts_a_configured_alias_without_changing_the_family():
    identity = AgentIdentity(**identity_kwargs(model_alias="team/example-alias"))
    assert identity.model_alias == "team/example-alias"
    assert identity.preferred_model_family == "Family"


def test_identity_files_reuse_none_of_the_operational_model_environment_names():
    text = "".join(p.read_text() for p in IDENTITY_DIR.glob("*.toml"))
    assert "openai/" not in text
    assert not re.search(r"(RESEARCH|SECURITY|FACT_CHECK|SOURCE_QUALITY|PRIVACY|SYNTHESIS)_MODEL", text)


# 11, 12: side-effect free
def test_loading_makes_no_network_calls(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    assert len(load_registry()) == 8


def test_loading_does_not_import_inference_or_cloud_clients():
    code = (
        "import sys\n"
        "from viharbot.identity_registry import load_registry\n"
        "load_registry()\n"
        "bad = [m for m in ('strands', 'litellm', 'boto3', 'openai') if m in sys.modules]\n"
        "assert not bad, bad\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_operational_modules_do_not_import_identities():
    src = Path(identity_registry.__file__).parent
    # web_app is excluded since Phase 3: it imports identity_execution behind a feature flag.
    for name in ("research_agent", "security_agent", "specialist_agent", "model_agent",
                 "demo", "pipeline_state", "contracts"):
        text = (src / f"{name}.py").read_text()
        assert "from .identity" not in text and "viharbot.identity" not in text, name


# 13: no credentials
CREDENTIAL_PATTERNS = [
    r"sk-[A-Za-z0-9]{10,}", r"AKIA[0-9A-Z]{16}", r"Bearer\s+[A-Za-z0-9._-]{8,}",
    r"(api[_-]?key|secret|token|password)\s*[:=]\s*\S+", r"-----BEGIN", r"https?://\S*@",
    r"https?://",
]


def test_identity_files_and_prompts_contain_no_credentials_or_endpoints():
    files = list(IDENTITY_DIR.glob("*.toml")) + list((IDENTITY_DIR / "prompts").glob("*.md"))
    assert len(files) == 16
    for path in files:
        text = path.read_text(encoding="utf-8")
        for pattern in CREDENTIAL_PATTERNS:
            assert not re.search(pattern, text, re.IGNORECASE), (path.name, pattern)


# 14: runtime is separate from identity
def test_runtime_config_is_a_separate_model():
    assert not set(AgentRuntimeConfig.model_fields) & {"name", "title", "personality", "system_prompt"}
    assert "endpoint" not in AgentIdentity.model_fields
    assert "runtime_type" not in AgentIdentity.model_fields
    runtime = AgentRuntimeConfig(agent_id="cipher", runtime_type="docker",
                                 endpoint="http://security:8000/assess")
    assert runtime.agent_id in load_registry()


@pytest.mark.parametrize("endpoint", [
    "http://user:pass@host/x", "https://host/x?api_key=abc", "ftp://host", "not a url", "https://",
])
def test_runtime_endpoint_rejects_credentials_and_bad_schemes(endpoint):
    with pytest.raises(ValidationError):
        AgentRuntimeConfig(agent_id="cipher", endpoint=endpoint)


def test_runtime_config_accepts_agentcore_and_rejects_unknown_fields():
    AgentRuntimeConfig(agent_id="orbit", runtime_type="agentcore",
                       endpoint="agentcore://arn:aws:bedrock-agentcore:us-east-1:1:runtime/x")
    with pytest.raises(ValidationError):
        AgentRuntimeConfig(agent_id="orbit", api_key="x")


# capabilities: declared vs implemented
def test_capability_vocabulary_and_status():
    assert {c.value for c in Capability} == {
        "research", "fact_check", "source_quality", "security_assessment", "privacy_review",
        "synthesis", "creative_design", "philosophical_analysis", "logistics_planning",
        "automation_engineering", "evidence_discovery", "slide_generation",
    }
    implemented = {c for c in Capability if capability_status(c) is CapabilityStatus.IMPLEMENTED}
    assert {c.value for c in implemented} == {
        "research", "fact_check", "source_quality", "security_assessment", "privacy_review",
        "synthesis", "slide_generation",
    }
    assert capability_status(Capability.AUTOMATION_ENGINEERING) is CapabilityStatus.PLANNED


# 15, 17: existing behavior and schemas unchanged
def test_chat_response_contract_is_unchanged():
    from viharbot import web_app

    # Phase 6.2 added one optional field; the original eight fields and their order are unchanged.
    assert list(web_app.ChatResponse.model_fields) == [
        "request_id", "findings", "assessment", "fact_check", "source_quality",
        "privacy_review", "synthesis", "agent_activity",
        "orchestration",
    ]
    assert list(web_app.ChatRequest.model_fields) == ["message", "history"]
