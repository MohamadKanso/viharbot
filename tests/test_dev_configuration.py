from __future__ import annotations

import re
import subprocess

from shipped_config_helpers import DEPLOY_SPEC, ROOT

COMPOSE = (ROOT / "compose.yaml").read_text()
REMOVED_FLAGS = ("VIHARBOT_IDENTITY_ROUTING_ENABLED", "VIHARBOT_DELEGATION_ENABLED",
                 "VIHARBOT_ITERATIVE_ORCHESTRATION_ENABLED", "VIHARBOT_LIVE_TEAM_ENABLED")
AGENTS = ("research", "security", "fact-check", "source-quality", "privacy-review", "synthesis", "pixel")


def service_block(name: str) -> str:
    match = re.search(rf"^  {re.escape(name)}:\n(.*?)(?=^  \S|\Z)", COMPOSE, re.S | re.M)
    assert match, name
    return match.group(1)


def test_the_removed_feature_flags_are_not_shipped_anywhere():
    example = (ROOT / ".env.example").read_text()
    for flag in REMOVED_FLAGS:
        assert flag not in COMPOSE and flag not in example, flag
    spec = ROOT / DEPLOY_SPEC
    if spec.exists():
        text = spec.read_text()
        for name in (*REMOVED_FLAGS, "CHARLIE_PLANNER_MODEL", "WEB_LITELLM_API_KEY"):
            assert name not in text, name  # the cloud spec never carries the live team's inputs


def test_the_web_service_receives_the_planner_settings_with_safe_defaults():
    web = service_block("web")
    assert "CHARLIE_PLANNER_MODEL: ${CHARLIE_PLANNER_MODEL:-gpt-6-luna}" in web  # the live team stays off until the web container also gets a key
    assert "LITELLM_BASE_URL: ${LITELLM_BASE_URL:-https://litellm.viharchokshi.com}" in web
    for name in AGENTS:
        assert "CHARLIE_PLANNER_MODEL" not in service_block(name)


def test_the_web_container_gets_a_litellm_key_only_by_explicit_opt_in():
    web = service_block("web")
    assert "LITELLM_API_KEY: ${WEB_LITELLM_API_KEY:-}" in web
    assert "${LITELLM_API_KEY" not in web  # the agents' key is never forwarded to the browser tier
    for name in AGENTS:
        assert "LITELLM_API_KEY: ${LITELLM_API_KEY:-}" in service_block(name)


def test_no_secret_or_alias_is_committed_in_shipped_files():
    example = (ROOT / ".env.example").read_text()
    assert re.search(r"^LITELLM_API_KEY=$", example, re.M)
    assert re.search(r"^WEB_LITELLM_API_KEY=$", example, re.M)
    assert re.search(r"^CHARLIE_PLANNER_MODEL=gpt-6-luna$", example, re.M)
    assert not re.search(r"(sk-[A-Za-z0-9]{10,}|Bearer\s+\S{8,})", example + COMPOSE)
    spec = ROOT / DEPLOY_SPEC
    if spec.exists():
        for name in ("CHARLIE_PLANNER_MODEL", "WEB_LITELLM_API_KEY"):
            assert name not in spec.read_text()


def test_only_the_web_service_is_published_and_only_on_localhost():
    assert re.search(r'"127\.0\.0\.1:8080:8080"', service_block("web"))
    for name in AGENTS:
        assert "ports:" not in service_block(name), name


def test_every_default_service_keeps_its_healthcheck():
    for name in ("web", *AGENTS):
        assert "healthcheck:" in service_block(name), name


def test_the_web_image_trusts_the_gateway_ca_like_the_agent_images():
    web = (ROOT / "Dockerfile.web").read_text()
    agent = (ROOT / "Dockerfile.specialist").read_text()
    for line in ("COPY certs/cloudflare-gateway-ca.crt /usr/local/share/ca-certificates/cloudflare-gateway-ca.crt",
                 "RUN update-ca-certificates",
                 "ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt"):
        assert line in web and line in agent
    # the certificate is added while still root, before the process drops to the app user
    assert web.index("update-ca-certificates") < web.index("USER 1001:1001")


def test_the_env_file_is_ignored_by_git_and_never_tracked():
    tracked = subprocess.run(["git", "ls-files", ".env", ".env.local"], cwd=ROOT,
                             capture_output=True, text=True).stdout.split()
    assert tracked == []
    ignored = subprocess.run(["git", "check-ignore", ".env"], cwd=ROOT, capture_output=True, text=True)
    assert ignored.returncode == 0
