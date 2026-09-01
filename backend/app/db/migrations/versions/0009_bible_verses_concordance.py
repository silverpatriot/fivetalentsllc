"""bible_verses: concordance (exact/stemmed word search across scripture)

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-31

A concordance ("every verse containing 'grace'") is exact/stemmed
LEXICAL matching, not nearest-neighbor semantic similarity — the
pgvector/embedding pattern reference_chunks (migration 0007) uses is the
wrong tool here on purpose: embedding all ~31K verses would be wasteful
API spend for a lookup Postgres full-text search solves for free, AND
semantically wrong (a query for "grace" should surface exact/stemmed
occurrences, not topically-similar verses about mercy). search_vector is
a STORED generated column (computed once at write time, not on every
query) with a GIN index for plainto_tsquery lookups — see
app/services/concordance.py.

Same tenant-agnostic shape as reference_documents/reference_chunks: no
tenant_id, no RLS — this is public-domain scripture text, identical for
every church, not private tenant data. See migration 0007's docstring
for the fuller reasoning, which applies identically here.

UNIQUE(translation, book, chapter, verse) is what
scripts/ingest_bible_verses.py upserts against, same
upsert-on-unique-constraint pattern as reference_documents.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE bible_verses (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            translation VARCHAR(20) NOT NULL,
            book VARCHAR(50) NOT NULL,
            chapter INTEGER NOT NULL,
            verse INTEGER NOT NULL,
            text TEXT NOT NULL,
            search_vector tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (translation, book, chapter, verse)
        )
        """
    )
    op.execute("CREATE INDEX ix_bible_verses_search_vector ON bible_verses USING gin (search_vector)")
    op.execute("CREATE INDEX ix_bible_verses_translation ON bible_verses (translation)")
    op.execute("CREATE INDEX ix_bible_verses_book_chapter ON bible_verses (book, chapter)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS bible_verses")
