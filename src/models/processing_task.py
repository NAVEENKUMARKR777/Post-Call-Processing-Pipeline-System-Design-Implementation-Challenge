"""Durable processing-task ledger — the source of truth that replaces
the ephemeral Celery+Redis pair for workflow state.

Each (interaction, step) pair gets exactly one row. Workers claim rows
by atomically updating `locked_by` + `locked_until` under
`SELECT ... FOR UPDATE SKIP LOCKED`. A crashed worker's lease expires
and another worker picks it up — no work is lost.

Idempotency: the (interaction_id, step) UNIQUE constraint means a
duplicate enqueue is a no-op at the table level.
"""

import enum
import uuid

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from src.models.base import Base


class TaskStep(str, enum.Enum):
    RECORDING_POLL = "recording_poll"
    LLM_ANALYSIS = "llm_analysis"
    SIGNAL_JOBS = "signal_jobs"
    LEAD_STAGE = "lead_stage"
    CRM_PUSH = "crm_push"


class TaskLane(str, enum.Enum):
    HOT = "hot"
    COLD = "cold"
    SKIP = "skip"


class TaskStatus(str, enum.Enum):
    PENDING = "pending"
    SCHEDULED = "scheduled"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"


class ProcessingTask(Base):
    __tablename__ = "processing_tasks"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    interaction_id = Column(
        UUID(as_uuid=True),
        ForeignKey("interactions.id", ondelete="CASCADE"),
        nullable=False,
    )
    customer_id = Column(UUID(as_uuid=True), nullable=False)
    campaign_id = Column(UUID(as_uuid=True), nullable=False)
    correlation_id = Column(Text, nullable=False)

    step = Column(String(64), nullable=False)
    lane = Column(String(16), nullable=False, default=TaskLane.COLD.value)
    status = Column(String(16), nullable=False, default=TaskStatus.PENDING.value)

    attempts = Column(Integer, nullable=False, default=0)
    max_attempts = Column(Integer, nullable=False, default=6)
    next_run_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_error = Column(Text)
    payload = Column(JSONB, nullable=False, default=dict)

    estimated_tokens = Column(Integer, nullable=False, default=0)
    actual_tokens = Column(Integer)

    locked_by = Column(Text)
    locked_until = Column(DateTime(timezone=True))
    completed_at = Column(DateTime(timezone=True))

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint("interaction_id", "step", name="processing_tasks_step_unique"),
        CheckConstraint("lane IN ('hot', 'cold', 'skip')", name="processing_tasks_lane_check"),
        CheckConstraint(
            "status IN ('pending', 'scheduled', 'in_progress', "
            "'completed', 'failed', 'dead_letter')",
            name="processing_tasks_status_check",
        ),
    )
