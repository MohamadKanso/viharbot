from __future__ import annotations

import re
import socket
import subprocess
import sys

import pytest

from viharbot.execution_bindings import (
    BINDINGS_FILE,
    OPERATIONAL_SERVICES,
    ExecutionBinding,
    ExecutionBindingSet,
    ExecutionConfigError,
    default_bindings,
    load_bindings,
)
from viharbot.identity import Capability, CapabilityStatus, capability_status
from viharbot.identity_registry import load_registry

REGISTRY = load_registry()
EXPECTED = {
    ("nova", "research", "research"),
    ("scout", "fact_check", "fact-check"),
    ("scout", "source_quality", "source-quality"),
    ("cipher", "security_assessment", "security"),
    ("cipher", "privacy_review", "privacy-review"),
    ("charlie", "synthesis", "synthesis"),
    ("pixel", "slide_generation", "pixel"),
    ("sage", "philosophical_analysis", "sage"),
    ("sage", "code_review", "sage-code-review"),
}


def binding(identity, capability, service, enabled=True):
    return ExecutionBinding(identity_id=identity, capability=capability, service_name=service,
                            enabled=enabled)


def build(*bindings):
    return ExecutionBindingSet(bindings, REGISTRY)


# 1. valid bindings load
def test_packaged_bindings_load_and_match_the_approved_mapping():
    bindings = load_bindings()
    assert {(b.identity_id, b.capability.value, b.service_name) for b in bindings.bindings} == EXPECTED
    assert all(b.enabled for b in bindings.bindings)
    assert default_bindings() is default_bindings()


def test_every_implemented_capability_has_exactly_one_binding():
    bindings = default_bindings()
    implemented = {c for c in Capability if capability_status(c) is CapabilityStatus.IMPLEMENTED}
    assert set(OPERATIONAL_SERVICES) == implemented
    for capability in implemented:
        assert bindings.lookup(capability).service_name == OPERATIONAL_SERVICES[capability]


def test_only_approved_identities_have_bindings():
    bound = {b.identity_id for b in default_bindings().bindings}
    assert bound == {"nova", "scout", "cipher", "charlie", "pixel", "sage"}
    assert not bound & {"atlas", "orbit"}
    # Charlie also declares research but is deliberately not bound to it.
    assert default_bindings().lookup(Capability.RESEARCH).identity_id == "nova"


# 2-6. rejections
def test_unknown_identity_is_rejected():
    with pytest.raises(ExecutionConfigError, match="unknown identity"):
        build(binding("ghost", Capability.RESEARCH, "research"))


def test_unknown_capability_is_rejected(tmp_path):
    path = tmp_path / "bindings.toml"
    path.write_text('[[binding]]\nidentity_id = "nova"\ncapability = "SECRET-CAP"\n'
                    'service_name = "research"\n')
    with pytest.raises(ExecutionConfigError, match=r"binding\[0\].*capability") as caught:
        load_bindings(REGISTRY, path)
    assert "SECRET-CAP" not in str(caught.value)


def test_planned_capability_cannot_be_bound():
    with pytest.raises(ExecutionConfigError, match="planned and cannot execute"):
        build(binding("atlas", Capability.LOGISTICS_PLANNING, "research"))
    with pytest.raises(ExecutionConfigError, match="planned and cannot execute"):
        build(binding("orbit", Capability.AUTOMATION_ENGINEERING, "security"))


def test_identity_must_declare_the_capability():
    with pytest.raises(ExecutionConfigError, match="does not declare"):
        build(binding("nova", Capability.SYNTHESIS, "synthesis"))
    with pytest.raises(ExecutionConfigError, match="does not declare"):
        build(binding("atlas", Capability.RESEARCH, "research"))


def test_unknown_or_mismatched_service_is_rejected():
    with pytest.raises(ExecutionConfigError, match="unknown service"):
        build(binding("nova", Capability.RESEARCH, "orbit-runner"))
    with pytest.raises(ExecutionConfigError, match="does not fulfill"):
        build(binding("nova", Capability.RESEARCH, "security"))


def test_duplicate_and_ambiguous_bindings_are_rejected():
    first = binding("nova", Capability.RESEARCH, "research")
    with pytest.raises(ExecutionConfigError, match="duplicate binding"):
        build(first, first)
    with pytest.raises(ExecutionConfigError, match="ambiguous"):
        build(first, binding("charlie", Capability.RESEARCH, "research"))


def test_a_disabled_alternative_is_not_ambiguous_and_lookup_prefers_enabled():
    bindings = build(binding("charlie", Capability.RESEARCH, "research", enabled=False),
                     binding("nova", Capability.RESEARCH, "research"))
    assert bindings.lookup(Capability.RESEARCH).identity_id == "nova"
    only_disabled = build(binding("nova", Capability.RESEARCH, "research", enabled=False))
    assert only_disabled.lookup(Capability.RESEARCH).enabled is False
    assert build().lookup(Capability.RESEARCH) is None


def test_binding_model_is_strict_and_immutable():
    with pytest.raises(ValueError):
        ExecutionBinding(identity_id="nova", capability="research", service_name="research",
                         url="http://evil")
    with pytest.raises(ValueError):
        default_bindings().bindings[0].enabled = False


@pytest.mark.parametrize("content,message", [
    ("", "no bindings"),
    ("not toml = = =", "invalid TOML"),
    ('extra = 1\n[[binding]]\nidentity_id = "nova"\ncapability = "research"\n'
     'service_name = "research"\n', "unexpected top-level"),
    ('[[binding]]\nidentity_id = "nova"\ncapability = "research"\nservice_name = "research"\n'
     'endpoint = "http://x"\n', "endpoint"),
])
def test_bad_bindings_files_are_reported(tmp_path, content, message):
    path = tmp_path / "bindings.toml"
    path.write_text(content)
    with pytest.raises(ExecutionConfigError, match=message):
        load_bindings(REGISTRY, path)


def test_bindings_file_carries_no_urls_or_credentials():
    text = BINDINGS_FILE.read_text()
    assert "://" not in text
    assert not re.search(r"(api[_-]?key|secret|token|password|endpoint|url)\s*=", text, re.I)


# side effects
def test_loading_bindings_makes_no_network_calls(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    assert len(load_bindings()) == 9


def test_binding_and_planner_modules_do_not_import_inference_clients():
    code = (
        "import sys\n"
        "import viharbot.execution_bindings, viharbot.execution_planner, "
        "viharbot.identity_execution\n"
        "viharbot.execution_bindings.load_bindings()\n"
        "bad = [m for m in ('strands', 'litellm', 'boto3', 'openai') if m in sys.modules]\n"
        "assert not bad, bad\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
