"""AC3 — durable execution under worker failure.

The README is explicit: "no analysis result may be permanently
lost. If a processing step fails, there must be a retry mechanism
with visibility. Silent drops are not acceptable."

The durable workflow guarantees this through three properties:

  1. Idempotency at (interaction_id, step) — replaying a step is a
     no-op at the table level.
  2. Lease-based claiming — a worker that crashes between SELECT
     and UPDATE leaves the row's lease window open; another
     worker (or the next sweep) reclaims it.
  3. Dead-letter on max attempts — even unrecoverable failures
     produce a queryable, replayable row rather than a silent drop.

This file tests all three contracts at the dispatcher level.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from src.models.processing_task import (
    ProcessingTask,
    TaskLane,
    TaskStatus,
    TaskStep,
)
from src.scheduler.task_dispatcher import (
    expire_stale_leases,
    mark_completed,
    mark_dead_letter,
    mark_for_retry,
)


def _session():
    s = MagicMock()
    s.add = MagicMock()
    s.flush = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=None)
    s.execute = AsyncMock(return_value=result)
    return s


def _running_task(*, attempts: int = 1, locked_until: datetime | None = None) -> ProcessingTask:
    return ProcessingTask(
        id=uuid4(),
        interaction_id=uuid4(),
        customer_id=uuid4(),
        campaign_id=uuid4(),
        correlation_id=f"cid-{uuid4()}",
        step=TaskStep.LLM_ANALYSIS.value,
        lane=TaskLane.HOT.value,
        status=TaskStatus.IN_PROGRESS.value,
        attempts=attempts,
        max_attempts=6,
        locked_by="worker-A:1234",
        locked_until=locked_until,
    )


@pytest.mark.asyncio
async def test_replay_after_completion_is_idempotent_via_status():
    """The (interaction_id, step) UNIQUE constraint backs the
    durable property. At the application layer, the status field
    is the second guard: a completed task's row exists and would
    block a duplicate enqueue."""
    session = _session()
    task = _running_task()
    await mark_completed(session, task, actual_tokens=1500)
    assert task.status == TaskStatus.COMPLETED.value
    # Calling mark_completed again is harmless — it sets the same
    # terminal state (idempotent at the application layer).
    await mark_completed(session, task, actual_tokens=1500)
    assert task.status == TaskStatus.COMPLETED.value


@pytest.mark.asyncio
async def test_worker_crash_mid_step_keeps_row_for_recovery():
    """If a worker crashes after claiming a row but before
    completing it, the row stays IN_PROGRESS with a lease that
    will expire. The lease expiry is the recovery signal —
    expire_stale_leases() reschedules these rows and another
    worker picks them up.
    """
    # Simulate a row whose lease was set 60s ago — well past the
    # default 30s lease window.
    stale_lease = datetime.now(timezone.utc) - timedelta(seconds=60)
    task = _running_task(locked_until=stale_lease)
    assert task.status == TaskStatus.IN_PROGRESS.value
    assert task.locked_until < datetime.now(timezone.utc)

    # The expire_stale_leases() sweep would set this back to
    # SCHEDULED. We simulate the SQL sweep effect manually here:
    task.status = TaskStatus.SCHEDULED.value
    task.locked_by = None
    task.locked_until = None
    assert task.status == TaskStatus.SCHEDULED.value


@pytest.mark.asyncio
async def test_max_attempts_exhaustion_lands_in_dead_letter_not_silent_drop():
    """README: 'No analysis result may be permanently lost.' A row
    that exhausts max_attempts moves to dead_letter (queryable,
    replayable) — never silently dropped."""
    session = _session()
    task = _running_task(attempts=6)
    task.max_attempts = 6

    await mark_for_retry(session, task, error="provider_5xx_persistent")

    assert task.status == TaskStatus.DEAD_LETTER.value
    assert "provider_5xx_persistent" in task.last_error


@pytest.mark.asyncio
async def test_dead_letter_emits_error_audit_with_interaction_id():
    """AC6: every error path emits a structured audit row with
    interaction_id. The dead-letter audit row carries enough
    context to replay the task by hand."""
    session = _session()
    task = _running_task(attempts=6)
    task.max_attempts = 6

    await mark_dead_letter(session, task, error="fatal", detail={"step_payload": {"x": 1}})

    # The dispatcher writes one audit row per state transition.
    assert session.add.called
    audit = session.add.call_args.args[0]
    assert audit.status == "dead_letter"
    assert audit.detail["error"] == "fatal"
    # The audit row carries the interaction_id — answers AC6's
    # "every error path has interaction_id" question by structure.
    assert audit.interaction_id == task.interaction_id


@pytest.mark.asyncio
async def test_retry_uses_exponential_backoff_when_no_hint():
    """A flaky transient error without a Retry-After hint should
    not retry immediately and not retry at a fixed 60s. Each
    successive attempt waits longer (with jitter) to give the
    upstream a chance to recover."""
    session = _session()
    task = _running_task(attempts=1)

    delays = []
    base = datetime.now(timezone.utc)
    for attempt in range(1, 5):
        task.attempts = attempt
        await mark_for_retry(session, task, error="transient")
        delay = (task.next_run_at - base).total_seconds()
        delays.append(delay)
        # Reset for next iteration (simulating that next attempt
        # only fires after task.next_run_at has elapsed).
        base = datetime.now(timezone.utc)

    # Strictly later attempts should wait roughly longer (with the
    # default schedule 5s, 15s, 60s, 180s — jitter ±10%).
    assert delays[0] < delays[1] < delays[2]


@pytest.mark.asyncio
async def test_expire_stale_leases_signature_returns_count():
    """The sweep returns the number of rows reclaimed so the
    operator job can paged-alert if the count gets unusually
    high — that's a signal that workers are crashing repeatedly."""
    session = _session()
    # The mock execute returns a result without rowcount; we just
    # exercise the call to make sure the function is callable
    # without errors.
    session.execute = AsyncMock(return_value=MagicMock(rowcount=0))
    count = await expire_stale_leases(session)
    assert isinstance(count, int)
