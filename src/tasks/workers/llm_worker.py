"""LLM analysis worker — the core of the pipeline.

Runs one llm_analysis processing_task to completion. The contract:

  1. Pre-flight: ask the budget manager whether this call is allowed
     under the customer's reservation + global headroom. If not,
     defer the task by exactly the rate limiter's calculated
     retry-after — never a blanket 60s, never a 429 surfaced to
     anything upstream.

  2. Estimate tokens BEFORE the call so the reservation is realistic.
     Default = LLM_AVG_TOKENS_PER_CALL (1500); transcript-aware
     estimation is documented as future work in SUBMISSION.md §15.

  3. After the LLM returns, reconcile the reservation with the actual
     token count and append a row to llm_token_ledger so per-customer
     attribution stays accurate.

  4. On failure, release the reservation and either retry (with the
     limiter's hint, if it was a rate-limit error) or dead-letter
     (if attempts exhausted) — never silently drop.

  5. Skip-lane interactions are handled separately: the dispatcher
     never gives this worker a skip-lane task because skip means
     no LLM call. The worker only runs for hot/cold lanes.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.models.interaction import Interaction
from src.models.processing_task import ProcessingTask
from src.observability import bind_interaction_context, get_logger
from src.scheduler import BudgetManager, BudgetReservation, RateLimitDecision
from src.scheduler.task_dispatcher import (
    mark_completed,
    mark_for_retry,
)
from src.services.audit_logger import record_event
from src.services.post_call_processor import (
    AnalysisResult,
    PostCallContext,
    PostCallProcessor,
)
from src.services.token_ledger import record_consumption

logger = get_logger(__name__)


def estimate_tokens(transcript_text: Optional[str]) -> int:
    """Cheap heuristic: 1 token ≈ 4 chars. Caps at 6000 so a runaway
    transcript estimate doesn't single-handedly fail every reservation
    by claiming 90K tokens. Reconciliation against actual_tokens
    corrects systematic drift over time.

    The default LLM_AVG_TOKENS_PER_CALL=1500 is used when the
    transcript is missing or empty so the reservation never under-
    reserves to zero (which would let the rate limiter pass burst
    callers through unchecked).
    """
    if not transcript_text:
        return settings.LLM_AVG_TOKENS_PER_CALL
    char_estimate = len(transcript_text) // 4
    return max(settings.LLM_AVG_TOKENS_PER_CALL, min(char_estimate + 500, 6000))


_RATE_LIMIT_HINT_PATTERN = re.compile(r"rate.?limit|429", re.IGNORECASE)


def _is_rate_limit_error(message: str) -> bool:
    return bool(_RATE_LIMIT_HINT_PATTERN.search(message))


async def execute_llm_analysis(
    session: AsyncSession,
    task: ProcessingTask,
    *,
    budget_manager: BudgetManager,
    processor: Optional[PostCallProcessor] = None,
) -> AnalysisResult | None:
    """Run one LLM analysis attempt for the given task row.

    Returns the AnalysisResult on success, None if the call was
    deferred or failed. Always updates the task row to a terminal
    or scheduled state — the caller does not need to follow up.
    """
    bind_interaction_context(
        interaction_id=str(task.interaction_id),
        customer_id=str(task.customer_id),
        campaign_id=str(task.campaign_id),
        step=task.step,
    )

    payload = task.payload or {}
    transcript_text: str = payload.get("transcript_text") or ""
    estimated = estimate_tokens(transcript_text)

    reservation: BudgetReservation = await budget_manager.check_and_reserve(
        task.customer_id,
        estimated_tokens=estimated,
    )
    if not reservation.allowed:
        await record_event(
            session,
            interaction_id=task.interaction_id,
            customer_id=task.customer_id,
            correlation_id=task.correlation_id,
            step=task.step,
            status="rate_limit_deferred",
            attempt=task.attempts,
            detail={
                "reason": reservation.reason,
                "retry_after_ms": reservation.retry_after_ms,
                "estimated_tokens": estimated,
            },
        )
        await mark_for_retry(
            session,
            task,
            error=f"deferred:{reservation.reason}",
            retry_after_ms=reservation.retry_after_ms,
            detail={"deferred_reason": reservation.reason},
        )
        return None

    proc = processor or PostCallProcessor()
    ctx = PostCallContext(
        interaction_id=str(task.interaction_id),
        session_id=str(payload.get("session_id", "")),
        lead_id=str(payload.get("lead_id", "")),
        campaign_id=str(task.campaign_id),
        customer_id=str(task.customer_id),
        agent_id=str(payload.get("agent_id", "")),
        call_sid=str(payload.get("call_sid", "")),
        transcript_text=transcript_text,
        conversation_data=payload.get("conversation_data") or {},
        additional_data=payload.get("additional_data") or {},
        ended_at=datetime.fromisoformat(payload["ended_at"])
        if payload.get("ended_at")
        else datetime.now(timezone.utc),
        exotel_account_id=payload.get("exotel_account_id"),
    )

    try:
        result = await proc.process_post_call(ctx, single_prompt=True)
    except Exception as exc:
        # Roll back the reservation so a failed call does not hold
        # phantom capacity in the limiter window.
        await budget_manager.release(reservation)

        message = str(exc)
        rate_limited = _is_rate_limit_error(message)
        # If the provider really did 429 us despite the limiter
        # (clock skew, multi-region drift), defer with a sane retry.
        retry_after_ms = 5000 if rate_limited else None
        await record_event(
            session,
            interaction_id=task.interaction_id,
            customer_id=task.customer_id,
            correlation_id=task.correlation_id,
            step=task.step,
            status="failed",
            attempt=task.attempts,
            detail={"error": message, "rate_limited": rate_limited},
        )
        await mark_for_retry(
            session,
            task,
            error=message,
            retry_after_ms=retry_after_ms,
            detail={"rate_limited": rate_limited},
        )
        return None

    actual_tokens = int(result.tokens_used or 0)
    await budget_manager.reconcile(reservation, actual_tokens=actual_tokens)
    await record_consumption(
        session,
        customer_id=task.customer_id,
        interaction_id=task.interaction_id,
        campaign_id=task.campaign_id,
        tokens_used=actual_tokens,
        model=result.model,
        correlation_id=task.correlation_id,
    )

    await session.execute(
        update(Interaction)
        .where(Interaction.id == task.interaction_id)
        .values(
            analysis_status="completed",
            interaction_metadata=_merge_metadata(result),
        )
    )

    await mark_completed(
        session,
        task,
        actual_tokens=actual_tokens,
        detail={
            "call_stage": result.call_stage,
            "latency_ms": int(result.latency_ms or 0),
            "estimated_tokens": estimated,
            "drift": actual_tokens - estimated,
        },
    )

    logger.info(
        "llm_analysis_complete",
        extra={
            "interaction_id": str(task.interaction_id),
            "customer_id": str(task.customer_id),
            "tokens_used": actual_tokens,
            "estimated_tokens": estimated,
            "call_stage": result.call_stage,
            "latency_ms": int(result.latency_ms or 0),
        },
    )
    return result


def _merge_metadata(result: AnalysisResult) -> dict:
    """Project the AnalysisResult into the JSONB shape the dashboard
    expects. We don't read-modify-write — the caller's UPDATE
    overwrites the JSONB column, which is fine because everything
    that mutated it lived inside the LLM result."""
    return {
        "call_stage": result.call_stage,
        "entities": result.entities or {},
        "summary": result.summary,
        "analysis_status": "completed",
        "model": result.model,
    }
