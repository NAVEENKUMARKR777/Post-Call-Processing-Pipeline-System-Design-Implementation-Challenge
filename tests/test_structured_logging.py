"""Verifies the structured-logging foundation that every other commit
relies on. If correlation_id propagation breaks, AC5/AC6 (debug a
3-day-old failure by interaction_id) become impossible.
"""

import asyncio
import io
import json
import logging

import pytest

from src.observability import (
    bind_correlation_id,
    bind_interaction_context,
    clear_context,
    configure_structured_logging,
    get_correlation_id,
    get_logger,
)
from src.observability.logging import StructuredJsonFormatter


@pytest.fixture(autouse=True)
def _isolate_context():
    clear_context()
    yield
    clear_context()


def _capture(level: int = logging.INFO):
    """Return (logger, buffer) where the logger writes JSON lines into
    the buffer. Uses the StructuredJsonFormatter directly so the test
    is independent of the global configure_structured_logging() state."""
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(StructuredJsonFormatter())
    logger = logging.getLogger(f"test.{id(buf)}")
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    return logger, buf


def _last_record(buf: io.StringIO) -> dict:
    line = buf.getvalue().strip().splitlines()[-1]
    return json.loads(line)


def test_log_line_is_valid_json_with_required_fields():
    logger, buf = _capture()
    logger.info("step_started")
    record = _last_record(buf)

    assert record["event"] == "step_started"
    assert record["level"] == "INFO"
    assert record["timestamp"].endswith("Z")
    assert record["logger"].startswith("test.")


def test_correlation_id_is_inherited_by_log_lines():
    logger, buf = _capture()
    cid = bind_correlation_id("fixed-cid-001")
    assert cid == "fixed-cid-001"
    logger.info("worker_claimed_task")

    record = _last_record(buf)
    assert record["correlation_id"] == "fixed-cid-001"


def test_interaction_context_propagates_to_extra_fields():
    logger, buf = _capture()
    bind_correlation_id("cid-xyz")
    bind_interaction_context(
        interaction_id="int-1",
        customer_id="cust-A",
        campaign_id="camp-9",
    )
    logger.info("llm_call_started", extra={"step": "llm_analysis", "attempt": 1})

    record = _last_record(buf)
    assert record["correlation_id"] == "cid-xyz"
    assert record["interaction_id"] == "int-1"
    assert record["customer_id"] == "cust-A"
    assert record["campaign_id"] == "camp-9"
    assert record["step"] == "llm_analysis"
    assert record["attempt"] == 1


def test_clear_context_isolates_consecutive_tasks():
    logger, buf = _capture()
    bind_correlation_id("first-task")
    bind_interaction_context(interaction_id="A")
    logger.info("task_one")

    clear_context()
    logger.info("between_tasks")

    bind_correlation_id("second-task")
    bind_interaction_context(interaction_id="B")
    logger.info("task_two")

    lines = [json.loads(l) for l in buf.getvalue().strip().splitlines()]
    assert lines[0]["correlation_id"] == "first-task"
    assert lines[0]["interaction_id"] == "A"
    assert "correlation_id" not in lines[1]
    assert "interaction_id" not in lines[1]
    assert lines[2]["correlation_id"] == "second-task"
    assert lines[2]["interaction_id"] == "B"


def test_correlation_id_is_isolated_between_async_contexts():
    """ContextVars copy on Task creation, so concurrent tasks must not
    bleed correlation_ids into each other. This is the property that
    makes the design safe to use under FastAPI/Celery concurrency.
    """
    logger, buf = _capture()

    async def worker(name: str):
        bind_correlation_id(f"cid-{name}")
        await asyncio.sleep(0)
        logger.info("inside_task", extra={"task_name": name})
        return get_correlation_id()

    async def driver():
        results = await asyncio.gather(
            asyncio.create_task(worker("alpha")),
            asyncio.create_task(worker("beta")),
            asyncio.create_task(worker("gamma")),
        )
        return results

    results = asyncio.run(driver())
    assert sorted(results) == ["cid-alpha", "cid-beta", "cid-gamma"]

    lines = [json.loads(l) for l in buf.getvalue().strip().splitlines()]
    name_to_cid = {r["task_name"]: r["correlation_id"] for r in lines}
    assert name_to_cid == {
        "alpha": "cid-alpha",
        "beta": "cid-beta",
        "gamma": "cid-gamma",
    }


def test_get_logger_returns_a_logger_instance():
    logger = get_logger("verify")
    assert isinstance(logger, logging.Logger)


def test_configure_structured_logging_is_idempotent():
    configure_structured_logging()
    configure_structured_logging()
    root = logging.getLogger()
    assert sum(1 for h in root.handlers if isinstance(h, logging.StreamHandler)) == 1
