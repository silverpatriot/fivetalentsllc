"""webhook_events table + tenants.subscription_status default 'pending'

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-31

webhook_events is not tenant-scoped (no RLS) — the same reasoning as
`tenants` itself: a webhook is platform-level and, for Clerk's
organization.created in particular, arrives before any tenants row exists
for it at all. sermon_engine_app gets DML on it automatically via 0002's
`ALTER DEFAULT PRIVILEGES ... GRANT ... ON TABLES` (default privileges
apply to any table a later migration creates under the same admin role —
no separate GRANT needed here, deliberately, so this doesn't have to be
remembered per migration).

subscription_status defaulting to 'pending' matches the Phase 2 spec's
signup flow exactly: a tenants row is created (via the Clerk
organization.created webhook) before Stripe Checkout ever happens, and
should read as 'pending' from the moment it exists, not NULL.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE webhook_events (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            source VARCHAR(20) NOT NULL,
            external_event_id VARCHAR(255) NOT NULL,
            event_type VARCHAR(100) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_webhook_events_source_event UNIQUE (source, external_event_id)
        )
        """
    )

    op.execute("ALTER TABLE tenants ALTER COLUMN subscription_status SET DEFAULT 'pending'")
    # Existing rows (Phase 1 test fixtures etc.) with a NULL status: bring
    # them in line with the new default rather than leaving them in a
    # state the app now never produces on purpose.
    op.execute(
        "UPDATE tenants SET subscription_status = 'pending' WHERE subscription_status IS NULL"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE tenants ALTER COLUMN subscription_status DROP DEFAULT")
    op.execute("DROP TABLE IF EXISTS webhook_events")
