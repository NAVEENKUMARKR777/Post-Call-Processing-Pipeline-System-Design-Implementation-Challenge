"""Rate-limiter contract tests, including AC1 (1000-call burst).

The limiter is the core fix for the assignment's stated primary
problem: "LLM APIs have hard rate limits. The current system has
no awareness of them." If this test file's `test_burst_1000_calls_no_429`
fails, the assignment is failed at AC1.

Tests run against a fakeredis instance that emulates the Lua
scripting engine, so they pass without docker-compose.
"""

from __future__ import annotations

import asyncio

import fakeredis.aioredis as fakeredis
import pytest

from src.scheduler.rate_limiter import RateLimitDecision, RateLimiter


@pytest.fixture
async def redis_client():
    client = fakeredis.FakeRedis(decode_responses=False)
    yield client
    await client.flushall()
    # fakeredis uses close(); real redis-py uses aclose() too. Either works.
    close = getattr(client, "aclose", None) or getattr(client, "close", None)
    if close:
        result = close()
        if hasattr(result, "__await__"):
            await result


@pytest.fixture
async def limiter(redis_client):
    return RateLimiter(redis_client, key_prefix="test")


@pytest.mark.asyncio
async def test_first_reservation_under_limit_is_allowed(limiter):
    res = await limiter.check_and_reserve(
        "global",
        tpm_limit=10000,
        rpm_limit=100,
        estimated_tokens=1500,
    )
    assert res.allowed
    assert res.current_tpm == 1500
    assert res.current_rpm == 1
    assert res.retry_after_ms == 0


@pytest.mark.asyncio
async def test_reservations_accumulate_within_window(limiter):
    for i in range(5):
        res = await limiter.check_and_reserve(
            "global",
            tpm_limit=10000,
            rpm_limit=100,
            estimated_tokens=1000,
        )
        assert res.allowed
    assert res.current_tpm == 5000
    assert res.current_rpm == 5


@pytest.mark.asyncio
async def test_tpm_exhaustion_denies_with_retry_after(limiter):
    # 5 reservations of 2000 tokens fits exactly into the 10000-tpm
    # budget; the 6th must be denied.
    base_now = 1_700_000_000_000
    for i in range(5):
        res = await limiter.check_and_reserve(
            "global",
            tpm_limit=10000,
            rpm_limit=100,
            estimated_tokens=2000,
            now_ms=base_now + i * 100,
        )
        assert res.allowed, f"reservation {i} should pass"

    denied = await limiter.check_and_reserve(
        "global",
        tpm_limit=10000,
        rpm_limit=100,
        estimated_tokens=2000,
        now_ms=base_now + 600,
    )
    assert not denied.allowed
    assert denied.decision is RateLimitDecision.DENY
    # Retry should be scheduled relative to the OLDEST entry's expiry
    # (= window_ms − age_of_oldest). Oldest is at base_now, current is
    # base_now+600 → expires at base_now+60000 → retry_after ≈ 59400 ms.
    assert 59000 <= denied.retry_after_ms <= 60000


@pytest.mark.asyncio
async def test_rpm_exhaustion_independent_of_tpm(limiter):
    # tiny token reservations but rpm limit blocks
    for i in range(3):
        res = await limiter.check_and_reserve(
            "global",
            tpm_limit=1_000_000,
            rpm_limit=3,
            estimated_tokens=10,
        )
        assert res.allowed

    denied = await limiter.check_and_reserve(
        "global",
        tpm_limit=1_000_000,
        rpm_limit=3,
        estimated_tokens=10,
    )
    assert not denied.allowed
    assert denied.retry_after_ms > 0


@pytest.mark.asyncio
async def test_window_eviction_releases_capacity(limiter):
    """An entry older than 60s must be pruned automatically on the
    next check, so steady traffic at the limit drains smoothly
    rather than stalling."""
    base_now = 1_700_000_000_000
    res1 = await limiter.check_and_reserve(
        "global",
        tpm_limit=2000,
        rpm_limit=10,
        estimated_tokens=1500,
        now_ms=base_now,
    )
    assert res1.allowed

    # Second reservation also under limit.
    res2 = await limiter.check_and_reserve(
        "global",
        tpm_limit=2000,
        rpm_limit=10,
        estimated_tokens=400,
        now_ms=base_now + 1000,
    )
    assert res2.allowed
    assert res2.current_tpm == 1900

    # Now we'd be over: 1900 + 500 = 2400 > 2000.
    denied = await limiter.check_and_reserve(
        "global",
        tpm_limit=2000,
        rpm_limit=10,
        estimated_tokens=500,
        now_ms=base_now + 2000,
    )
    assert not denied.allowed

    # Step past the window; oldest entry expires and the third
    # reservation now fits.
    later = await limiter.check_and_reserve(
        "global",
        tpm_limit=2000,
        rpm_limit=10,
        estimated_tokens=500,
        now_ms=base_now + 60_500,
    )
    assert later.allowed


@pytest.mark.asyncio
async def test_release_rolls_back_a_reservation(limiter):
    res = await limiter.check_and_reserve(
        "global",
        tpm_limit=2000,
        rpm_limit=10,
        estimated_tokens=1500,
    )
    assert res.allowed

    await limiter.release(res)

    # After release we should be back at zero usage.
    next_res = await limiter.check_and_reserve(
        "global",
        tpm_limit=2000,
        rpm_limit=10,
        estimated_tokens=1500,
    )
    assert next_res.allowed
    assert next_res.current_tpm == 1500


@pytest.mark.asyncio
async def test_buckets_are_isolated(limiter):
    """Customer A's bucket exhaustion must not affect Customer B."""
    for _ in range(3):
        res = await limiter.check_and_reserve(
            "cust-a",
            tpm_limit=3000,
            rpm_limit=10,
            estimated_tokens=1000,
        )
        assert res.allowed

    cust_a_denied = await limiter.check_and_reserve(
        "cust-a",
        tpm_limit=3000,
        rpm_limit=10,
        estimated_tokens=1000,
    )
    assert not cust_a_denied.allowed

    cust_b_allowed = await limiter.check_and_reserve(
        "cust-b",
        tpm_limit=3000,
        rpm_limit=10,
        estimated_tokens=1000,
    )
    assert cust_b_allowed.allowed
    assert cust_b_allowed.current_tpm == 1000


@pytest.mark.asyncio
async def test_concurrent_reservations_do_not_overshoot_limit(limiter):
    """Atomicity test: 100 concurrent callers, only ones whose
    reservations fit the 5000-token budget should pass. The total
    reserved tokens across allowed reservations must not exceed
    the limit. This is the property a non-atomic check-then-set
    cannot guarantee."""
    base_now = 1_700_000_000_000

    async def attempt(i: int):
        return await limiter.check_and_reserve(
            "global",
            tpm_limit=5000,
            rpm_limit=10_000,
            estimated_tokens=1000,
            now_ms=base_now + i,
        )

    results = await asyncio.gather(*(attempt(i) for i in range(100)))
    allowed = [r for r in results if r.allowed]
    denied = [r for r in results if not r.allowed]

    assert len(allowed) == 5, f"expected exactly 5 allowed, got {len(allowed)}"
    assert len(denied) == 95
    # No retry hint is zero on a denial.
    for d in denied:
        assert d.retry_after_ms > 0


@pytest.mark.asyncio
async def test_burst_1000_calls_no_429(limiter):
    """AC1: a burst of 1000 calls in 60 seconds against an
    LLM_TOKENS_PER_MINUTE=90000 budget. The limiter must defer the
    excess (no 429 surfaced to the caller); approximately 60 should
    be allowed and the rest deferred with retry hints.

    "No 429 surfaced" is the constraint. Equivalent here means: the
    limiter returned a structured DENY rather than letting the
    request through."""
    base_now = 1_700_000_000_000
    allowed = 0
    denied = 0
    for i in range(1000):
        res = await limiter.check_and_reserve(
            "global",
            tpm_limit=90_000,
            rpm_limit=500,
            estimated_tokens=1500,
            now_ms=base_now + i * 5,
        )
        if res.allowed:
            allowed += 1
        else:
            denied += 1
            assert res.retry_after_ms > 0, "denied reservations must include a retry hint"

    # 60 calls × 1500 tokens = 90,000 — exactly at the TPM ceiling.
    # RPM cap of 500 is well above 60 so doesn't bind.
    assert allowed == 60, f"expected 60 allowed under 90K TPM / 1.5K per call, got {allowed}"
    assert denied == 940
