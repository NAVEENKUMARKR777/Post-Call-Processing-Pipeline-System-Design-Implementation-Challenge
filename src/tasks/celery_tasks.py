"""
Celery tasks for post-call processing.

This is the main background processing pipeline. Every completed interaction
is enqueued here for full LLM analysis, recording upload, signal jobs, and
lead stage updates.

KNOWN ISSUES:
- Single queue, no priority: A rebook confirmation waits behind 10,000
  "not interested" calls in the same Celery queue.
- Tasks drop silently: If Redis (the Celery broker) restarts, in-flight
  tasks are lost with no recovery mechanism.
- No workflow visibility: There is no way to see which step of processing
  a given interaction is currently in.
- Full LLM cost on every call: No filtering or triage before running
  expensive LLM analysis.
"""

import asyncio
import logging
from datetime import datetime
from typing import Any, Dict

from src.tasks.celery_app import celery_app
from src.services.post_call_processor import PostCallProcessor, PostCallContext
from src.services.recording import fetch_and_upload_recording
from src.services.signal_jobs import trigger_signal_jobs, update_lead_stage
from src.services.retry_queue import retry_queue
from src.services.metrics import metrics_tracker

logger = logging.getLogger(__name__)


@celery_app.task(
    name="process_interaction_end_background_task",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    acks_late=True,
    queue="postcall_processing",
)
def process_interaction_end_background_task(self, payload: Dict[str, Any]):
    """
    Main Celery task for post-call processing.
    Called for EVERY completed interaction — no filtering.

    Steps:
    1. Wait 45s for recording, then try to fetch + upload (single attempt)
    2. Run full LLM analysis on the transcript (streaming API, full cost)
    3. Update interaction metadata (dashboard hot cache)
    4. Trigger signal jobs (fire-and-forget)
    5. Update lead stage

    All steps run sequentially. If step 2 fails, steps 3-5 are skipped.
    Recording upload (step 1) is independent but blocks step 2 by 45 seconds.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        loop.run_until_complete(_process_interaction(self, payload))
    except Exception as e:
        logger.exception(
            "celery_task_failed",
            extra={
                "interaction_id": payload.get("interaction_id"),
                "error": str(e),
                "attempt": self.request.retries,
            },
        )
        # Enqueue to retry queue — which itself is Redis-based and not durable
        loop.run_until_complete(
            retry_queue.enqueue_retry(
                interaction_id=payload["interaction_id"],
                error=str(e),
                payload=payload,
            )
        )
        raise self.retry(exc=e)
    finally:
        loop.close()


async def _process_interaction(task, payload: Dict[str, Any]):
    interaction_id = payload["interaction_id"]

    await metrics_tracker.track_processing_started(interaction_id)

    ctx = PostCallContext(
        interaction_id=interaction_id,
        session_id=payload["session_id"],
        lead_id=payload["lead_id"],
        campaign_id=payload["campaign_id"],
        customer_id=payload["customer_id"],
        agent_id=payload["agent_id"],
        call_sid=payload.get("call_sid", ""),
        transcript_text=payload.get("transcript_text", ""),
        conversation_data=payload.get("conversation_data", {}),
        additional_data=payload.get("additional_data", {}),
        ended_at=datetime.fromisoformat(payload["ended_at"]),
        exotel_account_id=payload.get("exotel_account_id"),
    )

    # Step 1: Recording upload (blocks for 45 seconds regardless)
    recording_s3_key = await fetch_and_upload_recording(
        interaction_id=ctx.interaction_id,
        call_sid=ctx.call_sid,
        exotel_account_id=ctx.exotel_account_id or "",
    )

    if recording_s3_key:
        logger.info(
            "recording_uploaded",
            extra={
                "interaction_id": interaction_id,
                "s3_key": recording_s3_key,
            },
        )

    # Step 2: Full LLM analysis (same cost whether call was 10s or 10min)
    processor = PostCallProcessor()
    result = await processor.process_post_call(ctx, single_prompt=True)

    await metrics_tracker.track_processing_completed(
        interaction_id, result.tokens_used, result.latency_ms
    )

    # Step 3: Signal jobs (fire-and-forget, no retry if it fails)
    try:
        await trigger_signal_jobs(
            interaction_id=ctx.interaction_id,
            session_id=ctx.session_id,
            campaign_id=ctx.campaign_id,
            analysis_result=result.raw_response,
        )
    except Exception as e:
        logger.warning("signal_jobs_failed", extra={"error": str(e)})

    # Step 4: Lead stage update
    try:
        await update_lead_stage(
            lead_id=ctx.lead_id,
            interaction_id=ctx.interaction_id,
            call_stage=result.call_stage,
        )
    except Exception as e:
        logger.warning("lead_stage_update_failed", extra={"error": str(e)})
