import uuid

from sqlalchemy import ForeignKey, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, UUIDPkMixin


class MediaFile(Base, UUIDPkMixin, CreatedAtMixin):
    __tablename__ = "media_files"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sermon_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sermons.id", ondelete="SET NULL"), nullable=True, index=True
    )
    original_filename: Mapped[str] = mapped_column(String(500), nullable=False)
    storage_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    duration_seconds: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    # pending -> processing -> completed | failed
    transcription_status: Mapped[str] = mapped_column(String(50), nullable=False, server_default="pending")
    transcript_text: Mapped[str | None] = mapped_column(Text, nullable=True)
