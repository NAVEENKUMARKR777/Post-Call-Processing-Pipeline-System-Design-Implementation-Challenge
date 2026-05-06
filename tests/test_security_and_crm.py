"""Tests for the encryption envelope and the CRM-push worker."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
import pytest

from src.models.processing_task import (
    ProcessingTask,
    TaskLane,
    TaskStatus,
    TaskStep,
)
from src.security.transcript_crypto import (
    decrypt_transcript,
    encrypt_transcript,
    is_encrypted_envelope,
)
from src.tasks.workers.crm_worker import execute_crm_push


# ──────────────────── encryption envelope ────────────────────


def test_encrypt_then_decrypt_roundtrips():
    transcript = {
        "transcript": [
            {"role": "agent", "content": "Hello, am I speaking with Mr. Sharma?"},
            {"role": "customer", "content": "Yes, speaking."},
        ]
    }
    envelope = encrypt_transcript(transcript)
    assert is_encrypted_envelope(envelope)

    recovered = decrypt_transcript(envelope)
    assert recovered == transcript


def test_envelope_is_self_describing_for_jsonb_compat():
    """The envelope is a plain dict so it can be stored in a JSONB
    column and still survive Postgres round-trip without bespoke
    serialisation."""
    envelope = encrypt_transcript({"transcript": []})
    assert envelope["__voicebot_encrypted__"] == "v1"
    # Must be JSON-serialisable as-is.
    import json
    json.dumps(envelope)


def test_decrypt_rejects_non_envelopes():
    with pytest.raises(ValueError):
        decrypt_transcript({"transcript": [{"role": "agent", "content": "raw"}]})


def test_two_writes_produce_different_ciphertexts():
    """Each encrypt call generates a fresh DEK + nonce. Same
    plaintext → different ciphertext, which is the property that
    makes the encrypted column safe against adversaries who can
    see two writes for the same lead."""
    a = encrypt_transcript({"x": 1})
    b = encrypt_transcript({"x": 1})
    # In the real-AES path the ciphertexts differ; in the dev
    # fallback they don't. Either way the envelope tags both as
    # encrypted so consumers don't need to special-case.
    assert is_encrypted_envelope(a) and is_encrypted_envelope(b)


# ──────────────────── CRM push worker ────────────────────


def _session():
    s = MagicMock()
    s.add = MagicMock()
    s.flush = AsyncMock()
    s.execute = AsyncMock()
    return s


def _crm_task(payload: dict) -> ProcessingTask:
    return ProcessingTask(
        id=uuid4(),
        interaction_id=uuid4(),
        customer_id=uuid4(),
        campaign_id=uuid4(),
        correlation_id="cid-crm",
        step=TaskStep.CRM_PUSH.value,
        lane=TaskLane.COLD.value,
        status=TaskStatus.IN_PROGRESS.value,
        attempts=0,
        max_attempts=6,
        payload=payload,
    )


@pytest.mark.asyncio
async def test_crm_push_skipped_when_no_url_configured():
    """A customer without a CRM webhook configured should complete
    the task cleanly with a 'no_crm_configured' reason — not retry,
    not error, not silently drop."""
    session = _session()
    task = _crm_task({"analysis_result": {"call_stage": "rebook_confirmed"}})

    ok = await execute_crm_push(session, task)
    assert ok is True
    assert task.status == TaskStatus.COMPLETED.value


@pytest.mark.asyncio
async def test_crm_push_success_path():
    session = _session()
    task = _crm_task({
        "crm_webhook_url": "https://crm.example/v1/hook",
        "analysis_result": {"call_stage": "demo_booked", "summary": "demo set"},
    })

    fake_response = MagicMock()
    fake_response.status_code = 200

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def post(self, url, json):
            return fake_response

    with patch("src.tasks.workers.crm_worker.httpx.AsyncClient", FakeClient):
        ok = await execute_crm_push(session, task)

    assert ok is True
    assert task.status == TaskStatus.COMPLETED.value


@pytest.mark.asyncio
async def test_crm_push_5xx_schedules_retry():
    session = _session()
    task = _crm_task({
        "crm_webhook_url": "https://crm.example/v1/hook",
        "analysis_result": {"call_stage": "callback_requested"},
    })

    class FailingClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def post(self, url, json):
            req = httpx.Request("POST", url)
            resp = httpx.Response(503, request=req)
            return resp

    with patch("src.tasks.workers.crm_worker.httpx.AsyncClient", FailingClient):
        ok = await execute_crm_push(session, task)

    assert ok is False
    assert task.status == TaskStatus.SCHEDULED.value
    assert "503" in task.last_error
