from fastapi import FastAPI

from src.api.dialler import router as dialler_router
from src.api.dialler import set_controller
from src.api.endpoints import router
from src.observability import configure_structured_logging
from src.scheduler import BackpressureController, BudgetManager, RateLimiter
from src.utils.db import async_session_factory
from src.utils.redis_client import redis_client

# Install the JSON formatter exactly once at process startup. The
# Celery worker entrypoint also calls this so workers and the API
# share the same log schema.
configure_structured_logging()


def _build_backpressure_controller() -> BackpressureController:
    rate_limiter = RateLimiter(redis_client)
    budget_manager = BudgetManager(async_session_factory, rate_limiter)
    return BackpressureController(budget_manager)


# Wire the singleton controller used by the /dialler/backpressure
# endpoint. Done at import time so a 1-second startup race never
# returns 503 to a request that arrives during init.
set_controller(_build_backpressure_controller())


app = FastAPI(
    title="VoiceBot Post-Call Processing",
    version="1.0.0",
)

app.include_router(router, prefix="/api/v1")
app.include_router(dialler_router, prefix="/api/v1")
