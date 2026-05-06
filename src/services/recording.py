"""Recording fetch primitive — refactored to remove the 45-second
blocking sleep that used to live here.

This module now exposes a single `try_fetch_recording()` call that
runs at most one Exotel poll + S3 upload attempt. The decision of
*when* to retry, *how often*, and *when to give up* lives in the
new src/services/recording_poller.py worker, which wraps this
primitive in a durable processing_tasks-backed loop.

The old behaviour (asyncio.sleep(45) before a single try) was the
core of the assignment's recording-pipeline problem: it blocked
every Celery task for 45s regardless of recording availability,
silently dropped recordings that arrived late, and emitted only a
DEBUG-level log on failure (effectively invisible).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Optional

import httpx

from src.config import settings
from src.observability import get_logger

logger = get_logger(__name__)


class RecordingProbeStatus(str, enum.Enum):
    AVAILABLE = "available"          # 200 OK, recording_url returned
    NOT_READY = "not_ready"          # 404 / no recording_url; provider hasn't published yet
    PROVIDER_ERROR = "provider_error"  # 5xx, network error, timeout
    NO_CALL = "no_call"              # provider says recording for this call_sid does not exist


@dataclass(frozen=True)
class ProbeResult:
    status: RecordingProbeStatus
    recording_url: Optional[str] = None
    s3_key: Optional[str] = None
    detail: Optional[str] = None


async def try_fetch_recording(
    *,
    interaction_id: str,
    call_sid: str,
    exotel_account_id: str,
) -> ProbeResult:
    """Attempt one fetch + upload cycle.

    Distinguishes four outcomes so the poller can decide intelligently:
      AVAILABLE       upload happened; we have an s3_key.
      NOT_READY       try again later.
      PROVIDER_ERROR  transient; try again later.
      NO_CALL         the provider says this call has no recording
                      ever (vs. just not yet) — stop polling.

    Lumping all of these into a single None return was the previous
    silent-drop bug.
    """
    try:
        recording_url = await _fetch_exotel_recording_url(call_sid, exotel_account_id)
    except _NoCallSid as exc:
        return ProbeResult(status=RecordingProbeStatus.NO_CALL, detail=str(exc))
    except httpx.HTTPError as exc:
        return ProbeResult(status=RecordingProbeStatus.PROVIDER_ERROR, detail=str(exc))

    if not recording_url:
        return ProbeResult(status=RecordingProbeStatus.NOT_READY)

    try:
        s3_key = await _upload_to_s3(recording_url, interaction_id)
    except Exception as exc:
        return ProbeResult(
            status=RecordingProbeStatus.PROVIDER_ERROR,
            recording_url=recording_url,
            detail=f"s3_upload_failed: {exc}",
        )

    return ProbeResult(
        status=RecordingProbeStatus.AVAILABLE,
        recording_url=recording_url,
        s3_key=s3_key,
    )


class _NoCallSid(Exception):
    """Raised when the provider definitively says the call_sid has no
    recording (e.g., a structured 404 with a 'call not found' code).
    Used to short-circuit the poll loop instead of retrying forever
    on a known-empty case."""


async def _fetch_exotel_recording_url(call_sid: str, account_id: str) -> Optional[str]:
    """Mocked fetch — production hits the real Exotel REST API."""
    if not call_sid:
        raise _NoCallSid("missing call_sid")

    url = f"https://api.exotel.com/v1/Accounts/{account_id}/Calls/{call_sid}/Recording"
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url)

    if resp.status_code == 200:
        data = resp.json()
        return data.get("recording_url")
    if resp.status_code == 404:
        # Exotel returns 404 both for "not yet" AND "this call_sid
        # never had a recording". Without a more specific signal we
        # treat 404 as NOT_READY and let the poller exhaust attempts;
        # the dead-letter is the eventual signal that there was
        # never going to be one.
        return None
    raise httpx.HTTPError(f"unexpected status {resp.status_code}")


async def fetch_and_upload_recording(
    interaction_id: str,
    call_sid: str,
    exotel_account_id: str,
) -> Optional[str]:
    """Deprecated single-shot helper preserved for the legacy
    celery_tasks pipeline. The new code path uses the durable
    poller; this shim exists only until the endpoint cut-over
    in commit 12 retires the old task entirely."""
    result = await try_fetch_recording(
        interaction_id=interaction_id,
        call_sid=call_sid,
        exotel_account_id=exotel_account_id,
    )
    return result.s3_key


async def _upload_to_s3(recording_url: str, interaction_id: str) -> str:
    """Mocked S3 upload — production downloads the recording and
    PUTs it to S3_BUCKET / recordings/{interaction_id}.mp3."""
    s3_key = f"recordings/{interaction_id}.mp3"
    logger.info(
        "recording_uploaded_to_s3",
        extra={
            "interaction_id": interaction_id,
            "s3_key": s3_key,
            "bucket": settings.S3_BUCKET,
        },
    )
    return s3_key
