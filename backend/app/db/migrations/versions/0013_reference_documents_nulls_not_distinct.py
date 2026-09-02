"""reference_documents: NULLS NOT DISTINCT on the upsert constraint

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-01

Migration 0008's own docstring reasoned that "Postgres treats NULL as
distinct per row in a UNIQUE constraint... so cross-reference rows —
which all carry source_id IS NULL — are unaffected by this widening and
remain free to coexist one-per-passage_reference exactly as before." That
reasoning is backwards, and this migration exists because running
scripts/ingest_baseline_corpus.py's cross-reference ingestion a second
time against the same data proved it live: two rows with the same
(reference_type, passage_reference) and both source_id IS NULL do NOT
violate UNIQUE(reference_type, passage_reference, source_id) — standard
SQL treats NULL as distinct from NULL, so the constraint (and therefore
ON CONFLICT, which targets the same index) never fires for them at all.
Every re-run of cross-reference ingestion was silently duplicating the
entire cross-reference corpus rather than upserting it, directly
contradicting that script's own "safe to re-run" claim. Confirmed by
running it twice against a live 5-document slice: 5 rows became 10,
then 15 — see the session that added this migration for the exact
before/after counts. Commentary rows (source_id always a real string)
were never affected.

Postgres 15+ supports UNIQUE ... NULLS NOT DISTINCT specifically for
this — two NULLs in the constrained columns now DO count as equal, which
is the behavior an upsert on this column actually needs. This database
runs 16.15 (confirmed live), so this doesn't need a version gate.

Dedupes existing duplicate rows FIRST (keeping the earliest created_at
per (reference_type, passage_reference, source_id) group; source_id
compares equal-to-itself here as plain data, this is pre-constraint
cleanup, not the constraint itself) — the new constraint can't be added
while duplicates already violate it. reference_chunks cascades on
delete (reference_chunks_reference_document_id_fkey ON DELETE CASCADE),
so a deleted duplicate document's orphaned chunks go with it, not left
dangling.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        DELETE FROM reference_documents
        WHERE id IN (
            SELECT id FROM (
                SELECT id, ROW_NUMBER() OVER (
                    PARTITION BY reference_type, passage_reference, source_id
                    ORDER BY created_at ASC, id ASC
                ) AS rn
                FROM reference_documents
            ) ranked
            WHERE rn > 1
        )
        """
    )
    op.execute("ALTER TABLE reference_documents DROP CONSTRAINT reference_documents_type_passage_source_key")
    op.execute(
        "ALTER TABLE reference_documents ADD CONSTRAINT reference_documents_type_passage_source_key "
        "UNIQUE NULLS NOT DISTINCT (reference_type, passage_reference, source_id)"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE reference_documents DROP CONSTRAINT reference_documents_type_passage_source_key")
    op.execute(
        "ALTER TABLE reference_documents ADD CONSTRAINT reference_documents_type_passage_source_key "
        "UNIQUE (reference_type, passage_reference, source_id)"
    )
