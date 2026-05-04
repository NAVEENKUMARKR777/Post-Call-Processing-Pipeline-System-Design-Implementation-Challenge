"""
Signal jobs — triggers downstream actions after post-call analysis.

Examples: sending WhatsApp follow-ups, scheduling callbacks, updating external CRMs.
Currently fired via asyncio.create_task inside the FastAPI background task,
meaning they share the same event loop and have no retry/durability guarantees.
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
    Fire signal jobs based on the analysis result.
    Currently runs as a fire-and-forget asyncio task — if it fails, nobody knows.
    """
    logger.info(
        "signal_jobs_triggered",
        extra={
            "interaction_id": interaction_id,
            "campaign_id": campaign_id,
        },
    )
    # Mock: In production, this dispatches to various downstream services


async def update_lead_stage(
    lead_id: str,
    interaction_id: str,
    call_stage: str,
) -> None:
    """
    Update the lead's stage based on the call outcome.
    Currently runs inline — if it fails, the entire post-call flow fails.
    """
    logger.info(
        "lead_stage_updated",
        extra={
            "lead_id": lead_id,
            "interaction_id": interaction_id,
            "new_stage": call_stage,
        },
    )
    # Mock: In production, this updates the leads table
