"""The live team conversation has no feature flag.

It runs when Charlie's planner is configured: a planner alias (CHARLIE_PLANNER_MODEL) and a
LiteLLM key in the web container (LITELLM_API_KEY, which Compose fills only from the opt-in
WEB_LITELLM_API_KEY). The key was already the real gate, since a web container without one cannot
plan, so a separate flag only added a second way to be half-configured. Without the key the web
service runs the fixed pipeline and /api/chat/stream answers 409. To turn the mode off, do not
pass the key. The cloud spec carries neither value, so Stage never runs it.
"""

from __future__ import annotations

import os


def live_team_enabled() -> bool:
    return bool(os.getenv("CHARLIE_PLANNER_MODEL", "").strip() and os.getenv("LITELLM_API_KEY", "").strip())
