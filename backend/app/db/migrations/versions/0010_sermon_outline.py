"""sermons.outline + generation_stage 'outline_condense'

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-01

Sermon generation always ran an internal outline pass (build_outline_messages
in app/services/context_assembly.py) before drafting the full manuscript,
but only ever persisted the manuscript (sermon.content) — the outline
streamed to the frontend via SSE and was discarded, never saved. This adds
a real, separately-triggered outline: sermons.outline, populated on demand
by POST /sermons/{id}/outline (app/services/generation.py's
generate_outline_from_manuscript) — condensed FROM an already-generated
manuscript, not the old pre-draft outline pass, and actually persisted
this time. The manuscript stays the primary generation output; this is an
additional artifact, not a replacement.

generation_stage (Postgres native enum, migration 0004) gains
'outline_condense' so this new LLM call is tracked distinctly in
generation_logs/usage_events, same "one row per real LLM call" discipline
the OUTLINE/DRAFT stages already have. ALTER TYPE ... ADD VALUE is a
single statement with nothing in the same transaction that uses the new
value yet, which is the one combination Postgres actually restricts
(using a brand-new enum value in the SAME transaction it was added in) —
safe here.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE sermons ADD COLUMN outline TEXT")
    op.execute("ALTER TYPE generation_stage ADD VALUE IF NOT EXISTS 'outline_condense'")


def downgrade() -> None:
    op.execute("ALTER TABLE sermons DROP COLUMN outline")
    # Postgres has no ALTER TYPE ... DROP VALUE — removing an enum value
    # means recreating the type without it. This intentionally fails
    # loudly (the USING cast below) rather than silently dropping any
    # generation_logs row already using 'outline_condense' — a downgrade
    # can't retroactively un-happen data that used the new value; that's
    # correct behavior, not a bug in this migration.
    op.execute("ALTER TYPE generation_stage RENAME TO generation_stage_old")
    op.execute("CREATE TYPE generation_stage AS ENUM ('outline', 'draft')")
    op.execute(
        "ALTER TABLE generation_logs ALTER COLUMN stage TYPE generation_stage "
        "USING stage::text::generation_stage"
    )
    op.execute("DROP TYPE generation_stage_old")
