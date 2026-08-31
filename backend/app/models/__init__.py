"""Import every model here so Alembic's autogenerate (and Base.metadata in
general) sees the full schema from a single import of app.models."""
from app.models.base import Base
from app.models.clip import Clip
from app.models.generation_log import GenerationLog, GenerationStage
from app.models.media_file import MediaFile
from app.models.sermon import Sermon, SermonFormat
from app.models.sermon_embedding import SermonEmbedding
from app.models.tenant import Tenant
from app.models.usage_event import UsageEvent, UsageEventType
from app.models.user import User, UserRole
from app.models.webhook_event import WebhookEvent

__all__ = [
    "Base",
    "Tenant",
    "User",
    "UserRole",
    "Sermon",
    "SermonFormat",
    "SermonEmbedding",
    "MediaFile",
    "Clip",
    "UsageEvent",
    "UsageEventType",
    "GenerationLog",
    "GenerationStage",
    "WebhookEvent",
]
