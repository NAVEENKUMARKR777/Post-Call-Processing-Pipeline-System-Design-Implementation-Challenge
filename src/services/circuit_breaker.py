"""
PostCallCircuitBreaker — Controls dialler behaviour based on post-call LLM load.

KNOWN ISSUE: When post-call analysis backs up (e.g., Celery queue depth > 1000),
the circuit breaker trips and freezes the dialler for the affected agent_id for
1800 seconds. This means a post-call backlog directly stops new outbound calls —
a cascading failure with no visibility or recovery mechanism.

The dialler team has no way to distinguish "LLM quota genuinely exhausted" from
"Celery queue backed up because of a transient Redis hiccup".
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

from src.config import settings
from src.utils.redis_client import redis_client

logger = logging.getLogger(__name__)


@dataclass
class CircuitState:
    agent_id: str
    is_open: bool = False
    opened_at: Optional[float] = None
    freeze_until: Optional[float] = None
    consecutive_failures: int = 0


class PostCallCircuitBreaker:
    """
    Monitors LLM capacity usage for post-call processing.
    When usage >= 90% of capacity, trips the circuit breaker for the agent,
    freezing all outbound dials for that agent for 1800 seconds.

    Problems:
    - No gradual backpressure — it's binary (open/closed)
    - Freeze duration is hardcoded (1800s)
    - Agent-level granularity is too coarse (one bad campaign freezes all)
    - No visibility — the dialler just sees "circuit open" with no context
    - Capacity is measured by in-flight Celery tasks, not actual LLM quota usage
    """

    def __init__(self):
        self._states: Dict[str, CircuitState] = {}
        self._capacity_threshold = settings.CIRCUIT_BREAKER_CAPACITY_THRESHOLD
        self._freeze_seconds = settings.CIRCUIT_BREAKER_FREEZE_SECONDS

    async def check_capacity(self, agent_id: str) -> bool:
        """Returns True if the agent is allowed to make new calls."""
        state = self._states.get(agent_id)

        if state and state.is_open:
            if time.time() < state.freeze_until:
                logger.warning(
                    "circuit_breaker_open",
                    extra={
                        "agent_id": agent_id,
                        "freeze_remaining_s": state.freeze_until - time.time(),
                    },
                )
                return False
            # Freeze expired — close the circuit
            state.is_open = False
            state.consecutive_failures = 0
            logger.info("circuit_breaker_closed", extra={"agent_id": agent_id})

        # Check current LLM capacity via Redis counters
        current_rpm = int(await redis_client.get(f"llm:postcall:rpm") or 0)
        max_rpm = settings.LLM_REQUESTS_PER_MINUTE

        usage_ratio = current_rpm / max_rpm if max_rpm > 0 else 0

        if usage_ratio >= self._capacity_threshold:
            self._trip(agent_id)
            return False

        return True

    def _trip(self, agent_id: str):
        now = time.time()
        state = CircuitState(
            agent_id=agent_id,
            is_open=True,
            opened_at=now,
            freeze_until=now + self._freeze_seconds,
        )
        self._states[agent_id] = state

        logger.error(
            "circuit_breaker_tripped",
            extra={
                "agent_id": agent_id,
                "freeze_seconds": self._freeze_seconds,
                "capacity_threshold": self._capacity_threshold,
            },
        )

    async def record_postcall_start(self):
        """Increment in-flight counter when a post-call LLM request starts."""
        await redis_client.incr("llm:postcall:rpm")
        await redis_client.expire("llm:postcall:rpm", 60)

    async def record_postcall_end(self):
        """Decrement in-flight counter when a post-call LLM request completes."""
        await redis_client.decr("llm:postcall:rpm")


circuit_breaker = PostCallCircuitBreaker()
