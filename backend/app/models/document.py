"""Document/DocumentChunk: the shared RAG infrastructure (Phase 4 Task 1)
behind both corpora — cadence (sermons, as voice examples) and theology
(uploaded reference material). One generic pipeline, not two forked ones;
see app/services/ingestion.py and app/services/retrieval.py.

corpus_type/source/status are plain strings, not native Postgres enums —
deliberately the same choice already made for Sermon.status/
Tenant.subscription_status in this codebase ("the state machine isn't
locked in this phase"), and for the same reason: Phase 4 Task 3 already
flags a probable third corpus_type (a shared public-domain baseline
corpus, separate from per-tenant uploads) — a native enum would need a
migration to add that value later; a plain string with app-level
constants doesn't.
"""
import enum
import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, UUIDPkMixin

# text-embedding-3-small lands on 1536 dims — see app/services/embeddings.py.
EMBEDDING_DIM = 1536


class CorpusType(str, enum.Enum):
    CADENCE = "cadence"
    THEOLOGY = "theology"


class DocumentSource(str, enum.Enum):
    GENERATED = "generated"  # a sermon finalized in Kerygma itself
    UPLOADED = "uploaded"


class DocumentStatus(str, enum.Enum):
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"


class Document(Base, UUIDPkMixin, CreatedAtMixin):
    """One ingested unit — either a finalized sermon or an uploaded file.
    Chunked+embedded content lives in DocumentChunk; this row is the
    ingestion-status/metadata record, and (for source=UPLOADED) the only
    place any trace of the original file lives — the raw bytes
    themselves are never persisted (extracted synchronously at upload
    time, then discarded; see app/services/ingestion.py's docstring for
    why, and surface that to the user in the UI so a pastor doesn't
    expect to re-download something they uploaded)."""

    __tablename__ = "documents"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    corpus_type: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    # Set only when source=GENERATED — links back to the sermon this
    # document was ingested from. NOT the sermon's content itself
    # (that stays in sermons.content, the single source of truth); this
    # is purely provenance, so a cadence example can be traced back to
    # "which sermon did this come from."
    sermon_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sermons.id", ondelete="CASCADE"), nullable=True
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    original_filename: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=DocumentStatus.PROCESSING.value)
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now(), nullable=False)


class DocumentChunk(Base, UUIDPkMixin, CreatedAtMixin):
    __tablename__ = "document_chunks"

    # Denormalized from documents (both values) rather than requiring a
    # join for every retrieval query — see this module's docstring.
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    corpus_type: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chunk_index: Mapped[int] = mapped_column(nullable=False)
    content: Mapped[str] = mapped_column(nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM), nullable=False)
