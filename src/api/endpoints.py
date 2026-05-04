"""
FastAPI endpoint for ending an interaction.

POST /session/{session_id}/interaction/{interaction_id}/end

This is the entry point for all post-call processing. When a call ends,
the telephony provider (Exotel) calls this endpoint, which:
1. Updates the interaction status to ENDED
2. Kicks off background processing via FastAPI BackgroundTask + Celery

KNOWN ISSUES:
- Uses both FastAPI BackgroundTask AND Celery — two layers of async with
  no coordination between them
- asyncio.create_task for signal jobs and lead stage runs in the FastAPI
  event loop — if the server restarts, these are lost
- No short-circuit for short transcripts at the Celery level (they still
  get enqueued and wait for their turn)
"""

import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, Optional
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel

from src.services.signal_jobs import trigger_signal_jobs, update_lead_stage
from src.tasks.celery_tasks import process_interaction_end_background_task

logger = logging.getLogger(__name__)
router = APIRouter()


class InteractionEndRequest(BaseModel):
    call_sid: Optional[str] = None
    duration_seconds: Optional[int] = None
    call_status: Optional[str] = None
    additional_data: Optional[Dict[str, Any]] = None


class InteractionEndResponse(BaseModel):
    status: str
    interaction_id: str
    message: str


@router.post(
    "/session/{session_id}/interaction/{interaction_id}/end",
    response_model=InteractionEndResponse,
)
async def end_interaction(
    session_id: UUID,
    interaction_id: UUID,
    request: InteractionEndRequest,
    background_tasks: BackgroundTasks,
):
    """
    End an interaction and trigger post-call processing.

    Current flow:
    1. Load interaction from DB
    2. Update status to ENDED
    3. Check if transcript is short
       - Short: just update status, fire signal jobs (asyncio.create_task)
       - Long: enqueue full processing to Celery
    4. Return 200 immediately
    """
    try:
        # Mock: In production, this loads from DB
        interaction = await _load_interaction(interaction_id)

        if not interaction:
            raise HTTPException(status_code=404, detail="Interaction not found")

        # Update status
        await _update_interaction_status(
            interaction_id=str(interaction_id),
            status="ENDED",
            ended_at=datetime.utcnow(),
            duration=request.duration_seconds,
            call_sid=request.call_sid,
        )

        # Check transcript length
        transcript = interaction.get("conversation_data", {}).get("transcript", [])
        is_short = len(transcript) < 4

        if is_short:
            # Short transcript: minimal processing, no LLM
            logger.info(
                "short_transcript_fast_path",
                extra={"interaction_id": str(interaction_id)},
            )

            # BUG: These run in the FastAPI event loop — lost on server restart
            asyncio.create_task(
                trigger_signal_jobs(
                    interaction_id=str(interaction_id),
                    session_id=str(session_id),
                    campaign_id=interaction["campaign_id"],
                    analysis_result={"call_stage": "short_call"},
                )
            )
            asyncio.create_task(
                update_lead_stage(
                    lead_id=interaction["lead_id"],
                    interaction_id=str(interaction_id),
                    call_stage="short_call",
                )
            )
        else:
            # Long transcript: full processing via Celery
            # Every call gets the same priority, same queue, same processing
            transcript_text = "\n".join(
                f"{turn.get('role', 'unknown')}: {turn.get('content', '')}"
                for turn in transcript
            )

            celery_payload = {
                "interaction_id": str(interaction_id),
                "session_id": str(session_id),
                "lead_id": interaction["lead_id"],
                "campaign_id": interaction["campaign_id"],
                "customer_id": interaction["customer_id"],
                "agent_id": interaction["agent_id"],
                "call_sid": request.call_sid,
                "transcript_text": transcript_text,
                "conversation_data": interaction.get("conversation_data", {}),
                "additional_data": request.additional_data or {},
                "ended_at": datetime.utcnow().isoformat(),
                "exotel_account_id": interaction.get("exotel_account_id"),
            }

            # Enqueue to single Celery queue — no priority, no filtering
            task = process_interaction_end_background_task.apply_async(
                args=[celery_payload],
                queue="postcall_processing",
            )

            logger.info(
                "postcall_enqueued",
                extra={
                    "interaction_id": str(interaction_id),
                    "celery_task_id": task.id,
                },
            )

            # BUG: Also fire signal jobs and lead stage from FastAPI event loop
            # These run BEFORE the Celery task completes analysis
            asyncio.create_task(
                trigger_signal_jobs(
                    interaction_id=str(interaction_id),
                    session_id=str(session_id),
                    campaign_id=interaction["campaign_id"],
                    analysis_result={},
                )
            )
            asyncio.create_task(
                update_lead_stage(
                    lead_id=interaction["lead_id"],
                    interaction_id=str(interaction_id),
                    call_stage="processing",
                )
            )

        return InteractionEndResponse(
            status="ok",
            interaction_id=str(interaction_id),
            message="Interaction ended, processing enqueued",
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(
            "end_interaction_failed",
            extra={"interaction_id": str(interaction_id), "error": str(e)},
        )
        raise HTTPException(status_code=500, detail="Internal server error")


async def _load_interaction(interaction_id: UUID) -> Optional[Dict[str, Any]]:
    """Mock: Load interaction from database."""
    # In production, this queries the interactions table
    return {
        "id": str(interaction_id),
        "lead_id": "mock-lead-id",
        "campaign_id": "mock-campaign-id",
        "customer_id": "mock-customer-id",
        "agent_id": "mock-agent-id",
        "exotel_account_id": "mock-exotel-account",
        "conversation_data": {
            "transcript": [
                {"role": "agent", "content": "Hello, am I speaking with Mr. Sharma?"},
                {"role": "customer", "content": "Yes, speaking."},
                {"role": "agent", "content": "I'm calling from XYZ about your recent inquiry."},
                {"role": "customer", "content": "Oh yes, I was looking at the product."},
                {"role": "agent", "content": "Would you like to schedule a demo?"},
                {"role": "customer", "content": "Sure, let's do tomorrow at 3 PM."},
                {"role": "agent", "content": "Perfect, I've booked a demo for tomorrow at 3 PM."},
                {"role": "customer", "content": "Thank you, bye."},
            ]
        },
    }


async def _update_interaction_status(
    interaction_id: str,
    status: str,
    ended_at: datetime,
    duration: Optional[int],
    call_sid: Optional[str],
) -> None:
    """Mock: Update interaction status in database."""
    logger.info(
        "interaction_status_updated",
        extra={
            "interaction_id": interaction_id,
            "status": status,
            "ended_at": ended_at.isoformat(),
        },
    )
