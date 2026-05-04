"""
PostCallProcessor — Runs full LLM analysis on every completed interaction.

KNOWN ISSUE: This processor runs on 100% of calls with identical priority.
A call where the lead confirmed a rebook gets the same treatment as a call
where the lead said "not interested" and hung up in 10 seconds. At 100K calls
per campaign, this creates a massive backlog where high-value calls are delayed
by hours behind thousands of non-actionable ones.

There is no triage, no prioritisation, and no way to distinguish actionable
outcomes before committing full LLM cost.
"""

import json
import logging
from datetime import datetime
from typing import Any, Dict, Optional
from dataclasses import dataclass

from src.config import settings
from src.services.circuit_breaker import circuit_breaker

logger = logging.getLogger(__name__)


@dataclass
class PostCallContext:
    interaction_id: str
    session_id: str
    lead_id: str
    campaign_id: str
    customer_id: str
    agent_id: str
    call_sid: str
    transcript_text: str
    conversation_data: dict
    additional_data: dict
    ended_at: datetime
    exotel_account_id: Optional[str] = None


@dataclass
class AnalysisResult:
    call_stage: str
    entities: Dict[str, Any]
    summary: str
    raw_response: Dict[str, Any]
    tokens_used: int
    latency_ms: float
    provider: str
    model: str


class PostCallProcessor:
    """
    Processes every interaction through full LLM analysis.

    Problems:
    1. No filtering — every call gets full LLM analysis regardless of outcome
    2. Single priority queue — rebook confirmations wait behind "not interested" calls
    3. Full streaming API cost on every call — no batch API option
    4. Tightly coupled to circuit breaker — backlog triggers dialler freeze
    5. No per-customer configuration — same prompt/model for everyone
    """

    async def process_post_call(
        self, ctx: PostCallContext, single_prompt: bool = True
    ) -> AnalysisResult:
        """
        Run full LLM analysis on the interaction transcript.

        This is called for EVERY completed interaction, regardless of whether
        the call produced an actionable outcome.
        """

        # Track LLM usage for circuit breaker
        await circuit_breaker.record_postcall_start()

        try:
            prompt = self._build_analysis_prompt(
                ctx.transcript_text,
                ctx.additional_data,
                single_prompt,
            )

            # Call LLM API (streaming, full cost)
            start_time = datetime.utcnow()
            response = await self._call_llm(prompt)
            elapsed_ms = (datetime.utcnow() - start_time).total_seconds() * 1000

            result = self._parse_response(response, elapsed_ms)

            # Write result directly to interaction_metadata
            await self._update_interaction_metadata(ctx.interaction_id, result)

            logger.info(
                "postcall_analysis_complete",
                extra={
                    "interaction_id": ctx.interaction_id,
                    "call_stage": result.call_stage,
                    "tokens_used": result.tokens_used,
                    "latency_ms": result.latency_ms,
                },
            )

            return result

        except Exception as e:
            logger.exception(
                "postcall_analysis_failed",
                extra={
                    "interaction_id": ctx.interaction_id,
                    "error": str(e),
                },
            )
            raise

        finally:
            await circuit_breaker.record_postcall_end()

    def _build_analysis_prompt(
        self,
        transcript: str,
        additional_data: dict,
        single_prompt: bool,
    ) -> str:
        """
        Build the LLM prompt for post-call analysis.
        Uses a single combined prompt (entity extraction + classification + summary).
        """
        system_prompt = """You are a call analysis assistant. Analyze the following
call transcript and extract:
1. call_stage: The outcome/disposition of the call
2. entities: Key information mentioned (dates, times, amounts, names, preferences)
3. summary: A brief summary of what happened in the call

Respond in JSON format:
{
    "call_stage": "...",
    "entities": {...},
    "summary": "..."
}"""

        return f"{system_prompt}\n\nTranscript:\n{transcript}\n\nAdditional context:\n{json.dumps(additional_data)}"

    async def _call_llm(self, prompt: str) -> dict:
        """
        Call the LLM API. In production, this uses the configured provider.
        Mock implementation for assessment.
        """
        # Mock: In production, this calls OpenAI/Gemini/etc via httpx
        return {
            "call_stage": "unknown",
            "entities": {},
            "summary": "Mock analysis result",
            "usage": {"total_tokens": 1500},
        }

    def _parse_response(self, response: dict, latency_ms: float) -> AnalysisResult:
        return AnalysisResult(
            call_stage=response.get("call_stage", "unknown"),
            entities=response.get("entities", {}),
            summary=response.get("summary", ""),
            raw_response=response,
            tokens_used=response.get("usage", {}).get("total_tokens", 0),
            latency_ms=latency_ms,
            provider=settings.LLM_PROVIDER,
            model=settings.LLM_MODEL,
        )

    async def _update_interaction_metadata(
        self, interaction_id: str, result: AnalysisResult
    ) -> None:
        """
        Write analysis results to interaction_metadata JSONB column.
        This is the hot cache the dashboard reads.
        """
        # Mock: In production, this runs an UPDATE query
        logger.info(
            "metadata_updated",
            extra={
                "interaction_id": interaction_id,
                "call_stage": result.call_stage,
            },
        )


post_call_processor = PostCallProcessor()
