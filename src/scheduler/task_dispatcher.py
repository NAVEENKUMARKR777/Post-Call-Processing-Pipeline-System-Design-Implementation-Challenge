"""Durable processing-task dispatcher.

The processing_tasks table is the workflow's source of truth (see
migration 001 and src/models/processing_task.py). This module is
the layer that:

  - enqueues tasks atomically with their parent interaction
    (claim_or_enqueue / enqueue_step)
  - claims due tasks via SELECT ... FOR UPDATE SKIP LOCKED with a
    lease window so a crashed worker's row is picked up by another
  - marks tasks completed / failed / dead-lettered, advancing the
    audit log on every transition
  - schedules retries with exponential backoff respecting Retry-After
    style hints

These primitives are what the worker tasks (recording_worker,
llm_worker, signal_worker, lead_stage_worker, crm_worker) layer
on top of. The workers themselves are kept thin so the durable
state-machine logic stays in one place.

Locking model:

  Worker A runs:
    SELECT * FROM processing_tasks
     WHERE status IN ('pending','scheduled')
       AND lane = ANY(:lanes)
       AND next_run_at <= NOW()
       AND (locked_until IS NULL OR locked_until < NOW())
     ORDER BY next_run_at ASC
     LIMIT :n
     FOR UPDATE SKIP LOCKED

  ... then immediately UPDATEs locked_by/locked_until/status='in_progress'
  and commits, releasing the row-level lock. The lease (locked_until)
  is the safety net: if Worker A then dies before completing, no
  other worker will see the row again until lease expires (~30s)
  and then it becomes claimable again.
"""

from __future__ import annotations

import enum
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional, Sequence
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.processing_task import (
    ProcessingTask,
    TaskLane,
    TaskStatus,
    TaskStep,
)
from src.observability import get_correlation_id, get_logger
from src.services.audit_logger import record_event

logger = get_logger(__name__)


# Backoff schedule (seconds) for the LLM-analysis path. Recording
# poll uses its own schedule defined alongside the recording worker.
DEFAULT_BACKOFF_SCHEDULE: tuple[int, ...] = (5, 15, 60, 180, 300, 600)
DEFAULT_LEASE_SECONDS = 30
DEFAULT_RETRY_JITTER_PCT = 10  # +/- 10% spread to avoid herd retries


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class WorkerIdentity:
    """Stable id of the worker process; written into locked_by so a
    surviving worker can tell whether a stale lease is its own."""
    name: str

    @classmethod
    def for_process(cls) -> "WorkerIdentity":
        return cls(name=f"{os.getenv('HOSTNAME', 'host')}:{os.getpid()}")


class TaskOutcome(str, enum.Enum):
    SUCCESS = "success"
    RETRY = "retry"
    FAIL = "fail"


def _next_run_at(attempt: int, *, schedule: Sequence[int] = DEFAULT_BACKOFF_SCHEDULE) -> datetime:
    """Pick the next-attempt delay from the schedule, falling back to
    the last entry if attempts exceed the schedule length. Adds a
    small random jitter so a synchronised wave of retries doesn't
    re-converge on the same wall-clock instant.

    Determinism isn't required here — log the actual next_run_at on
    write for traceability.
    """
    import random

    base = schedule[min(max(attempt, 0), len(schedule) - 1)]
    jitter = base * (random.uniform(-DEFAULT_RETRY_JITTER_PCT, DEFAULT_RETRY_JITTER_PCT) / 100)
    return _utcnow() + timedelta(seconds=max(1, int(base + jitter)))


async def enqueue_step(
    session: AsyncSession,
    *,
    interaction_id: UUID | str,
    customer_id: UUID | str,
    campaign_id: UUID | str,
    correlation_id: Optional[str],
    step: TaskStep,
    lane: TaskLane,
    payload: Optional[dict[str, Any]] = None,
    estimated_tokens: int = 0,
    next_run_at: Optional[datetime] = None,
    max_attempts: int = 6,
) -> ProcessingTask:
    """Insert (or no-op-update) a processing_tasks row.

    Idempotent: the (interaction_id, step) UNIQUE constraint means
    that double-enqueue is a no-op even under concurrent endpoint
    calls. Returns the existing row if one was already there.
    """
    cid = correlation_id or get_correlation_id() or uuid4().hex

    stmt = select(ProcessingTask).where(
        and_(
            ProcessingTask.interaction_id == _coerce_uuid(interaction_id),
            ProcessingTask.step == step.value,
        )
    )
    existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing is not None:
        return existing

    task = ProcessingTask(
        interaction_id=_coerce_uuid(interaction_id),
        customer_id=_coerce_uuid(customer_id),
        campaign_id=_coerce_uuid(campaign_id),
        correlation_id=cid,
        step=step.value,
        lane=lane.value,
        status=TaskStatus.PENDING.value,
        next_run_at=next_run_at or _utcnow(),
        payload=payload or {},
        estimated_tokens=estimated_tokens,
        max_attempts=max_attempts,
    )
    session.add(task)
    await session.flush()

    await record_event(
        session,
        interaction_id=interaction_id,
        customer_id=customer_id,
        correlation_id=cid,
        step=step.value,
        status="enqueued",
        attempt=0,
        detail={"lane": lane.value, "estimated_tokens": estimated_tokens},
    )

    logger.info(
        "task_enqueued",
        extra={
            "interaction_id": str(interaction_id),
            "step": step.value,
            "lane": lane.value,
            "estimated_tokens": estimated_tokens,
        },
    )
    return task


async def claim_due_tasks(
    session: AsyncSession,
    *,
    worker: WorkerIdentity,
    lanes: Iterable[TaskLane],
    limit: int = 10,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> list[ProcessingTask]:
    """Claim up to `limit` due tasks atomically.

    Uses SELECT ... FOR UPDATE SKIP LOCKED so concurrent workers
    never see the same row. The lease (`locked_until`) is set in
    the same transaction so a worker that crashes after this call
    cannot orphan the row indefinitely.
    """
    now = _utcnow()
    lease_until = now + timedelta(seconds=lease_seconds)
    lane_values = [lane.value for lane in lanes]

    stmt = (
        select(ProcessingTask)
        .where(
            and_(
                ProcessingTask.status.in_(
                    [TaskStatus.PENDING.value, TaskStatus.SCHEDULED.value]
                ),
                ProcessingTask.lane.in_(lane_values),
                ProcessingTask.next_run_at <= now,
                or_(
                    ProcessingTask.locked_until.is_(None),
                    ProcessingTask.locked_until < now,
                ),
            )
        )
        .order_by(ProcessingTask.next_run_at.asc())
        .limit(limit)
        .with_for_update(skip_locked=True)
    )

    rows = (await session.execute(stmt)).scalars().all()
    if not rows:
        return []

    claimed: list[ProcessingTask] = []
    for row in rows:
        row.status = TaskStatus.IN_PROGRESS.value
        row.attempts = (row.attempts or 0) + 1
        row.locked_by = worker.name
        row.locked_until = lease_until
        await session.flush()

        await record_event(
            session,
            interaction_id=row.interaction_id,
            customer_id=row.customer_id,
            correlation_id=row.correlation_id,
            step=row.step,
            status="in_progress",
            attempt=row.attempts,
            detail={"locked_by": worker.name, "lease_seconds": lease_seconds},
        )
        claimed.append(row)

    logger.info(
        "tasks_claimed",
        extra={
            "worker": worker.name,
            "count": len(claimed),
            "lanes": lane_values,
        },
    )
    return claimed


async def mark_in_progress(
    session: AsyncSession,
    task: ProcessingTask,
    *,
    worker_name: str = "celery",
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> None:
    """Transition a task to in_progress and emit the audit row.

    Called by the Celery harness before handing off to the executor so
    the audit trail always shows enqueued → in_progress → completed,
    matching the polling-worker path in claim_due_tasks.
    """
    now = _utcnow()
    task.status = TaskStatus.IN_PROGRESS.value
    task.attempts = (task.attempts or 0) + 1
    task.locked_by = worker_name
    task.locked_until = now + timedelta(seconds=lease_seconds)
    await session.flush()

    await record_event(
        session,
        interaction_id=task.interaction_id,
        customer_id=task.customer_id,
        correlation_id=task.correlation_id,
        step=task.step,
        status="in_progress",
        attempt=task.attempts,
        detail={"locked_by": worker_name, "lease_seconds": lease_seconds},
    )


async def mark_completed(
    session: AsyncSession,
    task: ProcessingTask,
    *,
    detail: Optional[dict[str, Any]] = None,
    actual_tokens: Optional[int] = None,
) -> None:
    task.status = TaskStatus.COMPLETED.value
    task.completed_at = _utcnow()
    task.locked_by = None
    task.locked_until = None
    task.last_error = None
    if actual_tokens is not None:
        task.actual_tokens = actual_tokens
    await session.flush()

    await record_event(
        session,
        interaction_id=task.interaction_id,
        customer_id=task.customer_id,
        correlation_id=task.correlation_id,
        step=task.step,
        status="completed",
        attempt=task.attempts,
        detail=detail or {},
    )


async def mark_for_retry(
    session: AsyncSession,
    task: ProcessingTask,
    *,
    error: str,
    retry_after_ms: Optional[int] = None,
    backoff_schedule: Sequence[int] = DEFAULT_BACKOFF_SCHEDULE,
    detail: Optional[dict[str, Any]] = None,
) -> None:
    """Schedule a retry. If retry_after_ms is provided (e.g., the
    rate limiter calculated when capacity frees up, or a 429
    Retry-After header was returned), it overrides the backoff
    schedule. Otherwise we use the schedule indexed by attempt
    count.

    If the task has run out of attempts, dead-letter it instead.
    """
    if task.attempts >= task.max_attempts:
        await mark_dead_letter(session, task, error=error, detail=detail)
        return

    if retry_after_ms is not None and retry_after_ms > 0:
        delay_seconds = max(1, retry_after_ms // 1000)
        task.next_run_at = _utcnow() + timedelta(seconds=delay_seconds)
    else:
        task.next_run_at = _next_run_at(task.attempts, schedule=backoff_schedule)

    task.status = TaskStatus.SCHEDULED.value
    task.last_error = error
    task.locked_by = None
    task.locked_until = None
    await session.flush()

    await record_event(
        session,
        interaction_id=task.interaction_id,
        customer_id=task.customer_id,
        correlation_id=task.correlation_id,
        step=task.step,
        status="retry_scheduled",
        attempt=task.attempts,
        detail={
            "error": error,
            "next_run_at": task.next_run_at.isoformat(),
            "retry_after_ms": retry_after_ms,
            **(detail or {}),
        },
    )


async def mark_dead_letter(
    session: AsyncSession,
    task: ProcessingTask,
    *,
    error: str,
    detail: Optional[dict[str, Any]] = None,
) -> None:
    """Move the task to the dead-letter status. The row is preserved;
    an admin can replay it. This is the README's "no analysis result
    may be permanently lost" constraint in code form."""
    task.status = TaskStatus.DEAD_LETTER.value
    task.last_error = error
    task.locked_by = None
    task.locked_until = None
    await session.flush()

    await record_event(
        session,
        interaction_id=task.interaction_id,
        customer_id=task.customer_id,
        correlation_id=task.correlation_id,
        step=task.step,
        status="dead_letter",
        attempt=task.attempts,
        detail={"error": error, **(detail or {})},
    )

    logger.error(
        "task_dead_letter",
        extra={
            "interaction_id": str(task.interaction_id),
            "step": task.step,
            "lane": task.lane,
            "attempts": task.attempts,
            "error": error,
        },
    )


async def expire_stale_leases(session: AsyncSession) -> int:
    """Sweep job: any in_progress row whose lease is older than now
    is rescheduled. Run on a periodic timer (Celery Beat / cron) so
    a worker that vanishes without a clean exit can't permanently
    park a row.

    Returns the number of rows reclaimed.
    """
    now = _utcnow()
    stmt = (
        update(ProcessingTask)
        .where(
            and_(
                ProcessingTask.status == TaskStatus.IN_PROGRESS.value,
                ProcessingTask.locked_until < now,
            )
        )
        .values(
            status=TaskStatus.SCHEDULED.value,
            locked_by=None,
            locked_until=None,
            next_run_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    result = await session.execute(stmt)
    count = result.rowcount or 0
    if count:
        logger.warning(
            "stale_leases_reclaimed",
            extra={"count": count},
        )
    return count


def _coerce_uuid(value):
    if value is None or isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (ValueError, TypeError):
        return value
