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
)

# Task modules register themselves here as they're added, e.g.:
# celery_app.autodiscover_tasks(["app.tasks.transcription", "app.tasks.generation"])
