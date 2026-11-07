"""The Docker test profile stays hermetic and keeps test tooling out of production images (#19).

Static checks on the repository's own files; they start no container and make no network call.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRODUCTION = ["Dockerfile.web", "Dockerfile.specialist", "Dockerfile.research", "Dockerfile.security",
              "Dockerfile.pixel", "Dockerfile.sage", "Dockerfile.orchestrator"]


def service_block() -> str:
    """The text of the `tests` service block in compose.yaml (two-space indented, up to the next one)."""
    compose = (ROOT / "compose.yaml").read_text()
    match = re.search(r"^  tests:\n(.*?)(?=^\S|^  \S)", compose, re.S | re.M)
    assert match, "compose.yaml has no `tests` service"
    return match.group(1)


def test_the_tests_service_takes_nothing_from_the_host_environment():
    block = service_block()
    assert "${" not in block, "a ${VAR} would read the shell or a developer's .env"
    for forbidden in ("env_file", "secrets:", "args:", "volumes:"):
        assert forbidden not in block
    assert "LITELLM_API_KEY" not in block and "TOKEN" not in block


def test_the_tests_service_keeps_live_checks_off_and_has_no_runtime_network():
    block = service_block()
    assert re.search(r'RUN_LITELLM_INTEGRATION: "0"', block)
    assert re.search(r'RUN_LITELLM_INFERENCE: "0"', block)
    assert re.search(r"^    network_mode: none$", block, re.M)


def test_the_test_image_context_is_an_allowlist_that_never_admits_a_real_env_file():
    rules = [line.strip() for line in (ROOT / "Dockerfile.tests.dockerignore").read_text().splitlines()
             if line.strip() and not line.lstrip().startswith("#")]
    assert rules[0] == "*", "the context must start from exclude-everything"
    # only the placeholder template may be re-included, and it must be the last rule so nothing hides it
    env_rules = [r for r in rules if ".env" in r]
    assert env_rules[-1] == "!.env.example" and rules[-1] == "!.env.example"
    assert all(r.startswith("**/.env") for r in env_rules[:-1])
    for secret_dir in ("!.aws", "!data", "!.git", "!.venv"):
        assert secret_dir not in rules


def test_the_test_image_has_node_24_and_no_credentials_in_its_build():
    text = (ROOT / "Dockerfile.tests").read_text()
    assert re.search(r"^FROM node:24[\w.-]* AS node$", text, re.M)
    assert "COPY --from=node /usr/local/bin/node" in text
    assert not re.search(r"^\s*(ARG|ENV)\s+\S*(KEY|TOKEN|SECRET|PASSWORD)", text, re.M | re.I)
    copies = [ln for ln in text.splitlines() if re.match(r"\s*(COPY|ADD)\b", ln)]
    assert not [ln for ln in copies if re.search(r"\.env|\.aws|credentials", ln)], copies


def test_production_images_carry_no_test_tooling_or_node():
    for name in PRODUCTION:
        text = (ROOT / name).read_text()
        assert not re.search(r"\.\[test\]|pytest|node:|nodejs|--from=node", text, re.I), name
