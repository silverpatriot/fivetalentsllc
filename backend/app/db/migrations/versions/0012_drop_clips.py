"""Drop clips table — clip generation moves to a separate product

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-01

Clip generation (cutting short clips from a sermon's media file) is out of
scope for this product going forward — it's being built as its own SaaS
(Cluos) instead. `clips` was schema/model/schema-only scaffolding from
Phase 1: no API route, Celery task, or frontend UI was ever built on it
(checked before writing this migration), so there's no feature code to
unwind, just the table itself plus the RLS policy and app-role grant 0001
and 0002 put on it.

Reversing those in the opposite order they were applied: policy (0001,
applied last) before grant (0002) before the table (0001) itself.
"""
from typing import Sequence, Union

from alembic import op

from app.core.config import get_settings

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

APP_ROLE = get_settings().app_db_user


def upgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON clips")
    op.execute(f'REVOKE ALL ON clips FROM "{APP_ROLE}"')
    op.execute("DROP TABLE IF EXISTS clips")


def downgrade() -> None:
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
    op.execute("ALTER TABLE clips ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE clips FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON clips
        USING (tenant_id = current_setting('app.current_tenant_id')::uuid)
        WITH CHECK (tenant_id = current_setting('app.current_tenant_id')::uuid)
        """
    )
    op.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON clips TO "{APP_ROLE}"')
