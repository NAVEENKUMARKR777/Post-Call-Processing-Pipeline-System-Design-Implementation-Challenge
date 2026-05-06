"""Per-step Celery dispatchers — thin shells over the durable
processing_tasks workflow.

The previous monolithic process_interaction_end_background_task is
gone. In its place, one Celery task per step type, each one a thin
wrapper that:

  1. Pulls a processing_tasks row by id from the DB
  2. Calls the matching worker function in src/tasks/workers/
  3. Lets the worker mutate task state (the dispatcher in
     src/scheduler/task_dispatcher.py owns the state machine)
  4. Commits the transaction

If Celery dies between (1) and (4), the row's lease (locked_until)
expires and another worker reclaims it (AC3). If the WORKER
function itself raises (a true bug, not a deferred reservation),
Celery's normal acks_late behaviour redelivers; the dispatcher's
double-claim guard (worker_id check) prevents the same row from
running concurrently.

Three queues are registered: hot_lane, cold_lane, and
recording_poll. The dispatcher (commit 12+) routes processing_tasks
rows into the right queue based on their lane.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict
from uuid import UUID

from sqlalchemy import select

from src.config import settings
from src.models.processing_task import ProcessingTask, TaskStep
from src.observability import bind_correlation_id, bind_interaction_context, clear_context
from src.scheduler import BudgetManager, RateLimiter
from src.tasks.celery_app import celery_app
from src.tasks.workers.lead_stage_worker import execute_lead_stage
from src.tasks.workers.llm_worker import execute_llm_analysis
from src.tasks.workers.signal_worker import execute_signal_jobs
from src.services.recording_poller import execute_recording_poll
from src.utils.db import async_session_factory
from src.utils.redis_client import redis_client

logger = logging.getLogger(__name__)


# Singleton budget manager. Constructed lazily so the import graph
# stays loose (the workers don't need to know about BudgetManager
# directly).
_budget_manager: BudgetManager | None = None


def _get_budget_manager() -> BudgetManager:
    global _budget_manager
    if _budget_manager is None:
        _budget_manager = BudgetManager(async_session_factory, RateLimiter(redis_client))
    return _budget_manager


def _run_async(coro: Awaitable[Any]) -> Any:
    """Run an async coroutine inside a Celery worker that wasn't
    started with an event loop. We create one per task because
    Celery workers fork; reusing a loop across forks is unsafe."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _process_one(task_id: str, executor: Callable):
    """Common per-task harness: load the row, bind context, run the
    worker, commit. Errors propagate so Celery acks_late kicks in
    and the row's lease expires for another worker to reclaim."""
    clear_context()
    async with async_session_factory() as session:
        stmt = select(ProcessingTask).where(ProcessingTask.id == UUID(task_id))
        task = (await session.execute(stmt)).scalar_one_or_none()
        if task is None:
            logger.warning("processing_task_missing", extra={"task_id": task_id})
            return

        bind_correlation_id(task.correlation_id)
        bind_interaction_context(
            interaction_id=str(task.interaction_id),
            customer_id=str(task.customer_id),
            campaign_id=str(task.campaign_id),
            step=task.step,
            lane=task.lane,
        )

        try:
            await executor(session, task)
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@celery_app.task(name="dispatch.recording_poll", queue="recording_poll", acks_late=True, max_retries=0)
def dispatch_recording_poll(task_id: str):
    _run_async(_process_one(task_id, execute_recording_poll))


@celery_app.task(name="dispatch.llm_analysis_hot", queue="hot_lane", acks_late=True, max_retries=0)
def dispatch_llm_analysis_hot(task_id: str):
    _run_async(_process_one(
        task_id,
        lambda session, task: execute_llm_analysis(session, task, budget_manager=_get_budget_manager()),
    ))


@celery_app.task(name="dispatch.llm_analysis_cold", queue="cold_lane", acks_late=True, max_retries=0)
def dispatch_llm_analysis_cold(task_id: str):
    _run_async(_process_one(
        task_id,
        lambda session, task: execute_llm_analysis(session, task, budget_manager=_get_budget_manager()),
    ))


@celery_app.task(name="dispatch.signal_jobs", queue="hot_lane", acks_late=True, max_retries=0)
def dispatch_signal_jobs(task_id: str):
    _run_async(_process_one(task_id, execute_signal_jobs))


@celery_app.task(name="dispatch.lead_stage", queue="hot_lane", acks_late=True, max_retries=0)
def dispatch_lead_stage(task_id: str):
    _run_async(_process_one(task_id, execute_lead_stage))


# Legacy task name kept as a tombstone import target so any in-flight
# Celery message that references the old name fails loudly rather
# than silently executing the old code path. The replacement is
# the per-step dispatch.* tasks above.
@celery_app.task(name="process_interaction_end_background_task")
def process_interaction_end_background_task(payload: Dict[str, Any]):
    raise RuntimeError(
        "Legacy task removed. The endpoint now writes processing_tasks "
        "rows directly; see src/api/endpoints.py and the dispatch.* "
        "tasks below it."
    )
