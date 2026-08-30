import uuid

from sqlalchemy import ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, UUIDPkMixin


class Clip(Base, UUIDPkMixin, CreatedAtMixin):
    __tablename__ = "clips"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    media_file_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("media_files.id", ondelete="CASCADE"), nullable=False, index=True
    )
    start_seconds: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    end_seconds: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    # pending -> processing -> completed | failed
    status: Mapped[str] = mapped_column(String(50), nullable=False, server_default="pending")
    storage_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
