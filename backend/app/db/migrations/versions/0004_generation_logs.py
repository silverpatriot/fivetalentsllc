"""generation_logs table + RLS

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-31

Reviewable record of what was actually sent to the LLM and what came back,
per Task 3 in the Phase 3 kickoff spec. Tenant-scoped and RLS-protected
like every other tenant-owned table (see 0001's TENANT_SCOPED_TABLES
docstring for why: ENABLE + FORCE + a USING/WITH CHECK policy on
app.current_tenant_id, identically). sermon_engine_app's DML privileges on
it come for free from 0002's ALTER DEFAULT PRIVILEGES — no separate GRANT
needed here.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE TYPE generation_stage AS ENUM ('outline', 'draft')")

    op.execute(
        """
        CREATE TABLE generation_logs (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            sermon_id UUID NOT NULL REFERENCES sermons (id) ON DELETE CASCADE,
            stage generation_stage NOT NULL,
            model TEXT NOT NULL,
            prompt JSONB NOT NULL,
            raw_response TEXT NOT NULL,
            citation_flags JSONB,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_generation_logs_tenant_id ON generation_logs (tenant_id)")
    op.execute("CREATE INDEX ix_generation_logs_sermon_id ON generation_logs (sermon_id)")

    op.execute("ALTER TABLE generation_logs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE generation_logs FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON generation_logs
        USING (tenant_id = current_setting('app.current_tenant_id')::uuid)
        WITH CHECK (tenant_id = current_setting('app.current_tenant_id')::uuid)
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON generation_logs")
    op.execute("DROP TABLE IF EXISTS generation_logs")
    op.execute("DROP TYPE IF EXISTS generation_stage")
