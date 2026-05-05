"""LLM token consumption ledger — per-call attribution.

The README lists "all LLM spending must be attributable" as a hard
constraint. This table answers "how many tokens did Customer X spend
on Campaign Y in the last hour?" in a single SQL query.

Writes are append-only and happen *after* the LLM returns successful,
when actual_tokens is known. Reservations against the rate limiter
happen separately at request time using estimated tokens.
"""

from sqlalchemy import BigInteger, Column, DateTime, Integer, Text, func
from sqlalchemy.dialects.postgresql import UUID

from src.models.base import Base


class LLMTokenLedger(Base):
    __tablename__ = "llm_token_ledger"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    customer_id = Column(UUID(as_uuid=True), nullable=False)
    campaign_id = Column(UUID(as_uuid=True))
    interaction_id = Column(UUID(as_uuid=True), nullable=False)

    request_kind = Column(Text, nullable=False)
    tokens_used = Column(Integer, nullable=False)
    model = Column(Text)
    correlation_id = Column(Text)

    occurred_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
