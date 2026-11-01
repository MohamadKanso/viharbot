"""One umbrella switch for the live team conversation.

Iterative orchestration needs delegation, which needs identity routing, so running it used to
take three flags. `VIHARBOT_LIVE_TEAM_ENABLED` turns all three layers on at once, and the three
individual flags still work on their own and keep their meaning. The planner alias
(CHARLIE_PLANNER_MODEL) and the opt-in web key (WEB_LITELLM_API_KEY) are separate: the umbrella
flag grants no credentials. Off by default; the cloud spec does not set it.
"""

from __future__ import annotations

import os

LIVE_TEAM_FLAG = "VIHARBOT_LIVE_TEAM_ENABLED"


def flag_on(name: str) -> bool:
    return os.getenv(name, "false").strip().lower() in {"1", "true", "yes"}


def live_team_enabled() -> bool:
    return flag_on(LIVE_TEAM_FLAG)
