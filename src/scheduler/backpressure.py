"""Proportional backpressure controller.

Replaces the binary circuit breaker (src/services/circuit_breaker.py)
that halted ALL outbound dialling for a fixed half-hour window the
moment LLM utilisation crossed 90%. The README explicitly flags
that as a cascading failure pattern: "the sales team noticed before
the engineers did."

This controller exposes a `recommend()` call that returns a
throttle factor in [0.0, 1.0] based on current utilisation.

Curve:

  utilisation < 0.6      throttle = 1.0   (full speed)
  0.6 ≤ utilisation < 0.8  linear ramp 1.0 → 0.6 (soft slowdown)
  0.8 ≤ utilisation < 0.95 linear ramp 0.6 → 0.2 (hard slowdown;
                                              cold lane defers)
  utilisation ≥ 0.95     throttle = 0.0   for cold lane;
                          hot calls still flow because they consume
                          a customer's RESERVED budget rather than
                          shared headroom.

The controller never freezes anything for a fixed period; the
throttle factor recomputes every read against current utilisation.
A spike that drains in 30 seconds releases backpressure in 30
seconds. AC7 covered.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Optional

from src.observability import get_logger
from src.scheduler.budget_manager import BudgetManager

logger = get_logger(__name__)


class Lane(str, enum.Enum):
    HOT = "hot"
    COLD = "cold"


@dataclass(frozen=True)
class BackpressureRecommendation:
    throttle_factor: float
    reason: str
    utilisation: float
    customer_id: Optional[str]
    lane: Lane


# Curve breakpoints. Tuning lives in code (not config) intentionally
# — these inflection points should change only after analysis, never
# in response to a paging incident.
_FULL_SPEED_BELOW = 0.60
_SOFT_THROTTLE_BELOW = 0.80
_HARD_THROTTLE_BELOW = 0.95


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def _piecewise_throttle(util: float) -> tuple[float, str]:
    """Implements the proportional curve. Returns (throttle, reason)."""
    if util < _FULL_SPEED_BELOW:
        return 1.0, "headroom_available"
    if util < _SOFT_THROTTLE_BELOW:
        # 1.0 at 0.6 → 0.6 at 0.8: linear interpolation.
        progress = (util - _FULL_SPEED_BELOW) / (_SOFT_THROTTLE_BELOW - _FULL_SPEED_BELOW)
        throttle = 1.0 - 0.4 * progress
        return throttle, "soft_throttle"
    if util < _HARD_THROTTLE_BELOW:
        progress = (util - _SOFT_THROTTLE_BELOW) / (_HARD_THROTTLE_BELOW - _SOFT_THROTTLE_BELOW)
        throttle = 0.6 - 0.4 * progress
        return throttle, "hard_throttle"
    return 0.0, "saturated"


class BackpressureController:
    """Reads the budget manager's utilisation snapshot and applies
    the curve. Lane-aware: hot calls still flow even at 95%
    utilisation because they consume reserved budget — that's the
    whole point of the reservation contract."""

    def __init__(self, budget_manager: BudgetManager):
        self._budgets = budget_manager

    async def recommend(
        self,
        *,
        customer_id: Optional[str] = None,
        lane: Lane | str = Lane.COLD,
    ) -> BackpressureRecommendation:
        if isinstance(lane, str):
            lane = Lane(lane)

        util = await self._budgets.utilization(customer_id=customer_id)
        global_util = float(util.get("global_utilization", 0.0))
        global_util = _clamp(global_util, 0.0, 10.0)  # over-1.0 is pathological but possible briefly

        throttle, reason = _piecewise_throttle(global_util)

        # Hot calls bypass cold-lane saturation. They still defer if
        # the CUSTOMER's bucket itself is exhausted (handled by the
        # budget manager downstream); this controller only governs
        # fan-in throttling at the dialler.
        if lane is Lane.HOT and global_util >= _HARD_THROTTLE_BELOW:
            throttle = max(throttle, 0.5)
            reason = "hot_lane_protected"

        rec = BackpressureRecommendation(
            throttle_factor=_clamp(throttle, 0.0, 1.0),
            reason=reason,
            utilisation=global_util,
            customer_id=customer_id,
            lane=lane,
        )
        logger.info(
            "backpressure_recommendation",
            extra={
                "throttle_factor": rec.throttle_factor,
                "reason": rec.reason,
                "utilisation": rec.utilisation,
                "customer_id": rec.customer_id,
                "lane": rec.lane.value,
            },
        )
        return rec
