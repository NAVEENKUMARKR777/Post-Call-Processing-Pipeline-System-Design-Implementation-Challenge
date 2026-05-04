"""
Recording pipeline — fetches call recording from Exotel and uploads to S3.

KNOWN ISSUE: Uses a hardcoded asyncio.sleep(45s) to wait for the recording to
become available. If Exotel delivers the recording in 10 seconds, 35 seconds are
wasted. If delivery takes 60+ seconds (common under high concurrency), the
recording is silently skipped with no retry, no alert, and no visibility.
"""

import asyncio
import logging
from typing import Optional

import httpx

from src.config import settings

logger = logging.getLogger(__name__)


async def fetch_and_upload_recording(
    interaction_id: str,
    call_sid: str,
    exotel_account_id: str,
) -> Optional[str]:
    """
    Waits a fixed 45 seconds, then attempts ONE fetch of the recording URL.
    If the recording isn't available yet, it's silently skipped.

    Returns the S3 key if successful, None otherwise.
    """

    # BUG: Hardcoded sleep — no polling, no retry, no backoff
    await asyncio.sleep(settings.RECORDING_WAIT_SECONDS)

    try:
        recording_url = await _fetch_exotel_recording_url(call_sid, exotel_account_id)

        if not recording_url:
            # Recording not available after 45s — silently skipped
            # No retry mechanism, no alert, no logging of the miss
            logger.debug(
                "recording_not_available",
                extra={
                    "interaction_id": interaction_id,
                    "call_sid": call_sid,
                },
            )
            return None

        s3_key = await _upload_to_s3(recording_url, interaction_id)
        return s3_key

    except Exception as e:
        # Exceptions are swallowed — the caller never knows this failed
        logger.exception(
            "recording_upload_error",
            extra={"interaction_id": interaction_id, "error": str(e)},
        )
        return None


async def _fetch_exotel_recording_url(
    call_sid: str, account_id: str
) -> Optional[str]:
    """
    Calls Exotel API to get the recording URL for a completed call.
    In production, this hits the Exotel REST API.
    """
    # Mock implementation for assessment
    url = f"https://api.exotel.com/v1/Accounts/{account_id}/Calls/{call_sid}/Recording"

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                data = resp.json()
                return data.get("recording_url")
            return None
    except httpx.HTTPError:
        return None


async def _upload_to_s3(recording_url: str, interaction_id: str) -> str:
    """Downloads recording from URL and uploads to S3."""
    s3_key = f"recordings/{interaction_id}.mp3"

    # Mock: In production, this downloads the file and uploads via boto3
    logger.info(
        "recording_uploaded",
        extra={"interaction_id": interaction_id, "s3_key": s3_key},
    )
    return s3_key
