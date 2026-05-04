"""
Signal jobs — downstream actions triggered after post-call analysis.

Examples of what runs here in production:
  - Send a WhatsApp message to the lead ("Your appointment is confirmed for 3 PM tomorrow")
  - Book a callback slot in the scheduling system
  - Push the call outcome to the customer's CRM via webhook
  - Flag the interaction for human review if the lead was angry

These are the actions the business actually cares about. Getting the analysis
done is only valuable if these downstream triggers fire correctly and durably.

Current execution model: asyncio.create_task() in the FastAPI event loop.
  - Fire-and-forget with no return value
  - No retry if the downstream service is down
  - No record that it was attempted
  - Lost entirely if the FastAPI server restarts while the task is pending

There's a subtler timing problem too: for long transcripts, signal_jobs is
called twice — once from the endpoint (before Celery runs, with an empty
analysis_result) and once from the Celery task (after analysis, with the real
result). Downstream systems receive two triggers: one empty, one real.
Whether they handle that gracefully depends on the downstream system.
"""

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


async def trigger_signal_jobs(
    interaction_id: str,
    session_id: str,
    campaign_id: str,
    analysis_result: Dict[str, Any],
) -> None:
    """
    Dispatch downstream actions based on the call analysis.

    analysis_result contains call_stage and entities from the LLM.
    When called from the endpoint (before Celery), analysis_result is {}.
    When called from the Celery task, analysis_result has the real data.

    In production, this fans out to multiple downstream services based on
    campaign configuration. Each dispatch is currently fire-and-forget with
    no ack, no retry, and no record in the database.
    """
    logger.info(
        "signal_jobs_triggered",
        extra={
            "interaction_id": interaction_id,
            "campaign_id": campaign_id,
            "has_analysis": bool(analysis_result),
            # has_analysis=False means we fired with an empty payload.
            # That happens for every long-transcript call, from the endpoint.
        },
    )
    # Mock: production implementation dispatches to downstream services


async def update_lead_stage(
    lead_id: str,
    interaction_id: str,
    call_stage: str,
) -> None:
    """
    Update the lead's stage in the leads table.

    call_stage maps to a stage in the sales funnel:
      "rebook_confirmed" → "booked"
      "not_interested"   → "closed_lost"
      "callback_requested" → "follow_up"
      "processing"       → (placeholder, overwritten when analysis completes)

    For long transcripts, this is called twice: once from the endpoint with
    call_stage="processing", once from Celery with the real stage. The second
    write overwrites the first — which is fine, but it means the lead briefly
    appears as "processing" in the dashboard even after the call ended cleanly.
    """
    logger.info(
        "lead_stage_updated",
        extra={
            "lead_id": lead_id,
            "interaction_id": interaction_id,
            "new_stage": call_stage,
        },
    )
    # Mock: production implementation runs UPDATE leads SET stage = $2 WHERE id = $1
