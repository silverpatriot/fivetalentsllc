"""documents + document_chunks: shared RAG infrastructure, replaces sermon_embeddings

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-31

Phase 4 kickoff spec: cadence-matching and the theology/study corpus share
one generic ingestion+retrieval pipeline rather than forking it, so they
share these two tables (distinguished by corpus_type) rather than each
getting their own. This replaces the sermon-specific sermon_embeddings
table from Phase 1/the first draft of this phase's Task 1 — dropped here
rather than left alongside the new tables. Confirmed live before writing
this: the only tenant with any sermons at all (the manually-provisioned
test tenant, per this phase's kickoff spec) has zero, so there is no
embedding data anywhere in this database to migrate.

Both tables follow the exact same RLS pattern as every other tenant-
scoped table (migration 0001's docstring has the full reasoning: FORCE
ROW LEVEL SECURITY, no missing_ok on current_setting, USING + WITH CHECK)
— duplicated here rather than folded into TENANT_SCOPED_TABLES's loop
since these two tables didn't exist when that loop was written and
editing an already-applied migration is worse than a few duplicated
lines in a new one. No explicit GRANT needed — comes free from 0002's
ALTER DEFAULT PRIVILEGES, same as generation_logs (0004) and the
usage_events columns (0005) before this.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_RLS_TABLES = ["documents", "document_chunks"]


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS sermon_embeddings")

    op.execute(
        """
        CREATE TABLE documents (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            corpus_type VARCHAR(20) NOT NULL,
            source VARCHAR(20) NOT NULL,
            sermon_id UUID REFERENCES sermons (id) ON DELETE CASCADE,
            title VARCHAR(500) NOT NULL,
            original_filename VARCHAR(500),
            status VARCHAR(20) NOT NULL DEFAULT 'processing',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_documents_tenant_id ON documents (tenant_id)")
    op.execute("CREATE INDEX ix_documents_corpus_type ON documents (corpus_type)")

    op.execute(
        """
        CREATE TABLE document_chunks (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            corpus_type VARCHAR(20) NOT NULL,
            document_id UUID NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
            chunk_index INTEGER NOT NULL,
            content TEXT NOT NULL,
            embedding VECTOR(1536) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (document_id, chunk_index)
        )
        """
    )
    op.execute("CREATE INDEX ix_document_chunks_tenant_id ON document_chunks (tenant_id)")
    op.execute("CREATE INDEX ix_document_chunks_corpus_type ON document_chunks (corpus_type)")
    op.execute("CREATE INDEX ix_document_chunks_document_id ON document_chunks (document_id)")
    # ANN index for similarity search — same cosine choice as
    # sermon_embeddings had, for the same reason (matches
    # text-embedding-3-small's geometry).
    op.execute(
        "CREATE INDEX ix_document_chunks_embedding_hnsw "
        "ON document_chunks USING hnsw (embedding vector_cosine_ops)"
    )

    for table in _RLS_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
            USING (tenant_id = current_setting('app.current_tenant_id')::uuid)
            WITH CHECK (tenant_id = current_setting('app.current_tenant_id')::uuid)
            """
        )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON document_chunks")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON documents")
    op.execute("DROP TABLE IF EXISTS document_chunks")
    op.execute("DROP TABLE IF EXISTS documents")
    op.execute(
        """
        CREATE TABLE sermon_embeddings (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            sermon_id UUID NOT NULL REFERENCES sermons (id) ON DELETE CASCADE,
            embedding VECTOR(1536) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_sermon_embeddings_tenant_id ON sermon_embeddings (tenant_id)")
    op.execute("CREATE INDEX ix_sermon_embeddings_sermon_id ON sermon_embeddings (sermon_id)")
    op.execute(
        "CREATE INDEX ix_sermon_embeddings_embedding_hnsw "
        "ON sermon_embeddings USING hnsw (embedding vector_cosine_ops)"
    )
