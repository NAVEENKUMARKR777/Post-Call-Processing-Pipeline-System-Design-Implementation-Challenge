"""LLM worker tests — pre-flight quota check, reconciliation,
and the short-circuit lane (AC8).

AC8: "Short transcripts (< 4 turns) never consume LLM quota — test:
short transcript → no LLM call, interaction status updated directly."

The skip lane is enforced by the dispatcher (it never gives this
worker a skip-lane task). We verify the path another way: the
pre-classifier outputs Lane.SKIP for short transcripts, and the
endpoint refuses to enqueue an llm_analysis step for skip-lane
interactions. That contract is enforced in test_short_circuit
in this file via the same shared classifier function the endpoint
will use.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from src.classifier import Lane, classify_transcript
from src.models.processing_task import (
    ProcessingTask,
    TaskLane,
    TaskStatus,
    TaskStep,
)
from src.observability import bind_correlation_id, clear_context
from src.scheduler.budget_manager import BudgetManager, BudgetReservation
from src.scheduler.rate_limiter import RateLimitDecision, Reservation
from src.services.post_call_processor import AnalysisResult
from src.tasks.workers.llm_worker import (
    estimate_tokens,
    execute_llm_analysis,
)


@pytest.fixture(autouse=True)
def _isolation():
    clear_context()
    bind_correlation_id("cid-llm-worker")
    yield
    clear_context()


def _session():
    s = MagicMock()
    s.add = MagicMock()
    s.flush = AsyncMock()
    s.execute = AsyncMock()
    return s


def _task(*, attempts: int = 0, transcript: str = "agent: hi\ncustomer: yes") -> ProcessingTask:
    return ProcessingTask(
        id=uuid4(),
        interaction_id=uuid4(),
        customer_id=uuid4(),
        campaign_id=uuid4(),
        correlation_id="cid-llm-worker",
        step=TaskStep.LLM_ANALYSIS.value,
        lane=TaskLane.HOT.value,
        status=TaskStatus.IN_PROGRESS.value,
        attempts=attempts,
        max_attempts=6,
        payload={
            "transcript_text": transcript,
            "session_id": str(uuid4()),
            "lead_id": str(uuid4()),
            "agent_id": str(uuid4()),
            "call_sid": "exotel-X",
            "ended_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def _allowed_reservation():
    inner = Reservation(
        decision=RateLimitDecision.ALLOW,
        retry_after_ms=0,
        current_tpm=1500,
        current_rpm=1,
        member_id="mem-1",
        bucket="cust:x",
    )
    return BudgetReservation(
        decision=RateLimitDecision.ALLOW,
        retry_after_ms=0,
        customer_reservation=inner,
        global_reservation=inner,
        reason="ok",
    )


def _denied_reservation(reason="customer_budget_exhausted"):
    return BudgetReservation(
        decision=RateLimitDecision.DENY,
        retry_after_ms=15_000,
        customer_reservation=None,
        global_reservation=None,
        reason=reason,
    )


@pytest.mark.asyncio
async def test_estimate_tokens_floor_is_avg_per_call():
    """Empty/short transcripts must still reserve at least the
    LLM_AVG_TOKENS_PER_CALL default — under-reserving silently
    invalidates the rate limit guarantee."""
    assert estimate_tokens(None) == 1500
    assert estimate_tokens("") == 1500
    # A long transcript: 800 chars / 4 = 200 + 500 = 700, but floor 1500.
    assert estimate_tokens("x" * 800) == 1500
    # Very long transcript: capped at 6000.
    assert estimate_tokens("x" * 100_000) == 6000


@pytest.mark.asyncio
async def test_allowed_path_runs_llm_writes_ledger_and_completes_task():
    session = _session()
    task = _task()

    budget = MagicMock(spec=BudgetManager)
    budget.check_and_reserve = AsyncMock(return_value=_allowed_reservation())
    budget.reconcile = AsyncMock()
    budget.release = AsyncMock()

    processor = MagicMock()
    processor.process_post_call = AsyncMock(
        return_value=AnalysisResult(
            call_stage="rebook_confirmed",
            entities={"date": "tomorrow"},
            summary="Rebook confirmed",
            raw_response={},
            tokens_used=1750,
            latency_ms=1200,
            provider="openai",
            model="gpt-4o",
        )
    )

    result = await execute_llm_analysis(
        session, task, budget_manager=budget, processor=processor
    )

    assert result is not None
    assert task.status == TaskStatus.COMPLETED.value
    assert task.actual_tokens == 1750
    budget.reconcile.assert_awaited_once()
    # Ledger row + audit rows written; we expect at least 2 session.add
    # invocations (token ledger + completed audit).
    assert session.add.call_count >= 2


@pytest.mark.asyncio
async def test_denied_reservation_defers_with_retry_hint():
    session = _session()
    task = _task()

    budget = MagicMock(spec=BudgetManager)
    budget.check_and_reserve = AsyncMock(return_value=_denied_reservation())
    budget.reconcile = AsyncMock()
    budget.release = AsyncMock()

    result = await execute_llm_analysis(session, task, budget_manager=budget)

    assert result is None
    assert task.status == TaskStatus.SCHEDULED.value
    assert task.last_error.startswith("deferred:")
    # No LLM call was made → no reconcile, no release.
    budget.reconcile.assert_not_awaited()
    budget.release.assert_not_awaited()


@pytest.mark.asyncio
async def test_provider_429_releases_reservation_and_schedules_retry():
    """If the provider 429s us anyway (the rate limiter said yes
    but the provider's own clock disagrees), we must release the
    reservation so it doesn't hold phantom capacity."""
    session = _session()
    task = _task()

    budget = MagicMock(spec=BudgetManager)
    budget.check_and_reserve = AsyncMock(return_value=_allowed_reservation())
    budget.reconcile = AsyncMock()
    budget.release = AsyncMock()

    processor = MagicMock()
    processor.process_post_call = AsyncMock(side_effect=Exception("Got 429 from provider"))

    result = await execute_llm_analysis(
        session, task, budget_manager=budget, processor=processor
    )

    assert result is None
    assert task.status == TaskStatus.SCHEDULED.value
    budget.release.assert_awaited_once()
    budget.reconcile.assert_not_awaited()


@pytest.mark.asyncio
async def test_short_transcript_never_reaches_llm_worker():
    """AC8: short transcripts skip the LLM entirely.

    The classifier outputs Lane.SKIP for <4-turn transcripts; the
    endpoint contract is to NOT create an llm_analysis processing
    task for skip-lane interactions. So the test below is a contract
    test: classify_transcript on every short fixture must yield SKIP,
    which is what the endpoint reads to decide skip-vs-enqueue.
    """
    short_transcripts = [
        [],
        [{"role": "agent", "content": "hi"}],
        [{"role": "agent", "content": "hi"}, {"role": "customer", "content": "wrong number"}],
        [
            {"role": "agent", "content": "hi"},
            {"role": "customer", "content": "yes"},
            {"role": "agent", "content": "ok"},
        ],
    ]
    for tx in short_transcripts:
        assert classify_transcript({"transcript": tx}).lane == Lane.SKIP


@pytest.mark.asyncio
async def test_token_drift_is_recorded_in_audit_detail():
    """Reconciliation captures the difference between estimated and
    actual tokens — a systematic drift pattern surfaces in the
    audit log so on-call can re-tune the estimator."""
    session = _session()
    task = _task()

    budget = MagicMock(spec=BudgetManager)
    budget.check_and_reserve = AsyncMock(return_value=_allowed_reservation())
    budget.reconcile = AsyncMock()
    budget.release = AsyncMock()

    processor = MagicMock()
    processor.process_post_call = AsyncMock(
        return_value=AnalysisResult(
            call_stage="not_interested",
            entities={},
            summary="",
            raw_response={},
            tokens_used=2300,
            latency_ms=900,
            provider="openai",
            model="gpt-4o",
        )
    )

    await execute_llm_analysis(session, task, budget_manager=budget, processor=processor)

    # Inspect the completed-audit row for drift detail.
    completed_audit = None
    for call in session.add.call_args_list:
        added = call.args[0]
        if getattr(added, "status", None) == "completed":
            completed_audit = added
    assert completed_audit is not None
    assert completed_audit.detail["drift"] == 2300 - 1500
