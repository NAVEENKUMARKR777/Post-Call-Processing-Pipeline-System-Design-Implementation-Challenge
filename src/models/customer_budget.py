"""Customer LLM budget — the contract that drives per-customer fairness.

`reserved_tpm` / `reserved_rpm` are the guaranteed floor: no other
customer can consume them. `burst_tpm` is the maximum that customer is
allowed to borrow from shared headroom on top of the reservation. The
budget manager (src/scheduler/budget_manager.py) reads these rows on
every check_and_reserve() call.
"""

from sqlalchemy import Column, DateTime, Integer, SmallInteger, func
from sqlalchemy.dialects.postgresql import JSONB, UUID

from src.models.base import Base


class CustomerLLMBudget(Base):
    __tablename__ = "customer_llm_budgets"

    customer_id = Column(UUID(as_uuid=True), primary_key=True)
    reserved_tpm = Column(Integer, nullable=False, default=0)
    reserved_rpm = Column(Integer, nullable=False, default=0)
    burst_tpm = Column(Integer, nullable=False, default=0)
    priority = Column(SmallInteger, nullable=False, default=5)
    config = Column(JSONB, nullable=False, default=dict)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
