from fastapi import FastAPI

from src.api.endpoints import router
from src.observability import configure_structured_logging

# Install the JSON formatter exactly once at process startup. The
# Celery worker entrypoint also calls this so workers and the API
# share the same log schema.
configure_structured_logging()

app = FastAPI(
    title="VoiceBot Post-Call Processing",
    version="1.0.0",
)

app.include_router(router, prefix="/api/v1")
