"""Grant the runtime app role DML privileges (not DDL, not ownership)

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-31

The role itself is created by postgres/init/01-create-app-role.sh at
container init, not here — Alembic runs as the admin/superuser
(DATABASE_URL_SYNC) and has no business managing login credentials for
another role. This migration only grants privileges on objects it just
created in 0001, which *is* schema-migration's job.

This is the other half of the RLS fix: enabling RLS is necessary but not
sufficient. A superuser (which the migration/admin role is) bypasses RLS
unconditionally, FORCE or no FORCE. app_role is deliberately NOT a
superuser and NOT the table owner, so plain ENABLE ROW LEVEL SECURITY
(already set in 0001) is enough to bind it — no ownership tricks needed for
this role, only for the admin role, which is what FORCE was for.

The role name comes from Settings (APP_DB_USER), not a hardcoded string —
it MUST be the exact name postgres/init/01-create-app-role.sh actually
created. A previous version of this file hardcoded "sermon_engine_app"
here independently of that env var; if the two ever disagreed, this
GRANT would fail with "role does not exist", and since Alembic runs all
pending migrations in one transaction, that failure would roll back
0001's CREATE TABLEs too — not just this migration's own statements.
"""
from typing import Sequence, Union

from alembic import op

from app.core.config import get_settings

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

APP_ROLE = get_settings().app_db_user

TABLES = [
    "tenants",
    "users",
    "sermons",
    "sermon_embeddings",
    "media_files",
    "clips",
    "usage_events",
]


def upgrade() -> None:
    op.execute(f'GRANT USAGE ON SCHEMA public TO "{APP_ROLE}"')
    op.execute(
        f'GRANT SELECT, INSERT, UPDATE, DELETE ON {", ".join(TABLES)} TO "{APP_ROLE}"'
    )
    # Default privileges for anything a *future* migration adds under this
    # same admin role, so 0003+ doesn't need to remember this grant too.
    op.execute(
        f'ALTER DEFAULT PRIVILEGES IN SCHEMA public '
        f'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO "{APP_ROLE}"'
    )


def downgrade() -> None:
    op.execute(f'ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM "{APP_ROLE}"')
    op.execute(f'REVOKE ALL ON {", ".join(TABLES)} FROM "{APP_ROLE}"')
    op.execute(f'REVOKE USAGE ON SCHEMA public FROM "{APP_ROLE}"')
