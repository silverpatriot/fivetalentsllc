import enum
import uuid
from datetime import datetime

from sqlalchemy import Enum, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, UUIDPkMixin


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
    format: Mapped[SermonFormat] = mapped_column(
        Enum(SermonFormat, name="sermon_format", native_enum=True), nullable=False
    )
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Free-form status (draft/generating/ready/published/...) — deliberately
    # not a native enum yet; the state machine isn't locked in this phase.
    status: Mapped[str] = mapped_column(String(50), nullable=False, server_default="draft")
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )
