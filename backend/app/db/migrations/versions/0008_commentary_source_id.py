"""reference_documents.source_id: distinguish multiple commentaries

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-31

Migration 0007 shipped one commentary (Matthew Henry's) blended under
reference_type='commentary' with no way to tell it apart from a second
one. This adds source_id (e.g. "matthew-henry", "adam-clarke") so
scripts/ingest_baseline_corpus.py can ingest several distinct public-
domain commentaries from bible.helloao.org without them colliding or
being retrieved as one undifferentiated blob — app/services/study.py's
own "never blend sources" discipline (see its module docstring) applies
to distinct commentaries just as much as it does to commentary vs.
cross-reference vs. web search.

Existing rows backfill to 'matthew-henry' (the only commentary ingested
before this migration) so nothing goes from a real value to NULL.
cross_reference rows keep source_id NULL — it has no meaning for them,
same "not every column applies to every reference_type" shape
passage_reference already has (nullable, meaningful for cross-references,
optional for commentary).

The existing UNIQUE(reference_type, passage_reference) constraint
(confirmed live as reference_documents_reference_type_passage_reference_key
on the running database before writing this) is replaced with a 3-column
UNIQUE(reference_type, passage_reference, source_id) — necessary because
two different commentaries now legitimately share a passage_reference
(e.g. both matthew-henry and adam-clarke have their own "John 3" row).
Postgres treats NULL as distinct per row in a UNIQUE constraint (standard
SQL semantics, not NULLS NOT DISTINCT here) — cross-reference rows, which
all carry source_id IS NULL, are unaffected by this widening.

CORRECTION (migration 0013): the sentence this replaced claimed that
meant cross-reference rows "remain free to coexist one-per-
passage_reference exactly as before." That's backwards — NULL-distinct
semantics mean the constraint never fires between two cross-reference
rows at all, so nothing was ever enforcing "one per passage_reference"
for them, and ON CONFLICT (which targets this same index) silently
never matched either. Confirmed live: re-running cross-reference
ingestion duplicated rows rather than upserting them. 0013 adds
NULLS NOT DISTINCT to fix this — read that migration for the real
constraint this table runs under today.
scripts/ingest_baseline_corpus.py's upsert conflict target is updated to
this 3-column list in the same change (see that script) — an upsert
against the OLD 2-column list here would silently target the wrong
constraint and fail loudly on the first non-Matthew-Henry commentary
ingest, which is the intended fail-fast behavior if these two ever drift
apart again.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE reference_documents ADD COLUMN source_id VARCHAR(50)")
    op.execute("UPDATE reference_documents SET source_id = 'matthew-henry' WHERE reference_type = 'commentary'")
    op.execute("CREATE INDEX ix_reference_documents_source_id ON reference_documents (source_id)")
    op.execute(
        "ALTER TABLE reference_documents "
        "DROP CONSTRAINT reference_documents_reference_type_passage_reference_key"
    )
    op.execute(
        "ALTER TABLE reference_documents ADD CONSTRAINT reference_documents_type_passage_source_key "
        "UNIQUE (reference_type, passage_reference, source_id)"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE reference_documents DROP CONSTRAINT reference_documents_type_passage_source_key")
    op.execute(
        "ALTER TABLE reference_documents ADD CONSTRAINT reference_documents_reference_type_passage_reference_key "
        "UNIQUE (reference_type, passage_reference)"
    )
    op.execute("DROP INDEX IF EXISTS ix_reference_documents_source_id")
    op.execute("ALTER TABLE reference_documents DROP COLUMN source_id")
