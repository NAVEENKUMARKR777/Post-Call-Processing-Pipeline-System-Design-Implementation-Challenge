"""Audit-log service.

Writes one row to interaction_audit_log per state transition. The
processing_tasks row carries the *current* state of a step; this
table carries the *timeline*. They are deliberately separate so
the audit history survives even if a task row is later deleted or
overwritten.

Every write also emits a structured log line so on-call engineers
have two places to look (the table for queryable history, the log
stream for live debugging) and they always agree.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from src.models.audit_log import InteractionAuditLog
from src.observability import get_correlation_id, get_logger

logger = get_logger(__name__)


def _coerce_uuid(value: Optional[str | UUID]) -> Optional[UUID]:
    if value is None or isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (ValueError, TypeError):
        return None


async def record_event(
    session: AsyncSession,
    *,
    interaction_id: str | UUID,
    step: str,
    status: str,
    customer_id: Optional[str | UUID] = None,
    correlation_id: Optional[str] = None,
    attempt: Optional[int] = None,
    detail: Optional[Mapping[str, Any]] = None,
) -> InteractionAuditLog:
    """Append one audit-log row and emit a paired structured log line.

    The caller owns the transaction. We do not commit — that's the
    caller's responsibility so the audit row commits atomically with
    whatever change it documents (e.g. status transition on
    processing_tasks). This avoids the audit-log-says-yes-table-says-no
    inconsistency that plagues fire-and-forget audit systems.
    """
    cid = correlation_id or get_correlation_id() or "unknown"
    payload = dict(detail or {})

    row = InteractionAuditLog(
        interaction_id=_coerce_uuid(interaction_id),
        customer_id=_coerce_uuid(customer_id),
        correlation_id=cid,
        step=step,
        status=status,
        attempt=attempt,
        detail=payload,
    )
    session.add(row)
    # Flush so the BIGSERIAL id is populated for callers that need it
    # (e.g., to log the audit row id) but do not commit — the caller's
    # outer transaction owns the commit boundary.
    await session.flush()

    logger.info(
        "audit_event",
        extra={
            "interaction_id": str(interaction_id),
            "step": step,
            "status": status,
            "attempt": attempt,
            "audit_id": row.id,
            **{k: v for k, v in payload.items() if k not in {"interaction_id", "step", "status"}},
        },
    )
    return row
