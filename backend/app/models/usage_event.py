import enum
import uuid

from sqlalchemy import Boolean, ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, UUIDPkMixin, pg_enum
from app.models.generation_log import GenerationStage


class UsageEventType(str, enum.Enum):
    TRANSCRIPTION_MINUTE = "transcription_minute"
    AI_GENERATION = "ai_generation"


class UsageEvent(Base, UUIDPkMixin, CreatedAtMixin):
    """The raw ledger of what actually happened — feeds Stripe
    usage-record reporting for the overage portion of billing, but is
    deliberately not the same thing as "what gets billed" (see
    `billable` below). quantity is Numeric, not float — this ends up in
    invoices.

    sermon_id/generation_stage/outcome exist to make an AI_GENERATION row
    traceable to a specific sermon and LLM call: Phase 3 records one row
    per real LLM call (outline, draft — each independently, regardless of
    success/failure), not one row per user-facing generation action, so a
    church asking "why was I billed for this" can be answered from this
    table alone. All three are null for TRANSCRIPTION_MINUTE rows, which
    have no such breakdown.
    """

    __tablename__ = "usage_events"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    event_type: Mapped[UsageEventType] = mapped_column(
        pg_enum(UsageEventType, name="usage_event_type"), nullable=False
    )
    quantity: Mapped[float] = mapped_column(Numeric(12, 4), nullable=False)
    stripe_usage_record_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    sermon_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sermons.id", ondelete="SET NULL"), nullable=True, index=True
    )
    generation_stage: Mapped[GenerationStage | None] = mapped_column(
        pg_enum(GenerationStage, name="generation_stage"), nullable=True
    )
    # Free-form, like sermons.status — deliberately not locked into a
    # native enum yet. "succeeded" | "failed" for now.
    outcome: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # Whether this row should ever be reported to Stripe at all — see
    # report_usage_event/sweep_unreported_usage in
    # app/tasks/usage_reporting.py. Defaults True (every pre-Phase-3 call
    # site, and TRANSCRIPTION_MINUTE, is unaffected); the new per-LLM-call
    # AI_GENERATION rows are written with billable=False on purpose —
    # converting "an LLM call happened, outcome X" into "here's what to
    # actually charge for" is an explicit downstream decision this phase
    # deliberately did not make (see the Phase 3 completion notes), and
    # defaulting to billable=True for those rows would have silently
    # double-billed every successful generation (one row each for outline
    # and draft, where Phase 2 billed one unit per generation) the moment
    # this shipped.
    billable: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
