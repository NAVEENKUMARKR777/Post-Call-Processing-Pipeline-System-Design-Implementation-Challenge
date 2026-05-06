"""Tests for the audit-log and token-ledger writer services.

These services own the contract that:
  - every interaction is reconstructable from the audit log alone
  - every LLM token is attributable to (customer, campaign, interaction)

so this is the layer where a bug becomes invisible (silent drops,
wrong customer attribution). The tests inspect what the writer
*hands to the session*, not what the database persists — that
isolates writer-correctness from DB driver concerns and means
pytest passes without docker-compose running.
"""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from src.observability import bind_correlation_id, clear_context
from src.services.audit_logger import record_event
from src.services.token_ledger import record_consumption


@pytest.fixture(autouse=True)
def _clear_logging_context():
    clear_context()
    yield
    clear_context()


def _fake_session():
    """A thin stand-in for AsyncSession that captures session.add() calls."""
    session = MagicMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    return session


@pytest.mark.asyncio
async def test_record_event_persists_all_required_fields():
    session = _fake_session()
    interaction_id = uuid4()
    customer_id = uuid4()
    cid = bind_correlation_id("cid-test-1")

    row = await record_event(
        session,
        interaction_id=interaction_id,
        step="llm_analysis",
        status="started",
        customer_id=customer_id,
        attempt=2,
        detail={"estimated_tokens": 1500, "lane": "hot"},
    )

    session.add.assert_called_once()
    session.flush.assert_awaited_once()
    persisted = session.add.call_args.args[0]

    assert persisted.interaction_id == interaction_id
    assert persisted.customer_id == customer_id
    assert persisted.step == "llm_analysis"
    assert persisted.status == "started"
    assert persisted.attempt == 2
    assert persisted.correlation_id == cid
    assert persisted.detail["estimated_tokens"] == 1500
    assert persisted.detail["lane"] == "hot"
    # Returns the row object so callers can chain (e.g. log row.id).
    assert row is persisted


@pytest.mark.asyncio
async def test_record_event_uses_inherited_correlation_id():
    """If the caller does not pass correlation_id, the writer must
    pull it from the ContextVar — that's how a single endpoint
    request threads its id through every audit row."""
    session = _fake_session()
    bind_correlation_id("inherited-cid-9")

    await record_event(
        session,
        interaction_id=str(uuid4()),
        step="recording_poll",
        status="completed",
    )

    persisted = session.add.call_args.args[0]
    assert persisted.correlation_id == "inherited-cid-9"


@pytest.mark.asyncio
async def test_record_event_falls_back_when_no_context():
    session = _fake_session()
    # No bind_correlation_id() and no parameter → must not crash, and
    # must not be empty (an empty correlation_id breaks log-grep flows).
    await record_event(
        session,
        interaction_id=str(uuid4()),
        step="signal_jobs",
        status="failed",
    )
    persisted = session.add.call_args.args[0]
    assert persisted.correlation_id == "unknown"


@pytest.mark.asyncio
async def test_record_event_does_not_commit_caller_owns_transaction():
    """The audit row must commit atomically with whatever change it
    documents (e.g., processing_tasks status). If this writer commits
    on its own, audit-log-says-yes-table-says-no inconsistencies
    appear when the caller rolls back."""
    session = _fake_session()
    session.commit = AsyncMock()
    await record_event(
        session,
        interaction_id=str(uuid4()),
        step="lead_stage",
        status="completed",
    )
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_record_consumption_persists_attribution():
    session = _fake_session()
    customer_id = uuid4()
    campaign_id = uuid4()
    interaction_id = uuid4()
    bind_correlation_id("ledger-cid-42")

    row = await record_consumption(
        session,
        customer_id=customer_id,
        interaction_id=interaction_id,
        campaign_id=campaign_id,
        tokens_used=2300,
        model="gpt-4o",
    )

    session.add.assert_called_once()
    persisted = session.add.call_args.args[0]
    assert persisted.customer_id == customer_id
    assert persisted.campaign_id == campaign_id
    assert persisted.interaction_id == interaction_id
    assert persisted.tokens_used == 2300
    assert persisted.model == "gpt-4o"
    assert persisted.request_kind == "post_call_analysis"
    assert persisted.correlation_id == "ledger-cid-42"
    assert row is persisted


@pytest.mark.asyncio
async def test_record_consumption_rejects_negative_tokens():
    """Defensive: a buggy provider returning -1 tokens must not be
    silently absorbed into a customer's bill."""
    session = _fake_session()
    with pytest.raises(ValueError):
        await record_consumption(
            session,
            customer_id=uuid4(),
            interaction_id=uuid4(),
            tokens_used=-5,
        )
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_record_consumption_default_request_kind_is_post_call_analysis():
    session = _fake_session()
    await record_consumption(
        session,
        customer_id=uuid4(),
        interaction_id=uuid4(),
        tokens_used=1200,
    )
    persisted = session.add.call_args.args[0]
    assert persisted.request_kind == "post_call_analysis"
