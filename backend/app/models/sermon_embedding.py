import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, UUIDPkMixin

# text-embedding-3-small / -large family and most current sermon-generation
# embedding models land on 1536 dims; revisit if the embedding model changes.
EMBEDDING_DIM = 1536


class SermonEmbedding(Base, UUIDPkMixin, CreatedAtMixin):
    """Replaces a separate vector DB (Qdrant) — pgvector lives alongside the
    relational data it's embedding."""

    __tablename__ = "sermon_embeddings"

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
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM), nullable=False)
