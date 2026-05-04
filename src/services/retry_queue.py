"""
PostCallRetryQueue — Redis-based retry mechanism for failed post-call tasks.

KNOWN ISSUE: Retry state is stored in Redis with no durability guarantee.
If Redis restarts, all pending retries are lost. There is no dead-letter queue,
no visibility into retry state, and no alerting on repeated failures.
"""

import json
import logging
import time
from dataclasses import dataclass
from typing import List, Optional

from src.utils.redis_client import redis_client

logger = logging.getLogger(__name__)

RETRY_QUEUE_KEY = "postcall:retry_queue"
RETRY_STATE_PREFIX = "postcall:retry_state:"


@dataclass
class RetryEntry:
    interaction_id: str
    attempt: int
    last_error: str
    next_retry_at: float
    payload: dict


class PostCallRetryQueue:
    """
    Manages retry state for failed post-call processing tasks.

    Problems:
    - State lives entirely in Redis — no durability
    - No dead-letter queue for permanently failed tasks
    - No visibility dashboard — ops has to manually inspect Redis keys
    - Retry delay is fixed, not exponential
    - No correlation between retry attempts and the original Celery task
    """

    def __init__(self, max_retries: int = 3, retry_delay_seconds: int = 60):
        self.max_retries = max_retries
        self.retry_delay = retry_delay_seconds

    async def enqueue_retry(
        self, interaction_id: str, error: str, payload: dict
    ) -> bool:
        """
        Enqueue a failed interaction for retry.
        Returns False if max retries exceeded.
        """
        state_key = f"{RETRY_STATE_PREFIX}{interaction_id}"
        current_attempt = int(await redis_client.get(state_key) or 0)

        if current_attempt >= self.max_retries:
            logger.error(
                "retry_exhausted",
                extra={
                    "interaction_id": interaction_id,
                    "attempts": current_attempt,
                    "last_error": error,
                },
            )
            # Task is silently dropped — no dead-letter, no alert, no recovery
            return False

        next_attempt = current_attempt + 1
        entry = {
            "interaction_id": interaction_id,
            "attempt": next_attempt,
            "last_error": error,
            "next_retry_at": time.time() + self.retry_delay,
            "payload": payload,
        }

        await redis_client.set(state_key, next_attempt)
        # No TTL on the state key — leaked memory if interaction is never retried
        await redis_client.rpush(RETRY_QUEUE_KEY, json.dumps(entry))

        logger.info(
            "retry_enqueued",
            extra={
                "interaction_id": interaction_id,
                "attempt": next_attempt,
                "next_retry_at": entry["next_retry_at"],
            },
        )
        return True

    async def dequeue_ready(self) -> List[RetryEntry]:
        """
        Pop all entries whose retry time has passed.
        NOTE: This is not atomic — concurrent consumers can double-process.
        """
        now = time.time()
        ready = []

        queue_length = await redis_client.llen(RETRY_QUEUE_KEY)
        for _ in range(queue_length):
            raw = await redis_client.lpop(RETRY_QUEUE_KEY)
            if not raw:
                break

            entry = json.loads(raw)
            if entry["next_retry_at"] <= now:
                ready.append(RetryEntry(**entry))
            else:
                # Not ready yet — push back to queue (causes reordering)
                await redis_client.rpush(RETRY_QUEUE_KEY, raw)

        return ready

    async def get_queue_depth(self) -> int:
        return await redis_client.llen(RETRY_QUEUE_KEY)


retry_queue = PostCallRetryQueue()
