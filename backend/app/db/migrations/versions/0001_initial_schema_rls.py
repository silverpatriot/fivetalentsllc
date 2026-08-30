"""Initial schema + row-level security

Revision ID: 0001
Revises:
Create Date: 2026-08-30

Every tenant-scoped table (everything except `tenants` itself) gets:
  - ENABLE ROW LEVEL SECURITY
  - FORCE ROW LEVEL SECURITY  <- also applies RLS to the table owner. Without
    this, if the app connects as the same role that owns the tables (true
    here — one POSTGRES_USER), RLS is silently bypassed for that role. This
    is the single most common way to think RLS is enforced when it isn't.
  - a policy scoped to current_setting('app.current_tenant_id', true)::uuid.
    The `true` (missing_ok) argument makes an *unset* tenant context return
    NULL rather than raise — and tenant_id = NULL is never true, so a
    request that forgot to set tenant context sees zero rows, not all
    rows. Fails closed.
  - the policy is USING + WITH CHECK, so it blocks both reads and writes
    (an INSERT/UPDATE trying to set a different tenant_id is rejected, not
    silently reassigned).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Tables that carry tenant_id and must be RLS-scoped. Kept as a single list
# so the enable/force/policy loop and the downgrade can't drift apart.
TENANT_SCOPED_TABLES = [
    "users",
    "sermons",
    "sermon_embeddings",
    "media_files",
    "clips",
    "usage_events",
]


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.execute("CREATE TYPE user_role AS ENUM ('admin', 'pastor', 'editor', 'viewer')")
    op.execute(
        "CREATE TYPE sermon_format AS ENUM "
        "('expository', 'topical', 'narrative', 'textual', 'custom')"
    )
    op.execute(
        "CREATE TYPE usage_event_type AS ENUM ('transcription_minute', 'ai_generation')"
    )

    # --- tenants: the tenancy root. Not RLS-scoped. ---
    op.execute(
        """
        CREATE TABLE tenants (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            slug VARCHAR(63) NOT NULL UNIQUE,
            name VARCHAR(255) NOT NULL,
            clerk_org_id VARCHAR(255) NOT NULL UNIQUE,
            plan_tier VARCHAR(50) NOT NULL DEFAULT 'free',
            stripe_customer_id VARCHAR(255) UNIQUE,
            stripe_subscription_id VARCHAR(255) UNIQUE,
            subscription_status VARCHAR(50),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )

    # --- users ---
    op.execute(
        """
        CREATE TABLE users (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            clerk_user_id VARCHAR(255) NOT NULL UNIQUE,
            tenant_id UUID NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            role user_role NOT NULL DEFAULT 'viewer',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_users_tenant_id ON users (tenant_id)")

    # --- sermons ---
    op.execute(
        """
        CREATE TABLE sermons (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            title VARCHAR(500) NOT NULL,
            format sermon_format NOT NULL,
            content TEXT,
            status VARCHAR(50) NOT NULL DEFAULT 'draft',
            created_by UUID REFERENCES users (id) ON DELETE SET NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_sermons_tenant_id ON sermons (tenant_id)")

    # --- sermon_embeddings (pgvector; replaces a separate Qdrant instance) ---
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
    # ANN index for similarity search. cosine is the standard choice for
    # text-embedding-style vectors; revisit if the embedding model changes.
    op.execute(
        "CREATE INDEX ix_sermon_embeddings_embedding_hnsw "
        "ON sermon_embeddings USING hnsw (embedding vector_cosine_ops)"
    )

    # --- media_files ---
    op.execute(
        """
        CREATE TABLE media_files (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            sermon_id UUID REFERENCES sermons (id) ON DELETE SET NULL,
            original_filename VARCHAR(500) NOT NULL,
            storage_path VARCHAR(1000) NOT NULL,
            duration_seconds NUMERIC(10, 2),
            transcription_status VARCHAR(50) NOT NULL DEFAULT 'pending',
            transcript_text TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_media_files_tenant_id ON media_files (tenant_id)")
    op.execute("CREATE INDEX ix_media_files_sermon_id ON media_files (sermon_id)")

    # --- clips ---
    op.execute(
        """
        CREATE TABLE clips (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            media_file_id UUID NOT NULL REFERENCES media_files (id) ON DELETE CASCADE,
            start_seconds NUMERIC(10, 2) NOT NULL,
            end_seconds NUMERIC(10, 2) NOT NULL,
            status VARCHAR(50) NOT NULL DEFAULT 'pending',
            storage_path VARCHAR(1000),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_clips_tenant_id ON clips (tenant_id)")
    op.execute("CREATE INDEX ix_clips_media_file_id ON clips (media_file_id)")

    # --- usage_events ---
    op.execute(
        """
        CREATE TABLE usage_events (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            event_type usage_event_type NOT NULL,
            quantity NUMERIC(12, 4) NOT NULL,
            stripe_usage_record_id VARCHAR(255),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_usage_events_tenant_id ON usage_events (tenant_id)")

    # --- RLS: enable + force + policy, identically, on every tenant-scoped table ---
    for table in TENANT_SCOPED_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
            USING (tenant_id = current_setting('app.current_tenant_id', true)::uuid)
            WITH CHECK (tenant_id = current_setting('app.current_tenant_id', true)::uuid)
            """
        )


def downgrade() -> None:
    for table in reversed(TENANT_SCOPED_TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")

    op.execute("DROP TABLE IF EXISTS usage_events")
    op.execute("DROP TABLE IF EXISTS clips")
    op.execute("DROP TABLE IF EXISTS media_files")
    op.execute("DROP TABLE IF EXISTS sermon_embeddings")
    op.execute("DROP TABLE IF EXISTS sermons")
    op.execute("DROP TABLE IF EXISTS users")
    op.execute("DROP TABLE IF EXISTS tenants")

    op.execute("DROP TYPE IF EXISTS usage_event_type")
    op.execute("DROP TYPE IF EXISTS sermon_format")
    op.execute("DROP TYPE IF EXISTS user_role")
