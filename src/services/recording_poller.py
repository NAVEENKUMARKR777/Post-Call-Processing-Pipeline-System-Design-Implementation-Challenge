"""Recording poller — replaces the asyncio.sleep(45s) gate.

Responsibility:

  Given a processing_tasks row of step=recording_poll, run one
  fetch attempt and decide what happens next:

    AVAILABLE    write recording_s3_key + recording_status='available'
                 to the interactions row; mark the task completed.

    NOT_READY    schedule the next poll via the configured backoff
                 schedule (5s, 15s, 30s, 60s, 120s, 240s). When all
                 attempts are exhausted, dead-letter the task and
                 set interactions.recording_status='failed' so the
                 dashboard surfaces it. Emit an ERROR-level
                 structured log line for alerting.

    PROVIDER_ERROR  same as NOT_READY but with the error string
                    captured in last_error so on-call can grep.

    NO_CALL      short-circuit: this call_sid has no recording.
                 Mark interactions.recording_status='no_recording'
                 and complete the task — but loud-log so we can
                 spot a misconfigured dialler that's sending
                 empty call_sids.

Crucially, the LLM analysis path no longer waits for this poller.
Both run independently; AC4 ("recording poller retries with backoff;
never silently skips") is satisfied without coupling the two
pipelines.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.models.interaction import Interaction
from src.models.processing_task import ProcessingTask
from src.observability import bind_interaction_context, get_logger
from src.scheduler.task_dispatcher import (
    mark_completed,
    mark_dead_letter,
    mark_for_retry,
)
from src.services.audit_logger import record_event
from src.services.recording import (
    ProbeResult,
    RecordingProbeStatus,
    try_fetch_recording,
)

logger = get_logger(__name__)


async def execute_recording_poll(
    session: AsyncSession,
    task: ProcessingTask,
) -> ProbeResult:
    """Run one poll attempt for the given recording_poll task.

    Updates the task row (completed | scheduled | dead_letter) and
    the interactions row's recording_status / recording_s3_key. The
    caller's transaction owns the commit.
    """
    bind_interaction_context(
        interaction_id=str(task.interaction_id),
        customer_id=str(task.customer_id),
        campaign_id=str(task.campaign_id),
        step=task.step,
    )

    payload = task.payload or {}
    call_sid: str = payload.get("call_sid") or ""
    exotel_account_id: str = payload.get("exotel_account_id") or ""

    result = await try_fetch_recording(
        interaction_id=str(task.interaction_id),
        call_sid=call_sid,
        exotel_account_id=exotel_account_id,
    )

    if result.status is RecordingProbeStatus.AVAILABLE:
        await _set_interaction_recording(
            session,
            interaction_id=task.interaction_id,
            status="available",
            s3_key=result.s3_key,
            recording_url=result.recording_url,
        )
        await mark_completed(
            session,
            task,
            detail={"recording_s3_key": result.s3_key},
        )
        logger.info(
            "recording_available",
            extra={"interaction_id": str(task.interaction_id), "s3_key": result.s3_key},
        )
        return result

    if result.status is RecordingProbeStatus.NO_CALL:
        await _set_interaction_recording(session, interaction_id=task.interaction_id, status="no_recording")
        await mark_completed(
            session,
            task,
            detail={"reason": "no_call_sid", "provider_detail": result.detail},
        )
        logger.warning(
            "recording_no_call",
            extra={"interaction_id": str(task.interaction_id), "detail": result.detail},
        )
        return result

    # NOT_READY or PROVIDER_ERROR — schedule a retry.
    schedule = settings.RECORDING_BACKOFF_SECONDS
    error_text = result.detail or result.status.value

    if task.attempts >= task.max_attempts:
        await _set_interaction_recording(session, interaction_id=task.interaction_id, status="failed")
        await mark_dead_letter(
            session,
            task,
            error=f"recording_unavailable_after_{task.attempts}_attempts: {error_text}",
            detail={"final_probe_status": result.status.value},
        )
        # ERROR log doubles as the alert source — see SUBMISSION.md §9.
        logger.error(
            "recording_dead_letter",
            extra={
                "interaction_id": str(task.interaction_id),
                "attempts": task.attempts,
                "last_probe_status": result.status.value,
                "last_error": result.detail,
            },
        )
        return result

    await record_event(
        session,
        interaction_id=task.interaction_id,
        customer_id=task.customer_id,
        correlation_id=task.correlation_id,
        step=task.step,
        status="probe_not_ready",
        attempt=task.attempts,
        detail={"probe_status": result.status.value, "detail": result.detail},
    )
    await mark_for_retry(
        session,
        task,
        error=error_text,
        backoff_schedule=schedule,
        detail={"probe_status": result.status.value},
    )
    return result


async def _set_interaction_recording(
    session: AsyncSession,
    *,
    interaction_id,
    status: str,
    s3_key: Optional[str] = None,
    recording_url: Optional[str] = None,
) -> None:
    """Update the interactions row's recording-related columns.

    We use a single UPDATE rather than reading then mutating an ORM
    object; the row is referenced from the task and we only ever
    touch the recording_* columns here, so a partial-write is
    impossible.
    """
    values = {"recording_status": status}
    if s3_key is not None:
        values["recording_s3_key"] = s3_key
    if recording_url is not None:
        values["recording_url"] = recording_url

    await session.execute(
        update(Interaction).where(Interaction.id == interaction_id).values(**values)
    )
