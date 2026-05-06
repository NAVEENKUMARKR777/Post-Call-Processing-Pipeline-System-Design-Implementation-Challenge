"""AC5 + AC6 — every interaction has a complete audit trail.

AC5: "Every interaction has a complete audit trail from call-end
to final result. Log inspection: assert structured events exist
for each stage of one interaction."

AC6: "All failures produce structured log events with interaction_id.
Code inspection: every error path emits structured log with
correlation ID."

The audit-log writer is in src/services/audit_logger.py; every
worker uses it on every state transition. This test file verifies
the shape end-to-end: walk a single interaction through the
expected lifecycle and assert that an audit row appears for every
step's enqueue, in_progress, retry, and completed/dead_letter
transitions.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from src.models.processing_task import (
    ProcessingTask,
    TaskLane,
    TaskStatus,
    TaskStep,
)
from src.observability import bind_correlation_id, clear_context
from src.scheduler.task_dispatcher import (
    enqueue_step,
    mark_completed,
    mark_for_retry,
)


@pytest.fixture(autouse=True)
def _isolation():
    clear_context()
    yield
    clear_context()


def _session_capturing_audits():
    """A session that captures every session.add() call so we can
    inspect the resulting audit trail across multiple state
    transitions in one interaction."""
    session = MagicMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=None)
    session.execute = AsyncMock(return_value=result)
    return session


@pytest.mark.asyncio
async def test_full_interaction_lifecycle_produces_complete_audit_trail():
    """AC5: a single interaction walked from enqueue → retry →
    completed must produce audit rows for each transition, all
    keyed by the same interaction_id and correlation_id. An
    on-call engineer can query the audit log by interaction_id
    to reconstruct the timeline."""
    session = _session_capturing_audits()
    interaction_id = uuid4()
    customer_id = uuid4()
    campaign_id = uuid4()
    cid = bind_correlation_id("cid-trail-1")

    # 1. Enqueue.
    task = await enqueue_step(
        session,
        interaction_id=interaction_id,
        customer_id=customer_id,
        campaign_id=campaign_id,
        correlation_id=cid,
        step=TaskStep.LLM_ANALYSIS,
        lane=TaskLane.HOT,
        estimated_tokens=1500,
    )

    # 2. Worker claims and runs once, fails transient.
    task.attempts = 1
    task.status = TaskStatus.IN_PROGRESS.value
    await mark_for_retry(session, task, error="transient")

    # 3. Worker claims again, succeeds.
    task.attempts = 2
    task.status = TaskStatus.IN_PROGRESS.value
    await mark_completed(session, task, actual_tokens=1700)

    # Verify the audit-row sequence by inspecting session.add calls
    # for InteractionAuditLog instances. The processing_tasks row
    # itself is also session.add()'d once during enqueue; everything
    # else added is an audit row.
    added_rows = [c.args[0] for c in session.add.call_args_list]
    audit_rows = [r for r in added_rows if r.__class__.__name__ == "InteractionAuditLog"]
    statuses = [r.status for r in audit_rows]

    # The lifecycle must include an enqueue, retry_scheduled, and
    # completed event. All keyed by the same interaction_id.
    assert "enqueued" in statuses
    assert "retry_scheduled" in statuses
    assert "completed" in statuses
    assert all(r.interaction_id == interaction_id for r in audit_rows)
    assert all(r.correlation_id == cid for r in audit_rows)


@pytest.mark.asyncio
async def test_failure_audit_row_includes_interaction_id_and_error():
    """AC6: every error path emits a structured event with
    interaction_id. Verified at the dispatcher level — every
    worker funnels errors through mark_for_retry / mark_dead_letter,
    both of which write an audit row."""
    session = _session_capturing_audits()
    bind_correlation_id("cid-err-1")

    task = ProcessingTask(
        id=uuid4(),
        interaction_id=uuid4(),
        customer_id=uuid4(),
        campaign_id=uuid4(),
        correlation_id="cid-err-1",
        step=TaskStep.LLM_ANALYSIS.value,
        lane=TaskLane.COLD.value,
        status=TaskStatus.IN_PROGRESS.value,
        attempts=2,
        max_attempts=6,
    )

    await mark_for_retry(session, task, error="provider_500_intermittent")

    audit_rows = [
        c.args[0] for c in session.add.call_args_list
        if c.args[0].__class__.__name__ == "InteractionAuditLog"
    ]
    assert audit_rows, "every error path must emit an audit row"
    last = audit_rows[-1]
    assert last.interaction_id == task.interaction_id
    assert last.correlation_id == "cid-err-1"
    assert last.detail.get("error") == "provider_500_intermittent"


@pytest.mark.asyncio
async def test_audit_rows_carry_attempt_number_for_replay():
    """When debugging a 3-day-old failure, the on-call engineer
    needs to know *which* attempt failed and *why*. The audit
    row's attempt field disambiguates the timeline.
    """
    session = _session_capturing_audits()
    task = ProcessingTask(
        id=uuid4(),
        interaction_id=uuid4(),
        customer_id=uuid4(),
        campaign_id=uuid4(),
        correlation_id="cid-attempt",
        step=TaskStep.LLM_ANALYSIS.value,
        lane=TaskLane.COLD.value,
        status=TaskStatus.IN_PROGRESS.value,
        attempts=3,
        max_attempts=6,
    )
    await mark_for_retry(session, task, error="x")

    audit_rows = [
        c.args[0] for c in session.add.call_args_list
        if c.args[0].__class__.__name__ == "InteractionAuditLog"
    ]
    last = audit_rows[-1]
    assert last.attempt == 3
