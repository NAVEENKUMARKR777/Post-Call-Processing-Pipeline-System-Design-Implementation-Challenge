"""Dialler-facing API: backpressure + admin endpoints.

The dialler is a separate service that owns the call dispatch loop.
Before placing a call, it polls /dialler/backpressure to learn the
current throttle factor — replacing the previous binary "circuit
breaker is open" signal that froze all dispatch for 30 minutes.

This module also exposes the admin endpoint for mutating
customer_llm_budgets at runtime (no deploy required) — see the
nice-to-have row in SUBMISSION.md §6.
"""

from __future__ import annotations

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.customer_budget import CustomerLLMBudget
from src.observability import get_logger
from src.scheduler.backpressure import BackpressureController, Lane
from src.utils.db import get_db

logger = get_logger(__name__)
router = APIRouter()


# Lazy global. The controller is instantiated by `get_controller`
# below so the API doesn't import the BudgetManager at module load
# time — that would create a circular dependency with the worker
# module that also constructs the manager.
_controller: Optional[BackpressureController] = None


def set_controller(controller: BackpressureController) -> None:
    global _controller
    _controller = controller


def get_controller() -> BackpressureController:
    if _controller is None:
        raise HTTPException(
            status_code=503,
            detail="backpressure controller not initialised",
        )
    return _controller


class BackpressureResponse(BaseModel):
    throttle_factor: float = Field(ge=0.0, le=1.0)
    reason: str
    utilisation: float
    customer_id: Optional[str] = None
    lane: str


@router.get("/dialler/backpressure", response_model=BackpressureResponse)
async def get_backpressure(
    customer_id: Optional[str] = None,
    lane: str = "cold",
    controller: BackpressureController = Depends(get_controller),
):
    """The dialler should query this before each batch of calls.

    Returns a throttle factor in [0.0, 1.0] that the dialler should
    multiply its baseline dispatch rate by. Hot-lane calls bypass
    saturation because they consume reserved capacity.
    """
    try:
        lane_enum = Lane(lane)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"unknown lane: {lane}")

    rec = await controller.recommend(customer_id=customer_id, lane=lane_enum)
    return BackpressureResponse(
        throttle_factor=rec.throttle_factor,
        reason=rec.reason,
        utilisation=rec.utilisation,
        customer_id=rec.customer_id,
        lane=rec.lane.value,
    )


class CustomerBudgetUpdate(BaseModel):
    reserved_tpm: int = Field(ge=0)
    reserved_rpm: int = Field(ge=0)
    burst_tpm: int = Field(ge=0)
    priority: int = Field(ge=0, le=10)


@router.put("/admin/customers/{customer_id}/budget")
async def upsert_customer_budget(
    customer_id: UUID,
    body: CustomerBudgetUpdate,
    session: AsyncSession = Depends(get_db),
):
    """Insert or update a customer's reservation contract.

    Auth is intentionally omitted in this assessment scope and
    flagged in SUBMISSION.md §11 — production would wrap this
    endpoint in a service-account-only middleware (mTLS or signed
    JWT). The point of the endpoint is to demonstrate that a
    customer's reservation can change at runtime without a deploy.
    """
    stmt = pg_insert(CustomerLLMBudget).values(
        customer_id=customer_id,
        reserved_tpm=body.reserved_tpm,
        reserved_rpm=body.reserved_rpm,
        burst_tpm=body.burst_tpm,
        priority=body.priority,
    ).on_conflict_do_update(
        index_elements=[CustomerLLMBudget.customer_id],
        set_={
            "reserved_tpm": body.reserved_tpm,
            "reserved_rpm": body.reserved_rpm,
            "burst_tpm": body.burst_tpm,
            "priority": body.priority,
        },
    )
    await session.execute(stmt)
    await session.commit()

    # Invalidate the controller's cached snapshot so the new values
    # take effect within the controller's polling interval.
    controller = get_controller()
    if controller is not None:
        controller._budgets.invalidate_cache(str(customer_id))

    logger.info(
        "customer_budget_updated",
        extra={
            "customer_id": str(customer_id),
            "reserved_tpm": body.reserved_tpm,
            "reserved_rpm": body.reserved_rpm,
            "burst_tpm": body.burst_tpm,
            "priority": body.priority,
        },
    )
    return {"status": "ok", "customer_id": str(customer_id)}
