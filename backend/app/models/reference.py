"""The baseline reference corpus (approved baseline-corpus proposal,
Phase 4) — public-domain/CC-licensed cross-references and commentary,
identical for every tenant. Deliberately NOT RLS-scoped and carrying NO
tenant_id column at all, in contrast to every other table this phase
added (Document/DocumentChunk) — the same category of exception `Tenant`
and `WebhookEvent` already are. See migration 0007's docstring for the
full reasoning; the short version: this content never differs between
churches, so a tenant-scoped design would mean either duplicating ~31K
cross-reference entries and a full historical commentary once per church
(pure waste), or granting cross-tenant read access some other way, which
is exactly the kind of RLS-boundary confusion avoided by not using RLS
here at all.
"""
import enum
import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, UUIDPkMixin

EMBEDDING_DIM = 1536


class ReferenceType(str, enum.Enum):
    CROSS_REFERENCE = "cross_reference"
    COMMENTARY = "commentary"


# The single source of truth for which commentary source_ids exist and
# their display names — scripts/ingest_baseline_corpus.py and
# app/services/study.py both import this rather than each keeping their
# own copy. All 7 are public-domain/CC0-marked commentaries confirmed
# live at bible.helloao.org/api/available_commentaries.json (migration
# 0008's docstring); matthew-henry is the only one ingested by default.
COMMENTARY_LABELS: dict[str, str] = {
    "matthew-henry": "Matthew Henry",
    "adam-clarke": "Adam Clarke",
    "jamieson-fausset-brown": "Jamieson-Fausset-Brown",
    "john-calvin": "John Calvin",
    "john-gill": "John Gill",
    "keil-delitzsch": "Keil & Delitzsch",  # Old Testament only
    "tyndale": "Tyndale Open Study Notes",
}


class ReferenceDocument(Base, UUIDPkMixin, CreatedAtMixin):
    __tablename__ = "reference_documents"

    reference_type: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    # The canonical passage this entry is anchored to, e.g. "Gen 1:1" (a
    # cross-reference source verse) or "John 3" (a commentary chapter).
    # Deliberately kept as the source dataset's own book-abbreviation
    # style, lightly formatted for readability — not remapped to full
    # book names, which would be a second thing to keep correct rather
    # than trusting the source data as-is.
    passage_reference: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    # Which specific commentary this is (e.g. "matthew-henry", "adam-clarke")
    # — migration 0008. Meaningful only for reference_type="commentary";
    # cross_reference rows leave this NULL (it has nothing to distinguish —
    # there's exactly one cross-reference source). See that migration's
    # docstring for why this is a column here rather than a second
    # ReferenceType per commentary.
    source_id: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)


class ReferenceChunk(Base, UUIDPkMixin, CreatedAtMixin):
    __tablename__ = "reference_chunks"

    reference_type: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    reference_document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("reference_documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chunk_index: Mapped[int] = mapped_column(nullable=False)
    content: Mapped[str] = mapped_column(nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM), nullable=False)
