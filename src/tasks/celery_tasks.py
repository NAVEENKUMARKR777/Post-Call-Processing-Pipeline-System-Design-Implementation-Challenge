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
from src.scheduler.task_dispatcher import mark_in_progress
from src.tasks.celery_app import celery_app
from src.tasks.workers.crm_worker import execute_crm_push
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
    """Run an async coroutine inside a Celery worker.

    Each task gets a fresh event loop. We dispose the SQLAlchemy
    engine's connection pool before AND after the run so asyncpg
    connections created in a previous loop don't leak into the new
    one. Without this, the second task in a --pool=solo worker sees
    stale asyncpg connections tied to the first loop's I/O proactor
    and raises 'NoneType has no attribute send'.
    """
    from src.utils.db import engine

    loop = asyncio.new_event_loop()
    try:
        # Flush any connections bound to a previous event loop.
        loop.run_until_complete(engine.dispose())
        return loop.run_until_complete(coro)
    finally:
        # Return connections cleanly before closing the loop.
        loop.run_until_complete(engine.dispose())
        loop.close()


async def _process_one(task_id: str, executor: Callable):
    """Common per-task harness: load the row, bind context, run the
    worker, commit. Errors propagate so Celery acks_late kicks in
    and the row's lease expires for another worker to reclaim.

    After commit, any new downstream rows the executor wrote (the
    LLM worker's signal_jobs/lead_stage fan-out) are dispatched as
    fresh Celery messages. Querying after commit avoids a race where
    the message would arrive before the row is durable.
    """
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

        if task.status == "completed":
            logger.info(
                "task_already_completed_skip",
                extra={"task_id": task_id, "step": task.step},
            )
            return

        interaction_id = task.interaction_id
        try:
            await mark_in_progress(session, task, worker_name=f"celery:{task.step}")
            await executor(session, task)
            await session.commit()
        except Exception:
            await session.rollback()
            raise

    # Post-commit downstream dispatch. Fresh session avoids reading
    # uncommitted state from the worker's session above.
    async with async_session_factory() as session:
        downstream_stmt = (
            select(ProcessingTask)
            .where(ProcessingTask.interaction_id == interaction_id)
            .where(ProcessingTask.status == "pending")
        )
        downstream_rows = (await session.execute(downstream_stmt)).scalars().all()

    for row in downstream_rows:
        celery_task = _DOWNSTREAM_DISPATCH.get(row.step)
        if celery_task is not None:
            celery_task.delay(str(row.id))


# Mapping from processing_task.step → Celery task that dispatches it.
# Populated below once the @celery_app.task definitions exist.
_DOWNSTREAM_DISPATCH: Dict[str, Any] = {}


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


@celery_app.task(name="dispatch.crm_push", queue="cold_lane", acks_late=True, max_retries=0)
def dispatch_crm_push(task_id: str):
    _run_async(_process_one(task_id, execute_crm_push))


# Populate the downstream-dispatch map now that the tasks exist. The
# step strings come from src.models.processing_task.TaskStep.
_DOWNSTREAM_DISPATCH.update({
    "signal_jobs":   dispatch_signal_jobs,
    "lead_stage":    dispatch_lead_stage,
    "crm_push":      dispatch_crm_push,
    "recording_poll": dispatch_recording_poll,
})


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
