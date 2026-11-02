from __future__ import annotations

import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from viharbot.execution_bindings import default_bindings
from viharbot.identity import CAPABILITY_STATUS, Capability, CapabilityStatus
from viharbot.identity_registry import load_registry
from viharbot.skills import (
    SKILLS_DIR,
    SkillConfigError,
    SkillDefinition,
    SkillStatus,
    default_skill_registry,
    load_skill_registry,
)

AGENTS = load_registry()
EXPECTED_IDS = {"charlie", "nova", "atlas", "pixel", "sage", "cipher", "scout", "orbit"}
EXPECTED_COUNTS = {"charlie": 8, "nova": 8, "scout": 8, "cipher": 12, "atlas": 10, "pixel": 8,
                   "sage": 10, "orbit": 14}
SRC = Path(__file__).resolve().parent.parent / "src" / "viharbot"
REGISTRY_DOC = (
    Path(__file__).resolve().parent.parent / "docs" / "governance" / "agent-skills-registry-v1.md"
)


def skill(**overrides) -> dict:
    base = dict(skill_id="consulting.discovery", version="0.1.0", name="Discovery",
                description="Discover the problem.", owner="charlie", category="consulting",
                status="proposed")
    return {**base, **overrides}


def write(directory: Path, agent: str, *skills: dict) -> None:
    def value(v):
        if isinstance(v, list):
            return "[" + ", ".join(f'"{i}"' for i in v) + "]"
        return f'"{v}"'

    blocks = ["\n".join(["[[skill]]", *(f"{k} = {value(v)}" for k, v in s.items())]) for s in skills]
    (directory / f"{agent}.toml").write_text("\n\n".join(blocks) + "\n", encoding="utf-8")


def load(tmp_path: Path, *skills: dict, agent: str = "charlie"):
    write(tmp_path, agent, *skills)
    return load_skill_registry(tmp_path, AGENTS)


# 1. exactly eight established identities remain registered
def test_exactly_the_eight_established_identities_remain_registered():
    assert {a.id for a in AGENTS.list()} == EXPECTED_IDS and len(AGENTS) == 8


# 2. all skill definitions load successfully, and all 78 are represented
def test_packaged_skills_load_and_cover_the_registry_document():
    registry = default_skill_registry()
    assert len(registry) == 78
    assert {a: len(registry.owned_by(a)) for a in EXPECTED_IDS} == EXPECTED_COUNTS
    documented = set(re.findall(r"(?m)^\| `([a-z_]+\.[a-z_]+)` \|", REGISTRY_DOC.read_text()))
    assert {s.skill_id for s in registry.list()} == documented
    assert [s.skill_id for s in registry.list()] == sorted(documented)  # deterministic order


# 3. skill ids are unique
def test_packaged_skill_ids_are_unique():
    ids = [s.skill_id for s in default_skill_registry().list()]
    assert len(ids) == len(set(ids))


# 4. owners and 5. eligible agents reference valid identities
def test_packaged_owners_and_eligible_agents_are_valid_identities():
    for s in default_skill_registry().list():
        assert s.owner in AGENTS
        assert all(a in AGENTS for a in s.eligible_agents)


def test_unknown_owner_is_rejected(tmp_path):
    write(tmp_path, "ghost", skill(owner="ghost"))
    with pytest.raises(SkillConfigError, match="unknown owner"):
        load_skill_registry(tmp_path, AGENTS)


def test_unknown_eligible_agent_is_rejected(tmp_path):
    with pytest.raises(SkillConfigError, match="unknown eligible agent"):
        load(tmp_path, skill(eligible_agents=["ghost"]))


def test_owner_must_match_the_file(tmp_path):
    with pytest.raises(SkillConfigError, match="owner must match the file name"):
        load(tmp_path, skill(owner="nova"))


# 6. invalid lifecycle states are rejected
def test_unknown_lifecycle_status_is_rejected(tmp_path):
    with pytest.raises(SkillConfigError, match="status"):
        load(tmp_path, skill(status="active"))


def test_defined_skills_need_a_contract(tmp_path):
    with pytest.raises(SkillConfigError, match="requires inputs, outputs and evaluation"):
        load(tmp_path, skill(status="defined"))
    contract = dict(inputs=["Brief"], outputs=["Report"], evaluation=["Complete"])
    assert load(tmp_path, skill(status="defined", **contract)).get("consulting.discovery")


def test_implemented_skills_need_an_existing_capability(tmp_path):
    contract = dict(inputs=["Brief"], outputs=["Report"], evaluation=["Complete"])
    with pytest.raises(SkillConfigError, match="requires an existing capability"):
        load(tmp_path, skill(status="implemented", **contract))


def test_planned_capability_cannot_back_an_executable_skill(tmp_path):
    contract = dict(inputs=["Brief"], outputs=["Report"], evaluation=["Complete"])
    planned = skill(skill_id="strategy.logistics", category="strategy", owner="atlas",
                    status="enabled", capability="logistics_planning", **contract)
    with pytest.raises(SkillConfigError, match="planned and cannot execute"):
        load(tmp_path, planned, agent="atlas")


def test_category_must_match_the_skill_domain(tmp_path):
    with pytest.raises(SkillConfigError, match="category must match"):
        load(tmp_path, skill(category="research"))


def test_capability_must_be_declared_by_the_owner(tmp_path):
    with pytest.raises(SkillConfigError, match="owner does not declare"):
        load(tmp_path, skill(capability="fact_check"))


# 7. invalid dependencies are rejected
def test_unknown_skill_dependency_is_rejected(tmp_path):
    with pytest.raises(SkillConfigError, match="unknown skill dependency"):
        load(tmp_path, skill(dependencies=["skill:consulting.missing"]))


def test_unknown_capability_dependency_is_rejected(tmp_path):
    with pytest.raises(SkillConfigError, match="unknown capability dependency"):
        load(tmp_path, skill(dependencies=["capability:telepathy"]))


def test_malformed_and_self_dependencies_are_rejected(tmp_path):
    with pytest.raises(SkillConfigError, match="dependencies"):
        load(tmp_path, skill(dependencies=["consulting.discovery"]))
    with pytest.raises(SkillConfigError, match="depend on itself"):
        load(tmp_path, skill(dependencies=["skill:consulting.discovery"]))


def test_circular_dependencies_are_rejected(tmp_path):
    a = skill(skill_id="consulting.a", dependencies=["skill:consulting.b"])
    b = skill(skill_id="consulting.b", dependencies=["skill:consulting.a"])
    with pytest.raises(SkillConfigError, match="circular dependency"):
        load(tmp_path, a, b)


def test_valid_dependencies_load(tmp_path):
    a = skill(skill_id="consulting.a", dependencies=["skill:consulting.b", "capability:research"])
    registry = load(tmp_path, a, skill(skill_id="consulting.b"))
    assert registry.get("consulting.a").dependencies == ("skill:consulting.b", "capability:research")


def test_advanced_skill_cannot_depend_on_a_merely_proposed_one(tmp_path):
    contract = dict(inputs=["Brief"], outputs=["Report"], evaluation=["Complete"])
    a = skill(skill_id="consulting.a", status="defined", dependencies=["skill:consulting.b"],
              **contract)
    with pytest.raises(SkillConfigError, match="only proposed"):
        load(tmp_path, a, skill(skill_id="consulting.b"))


# 8. duplicate definitions are rejected
def test_duplicate_definitions_are_rejected(tmp_path):
    with pytest.raises(SkillConfigError, match="duplicate skill id"):
        load(tmp_path, skill(), skill())
    write(tmp_path, "nova", skill(owner="nova", category="consulting"))
    with pytest.raises(SkillConfigError, match="duplicate skill id"):
        load_skill_registry(tmp_path, AGENTS)


# 9. unknown schema fields are rejected
def test_unknown_fields_are_rejected(tmp_path):
    with pytest.raises(ValidationError):
        SkillDefinition.model_validate(skill(can_execute=True))
    with pytest.raises(SkillConfigError, match="can_execute"):
        load(tmp_path, skill(can_execute="yes"))
    (tmp_path / "charlie.toml").write_text("extra = 1\n", encoding="utf-8")
    with pytest.raises(SkillConfigError, match="unexpected top-level keys"):
        load_skill_registry(tmp_path, AGENTS)


def test_invalid_toml_and_empty_directories_are_clear_errors(tmp_path):
    (tmp_path / "charlie.toml").write_text("[[skill\n", encoding="utf-8")
    with pytest.raises(SkillConfigError, match="invalid TOML"):
        load_skill_registry(tmp_path, AGENTS)
    (tmp_path / "charlie.toml").unlink()
    with pytest.raises(SkillConfigError, match="no skill definitions"):
        load_skill_registry(tmp_path, AGENTS)
    (tmp_path / "Bad Name.toml").write_text("", encoding="utf-8")
    with pytest.raises(SkillConfigError, match="not a valid agent id"):
        load_skill_registry(tmp_path, AGENTS)
    (tmp_path / "Bad Name.toml").unlink()
    (tmp_path / "charlie.toml").write_text("skill = []\n", encoding="utf-8")
    with pytest.raises(SkillConfigError, match="no skills defined"):
        load_skill_registry(tmp_path, AGENTS)


def test_errors_do_not_echo_rejected_values(tmp_path):
    with pytest.raises(SkillConfigError) as error:
        load(tmp_path, skill(version="secret-value"))
    assert "secret-value" not in str(error.value)


def test_registry_lookups(tmp_path):
    registry = default_skill_registry()
    assert "consulting.discovery" in registry and "nope.nope" not in registry
    with pytest.raises(KeyError):
        registry.get("nope.nope")


def test_ordering_is_independent_of_file_and_entry_order(tmp_path):
    shutil.copytree(SKILLS_DIR, tmp_path / "copy")
    reordered = load_skill_registry(tmp_path / "copy", AGENTS)
    assert [s.skill_id for s in reordered.list()] == [
        s.skill_id for s in default_skill_registry().list()
    ]
    registry = load(tmp_path, skill(skill_id="consulting.z"), skill(skill_id="consulting.a"))
    assert [s.skill_id for s in registry.list()] == ["consulting.a", "consulting.z"]


# 10. proposed skills cannot execute
def test_every_packaged_skill_is_proposed_and_none_can_execute():
    registry = default_skill_registry()
    assert {s.status for s in registry.list()} == {SkillStatus.PROPOSED}
    assert not any(s.is_executable for s in registry.list())
    assert registry.executable() == ()
    for s in registry.list():
        assert not s.inputs and not s.required_tools and not s.required_scopes


def test_only_an_enabled_skill_reports_as_executable(tmp_path):
    contract = dict(inputs=["Brief"], outputs=["Report"], evaluation=["Complete"])
    enabled = skill(skill_id="consulting.synthesis", status="enabled", capability="synthesis",
                    **contract)
    registry = load(tmp_path, enabled, skill(skill_id="consulting.other"))
    assert [s.skill_id for s in registry.executable()] == ["consulting.synthesis"]
    assert not registry.get("consulting.other").is_executable


def test_skill_capability_links_only_name_existing_declared_capabilities():
    linked = {s.skill_id: s.capability for s in default_skill_registry().list() if s.capability}
    assert linked == {
        "consulting.synthesis": Capability.SYNTHESIS,
        "research.technical": Capability.RESEARCH,
        "verification.fact_check": Capability.FACT_CHECK,
        "verification.source_quality": Capability.SOURCE_QUALITY,
        "security.risk_assessment": Capability.SECURITY_ASSESSMENT,
        "security.privacy": Capability.PRIVACY_REVIEW,
        "strategy.logistics": Capability.LOGISTICS_PLANNING,
    }


# 11. existing capability bindings remain unchanged
def test_existing_bindings_and_capability_status_are_unchanged():
    assert {(b.identity_id, b.capability.value, b.service_name) for b in default_bindings().bindings} == {
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
    implemented = {c for c, s in CAPABILITY_STATUS.items() if s is CapabilityStatus.IMPLEMENTED}
    assert {c.value for c in implemented} == {
        "research", "fact_check", "source_quality", "security_assessment", "privacy_review",
        "synthesis", "slide_generation", "philosophical_analysis", "code_review",
    }
    assert len(Capability) == 13


# 12. existing workflows remain functional: the runtime does not depend on skills
def test_application_runtime_does_not_import_the_skills_package():
    offenders = [
        p.name for p in SRC.glob("*.py") if re.search(r"\bskills\b", p.read_text(encoding="utf-8"))
        and re.search(r"(from|import)\s+\.?(viharbot\.)?skills", p.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_web_app_imports_with_skills_loading_untouched():
    code = (
        "import sys\n"
        "import viharbot.web_app\n"
        "assert 'viharbot.skills' not in sys.modules\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


# 13. registry loading performs no network or model calls
def test_loading_makes_no_network_calls(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    assert len(load_skill_registry(agents=AGENTS)) == 78


def test_skills_package_does_not_import_inference_clients():
    code = (
        "import sys\n"
        "import viharbot.skills\n"
        "viharbot.skills.load_skill_registry()\n"
        "bad = [m for m in ('strands', 'litellm', 'boto3', 'openai', 'httpx') if m in sys.modules]\n"
        "assert not bad, bad\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
