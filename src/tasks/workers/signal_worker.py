"""Signal-jobs worker — durable, idempotent fan-out to downstream
services (WhatsApp, callback scheduling, CRM webhooks).

Replaces the previous fire-and-forget asyncio.create_task() in the
endpoint, which would lose the dispatch entirely on a server
restart and was called twice for every long transcript (once with
an empty payload from the endpoint, once with the real result from
Celery).

Now:

  - The signal_jobs step is a row in processing_tasks. It runs
    exactly once per interaction in normal operation; on retry the
    (interaction_id, step) UNIQUE constraint blocks duplicate
    enqueues.

  - The actual downstream calls run inside this worker, after the
    LLM analysis is durably written to interaction_metadata. The
    payload received here always reflects the post-analysis state
    — the empty-payload pre-trigger from the old endpoint is
    eliminated.

  - Each downstream call is structured-logged. Failures are
    captured, the task is retried via the dispatcher's exponential
    backoff, and dead-lettered after max_attempts. Nothing is ever
    silently lost.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from src.models.processing_task import ProcessingTask
from src.observability import bind_interaction_context, get_logger
from src.scheduler.task_dispatcher import (
    mark_completed,
    mark_for_retry,
)
from src.services.audit_logger import record_event
from src.services.signal_jobs import trigger_signal_jobs

logger = get_logger(__name__)


async def execute_signal_jobs(
    session: AsyncSession,
    task: ProcessingTask,
) -> bool:
    """Run one signal_jobs attempt for the given task.

    Returns True on success. On failure schedules a retry via the
    dispatcher; the caller does not need to handle either case.
    """
    bind_interaction_context(
        interaction_id=str(task.interaction_id),
        customer_id=str(task.customer_id),
        campaign_id=str(task.campaign_id),
        step=task.step,
    )

    payload = task.payload or {}
    analysis_result = payload.get("analysis_result") or {}

    try:
        await trigger_signal_jobs(
            interaction_id=str(task.interaction_id),
            session_id=str(payload.get("session_id", "")),
            campaign_id=str(task.campaign_id),
            analysis_result=analysis_result,
        )
    except Exception as exc:
        await record_event(
            session,
            interaction_id=task.interaction_id,
            customer_id=task.customer_id,
            correlation_id=task.correlation_id,
            step=task.step,
            status="failed",
            attempt=task.attempts,
            detail={"error": str(exc)},
        )
        await mark_for_retry(
            session,
            task,
            error=str(exc),
            detail={"target": "signal_jobs"},
        )
        return False

    await mark_completed(
        session,
        task,
        detail={
            "call_stage": analysis_result.get("call_stage"),
            "has_analysis": bool(analysis_result),
        },
    )
    return True


def _summarise_dispatches(analysis_result: Dict[str, Any]) -> Optional[str]:
    """Project the analysis result into a one-line summary for the
    audit detail. Returns None if the analysis is empty."""
    stage = analysis_result.get("call_stage")
    if not stage:
        return None
    return f"call_stage={stage}"
