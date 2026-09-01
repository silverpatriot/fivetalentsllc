import enum
import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, UUIDPkMixin, pg_enum


class SermonFormat(str, enum.Enum):
    EXPOSITORY = "expository"
    TOPICAL = "topical"
    NARRATIVE = "narrative"
    TEXTUAL = "textual"
    CUSTOM = "custom"


class Sermon(Base, UUIDPkMixin, CreatedAtMixin):
    __tablename__ = "sermons"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    format: Mapped[SermonFormat] = mapped_column(pg_enum(SermonFormat, name="sermon_format"), nullable=False)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Migration 0010 — a preachable outline condensed FROM an already-
    # generated content (manuscript), created on demand via POST
    # /sermons/{id}/outline (app/services/generation.py's
    # generate_outline_from_manuscript). Distinct from the internal
    # pre-draft outline pass that already runs during generation
    # (context_assembly.build_outline_messages) — that one is streamed to
    # the frontend and never persisted; this is the real, saved artifact.
    outline: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Free-form status (draft/generating/ready/published/...) — deliberately
    # not a native enum yet; the state machine isn't locked in this phase.
    status: Mapped[str] = mapped_column(String(50), nullable=False, server_default="draft")
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )
