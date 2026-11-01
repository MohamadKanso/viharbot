"""The PPT-generation feature flag, shared by the web service and the pixel service.

While Pixel's deck output is being tested the flag is true in local Compose. When the feature is
done, set VIHARBOT_PPT_GENERATION_ENABLED to false there (compose.yaml default and .env) so no
request spends model calls building decks. The code default is false, and the DigitalOcean spec
does not set it, so a deployment without an explicit choice never generates decks.
"""

from __future__ import annotations

import os

PPT_FLAG = "VIHARBOT_PPT_GENERATION_ENABLED"


def ppt_generation_enabled() -> bool:
    return os.getenv(PPT_FLAG, "false").strip().lower() in {"1", "true", "yes"}
