import enum
import uuid

from sqlalchemy import Enum, ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, UUIDPkMixin


class UsageEventType(str, enum.Enum):
    TRANSCRIPTION_MINUTE = "transcription_minute"
    AI_GENERATION = "ai_generation"


class UsageEvent(Base, UUIDPkMixin, CreatedAtMixin):
    """Feeds Stripe usage-record reporting for the overage portion of
    billing. quantity is Numeric, not float — this ends up in invoices."""

    __tablename__ = "usage_events"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    event_type: Mapped[UsageEventType] = mapped_column(
        Enum(UsageEventType, name="usage_event_type", native_enum=True), nullable=False
    )
    quantity: Mapped[float] = mapped_column(Numeric(12, 4), nullable=False)
    stripe_usage_record_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
