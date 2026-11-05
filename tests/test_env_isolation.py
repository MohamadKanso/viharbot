"""A developer's `.env` cannot change the offline test run (issue #21).

Uses only a synthetic `.env` written under tmp_path. No real key is read, none is needed, and no
model call is made.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from env_isolation import LIVE_OPT_INS, isolate_dotenv

SRC = Path(__file__).resolve().parent.parent / "src"

# Child process: point python-dotenv at a known file (what its walk-up would find on a developer
# machine), import the real LiteLLM, and report whether the file's value reached the environment.
CHILD = """
import os, dotenv.main
dotenv.main.find_dotenv = lambda *a, **k: {path!r}
import litellm
print(os.environ.get("ISOLATION_SENTINEL", "unset"))
"""


def litellm_import_sees_dotenv(tmp_path, mode):
    env_file = tmp_path / ".env"
    env_file.write_text("ISOLATION_SENTINEL=loaded-from-dotenv\n")
    env = {k: v for k, v in os.environ.items() if k not in ("LITELLM_MODE", "ISOLATION_SENTINEL")}
    if mode is not None:
        env["LITELLM_MODE"] = mode
    done = subprocess.run([sys.executable, "-c", CHILD.format(path=str(env_file))], env=env,
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-500:]
    return done.stdout.strip().splitlines()[-1] == "loaded-from-dotenv"


def test_litellm_loads_dotenv_by_default_which_is_why_the_tests_must_opt_out(tmp_path):
    """Control: without the setting, importing LiteLLM does read the file. If LiteLLM ever stops
    doing that, this fails and tells us the isolation (and this test) can be retired."""
    assert litellm_import_sees_dotenv(tmp_path, None) is True


def test_the_isolation_setting_stops_litellm_loading_dotenv(tmp_path):
    assert litellm_import_sees_dotenv(tmp_path, "PRODUCTION") is False


def test_the_policy_isolates_by_default_and_respects_explicit_choices():
    env = {}
    assert isolate_dotenv(env) is True and env["LITELLM_MODE"] == "PRODUCTION"
    # an explicit choice already in the environment wins, in both directions
    env = {"LITELLM_MODE": "DEV"}
    assert isolate_dotenv(env) is False and env["LITELLM_MODE"] == "DEV"
    env = {"LITELLM_MODE": "PRODUCTION"}
    assert isolate_dotenv(env) is True and env["LITELLM_MODE"] == "PRODUCTION"


@pytest.mark.parametrize("name", LIVE_OPT_INS)
def test_a_live_opt_in_leaves_dotenv_loading_alone(name):
    """The billable live checks may need a key kept in `.env`, so opting in must not hide it."""
    env = {name: "1"}
    assert isolate_dotenv(env) is False and "LITELLM_MODE" not in env
    # any other value is not an opt-in
    assert isolate_dotenv({name: "0"}) is True


def test_this_run_was_isolated_unless_it_is_a_live_opt_in():
    live = any(os.environ.get(name) == "1" for name in LIVE_OPT_INS)
    assert live or "LITELLM_MODE" in os.environ


def test_the_application_never_sets_the_isolation_flag():
    """Production configuration loading is untouched: only the test configuration sets it."""
    offenders = [str(p.relative_to(SRC)) for p in SRC.rglob("*.py") if "LITELLM_MODE" in p.read_text()]
    assert offenders == []
