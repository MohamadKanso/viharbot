"""Epic #157 Phase A: persona specification schema, loader and shipped definitions.

Offline only: reads repository files, makes no model, network or LiteLLM calls.
"""

from __future__ import annotations

import itertools
import re
import shutil
import tomllib
from pathlib import Path

import pytest

from viharbot.identity_registry import default_registry
from viharbot.personas import (
    PERSONA_SCHEMA_VERSION,
    REQUIRED_PERSONA_IDS,
    PersonaConfigError,
    PersonaStatus,
    default_persona_registry,
    load_persona_registry,
)
from viharbot.personas.registry import PERSONAS_DIR

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "viharbot"

# Epic #157 table: id -> (name, organizational role, core identity words).
EPIC_TABLE = {
    "charlie": ("Charlie Chatwin", "Chief Strategist & AI Orchestrator",
                {"strategic", "diplomatic", "decisive"}),
    "nova": ("Nova", "Director of Research & Knowledge", {"curious", "analytical", "methodical"}),
    "atlas": ("Atlas", "Director of Exploration & Logistics",
              {"adventurous", "pragmatic", "resourceful"}),
    "pixel": ("Pixel", "Director of Creative Intelligence",
              {"imaginative", "expressive", "design-oriented"}),
    "sage": ("Sage", "Director of Philosophy & Human Perspective",
             {"reflective", "empathetic", "ethical"}),
    "cipher": ("Cipher", "Chief Security & Risk Officer", {"skeptical", "vigilant", "disciplined"}),
    "scout": ("Scout", "Director of Intelligence & Discovery",
              {"investigative", "independent", "evidence-driven"}),
    "orbit": ("Orbit", "Chief Automation & Engineering Officer",
              {"systematic", "inventive", "execution-focused"}),
}
# Phrases that would turn descriptive text into a claim of authority.
AUTHORITY_PHRASES = (
    "authorized to", "authorised to", "has permission", "is permitted to", "full access",
    "bypass", "override the", "elevate privilege", "grants access", "root access",
)


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """A writable copy of the packaged personas for building invalid configurations."""
    target = tmp_path / "definitions"
    shutil.copytree(PERSONAS_DIR, target)
    return target


def edit(directory: Path, agent: str, old: str, new: str) -> None:
    path = directory / f"{agent}.toml"
    text = path.read_text(encoding="utf-8")
    assert old in text, f"test setup: {old!r} not found in {agent}.toml"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def rejects(directory: Path, match: str) -> None:
    with pytest.raises(PersonaConfigError, match=match):
        load_persona_registry(directory)


# --- Shipped definitions --------------------------------------------------------------------


def test_exactly_the_eight_established_personas_ship():
    registry = default_persona_registry()
    assert {p.agent_id for p in registry.list()} == set(EPIC_TABLE) == REQUIRED_PERSONA_IDS
    assert len(registry) == 8


def test_established_identities_are_unchanged_and_match_the_epic_table():
    agents = default_registry()
    for agent_id, (name, title, _) in EPIC_TABLE.items():
        assert agents.get(agent_id).name == name
        assert agents.get(agent_id).title == title
    # The education identities remain, untouched and without a Phase A persona.
    assert {"archie", "ada"} <= {i.id for i in agents.list()}
    assert "archie" not in default_persona_registry()
    assert "ada" not in default_persona_registry()


def test_epic_core_identity_words_are_present_without_replacing_identity_traits():
    registry = default_persona_registry()
    for agent_id, (_, _, words) in EPIC_TABLE.items():
        traits = registry.get(agent_id).traits
        have = {t.lower() for t in (*traits.identity_traits, *traits.design_direction_traits)}
        identity_text = default_registry().get(agent_id).personality.lower()
        # Each epic word is either an approved identity trait/description or declared direction.
        missing = {w for w in words if w not in have and w not in identity_text}
        assert missing == set(), f"{agent_id}: {missing}"


def test_identity_traits_come_from_the_approved_identity():
    for persona in default_persona_registry().list():
        personality = default_registry().get(persona.agent_id).personality.lower()
        assert all(t.lower() in personality for t in persona.traits.identity_traits)


def test_every_persona_is_versioned_draft_and_schema_current():
    for persona in default_persona_registry().list():
        assert persona.provenance.schema_version == PERSONA_SCHEMA_VERSION
        assert re.fullmatch(r"\d+\.\d+\.\d+", persona.provenance.persona_version)
        # Founder approval of personas is a separate decision; none may claim it yet.
        assert persona.provenance.status is PersonaStatus.DRAFT
        assert persona.provenance.approval_ref is None
        assert persona.disagreement.requires_evidence is True


def test_model_preference_matches_identity_and_carries_no_alias():
    for persona in default_persona_registry().list():
        identity = default_registry().get(persona.agent_id)
        assert persona.model_preference.family == identity.preferred_model_family
        assert "model_alias" not in type(persona.model_preference).model_fields


def test_avatars_exist_and_are_served_by_the_web_app():
    images = ROOT / "images"
    if not images.exists():
        pytest.skip("images/ is not in this environment")
    from viharbot.web_app import TEAM_IMAGES

    for persona in default_persona_registry().list():
        avatar = persona.visual_identity.avatar_file
        assert (images / avatar).is_file(), avatar
        assert avatar in TEAM_IMAGES, avatar


def test_collaboration_names_known_peers_and_includes_the_epic_examples():
    registry = default_persona_registry()
    pairs = {(p.agent_id, c.agent_id, c.pattern.value) for p in registry.list() for c in p.collaboration}
    assert ("nova", "scout", "requests_review_from") in pairs  # Nova researches, Scout validates
    assert ("scout", "nova", "reviews") in pairs
    assert ("cipher", "orbit", "challenges") in pairs  # Cipher challenges Orbit's architecture
    assert ("atlas", "orbit", "reviews") in pairs  # Atlas assesses feasibility
    assert ("sage", "orbit", "reviews") in pairs  # Sage assesses human implications
    assert ("scout", "pixel", "hands_off_to") in pairs  # validated findings reach Pixel
    assert any(a == "charlie" and p == "consults" for a, _, p in pairs)  # Charlie synthesizes
    for persona in registry.list():
        assert all(c.agent_id in default_registry() for c in persona.collaboration)


def test_loading_is_deterministic():
    first = load_persona_registry()
    second = load_persona_registry()
    assert [p.agent_id for p in first.list()] == sorted(p.agent_id for p in first.list())
    assert [p.content_digest() for p in first.list()] == [p.content_digest() for p in second.list()]
    assert len({p.content_digest() for p in first.list()}) == 8


def test_digest_changes_when_content_changes(config_dir: Path):
    before = load_persona_registry(config_dir).get("nova").content_digest()
    edit(config_dir, "nova", "Curious and measured", "Curious and brisk")
    assert load_persona_registry(config_dir).get("nova").content_digest() != before


# --- Distinguishable, testable communication profiles ---------------------------------------


def test_communication_profiles_are_distinguishable():
    personas = default_persona_registry().list()
    assert len({p.communication.tone for p in personas}) == len(personas)
    examples = [e for p in personas for e in p.communication.illustrative_examples]
    assert len(set(examples)) == len(examples)
    for a, b in itertools.combinations(personas, 2):
        va = {w.lower() for w in a.communication.preferred_vocabulary}
        vb = {w.lower() for w in b.communication.preferred_vocabulary}
        overlap = len(va & vb) / len(va | vb)
        assert overlap <= 0.2, f"{a.agent_id} and {b.agent_id} vocabularies overlap {overlap:.2f}"


def test_epic_illustrative_voices_are_preserved():
    registry = default_persona_registry()
    expected = {
        "cipher": "authorization boundary that needs further validation",
        "orbit": "preserves the existing execution contract",
        "nova": "two assumptions remain unverified",
        "scout": "contradiction between the source material and the proposed finding",
        "sage": "impact on the people responsible for adopting it",
    }
    for agent_id, fragment in expected.items():
        assert any(fragment in e for e in registry.get(agent_id).communication.illustrative_examples)


def test_persona_text_claims_no_authority():
    for path in sorted(PERSONAS_DIR.glob("*.toml")):
        text = path.read_text(encoding="utf-8").lower()
        for phrase in AUTHORITY_PHRASES:
            assert phrase not in text, f"{path.name}: {phrase!r}"


def test_persona_files_contain_no_runtime_or_authorization_keys():
    forbidden = {
        "tools", "permissions", "scopes", "capabilities", "endpoint", "runtime", "binding",
        "model_alias", "credentials", "api_key", "approved_by", "trust_score", "seniority",
    }

    def keys(node: object) -> set[str]:
        if isinstance(node, dict):
            return set(node) | set().union(*(keys(v) for v in node.values()), set())
        if isinstance(node, list):
            return set().union(*(keys(v) for v in node), set())
        return set()

    for path in sorted(PERSONAS_DIR.glob("*.toml")):
        found = keys(tomllib.loads(path.read_text(encoding="utf-8")))
        assert found & forbidden == set(), f"{path.name}: {found & forbidden}"


# --- Persona cannot grant authority or carry unknown data -----------------------------------


@pytest.mark.parametrize("extra", [
    'tools = ["shell"]', 'permissions = ["deploy"]', 'model_alias = "x/y"',
    'capabilities = ["research"]', 'endpoint = "http://x"', 'trust_score = 0.9',
])
def test_top_level_authority_fields_are_rejected(config_dir: Path, extra: str):
    edit(config_dir, "orbit", 'agent_id = "orbit"', f'agent_id = "orbit"\n{extra}')
    rejects(config_dir, "orbit.toml.*Extra inputs are not permitted")


def test_nested_unknown_fields_are_rejected(config_dir: Path):
    edit(config_dir, "orbit", "[model_preference]", '[model_preference]\nmodel_alias = "x/y"')
    rejects(config_dir, "model_preference.model_alias")
    edit(config_dir, "cipher", "[provenance]", '[provenance]\nsigned_off = true')
    rejects(config_dir, "provenance.signed_off")


def test_disagreement_cannot_waive_the_evidence_requirement(config_dir: Path):
    edit(config_dir, "scout", "requires_evidence = true", "requires_evidence = false")
    rejects(config_dir, "scout.toml.*disagreement.requires_evidence")


@pytest.mark.parametrize("avatar", [
    "../../etc/passwd.png", "sub/dir.png", "http://example.com/a.png", "a.exe", ".hidden.png",
])
def test_avatar_must_be_a_bare_image_file_name(config_dir: Path, avatar: str):
    edit(config_dir, "pixel", 'avatar_file = "Pixel_headshot.png"', f'avatar_file = "{avatar}"')
    rejects(config_dir, "visual_identity.avatar_file")


def test_approval_rules(config_dir: Path):
    edit(config_dir, "nova", 'status = "draft"', 'status = "approved"')
    rejects(config_dir, "needs an approval_ref")
    edit(config_dir, "nova", 'status = "approved"', 'status = "draft"\napproval_ref = "PR-1"')
    rejects(config_dir, "only an approved persona may carry")
    edit(config_dir, "nova", 'status = "draft"\napproval_ref = "PR-1"', 'status = "review"')
    assert load_persona_registry(config_dir).get("nova").provenance.status is PersonaStatus.REVIEW


@pytest.mark.parametrize("status", ["implemented", "activated", "enabled"])
def test_persona_cannot_claim_implementation_or_activation(config_dir: Path, status: str):
    edit(config_dir, "nova", 'status = "draft"', f'status = "{status}"')
    rejects(config_dir, "provenance.status")


def test_approved_persona_with_reference_is_accepted(config_dir: Path):
    edit(config_dir, "nova", 'status = "draft"', 'status = "approved"\napproval_ref = "PR-1"')
    assert load_persona_registry(config_dir).get("nova").provenance.status is PersonaStatus.APPROVED


# --- Cross-checks against the approved identity ---------------------------------------------


def test_identity_trait_must_exist_in_the_approved_identity(config_dir: Path):
    edit(config_dir, "cipher", '"precise", "skeptical"', '"precise", "cheerful"')
    rejects(config_dir, "cipher: identity trait is not in the approved identity")


def test_model_family_must_match_identity(config_dir: Path):
    edit(config_dir, "atlas", 'family = "Claude Sonnet family"', 'family = "Claude Opus family"')
    rejects(config_dir, "atlas: model family does not match")


def test_collaboration_must_name_a_known_distinct_peer(config_dir: Path):
    edit(config_dir, "nova", 'agent_id = "scout"\npattern = "requests_review_from"',
         'agent_id = "ghost"\npattern = "requests_review_from"')
    rejects(config_dir, "nova: collaboration names an unknown agent")


def test_collaboration_must_not_name_self(config_dir: Path):
    edit(config_dir, "nova", 'agent_id = "scout"\npattern = "requests_review_from"',
         'agent_id = "nova"\npattern = "requests_review_from"')
    rejects(config_dir, "must not name the agent itself")


def test_persona_for_unknown_identity_is_rejected(config_dir: Path):
    text = (config_dir / "nova.toml").read_text(encoding="utf-8").replace('"nova"', '"ghost"', 1)
    (config_dir / "ghost.toml").write_text(text, encoding="utf-8")
    rejects(config_dir, "ghost: no such agent identity")


def test_all_eight_personas_are_required(config_dir: Path):
    (config_dir / "scout.toml").unlink()
    rejects(config_dir, "missing personas for: scout")


# --- Loader hygiene -------------------------------------------------------------------------


def test_file_name_and_agent_id_must_match(config_dir: Path):
    edit(config_dir, "nova", 'agent_id = "nova"', 'agent_id = "atlas"')
    rejects(config_dir, "nova.toml: agent_id must match the file name")


def test_invalid_toml_and_empty_directory(config_dir: Path, tmp_path: Path):
    (config_dir / "nova.toml").write_text("not = [valid", encoding="utf-8")
    rejects(config_dir, "nova.toml: invalid TOML")
    empty = tmp_path / "empty"
    empty.mkdir()
    rejects(empty, "no personas found")


def test_invalid_file_name_is_rejected(config_dir: Path):
    shutil.copy(config_dir / "nova.toml", config_dir / "Bad-Name.toml")
    rejects(config_dir, "file name is not a valid agent id")


def test_errors_do_not_echo_rejected_values(config_dir: Path):
    secret = "sk-test-SECRET-VALUE-123"
    edit(config_dir, "orbit", 'agent_id = "orbit"', f'agent_id = "orbit"\napi_key = "{secret}"')
    with pytest.raises(PersonaConfigError) as raised:
        load_persona_registry(config_dir)
    assert secret not in str(raised.value)


def test_duplicate_and_repeated_entries_are_rejected(config_dir: Path):
    edit(config_dir, "nova", 'values = ["Evidence before conclusion", ',
         'values = ["Evidence before conclusion", "evidence before conclusion", ')
    rejects(config_dir, "must not repeat")


# --- Phase A does not change runtime behavior -----------------------------------------------


def test_no_runtime_module_imports_personas():
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        if "personas" in path.relative_to(SRC).parts:
            continue
        if re.search(r"^\s*(from|import)\s+[.\w]*personas\b", path.read_text(encoding="utf-8"), re.M):
            offenders.append(str(path.relative_to(SRC)))
    assert offenders == []


def test_persona_definitions_ship_inside_the_package():
    # Relative to the imported package, not the repo checkout, so it also holds when the tests
    # run against an installed wheel (the Docker test image).
    import viharbot

    assert PERSONAS_DIR.is_relative_to(Path(viharbot.__file__).parent)
    assert len(list(PERSONAS_DIR.glob("*.toml"))) == 8
