"""Sliding-window rate limiter backed by Redis sorted sets.

Two limits are enforced together: tokens-per-minute and
requests-per-minute. Both are tracked as members in Redis
ZSETs scored by their reservation timestamp; the 60-second
window is enforced by pruning entries older than `now - 60s`
before each check.

The check + reserve is atomic via a Lua script — a fresh
reservation never sees stale state, and concurrent callers
cannot both squeak through under a tight limit.

When a reservation is denied, the limiter returns a
`retry_after_ms` calculated from the oldest in-window entry's
expiry — never a blanket 60s. This is the property the
README asks for ("recovery, not crash") and is what makes the
system drain a backlog smoothly instead of in 60s lurches.

Token reconciliation: we reserve at `estimated_tokens` to gate
the call, then call `reconcile()` once the LLM returns the actual
count to keep counters accurate over time.
"""

from __future__ import annotations

import enum
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol

from src.observability import get_logger

logger = get_logger(__name__)


class RateLimitDecision(str, enum.Enum):
    ALLOW = "allow"
    DENY = "deny"


@dataclass(frozen=True)
class Reservation:
    decision: RateLimitDecision
    retry_after_ms: int
    current_tpm: int
    current_rpm: int
    member_id: str
    bucket: str

    @property
    def allowed(self) -> bool:
        return self.decision is RateLimitDecision.ALLOW


class RedisLike(Protocol):
    """Subset of redis.asyncio.Redis we depend on. Defining a
    Protocol means we can mock the limiter cheaply in tests
    without bringing up a real Redis instance."""

    async def eval(self, script: str, numkeys: int, *args): ...

    async def zrem(self, name, *values): ...

    async def zadd(self, name, mapping, **kwargs): ...


_LUA_SCRIPT = (Path(__file__).parent / "lua" / "check_quota.lua").read_text(encoding="utf-8")


class RateLimiter:
    """Sliding-window rate limiter scoped by an arbitrary bucket name.

    The budget manager calls this with a global bucket and a
    per-customer bucket, taking the *minimum* of the two decisions
    as the final answer. That layering is intentional — keeping the
    limiter unaware of customer semantics keeps it small and
    auditable.
    """

    def __init__(
        self,
        redis_client: RedisLike,
        *,
        key_prefix: str = "llm",
    ):
        self._redis = redis_client
        self._key_prefix = key_prefix

    def _keys(self, bucket: str) -> tuple[str, str]:
        tpm_key = f"{self._key_prefix}:tpm:{bucket}"
        rpm_key = f"{self._key_prefix}:rpm:{bucket}"
        return tpm_key, rpm_key

    async def check_and_reserve(
        self,
        bucket: str,
        *,
        tpm_limit: int,
        rpm_limit: int,
        estimated_tokens: int,
        now_ms: Optional[int] = None,
    ) -> Reservation:
        """Atomic check + reserve.

        On ALLOW, the caller has consumed (estimated_tokens, 1)
        from this bucket's window and must call `reconcile()`
        with the actual token count once the LLM call completes.

        On DENY, retry_after_ms is the wall-clock millisecond
        delay until the bucket's oldest in-window entry expires
        — i.e., the earliest moment a retry can succeed for the
        same estimate.
        """
        if estimated_tokens < 0:
            raise ValueError(f"estimated_tokens must be non-negative, got {estimated_tokens}")

        tpm_key, rpm_key = self._keys(bucket)
        ts = now_ms if now_ms is not None else int(time.time() * 1000)
        member_uuid = uuid.uuid4().hex
        # The Lua script encodes the timestamp into the member name as
        # "<now_ms>:<uuid>"; we recompute that locally so reconcile()
        # and release() can address the same member without another
        # Redis roundtrip.
        member_id = f"{ts}:{member_uuid}"

        result = await self._redis.eval(
            _LUA_SCRIPT,
            2,
            tpm_key,
            rpm_key,
            ts,
            tpm_limit,
            rpm_limit,
            estimated_tokens,
            member_uuid,
        )

        # redis-py returns Lua arrays as Python lists.
        allowed_flag, retry_after_ms, current_tpm, current_rpm = (
            int(result[0]),
            int(result[1]),
            int(result[2]),
            int(result[3]),
        )
        decision = RateLimitDecision.ALLOW if allowed_flag else RateLimitDecision.DENY

        reservation = Reservation(
            decision=decision,
            retry_after_ms=retry_after_ms,
            current_tpm=current_tpm,
            current_rpm=current_rpm,
            member_id=member_id,
            bucket=bucket,
        )

        if decision is RateLimitDecision.DENY:
            logger.info(
                "rate_limit_denied",
                extra={
                    "bucket": bucket,
                    "current_tpm": current_tpm,
                    "current_rpm": current_rpm,
                    "tpm_limit": tpm_limit,
                    "rpm_limit": rpm_limit,
                    "retry_after_ms": retry_after_ms,
                    "estimated_tokens": estimated_tokens,
                },
            )
        return reservation

    async def reconcile(
        self,
        reservation: Reservation,
        *,
        actual_tokens: int,
    ) -> None:
        """Replace the reservation's estimated token count with the
        actual count. Required so that systematic estimation drift
        doesn't make the rate limiter slowly under- or over-permissive.

        Called once the LLM returns. If the call failed entirely,
        call `release()` instead to roll the reservation back.
        """
        if not reservation.allowed:
            return
        if actual_tokens < 0:
            raise ValueError(f"actual_tokens must be non-negative, got {actual_tokens}")

        tpm_key, _ = self._keys(reservation.bucket)
        # The TPM ZSET stores tokens as the score; reconciling means
        # replacing the score for this member. ZADD with XX (only
        # update existing) is single-roundtrip and naturally a no-op
        # if the member was already evicted by the window prune.
        await self._redis.zadd(tpm_key, {reservation.member_id: actual_tokens}, xx=True)

    async def release(self, reservation: Reservation) -> None:
        """Roll back a reservation entirely (e.g., the LLM call
        crashed before any tokens were consumed). Removes both the
        TPM and RPM ZSET entries.

        It is safe to call release() on a denied reservation — it's
        a no-op, since the reservation never landed in Redis.
        """
        if not reservation.allowed:
            return
        tpm_key, rpm_key = self._keys(reservation.bucket)
        await self._redis.zrem(tpm_key, reservation.member_id)
        await self._redis.zrem(rpm_key, reservation.member_id)
