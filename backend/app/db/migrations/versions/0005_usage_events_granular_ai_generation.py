"""usage_events: sermon_id / generation_stage / outcome / billable

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-31

Supports recording one usage_events row per real LLM call (outline,
draft — each independently, regardless of success/failure) instead of
one row per user-facing generation action, per the Phase 3 completion
review's usage-metering decision.

billable defaults to true so every existing/pre-Phase-3 row and call site
(TRANSCRIPTION_MINUTE included) is unaffected; the new granular
AI_GENERATION rows are written with billable=false explicitly — see
app/models/usage_event.py's docstring for why that's not just a caveat
but the thing that keeps this change from silently double-billing every
successful generation the moment it ships. generation_stage reuses the
`generation_stage` enum type 0004 already created (outline/draft) rather
than defining a second one — no new CREATE TYPE needed here.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE usage_events ADD COLUMN sermon_id UUID REFERENCES sermons (id) ON DELETE SET NULL")
    op.execute("ALTER TABLE usage_events ADD COLUMN generation_stage generation_stage")
    op.execute("ALTER TABLE usage_events ADD COLUMN outcome VARCHAR(20)")
    op.execute("ALTER TABLE usage_events ADD COLUMN billable BOOLEAN NOT NULL DEFAULT true")
    op.execute("CREATE INDEX ix_usage_events_sermon_id ON usage_events (sermon_id)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_usage_events_sermon_id")
    op.execute("ALTER TABLE usage_events DROP COLUMN IF EXISTS billable")
    op.execute("ALTER TABLE usage_events DROP COLUMN IF EXISTS outcome")
    op.execute("ALTER TABLE usage_events DROP COLUMN IF EXISTS generation_stage")
    op.execute("ALTER TABLE usage_events DROP COLUMN IF EXISTS sermon_id")
