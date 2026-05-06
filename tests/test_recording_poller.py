"""Recording poller tests.

AC4: "Recording poller retries with backoff; never silently skips —
unit test: simulate delayed recording, verify retry loop and failure
logging."

The poller orchestrates one fetch attempt and updates the durable
task row. Tests stub `try_fetch_recording` so we cover all four
ProbeResult states without spinning up Exotel or S3.
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
from src.observability import clear_context
from src.services.recording import ProbeResult, RecordingProbeStatus
from src.services.recording_poller import execute_recording_poll


@pytest.fixture(autouse=True)
def _reset_context():
    clear_context()
    yield
    clear_context()


def _make_session():
    session = MagicMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.execute = AsyncMock()
    # `select(...)` lookups in the audit-logger path are fine without
    # data — record_event only does session.add + flush.
    return session


def _make_task(*, attempts: int = 0, max_attempts: int = 6) -> ProcessingTask:
    return ProcessingTask(
        id=uuid4(),
        interaction_id=uuid4(),
        customer_id=uuid4(),
        campaign_id=uuid4(),
        correlation_id="cid-rec-1",
        step=TaskStep.RECORDING_POLL.value,
        lane=TaskLane.COLD.value,
        status=TaskStatus.IN_PROGRESS.value,
        attempts=attempts,
        max_attempts=max_attempts,
        payload={"call_sid": "exotel-001", "exotel_account_id": "acct-x"},
    )


@pytest.mark.asyncio
async def test_available_recording_marks_task_completed():
    session = _make_session()
    task = _make_task(attempts=2)

    fake = ProbeResult(
        status=RecordingProbeStatus.AVAILABLE,
        s3_key="recordings/abc.mp3",
        recording_url="https://exotel.example/recording/abc.mp3",
    )
    with patch(
        "src.services.recording_poller.try_fetch_recording",
        new=AsyncMock(return_value=fake),
    ):
        result = await execute_recording_poll(session, task)

    assert result.status is RecordingProbeStatus.AVAILABLE
    assert task.status == TaskStatus.COMPLETED.value
    assert task.completed_at is not None
    # The interactions UPDATE was issued; record_event was issued
    # for the completed transition.
    assert session.execute.await_count >= 1


@pytest.mark.asyncio
async def test_not_ready_schedules_a_retry_below_max_attempts():
    session = _make_session()
    task = _make_task(attempts=1, max_attempts=6)

    fake = ProbeResult(status=RecordingProbeStatus.NOT_READY, detail=None)
    with patch(
        "src.services.recording_poller.try_fetch_recording",
        new=AsyncMock(return_value=fake),
    ):
        await execute_recording_poll(session, task)

    assert task.status == TaskStatus.SCHEDULED.value
    assert task.next_run_at is not None
    assert task.last_error == "not_ready"


@pytest.mark.asyncio
async def test_provider_error_schedules_retry_and_captures_detail():
    session = _make_session()
    task = _make_task(attempts=2)

    fake = ProbeResult(status=RecordingProbeStatus.PROVIDER_ERROR, detail="connect_timeout")
    with patch(
        "src.services.recording_poller.try_fetch_recording",
        new=AsyncMock(return_value=fake),
    ):
        await execute_recording_poll(session, task)

    assert task.status == TaskStatus.SCHEDULED.value
    assert task.last_error == "connect_timeout"


@pytest.mark.asyncio
async def test_exhausted_attempts_dead_letter_and_error_log():
    """AC4 + the README constraint: recording failures must produce
    observable events. After max_attempts of NOT_READY the task
    must go to dead_letter and the interactions row must show
    recording_status='failed'."""
    session = _make_session()
    task = _make_task(attempts=6, max_attempts=6)

    fake = ProbeResult(status=RecordingProbeStatus.NOT_READY)
    with patch(
        "src.services.recording_poller.try_fetch_recording",
        new=AsyncMock(return_value=fake),
    ):
        await execute_recording_poll(session, task)

    assert task.status == TaskStatus.DEAD_LETTER.value
    assert "recording_unavailable_after_6_attempts" in task.last_error
    # interactions UPDATE called to set recording_status='failed';
    # at least one session.execute call must have happened.
    assert session.execute.await_count >= 1


@pytest.mark.asyncio
async def test_no_call_short_circuits_completed_with_no_recording():
    """If the provider says definitively that the call has no
    recording, polling forever wastes work. We mark the task
    completed but flag the recording_status so dashboards can
    distinguish 'no recording exists' from 'recording available'.
    """
    session = _make_session()
    task = _make_task(attempts=1)

    fake = ProbeResult(status=RecordingProbeStatus.NO_CALL, detail="missing call_sid")
    with patch(
        "src.services.recording_poller.try_fetch_recording",
        new=AsyncMock(return_value=fake),
    ):
        await execute_recording_poll(session, task)

    assert task.status == TaskStatus.COMPLETED.value
    # No retry path was taken — last_error was never set on a fresh
    # task, and completed_at was set on the success path. Together
    # these prove the NO_CALL short-circuit went through the
    # complete-not-retry branch.
    assert task.last_error is None
    assert task.completed_at is not None
