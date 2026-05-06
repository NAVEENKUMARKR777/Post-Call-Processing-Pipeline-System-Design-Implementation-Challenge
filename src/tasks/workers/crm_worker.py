"""CRM-push worker — sends the post-call analysis to a customer's
configured CRM webhook.

Demonstrates the durable workflow's natural extensibility: the
crm_push step is just another row in processing_tasks, with the
same retry / dead-letter semantics as every other step. No
bespoke retry logic, no fire-and-forget.

The actual HTTP call is mocked in this implementation — production
would read the CRM endpoint URL from customer_llm_budgets.config
JSONB and POST a signed payload there.
"""

from __future__ import annotations

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.processing_task import ProcessingTask
from src.observability import bind_interaction_context, get_logger
from src.scheduler.task_dispatcher import mark_completed, mark_for_retry
from src.services.audit_logger import record_event

logger = get_logger(__name__)


async def execute_crm_push(
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
    crm_url = payload.get("crm_webhook_url")

    if not crm_url:
        # No CRM configured for this customer — that's not a failure;
        # complete the task with a reason so dashboards can show
        # "CRM disabled for customer X" instead of a missing event.
        await mark_completed(
            session,
            task,
            detail={"reason": "no_crm_configured"},
        )
        return True

    body = {
        "interaction_id": str(task.interaction_id),
        "customer_id": str(task.customer_id),
        "campaign_id": str(task.campaign_id),
        "call_stage": (payload.get("analysis_result") or {}).get("call_stage"),
        "summary": (payload.get("analysis_result") or {}).get("summary"),
        "entities": (payload.get("analysis_result") or {}).get("entities") or {},
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(crm_url, json=body)
        if resp.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"CRM responded {resp.status_code}",
                request=resp.request,
                response=resp,
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
            detail={"error": str(exc), "crm_url_host": _host(crm_url)},
        )
        await mark_for_retry(
            session,
            task,
            error=str(exc),
            detail={"crm_url_host": _host(crm_url)},
        )
        return False

    await mark_completed(
        session,
        task,
        detail={"crm_url_host": _host(crm_url), "response_code": resp.status_code},
    )
    return True


def _host(url: str | None) -> str:
    if not url:
        return ""
    try:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        return parsed.hostname or ""
    except Exception:
        return ""
