"""LLM token ledger — per-call attribution writer.

Every successful LLM call appends one row. This is the answer to
"how many tokens did Customer X spend on Campaign Y in the last
hour?" — a single SQL query (sum on the ledger), not a log grep.

Failed calls do not write to the ledger: tokens that were never
charged should not be counted. Reservations against the rate
limiter (which use *estimated* tokens) are tracked separately in
Redis and reconciled when the actual count comes back.
"""

from __future__ import annotations

from typing import Optional
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from src.models.token_ledger import LLMTokenLedger
from src.observability import get_correlation_id, get_logger

logger = get_logger(__name__)


def _coerce_uuid(value: Optional[str | UUID]) -> Optional[UUID]:
    if value is None or isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (ValueError, TypeError):
        return None


async def record_consumption(
    session: AsyncSession,
    *,
    customer_id: str | UUID,
    interaction_id: str | UUID,
    tokens_used: int,
    request_kind: str = "post_call_analysis",
    campaign_id: Optional[str | UUID] = None,
    model: Optional[str] = None,
    correlation_id: Optional[str] = None,
) -> LLMTokenLedger:
    """Append a ledger row for one LLM call.

    Caller owns the transaction so this row commits atomically with
    the worker's other writes (status update, audit-log row).
    """
    if tokens_used < 0:
        # Defensive: a negative reading from a buggy provider must
        # not be silently absorbed into the customer's bill.
        raise ValueError(f"tokens_used must be non-negative, got {tokens_used}")

    row = LLMTokenLedger(
        customer_id=_coerce_uuid(customer_id),
        campaign_id=_coerce_uuid(campaign_id),
        interaction_id=_coerce_uuid(interaction_id),
        request_kind=request_kind,
        tokens_used=tokens_used,
        model=model,
        correlation_id=correlation_id or get_correlation_id(),
    )
    session.add(row)
    await session.flush()

    logger.info(
        "llm_tokens_recorded",
        extra={
            "interaction_id": str(interaction_id),
            "customer_id": str(customer_id),
            "campaign_id": str(campaign_id) if campaign_id else None,
            "tokens_used": tokens_used,
            "request_kind": request_kind,
            "model": model,
        },
    )
    return row
