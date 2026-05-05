"""Append-only audit log of per-step state transitions.

The README requires that an on-call engineer be able to reconstruct what
happened to a specific interaction 3 days later. The processing_tasks
row only carries the *current* state; this table carries the timeline.
Every transition (pending -> in_progress, success, failure, retry, etc.)
emits one row.

This is intentionally a separate table from processing_tasks so that
the audit history survives even if a task row is later deleted or
overwritten.
"""

from sqlalchemy import BigInteger, Column, DateTime, Integer, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID

from src.models.base import Base


class InteractionAuditLog(Base):
    __tablename__ = "interaction_audit_log"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    interaction_id = Column(UUID(as_uuid=True), nullable=False)
    customer_id = Column(UUID(as_uuid=True))
    correlation_id = Column(Text, nullable=False)

    step = Column(Text, nullable=False)
    status = Column(Text, nullable=False)
    attempt = Column(Integer)
    detail = Column(JSONB, nullable=False, default=dict)

    occurred_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
