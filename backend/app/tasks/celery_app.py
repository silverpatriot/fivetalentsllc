from celery import Celery

from app.core.config import get_settings

settings = get_settings()

celery_app = Celery(
    "sermon_engine",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    # Sermon media can be long; don't let a slow transcription starve the
    # queue for everyone else's tasks.
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    # String references, not direct imports — a task module importing
    # celery_app (to get the @celery_app.task decorator) while celery_app
    # imports it back would be circular. `imports` tells the worker
    # process to import these at startup instead.
    imports=["app.tasks.usage_reporting", "app.tasks.embeddings"],
    beat_schedule={
        "sweep-unreported-usage": {
            "task": "app.tasks.usage_reporting.sweep_unreported_usage",
            # Safety-net sweep — record_usage_event() already queues each
            # event's report immediately on write. This just catches
            # anything that path missed. 5 minutes is arbitrary and cheap
            # to tighten once there's real usage volume to tune against.
            "schedule": 300.0,
        },
    },
)
