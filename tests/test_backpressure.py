"""Backpressure controller tests — AC7.

AC7: "Dialler is not binary-frozen when LLM is under load — design
doc + code: no hardcoded 1800s freeze; backpressure is proportional."

The previous circuit_breaker froze dialling for 1800 seconds the
moment utilisation hit 90%. This test file verifies the new curve
is proportional (smooth ramp, no cliff), reads dynamically from
current utilisation, and protects the hot lane even at saturation.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.scheduler.backpressure import (
    BackpressureController,
    Lane,
    _piecewise_throttle,
)


def test_curve_starts_at_full_speed_below_60_percent():
    assert _piecewise_throttle(0.0)[0] == 1.0
    assert _piecewise_throttle(0.30)[0] == 1.0
    assert _piecewise_throttle(0.59)[0] == 1.0


def test_curve_soft_throttle_band_60_to_80_percent():
    """Linear ramp from 1.0 down to 0.6. No cliffs."""
    a = _piecewise_throttle(0.60)[0]
    b = _piecewise_throttle(0.70)[0]
    c = _piecewise_throttle(0.79)[0]
    assert 1.0 >= a > b > c >= 0.6
    # Reason is 'soft_throttle' across the band.
    assert _piecewise_throttle(0.65)[1] == "soft_throttle"


def test_curve_hard_throttle_band_80_to_95_percent():
    a = _piecewise_throttle(0.80)[0]
    b = _piecewise_throttle(0.85)[0]
    c = _piecewise_throttle(0.94)[0]
    assert 0.6 >= a > b > c >= 0.2
    assert _piecewise_throttle(0.85)[1] == "hard_throttle"


def test_curve_zeros_above_95_percent():
    assert _piecewise_throttle(0.95)[0] == 0.0
    assert _piecewise_throttle(0.99)[0] == 0.0
    assert _piecewise_throttle(1.0)[0] == 0.0
    assert _piecewise_throttle(2.0)[0] == 0.0


def test_no_hardcoded_1800s_freeze_anywhere():
    """The README explicitly calls out the 1800-second freeze as
    the failure mode to eliminate. Verify nothing in the module
    references that magic number."""
    import inspect

    from src.scheduler import backpressure
    src = inspect.getsource(backpressure)
    assert "1800" not in src
    assert "30 minutes" not in src.lower() or "freeze" not in src.lower()


@pytest.mark.asyncio
async def test_recommend_reads_live_utilisation():
    bm = MagicMock()
    bm.utilization = AsyncMock(return_value={"global_utilization": 0.45, "global_tpm": 4500})

    controller = BackpressureController(bm)
    rec = await controller.recommend(lane=Lane.COLD)

    assert rec.utilisation == pytest.approx(0.45)
    assert rec.throttle_factor == 1.0
    assert rec.reason == "headroom_available"


@pytest.mark.asyncio
async def test_hot_lane_bypasses_saturation():
    """Even when global util is 99%, hot calls still flow because
    they consume the customer's reserved budget — that is the
    point of the reservation contract."""
    bm = MagicMock()
    bm.utilization = AsyncMock(return_value={"global_utilization": 0.99})

    controller = BackpressureController(bm)
    cold = await controller.recommend(lane=Lane.COLD)
    hot = await controller.recommend(lane=Lane.HOT)

    assert cold.throttle_factor == 0.0
    assert cold.reason == "saturated"
    assert hot.throttle_factor >= 0.5
    assert hot.reason == "hot_lane_protected"


@pytest.mark.asyncio
async def test_recommend_recovers_immediately_when_utilisation_drops():
    """A spike that drains in seconds releases backpressure in
    seconds — the controller has no fixed-period freeze."""
    bm = MagicMock()
    util_value = {"v": 0.99}

    async def utilization(customer_id=None):
        return {"global_utilization": util_value["v"]}

    bm.utilization = utilization

    controller = BackpressureController(bm)
    saturated = await controller.recommend(lane=Lane.COLD)
    assert saturated.throttle_factor == 0.0

    # Util drops; the next recommendation reflects it.
    util_value["v"] = 0.30
    relaxed = await controller.recommend(lane=Lane.COLD)
    assert relaxed.throttle_factor == 1.0
