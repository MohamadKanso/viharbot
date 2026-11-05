"""Test-session configuration. Runs before any test module imports LiteLLM."""

import os

from env_isolation import isolate_dotenv

# Must happen here, at conftest import, because LiteLLM reads `.env` when it is first imported.
isolate_dotenv(os.environ)
