"""Lead-stage update worker — durable, idempotent.

Maps the LLM's call_stage to the lead's funnel stage and updates
the leads.stage column. Previously fired-and-forgotten from two
places (endpoint with empty stage, Celery with real one); now lives
on the durable processing_tasks ledger and runs exactly once per
interaction.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from src.models.processing_task import ProcessingTask
from src.observability import bind_interaction_context, get_logger
from src.scheduler.task_dispatcher import mark_completed, mark_for_retry
from src.services.audit_logger import record_event
from src.services.signal_jobs import update_lead_stage

logger = get_logger(__name__)


# Mapping from LLM's call_stage to the leads.stage column. Kept
# explicit so a new call_stage from the LLM does not silently land
# the lead in some unrelated funnel bucket.
_CALL_STAGE_TO_LEAD_STAGE = {
    "rebook_confirmed": "booked",
    "demo_booked": "booked",
    "callback_requested": "follow_up",
    "considering": "follow_up",
    "escalation_needed": "escalation",
    "not_interested": "closed_lost",
    "already_done": "closed_won",
    "short_call": "no_contact",
}


def map_call_stage_to_lead_stage(call_stage: str | None) -> str:
    if not call_stage:
        return "follow_up"
    return _CALL_STAGE_TO_LEAD_STAGE.get(call_stage, "follow_up")


async def execute_lead_stage(
    session: AsyncSession,
    task: ProcessingTask,
) -> bool:
    bind_interaction_context(
        interaction_id=str(task.interaction_id),
        customer_id=str(task.customer_id),
        campaign_id=str(task.campaign_id),
        step=task.step,
    )

    payload = task.payload or {}
    call_stage = (payload.get("analysis_result") or {}).get("call_stage")
    lead_stage = map_call_stage_to_lead_stage(call_stage)

    try:
        await update_lead_stage(
            lead_id=str(payload.get("lead_id", "")),
            interaction_id=str(task.interaction_id),
            call_stage=lead_stage,
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
            detail={"error": str(exc), "call_stage": call_stage, "lead_stage": lead_stage},
        )
        await mark_for_retry(session, task, error=str(exc))
        return False

    await mark_completed(
        session,
        task,
        detail={"call_stage": call_stage, "lead_stage": lead_stage},
    )
    return True
