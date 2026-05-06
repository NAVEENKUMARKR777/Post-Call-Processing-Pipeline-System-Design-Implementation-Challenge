"""Signal-jobs and lead-stage worker tests.

These two workers wrap simple downstream calls in the durable
processing_tasks workflow. The contract is that:

  - On success → task is COMPLETED, audit row written.
  - On exception → task is SCHEDULED for retry, audit row records
    the error, never silently swallowed.

The previous fire-and-forget asyncio.create_task() in the endpoint
let exceptions disappear; this layer enforces the README constraint
"every interaction has a complete audit trail from call-end to
final result".
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from src.models.processing_task import (
    ProcessingTask,
    TaskLane,
    TaskStatus,
    TaskStep,
)
from src.tasks.workers.lead_stage_worker import (
    execute_lead_stage,
    map_call_stage_to_lead_stage,
)
from src.tasks.workers.signal_worker import execute_signal_jobs


def _session():
    s = MagicMock()
    s.add = MagicMock()
    s.flush = AsyncMock()
    s.execute = AsyncMock()
    return s


def _task(step: TaskStep, payload: dict, attempts: int = 0) -> ProcessingTask:
    return ProcessingTask(
        id=uuid4(),
        interaction_id=uuid4(),
        customer_id=uuid4(),
        campaign_id=uuid4(),
        correlation_id="cid-w",
        step=step.value,
        lane=TaskLane.HOT.value,
        status=TaskStatus.IN_PROGRESS.value,
        attempts=attempts,
        max_attempts=6,
        payload=payload,
    )


# ──────────────────────────── signal-jobs ────────────────────────────


@pytest.mark.asyncio
async def test_signal_jobs_success_marks_completed():
    session = _session()
    task = _task(
        TaskStep.SIGNAL_JOBS,
        payload={
            "session_id": str(uuid4()),
            "analysis_result": {"call_stage": "rebook_confirmed"},
        },
    )

    with patch(
        "src.tasks.workers.signal_worker.trigger_signal_jobs",
        new=AsyncMock(),
    ):
        ok = await execute_signal_jobs(session, task)

    assert ok is True
    assert task.status == TaskStatus.COMPLETED.value


@pytest.mark.asyncio
async def test_signal_jobs_failure_schedules_retry_and_audits():
    session = _session()
    task = _task(
        TaskStep.SIGNAL_JOBS,
        payload={"analysis_result": {"call_stage": "demo_booked"}, "session_id": str(uuid4())},
    )

    with patch(
        "src.tasks.workers.signal_worker.trigger_signal_jobs",
        new=AsyncMock(side_effect=RuntimeError("downstream 500")),
    ):
        ok = await execute_signal_jobs(session, task)

    assert ok is False
    assert task.status == TaskStatus.SCHEDULED.value
    assert task.last_error == "downstream 500"


# ──────────────────────────── lead-stage ────────────────────────────


def test_map_call_stage_explicit_mappings():
    assert map_call_stage_to_lead_stage("rebook_confirmed") == "booked"
    assert map_call_stage_to_lead_stage("demo_booked") == "booked"
    assert map_call_stage_to_lead_stage("not_interested") == "closed_lost"
    assert map_call_stage_to_lead_stage("escalation_needed") == "escalation"
    assert map_call_stage_to_lead_stage("already_done") == "closed_won"
    assert map_call_stage_to_lead_stage("short_call") == "no_contact"


def test_map_call_stage_unknown_or_empty_falls_back_to_follow_up():
    """An LLM call_stage we haven't mapped lands in follow_up rather
    than no_contact or closed_lost. The conservative default keeps
    the lead in active outreach until a human classifies it."""
    assert map_call_stage_to_lead_stage(None) == "follow_up"
    assert map_call_stage_to_lead_stage("") == "follow_up"
    assert map_call_stage_to_lead_stage("brand_new_disposition") == "follow_up"


@pytest.mark.asyncio
async def test_lead_stage_success_marks_completed():
    session = _session()
    task = _task(
        TaskStep.LEAD_STAGE,
        payload={
            "lead_id": str(uuid4()),
            "analysis_result": {"call_stage": "rebook_confirmed"},
        },
    )

    with patch(
        "src.tasks.workers.lead_stage_worker.update_lead_stage",
        new=AsyncMock(),
    ):
        ok = await execute_lead_stage(session, task)

    assert ok is True
    assert task.status == TaskStatus.COMPLETED.value


@pytest.mark.asyncio
async def test_lead_stage_failure_schedules_retry():
    session = _session()
    task = _task(
        TaskStep.LEAD_STAGE,
        payload={
            "lead_id": str(uuid4()),
            "analysis_result": {"call_stage": "not_interested"},
        },
    )

    with patch(
        "src.tasks.workers.lead_stage_worker.update_lead_stage",
        new=AsyncMock(side_effect=Exception("DB write failed")),
    ):
        ok = await execute_lead_stage(session, task)

    assert ok is False
    assert task.status == TaskStatus.SCHEDULED.value
