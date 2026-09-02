"""reference_chunks: partial HNSW indexes per reference_type

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-02

Migration 0007's single HNSW index over ALL of reference_chunks.embedding
(cross-reference and commentary rows mixed together) silently breaks
search_reference_corpus() once the corpus has any real size and skew
between the two types. Confirmed live, right after the first full-scale
ingestion (195,262 real chunks: 165,898 commentary / 29,364
cross-reference, an ~85/15 split): a cross-reference search returned
ZERO rows for a perfectly reasonable query, not because nothing matched
— HNSW does an approximate-nearest-neighbor scan across the WHOLE index
first, and only THEN applies the reference_type filter (confirmed via
EXPLAIN: "Filter: reference_type = 'cross_reference'" applied on top of
the index scan, not folded into it). With the default hnsw.ef_search
(40 candidates), and cross-reference rows outnumbered ~5.6:1 by
commentary in the same index, the top-40 nearest-neighbor candidates for
a typical query can easily be ALL commentary, leaving nothing for the
filter to pass through — reproduced directly: raising hnsw.ef_search to
1000 made the same query return real results immediately, confirming
this precisely.

search_reference_corpus() ALWAYS filters on reference_type — it's a
required, non-optional parameter, not an occasional add-on — so the
correct fix is a partial index per value rather than a session-level
ef_search bump (which is probabilistic per-query and still degrades as
the corpus grows further; a partial index makes the ANN search itself
operate over only same-type rows, eliminating the cross-contamination
this bug depends on rather than just making it statistically less
likely). ReferenceType (app/models/reference.py) has exactly two values
today; one partial index each. A third value would need its own partial
index added here — this migration is not written to generalize past two
without a follow-up.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_reference_chunks_embedding_hnsw")
    op.execute(
        "CREATE INDEX ix_reference_chunks_embedding_hnsw_xref "
        "ON reference_chunks USING hnsw (embedding vector_cosine_ops) "
        "WHERE reference_type = 'cross_reference'"
    )
    op.execute(
        "CREATE INDEX ix_reference_chunks_embedding_hnsw_commentary "
        "ON reference_chunks USING hnsw (embedding vector_cosine_ops) "
        "WHERE reference_type = 'commentary'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_reference_chunks_embedding_hnsw_xref")
    op.execute("DROP INDEX IF EXISTS ix_reference_chunks_embedding_hnsw_commentary")
    op.execute(
        "CREATE INDEX ix_reference_chunks_embedding_hnsw "
        "ON reference_chunks USING hnsw (embedding vector_cosine_ops)"
    )
