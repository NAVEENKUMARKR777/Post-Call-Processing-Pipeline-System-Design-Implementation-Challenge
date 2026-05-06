from celery import Celery
from celery.signals import worker_process_init

from src.config import settings
from src.observability import configure_structured_logging

celery_app = Celery(
    "voicebot",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_default_queue=settings.POSTCALL_CELERY_QUEUE,
    # Three queues split by lane priority. Operators can give
    # hot_lane more workers than cold_lane during peak campaign
    # windows and thereby drain the hot lane in seconds even when
    # cold has thousands of pending tasks.
    task_queues=[
        {"name": "hot_lane",        "routing_key": "hot_lane"},
        {"name": "cold_lane",       "routing_key": "cold_lane"},
        {"name": "recording_poll",  "routing_key": "recording_poll"},
        # Legacy single-queue retained so existing in-flight messages
        # can still be processed by a transitional worker.
        {"name": settings.POSTCALL_CELERY_QUEUE, "routing_key": settings.POSTCALL_CELERY_QUEUE},
    ],
)


@worker_process_init.connect
def _init_worker_logging(**_: object) -> None:
    """Each Celery worker process must install the JSON formatter
    independently — the master's handlers don't propagate to forked
    children on Linux."""
    configure_structured_logging()
