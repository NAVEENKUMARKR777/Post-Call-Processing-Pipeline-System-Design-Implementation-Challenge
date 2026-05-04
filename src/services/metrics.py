"""
PostCallMetricsTracker — Tracks post-call processing metrics.

Currently a minimal implementation that logs to stdout.
No structured metrics, no Grafana integration, no alerting.
"""

import logging
import time
from typing import Optional

from src.utils.redis_client import redis_client

logger = logging.getLogger(__name__)


class PostCallMetricsTracker:
    async def track_processing_started(self, interaction_id: str) -> None:
        await redis_client.set(
            f"postcall:metrics:{interaction_id}:start", str(time.time()), ex=3600
        )

    async def track_processing_completed(
        self, interaction_id: str, tokens_used: int, latency_ms: float
    ) -> None:
        start = await redis_client.get(f"postcall:metrics:{interaction_id}:start")
        wall_time = time.time() - float(start) if start else 0

        logger.info(
            "postcall_metrics",
            extra={
                "interaction_id": interaction_id,
                "tokens_used": tokens_used,
                "latency_ms": latency_ms,
                "wall_time_s": wall_time,
            },
        )

    async def track_processing_failed(
        self, interaction_id: str, error: str
    ) -> None:
        logger.error(
            "postcall_failed",
            extra={"interaction_id": interaction_id, "error": error},
        )


metrics_tracker = PostCallMetricsTracker()
