"""Budget-manager tests, including AC2 (per-customer budget isolation).

The budget manager is the layer that turns the rate limiter into
the README's "reserved + shared headroom" model. AC2 says:

  Per-customer token budget enforced — Customer A's budget does
  not consume Customer B's allocation.

The test below exhausts Customer A's reservation and verifies that
Customer B's reservation, made under identical conditions, still
succeeds. Without isolation this test fails; with the two-bucket
design (cust:<id> + global) it passes by construction.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import fakeredis.aioredis as fakeredis
import pytest

from src.scheduler.budget_manager import (
    BudgetManager,
    BudgetSnapshot,
)
from src.scheduler.rate_limiter import RateLimiter


@pytest.fixture
async def redis_client():
    client = fakeredis.FakeRedis(decode_responses=False)
    yield client
    await client.flushall()
    close = getattr(client, "aclose", None) or getattr(client, "close", None)
    if close:
        result = close()
        if hasattr(result, "__await__"):
            await result


def _budget_manager(redis_client, budgets: dict[str, BudgetSnapshot], *, global_tpm=10000, global_rpm=200):
    limiter = RateLimiter(redis_client, key_prefix="test")
    manager = BudgetManager(
        session_factory=AsyncMock(),
        rate_limiter=limiter,
        global_tpm=global_tpm,
        global_rpm=global_rpm,
        cache_ttl_s=300,
    )

    # Bypass DB lookup with an in-test mapping. cache_ttl_s is high so
    # the inserted budgets are read on every check.
    async def _fake_loader(customer_id):
        key = str(customer_id)
        if key in budgets:
            return budgets[key]
        return BudgetSnapshot(customer_id=key, reserved_tpm=0, reserved_rpm=0, burst_tpm=2000, priority=9)

    manager._load_budget = _fake_loader  # type: ignore[assignment]
    return manager


@pytest.mark.asyncio
async def test_first_reservation_is_allowed_under_reserved_floor(redis_client):
    manager = _budget_manager(
        redis_client,
        {"cust-A": BudgetSnapshot("cust-A", reserved_tpm=5000, reserved_rpm=20, burst_tpm=2000, priority=2)},
    )

    res = await manager.check_and_reserve("cust-A", estimated_tokens=1500)
    assert res.allowed
    assert res.reason == "ok"


@pytest.mark.asyncio
async def test_customer_a_exhaustion_does_not_affect_b(redis_client):
    """AC2: Customer A consumes their full reserved+burst allowance
    and is denied; Customer B with the same configuration is still
    served from their own bucket."""
    budgets = {
        "cust-A": BudgetSnapshot("cust-A", reserved_tpm=4000, reserved_rpm=10, burst_tpm=1000, priority=2),
        "cust-B": BudgetSnapshot("cust-B", reserved_tpm=4000, reserved_rpm=10, burst_tpm=1000, priority=2),
    }
    manager = _budget_manager(redis_client, budgets, global_tpm=20000, global_rpm=200)

    # Customer A: 5 × 1000 = 5000 fits exactly into their 5K cap.
    base = 1_700_000_000_000
    for i in range(5):
        res = await manager.check_and_reserve("cust-A", estimated_tokens=1000, now_ms=base + i * 100)
        assert res.allowed, f"A iter {i}"

    # 6th call: A is over budget (5000 used, +1000 = 6000 > 5000).
    a_denied = await manager.check_and_reserve("cust-A", estimated_tokens=1000, now_ms=base + 600)
    assert not a_denied.allowed
    assert a_denied.reason == "customer_budget_exhausted"

    # Customer B with identical configuration is unaffected.
    b_allowed = await manager.check_and_reserve("cust-B", estimated_tokens=1000, now_ms=base + 700)
    assert b_allowed.allowed
    assert b_allowed.reason == "ok"


@pytest.mark.asyncio
async def test_global_headroom_can_deny_even_when_customer_has_room(redis_client):
    """A customer can be within their reserved + burst yet still be
    denied because the global pool is exhausted by other customers.
    The reason field must distinguish the two cases so on-call can
    tell whether to bump a customer's budget or scale the platform."""
    budgets = {
        "cust-X": BudgetSnapshot("cust-X", reserved_tpm=5000, reserved_rpm=10, burst_tpm=5000, priority=1),
        "cust-Y": BudgetSnapshot("cust-Y", reserved_tpm=5000, reserved_rpm=10, burst_tpm=5000, priority=1),
    }
    manager = _budget_manager(redis_client, budgets, global_tpm=8000, global_rpm=200)

    base = 1_700_000_000_000
    # Two customers each take 4000 → 8000 in the global bucket.
    res_x = await manager.check_and_reserve("cust-X", estimated_tokens=4000, now_ms=base)
    res_y = await manager.check_and_reserve("cust-Y", estimated_tokens=4000, now_ms=base + 100)
    assert res_x.allowed and res_y.allowed

    # cust-X tries another 1000. Their bucket has 6000 of 10K used —
    # they have room. But the global pool is full.
    denied = await manager.check_and_reserve("cust-X", estimated_tokens=1000, now_ms=base + 200)
    assert not denied.allowed
    assert denied.reason == "global_headroom_exhausted"


@pytest.mark.asyncio
async def test_partial_reservation_does_not_leak_when_global_denies(redis_client):
    """If the customer-tier check passes but the global tier denies,
    we must not leave a phantom customer-bucket reservation. Otherwise
    that customer's window slowly drifts toward exhaustion even
    though no LLM call happened."""
    budgets = {
        "cust-Z": BudgetSnapshot("cust-Z", reserved_tpm=10000, reserved_rpm=50, burst_tpm=10000, priority=1),
    }
    manager = _budget_manager(redis_client, budgets, global_tpm=1000, global_rpm=200)

    base = 1_700_000_000_000
    # Customer is fine for 5000, but global has only 1000 TPM total.
    res = await manager.check_and_reserve("cust-Z", estimated_tokens=5000, now_ms=base)
    assert not res.allowed
    assert res.reason == "global_headroom_exhausted"

    # If the customer reservation was rolled back, a 500-token call
    # against a fresh global bucket should still work.
    # Use a far-future timestamp so the global pool's 5000 token
    # phantom (had we leaked) would have to be in window — there's none.
    follow = await manager.check_and_reserve("cust-Z", estimated_tokens=500, now_ms=base + 100)
    assert follow.allowed
    # Verify customer's tracked usage is just 500 (the second call),
    # not 5500 (leaked first + second).
    assert follow.customer_reservation.current_tpm == 500


@pytest.mark.asyncio
async def test_unknown_customer_falls_back_to_default_budget(redis_client):
    """A customer with no row in customer_llm_budgets must still be
    served, just at the conservative default (no reservation, modest
    burst). This means provisioning a new customer doesn't require a
    deploy — they just appear with default best-effort capacity."""
    manager = _budget_manager(redis_client, {}, global_tpm=20000, global_rpm=200)

    res = await manager.check_and_reserve("brand-new-customer", estimated_tokens=1000)
    assert res.allowed


@pytest.mark.asyncio
async def test_reconcile_updates_both_buckets(redis_client):
    budgets = {
        "cust-R": BudgetSnapshot("cust-R", reserved_tpm=10000, reserved_rpm=20, burst_tpm=0, priority=1),
    }
    manager = _budget_manager(redis_client, budgets, global_tpm=20000, global_rpm=200)

    res = await manager.check_and_reserve("cust-R", estimated_tokens=1500)
    assert res.allowed

    # LLM came back with 2200 actual tokens — drift the counter up.
    await manager.reconcile(res, actual_tokens=2200)

    # Next utilization read should show the reconciled total.
    util = await manager.utilization("cust-R")
    assert util["customer_tpm"] == 2200
    assert util["global_tpm"] == 2200


@pytest.mark.asyncio
async def test_release_rolls_back_both_buckets(redis_client):
    budgets = {
        "cust-F": BudgetSnapshot("cust-F", reserved_tpm=10000, reserved_rpm=20, burst_tpm=0, priority=1),
    }
    manager = _budget_manager(redis_client, budgets, global_tpm=20000, global_rpm=200)

    res = await manager.check_and_reserve("cust-F", estimated_tokens=2000)
    assert res.allowed
    assert res.global_reservation.current_tpm == 2000

    await manager.release(res)

    util = await manager.utilization("cust-F")
    assert util["customer_tpm"] == 0
    assert util["global_tpm"] == 0
