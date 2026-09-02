"""Phase 6: iterative draft editing — generation_stage gains edit_locate/
edit, plus sermon_revisions

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-02

Two independent additions for iterative, section-scoped draft editing
(Phase 6 kickoff spec):

1. generation_stage (Postgres native enum, migration 0004) gains two
   values, same "ALTER TYPE ... ADD VALUE, nothing in this transaction
   uses it yet" pattern as migration 0010's outline_condense:
   - 'edit_locate': the cheap/fast-model call that identifies which exact
     span of an existing draft a no-selection instruction targets
     (app/services/generation.py's _run_edit). Only runs when the pastor
     didn't select text themselves.
   - 'edit': the actual scoped rewrite call (stronger model, streamed),
     producing the replacement text for that span. The backend splices
     this into the draft itself — the model never sees or returns the
     rest of the manuscript — see _run_edit's docstring for why that's
     the safety property this design is built around.
   Both get their own generation_logs + usage_events rows per real LLM
   call, identical "one row per call, success or failure" discipline as
   every existing stage (app/services/generation.py's _record_llm_call,
   reused unmodified — it already resolves billable=False for any stage
   other than a succeeded DRAFT, so no change was needed there for these
   two new stages to inherit the same non-billable treatment as
   outline_condense).

2. sermon_revisions: the minimum viable recoverability Task 2 asked for
   without building full version-history UI (that's its own later
   backlog phase). One row is inserted immediately BEFORE each edit
   overwrites sermons.content, holding the content as it was right
   before that edit — so the full lineage of a sermon's draft is
   reconstructable by reading sermon_revisions in created_at order and
   ending at the sermon's own current content, even though only the
   latest state is ever the "live" one. No UI reads this table yet
   (deliberately out of scope) — the goal here is only that a bad edit
   is never a UI limitation away from being recoverable at the data
   layer once that phase arrives, not designing that phase now.
   Tenant-scoped + RLS like every other tenant-owned table (0001's
   TENANT_SCOPED_TABLES rationale, identically).
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE generation_stage ADD VALUE IF NOT EXISTS 'edit_locate'")
    op.execute("ALTER TYPE generation_stage ADD VALUE IF NOT EXISTS 'edit'")

    op.execute(
        """
        CREATE TABLE sermon_revisions (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            sermon_id UUID NOT NULL REFERENCES sermons (id) ON DELETE CASCADE,
            content TEXT NOT NULL,
            instruction TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_sermon_revisions_tenant_id ON sermon_revisions (tenant_id)")
    op.execute("CREATE INDEX ix_sermon_revisions_sermon_id ON sermon_revisions (sermon_id)")

    op.execute("ALTER TABLE sermon_revisions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE sermon_revisions FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON sermon_revisions
        USING (tenant_id = current_setting('app.current_tenant_id')::uuid)
        WITH CHECK (tenant_id = current_setting('app.current_tenant_id')::uuid)
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON sermon_revisions")
    op.execute("DROP TABLE IF EXISTS sermon_revisions")
    # Postgres has no ALTER TYPE ... DROP VALUE — same irreversibility
    # migration 0010's downgrade already documents. Any generation_logs/
    # usage_events row already using 'edit'/'edit_locate' makes a clean
    # downgrade impossible without deciding what to do with that data;
    # failing loudly here (rather than silently recreating the type
    # without them) is the correct behavior, not a bug.
    op.execute("ALTER TYPE generation_stage RENAME TO generation_stage_old")
    op.execute("CREATE TYPE generation_stage AS ENUM ('outline', 'draft', 'outline_condense')")
    op.execute(
        "ALTER TABLE generation_logs ALTER COLUMN stage TYPE generation_stage "
        "USING stage::text::generation_stage"
    )
    op.execute(
        "ALTER TABLE usage_events ALTER COLUMN generation_stage TYPE generation_stage "
        "USING generation_stage::text::generation_stage"
    )
    op.execute("DROP TYPE generation_stage_old")
