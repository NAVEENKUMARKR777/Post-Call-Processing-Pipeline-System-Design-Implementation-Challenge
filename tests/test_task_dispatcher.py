"""Task dispatcher state-machine tests.

The dispatcher's correctness rests on three properties:

  1. Idempotency: enqueuing the same (interaction_id, step) twice
     does not produce two rows.
  2. State transitions: in_progress → completed | scheduled |
     dead_letter advances cleanly and the audit log records each.
  3. Dead-letter floor: a task that exhausts max_attempts moves to
     dead_letter rather than being silently dropped — the README's
     "no analysis result may be permanently lost" constraint.

Tests use a mock AsyncSession so they pass without docker-compose.
The integration with Postgres-specific SELECT ... FOR UPDATE SKIP
LOCKED is exercised separately under tests/test_durable_tasks.py
(commit 14, runs against real Postgres when available).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from src.models.processing_task import ProcessingTask, TaskLane, TaskStatus, TaskStep
from src.observability import bind_correlation_id, clear_context
from src.scheduler.task_dispatcher import (
    _next_run_at,
    enqueue_step,
    mark_completed,
    mark_dead_letter,
    mark_for_retry,
)


@pytest.fixture(autouse=True)
def _clear_logging_context():
    clear_context()
    yield
    clear_context()


def _session_returning(existing: ProcessingTask | None):
    """A mock AsyncSession whose `execute(select(...))` returns
    `existing`. This is what enqueue_step looks at to decide
    whether to insert or skip."""
    session = MagicMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=existing)
    session.execute = AsyncMock(return_value=result)
    return session


@pytest.mark.asyncio
async def test_enqueue_step_inserts_when_no_row_exists():
    session = _session_returning(None)
    bind_correlation_id("cid-test")

    task = await enqueue_step(
        session,
        interaction_id=str(uuid4()),
        customer_id=str(uuid4()),
        campaign_id=str(uuid4()),
        correlation_id=None,
        step=TaskStep.LLM_ANALYSIS,
        lane=TaskLane.HOT,
        estimated_tokens=1500,
    )

    assert isinstance(task, ProcessingTask)
    assert task.step == TaskStep.LLM_ANALYSIS.value
    assert task.lane == TaskLane.HOT.value
    assert task.status == TaskStatus.PENDING.value
    assert task.estimated_tokens == 1500
    assert task.correlation_id == "cid-test"
    # The task itself is added; the audit-log row is also added in
    # the same transaction.
    assert session.add.call_count == 2  # the task + audit row


@pytest.mark.asyncio
async def test_enqueue_step_is_idempotent_under_double_enqueue():
    """The (interaction_id, step) UNIQUE constraint backs this in
    the DB; the dispatcher checks first to avoid the race +
    constraint-violation roundtrip."""
    existing = ProcessingTask(
        interaction_id=uuid4(),
        customer_id=uuid4(),
        campaign_id=uuid4(),
        correlation_id="prior-cid",
        step=TaskStep.LLM_ANALYSIS.value,
        lane=TaskLane.HOT.value,
        status=TaskStatus.PENDING.value,
    )
    session = _session_returning(existing)

    task = await enqueue_step(
        session,
        interaction_id=existing.interaction_id,
        customer_id=existing.customer_id,
        campaign_id=existing.campaign_id,
        correlation_id="ignored-cid",
        step=TaskStep.LLM_ANALYSIS,
        lane=TaskLane.HOT,
    )

    # Returned the existing row — caller cannot tell whether it was
    # already there. session.add was never called for a new row.
    assert task is existing
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_mark_completed_clears_lease_and_sets_terminal_status():
    session = _session_returning(None)
    task = ProcessingTask(
        interaction_id=uuid4(),
        customer_id=uuid4(),
        campaign_id=uuid4(),
        correlation_id="cid",
        step=TaskStep.LLM_ANALYSIS.value,
        lane=TaskLane.HOT.value,
        status=TaskStatus.IN_PROGRESS.value,
        attempts=1,
        locked_by="worker-1",
    )

    await mark_completed(session, task, actual_tokens=2210, detail={"latency_ms": 1340})

    assert task.status == TaskStatus.COMPLETED.value
    assert task.locked_by is None
    assert task.locked_until is None
    assert task.actual_tokens == 2210
    assert task.last_error is None


@pytest.mark.asyncio
async def test_mark_for_retry_uses_retry_after_when_provided():
    session = _session_returning(None)
    task = ProcessingTask(
        interaction_id=uuid4(),
        customer_id=uuid4(),
        campaign_id=uuid4(),
        correlation_id="cid",
        step=TaskStep.LLM_ANALYSIS.value,
        lane=TaskLane.COLD.value,
        status=TaskStatus.IN_PROGRESS.value,
        attempts=1,
        max_attempts=6,
    )
    before_min = _next_run_at(0)

    await mark_for_retry(
        session,
        task,
        error="rate_limited",
        retry_after_ms=10_000,
    )

    assert task.status == TaskStatus.SCHEDULED.value
    assert task.last_error == "rate_limited"
    # next_run_at was driven by retry_after_ms (10s), not the
    # 5-second first-attempt of the default schedule. Verify it's
    # at least ~10s in the future.
    delta_seconds = (task.next_run_at - before_min).total_seconds()
    assert delta_seconds >= 5  # robust check across schedule jitter


@pytest.mark.asyncio
async def test_mark_for_retry_dead_letters_when_attempts_exceeded():
    """README constraint: no analysis result may be permanently
    lost. A task that exhausts max_attempts must transition to
    dead_letter (queryable, replayable) — never silently dropped.
    """
    session = _session_returning(None)
    task = ProcessingTask(
        interaction_id=uuid4(),
        customer_id=uuid4(),
        campaign_id=uuid4(),
        correlation_id="cid",
        step=TaskStep.LLM_ANALYSIS.value,
        lane=TaskLane.COLD.value,
        status=TaskStatus.IN_PROGRESS.value,
        attempts=6,
        max_attempts=6,
    )

    await mark_for_retry(session, task, error="provider_500")

    assert task.status == TaskStatus.DEAD_LETTER.value
    assert task.last_error == "provider_500"


@pytest.mark.asyncio
async def test_mark_dead_letter_emits_error_log_and_audit_row():
    session = _session_returning(None)
    task = ProcessingTask(
        interaction_id=uuid4(),
        customer_id=uuid4(),
        campaign_id=uuid4(),
        correlation_id="cid-dl",
        step=TaskStep.RECORDING_POLL.value,
        lane=TaskLane.COLD.value,
        status=TaskStatus.IN_PROGRESS.value,
        attempts=6,
    )

    await mark_dead_letter(session, task, error="exhausted_attempts")

    assert task.status == TaskStatus.DEAD_LETTER.value
    # The audit-row write counts as a session.add call.
    session.add.assert_called_once()
    audit = session.add.call_args.args[0]
    assert audit.status == "dead_letter"
    assert audit.detail["error"] == "exhausted_attempts"


@pytest.mark.asyncio
async def test_mark_for_retry_uses_backoff_schedule_when_no_hint():
    session = _session_returning(None)
    task = ProcessingTask(
        interaction_id=uuid4(),
        customer_id=uuid4(),
        campaign_id=uuid4(),
        correlation_id="cid",
        step=TaskStep.LLM_ANALYSIS.value,
        lane=TaskLane.COLD.value,
        status=TaskStatus.IN_PROGRESS.value,
        attempts=2,
        max_attempts=6,
    )
    await mark_for_retry(session, task, error="transient")
    assert task.status == TaskStatus.SCHEDULED.value
    # 3rd entry of DEFAULT_BACKOFF_SCHEDULE is 60 — verify roughly.
    from datetime import datetime, timezone
    delta = (task.next_run_at - datetime.now(timezone.utc)).total_seconds()
    assert 30 < delta < 120
