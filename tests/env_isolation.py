"""Keep a developer's `.env` out of the offline test run (Phase 6.5 audit D3, issue #21).

The repository never reads `.env` itself. The only loader is LiteLLM: on import it calls
python-dotenv's `load_dotenv()`, which walks up from LiteLLM's own install location, so a `.env`
in the repository (or any parent directory of the virtual environment) is found and its values
land in `os.environ`. That can switch the orchestration, diagnostics and planner paths on, change
test outcomes, and put a real key in a test process. LiteLLM skips the load when `LITELLM_MODE`
is anything other than `DEV`, so the tests set it before LiteLLM is imported.

This applies to the test run only. Nothing under `src/` sets it, so the application and the
services keep their normal behaviour.
"""

from __future__ import annotations

# Setting either to "1" in the shell is the explicit opt-in to the billable live checks. Those
# checks need the developer's key, which may live in `.env`, so loading is left alone for them.
LIVE_OPT_INS = ("RUN_LITELLM_INTEGRATION", "RUN_LITELLM_INFERENCE")


def isolate_dotenv(environ) -> bool:
    """Stop LiteLLM loading `.env` unless the run is a live opt-in. Returns True if it did.

    An explicit `LITELLM_MODE` already in the environment is respected (`setdefault`), so a
    developer can still choose `LITELLM_MODE=DEV` to load `.env` on purpose.
    """
    if any(environ.get(name) == "1" for name in LIVE_OPT_INS):
        return False
    environ.setdefault("LITELLM_MODE", "PRODUCTION")
    return environ["LITELLM_MODE"] != "DEV"
