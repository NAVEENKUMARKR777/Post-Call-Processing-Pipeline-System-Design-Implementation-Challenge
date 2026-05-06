"""Per-customer budget manager — composes the rate limiter into the
two-tier "reserved + shared headroom" model the README asks for.

For each LLM call, the manager runs the limiter twice and combines:

  1. Customer bucket  (limit = reserved_tpm + burst_tpm) — proves the
                       customer is within their reservation + their
                       allowed burst ceiling.
  2. Global bucket    (limit = LLM_TOKENS_PER_MINUTE − Σ reserved_tpm)
                       — proves there is *shared headroom* available
                       beyond what every customer has already
                       reserved.

  ALLOW only if both pass. The customer-bucket check is what makes
  Customer A's budget exhaustion not affect Customer B (AC2);
  reservations live in their own bucket and one customer cannot
  reach into another's. The global-bucket check protects the LLM
  provider from over-subscription when reservations sum below the
  hard limit.

If a customer exhausts both their reservation AND their burst
allowance, the call defers — never crashes. The manager returns
the longer of the two retry-after hints so callers don't poll
prematurely.

Headroom borrowing: a customer with reserved_tpm=20K, burst_tpm=10K
can use up to 30K TPM as long as the global bucket has room. Once
multiple customers compete for shared headroom, priority breaks ties
(lower number wins) — but only when global is the binding limit;
within a customer's own reservation, priority is irrelevant.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.models.customer_budget import CustomerLLMBudget
from src.observability import get_logger
from src.scheduler.rate_limiter import (
    RateLimitDecision,
    RateLimiter,
    Reservation,
)

logger = get_logger(__name__)


# Default budget applied to a customer that does not have a row in
# customer_llm_budgets. Conservative on reserved (=0) so unknown
# customers are best-effort, never displacing an explicit reservation.
DEFAULT_RESERVED_TPM = 0
DEFAULT_RESERVED_RPM = 0
DEFAULT_BURST_TPM = 5000
DEFAULT_PRIORITY = 9


@dataclass(frozen=True)
class BudgetReservation:
    """Outcome of a budget-aware reservation. Holds both inner
    reservations so callers can release/reconcile each on the
    appropriate path (LLM success → reconcile both; failure →
    release both)."""
    decision: RateLimitDecision
    retry_after_ms: int
    customer_reservation: Optional[Reservation]
    global_reservation: Optional[Reservation]
    reason: str

    @property
    def allowed(self) -> bool:
        return self.decision is RateLimitDecision.ALLOW


@dataclass(frozen=True)
class BudgetSnapshot:
    customer_id: UUID
    reserved_tpm: int
    reserved_rpm: int
    burst_tpm: int
    priority: int


class BudgetManager:
    """Layer between the LLM worker and the rate limiter.

    Reads customer_llm_budgets for the configured reservation; falls
    back to defaults for customers without a row. The DB read is
    not on the hot path of every call — the manager caches budgets
    in-process for `cache_ttl_s` seconds. This is fine because
    budgets change at human timescales (assumption #4).
    """

    GLOBAL_BUCKET = "global"

    def __init__(
        self,
        session_factory,
        rate_limiter: RateLimiter,
        *,
        global_tpm: Optional[int] = None,
        global_rpm: Optional[int] = None,
        cache_ttl_s: float = 30.0,
    ):
        self._session_factory = session_factory
        self._limiter = rate_limiter
        self._global_tpm = global_tpm if global_tpm is not None else settings.LLM_TOKENS_PER_MINUTE
        self._global_rpm = global_rpm if global_rpm is not None else settings.LLM_REQUESTS_PER_MINUTE
        self._cache_ttl_s = cache_ttl_s
        self._cache: dict[str, tuple[float, BudgetSnapshot]] = {}

    async def _load_budget(self, customer_id: UUID | str) -> BudgetSnapshot:
        import time

        cust_key = str(customer_id)
        cached = self._cache.get(cust_key)
        if cached and (time.monotonic() - cached[0]) < self._cache_ttl_s:
            return cached[1]

        async with self._session_factory() as session:  # type: AsyncSession
            stmt = select(CustomerLLMBudget).where(
                CustomerLLMBudget.customer_id == cust_key
            )
            row = (await session.execute(stmt)).scalar_one_or_none()

        if row is None:
            snapshot = BudgetSnapshot(
                customer_id=cust_key,
                reserved_tpm=DEFAULT_RESERVED_TPM,
                reserved_rpm=DEFAULT_RESERVED_RPM,
                burst_tpm=DEFAULT_BURST_TPM,
                priority=DEFAULT_PRIORITY,
            )
        else:
            snapshot = BudgetSnapshot(
                customer_id=row.customer_id,
                reserved_tpm=row.reserved_tpm,
                reserved_rpm=row.reserved_rpm,
                burst_tpm=row.burst_tpm,
                priority=row.priority,
            )

        self._cache[cust_key] = (time.monotonic(), snapshot)
        return snapshot

    def invalidate_cache(self, customer_id: Optional[str] = None) -> None:
        """Drop the in-process cache so an admin update via
        PUT /admin/customers/{id}/budget takes effect immediately."""
        if customer_id is None:
            self._cache.clear()
        else:
            self._cache.pop(str(customer_id), None)

    async def check_and_reserve(
        self,
        customer_id: UUID | str,
        *,
        estimated_tokens: int,
        now_ms: Optional[int] = None,
    ) -> BudgetReservation:
        """Run the customer-tier and global-tier checks; both must
        pass for the call to proceed. On either denial, returns the
        longer retry-after hint so the caller does not poll early.

        On a partial success (customer ALLOW but global DENY, or
        vice versa), the partial reservation is released so we don't
        consume a slot that the caller never used."""

        budget = await self._load_budget(customer_id)
        cust_bucket = f"cust:{budget.customer_id}"

        cust_tpm_limit = budget.reserved_tpm + budget.burst_tpm
        cust_rpm_limit = max(budget.reserved_rpm, 1) + max(self._global_rpm // 5, 1)

        cust_res = await self._limiter.check_and_reserve(
            cust_bucket,
            tpm_limit=cust_tpm_limit,
            rpm_limit=cust_rpm_limit,
            estimated_tokens=estimated_tokens,
            now_ms=now_ms,
        )
        if not cust_res.allowed:
            return BudgetReservation(
                decision=RateLimitDecision.DENY,
                retry_after_ms=cust_res.retry_after_ms,
                customer_reservation=None,
                global_reservation=None,
                reason="customer_budget_exhausted",
            )

        global_res = await self._limiter.check_and_reserve(
            self.GLOBAL_BUCKET,
            tpm_limit=self._global_tpm,
            rpm_limit=self._global_rpm,
            estimated_tokens=estimated_tokens,
            now_ms=now_ms,
        )
        if not global_res.allowed:
            # Roll back the customer-bucket reservation so we don't
            # leave a phantom hold against the customer's window.
            await self._limiter.release(cust_res)
            return BudgetReservation(
                decision=RateLimitDecision.DENY,
                retry_after_ms=global_res.retry_after_ms,
                customer_reservation=None,
                global_reservation=None,
                reason="global_headroom_exhausted",
            )

        return BudgetReservation(
            decision=RateLimitDecision.ALLOW,
            retry_after_ms=0,
            customer_reservation=cust_res,
            global_reservation=global_res,
            reason="ok",
        )

    async def reconcile(
        self,
        reservation: BudgetReservation,
        *,
        actual_tokens: int,
    ) -> None:
        """Update both the customer and global ZSET scores with the
        true post-call token count."""
        if not reservation.allowed:
            return
        if reservation.customer_reservation:
            await self._limiter.reconcile(
                reservation.customer_reservation, actual_tokens=actual_tokens
            )
        if reservation.global_reservation:
            await self._limiter.reconcile(
                reservation.global_reservation, actual_tokens=actual_tokens
            )

    async def release(self, reservation: BudgetReservation) -> None:
        """Roll back both the customer and global reservations
        (e.g., the LLM call crashed)."""
        if not reservation.allowed:
            return
        if reservation.customer_reservation:
            await self._limiter.release(reservation.customer_reservation)
        if reservation.global_reservation:
            await self._limiter.release(reservation.global_reservation)

    async def utilization(self, customer_id: Optional[UUID | str] = None) -> dict:
        """Snapshot of how much of the budget is in use right now —
        used by the backpressure controller and observability dashboards.

        Triggers a tiny zero-cost reservation against each bucket
        purely to read the current usage; the limiter returns the
        figures regardless of allow/deny.
        """
        # Use estimate=0 so a read never consumes capacity.
        global_snapshot = await self._limiter.check_and_reserve(
            self.GLOBAL_BUCKET,
            tpm_limit=self._global_tpm,
            rpm_limit=self._global_rpm * 1000,
            estimated_tokens=0,
        )
        result = {
            "global_tpm": global_snapshot.current_tpm,
            "global_rpm": global_snapshot.current_rpm,
            "global_tpm_limit": self._global_tpm,
            "global_rpm_limit": self._global_rpm,
            "global_utilization": global_snapshot.current_tpm / max(self._global_tpm, 1),
        }
        if customer_id is not None:
            budget = await self._load_budget(customer_id)
            cust_snapshot = await self._limiter.check_and_reserve(
                f"cust:{budget.customer_id}",
                tpm_limit=budget.reserved_tpm + budget.burst_tpm,
                rpm_limit=max(budget.reserved_rpm, 1) * 1000,
                estimated_tokens=0,
            )
            result.update({
                "customer_tpm": cust_snapshot.current_tpm,
                "customer_rpm": cust_snapshot.current_rpm,
                "customer_reserved_tpm": budget.reserved_tpm,
                "customer_burst_tpm": budget.burst_tpm,
            })
        return result
