"""Custom CloudWatch metrics in the `ViharBot/Kepler` namespace (the only namespace the runtime
role may publish to). A metrics failure never fails a poll: the heartbeat alarm notices the gap."""

from __future__ import annotations

import logging
from typing import Any

NAMESPACE = "ViharBot/Kepler"
logger = logging.getLogger("kepler")


class CloudWatchMetrics:
    def __init__(self, client: Any) -> None:
        self._cw = client

    def put(self, name: str, value: float, unit: str = "Count") -> None:
        try:
            self._cw.put_metric_data(Namespace=NAMESPACE, MetricData=[
                {"MetricName": name, "Value": value, "Unit": unit}])
        except Exception:
            logger.warning("metric could not be published", extra={"metric": name})


class NoMetrics:
    def put(self, name: str, value: float, unit: str = "Count") -> None:  # dry runs and tests
        return None
