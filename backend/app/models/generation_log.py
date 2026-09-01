import enum
import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, UUIDPkMixin, pg_enum


class GenerationStage(str, enum.Enum):
    OUTLINE = "outline"
    DRAFT = "draft"
    # Migration 0010 — the on-demand, post-manuscript outline-condensing
    # pass (app/services/generation.py's generate_outline_from_manuscript),
    # distinct from OUTLINE (the internal pre-draft pass every generation
    # already ran, never persisted on its own).
    OUTLINE_CONDENSE = "outline_condense"


class GenerationLog(Base, UUIDPkMixin, CreatedAtMixin):
    """What was actually sent to the model and what came back, per LLM
    call — Task 3's "if a church complains about bad output, you need to
    be able to see what happened, not just the polished final version."

    Tenant-scoped (RLS) like everything else a pastor's own data lives in
    — this is reviewable *with* the tenant, via the same access controls
    as the sermon itself, not a separate platform-admin-only audit log.
    """

    __tablename__ = "generation_logs"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sermon_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sermons.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    stage: Mapped[GenerationStage] = mapped_column(
        pg_enum(GenerationStage, name="generation_stage"), nullable=False
    )
    model: Mapped[str] = mapped_column(Text, nullable=False)
    # The exact structured prompt sent (see app/services/context_assembly.py)
    # — stored as JSON, not flattened, so the scripture/cadence/format
    # sections stay distinguishable on review, matching how they were kept
    # distinguishable when assembled.
    prompt: Mapped[dict] = mapped_column(JSONB, nullable=False)
    raw_response: Mapped[str] = mapped_column(Text, nullable=False)
    # Citation verification results (Task 3's trust/accuracy requirement)
    # — only ever populated on the "draft" stage log, null on "outline".
    citation_flags: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
