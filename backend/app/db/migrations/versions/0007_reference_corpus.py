"""reference_documents + reference_chunks: baseline public-domain corpus

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-31

Approved baseline-corpus proposal: cross-references and commentary text
are identical for every tenant (public-domain/CC-licensed reference
material, not a private upload) — genuinely global, not per-tenant data.
Deliberately NOT RLS-scoped and NOT carrying a tenant_id column at all,
in contrast to every other table this phase added (documents/
document_chunks, migration 0006) — the same category of exception
`tenants` and `webhook_events` already are. A tenant-scoped design here
would either duplicate ~31K cross-reference entries and a full
historical commentary once per church (pure storage/embedding-cost
waste for content that never differs) or need every tenant's session to
somehow be granted read access to another tenant's rows, which is
exactly the kind of RLS-boundary confusion this design avoids by not
using RLS at all here. No ENABLE ROW LEVEL SECURITY statement appears
below — that omission is the point, not an oversight.

UNIQUE(reference_type, passage_reference) on reference_documents is what
scripts/ingest_baseline_corpus.py upserts against — re-running the
ingestion script (a new dataset release, a correction) replaces existing
entries rather than accumulating duplicates, same reasoning as
document_chunks' (document_id, chunk_index) constraint in migration 0006.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE reference_documents (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            reference_type VARCHAR(30) NOT NULL,
            title VARCHAR(500) NOT NULL,
            passage_reference VARCHAR(100),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (reference_type, passage_reference)
        )
        """
    )
    op.execute("CREATE INDEX ix_reference_documents_reference_type ON reference_documents (reference_type)")
    op.execute("CREATE INDEX ix_reference_documents_passage_reference ON reference_documents (passage_reference)")

    op.execute(
        """
        CREATE TABLE reference_chunks (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            reference_type VARCHAR(30) NOT NULL,
            reference_document_id UUID NOT NULL REFERENCES reference_documents (id) ON DELETE CASCADE,
            chunk_index INTEGER NOT NULL,
            content TEXT NOT NULL,
            embedding VECTOR(1536) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (reference_document_id, chunk_index)
        )
        """
    )
    op.execute("CREATE INDEX ix_reference_chunks_reference_type ON reference_chunks (reference_type)")
    op.execute("CREATE INDEX ix_reference_chunks_document_id ON reference_chunks (reference_document_id)")
    op.execute(
        "CREATE INDEX ix_reference_chunks_embedding_hnsw "
        "ON reference_chunks USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS reference_chunks")
    op.execute("DROP TABLE IF EXISTS reference_documents")
