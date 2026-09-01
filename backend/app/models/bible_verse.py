"""Raw scripture verse text for the concordance feature (migration 0009)
— deliberately a separate model/table from app/models/reference.py's
ReferenceDocument/ReferenceChunk, even though both are tenant-agnostic
public-domain content with no RLS. Structurally different concepts: those
are chunked + embedded for semantic retrieval; this is atomic verse text
for exact/stemmed lexical search (see app/services/concordance.py) — no
chunking, no embedding. Kept separate the same way
app/services/reference_retrieval.py was kept separate from
app/services/retrieval.py, so an edit to one can't accidentally blur into
the other.
"""
from sqlalchemy import Computed, String
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, UUIDPkMixin


class BibleVerse(Base, UUIDPkMixin, CreatedAtMixin):
    __tablename__ = "bible_verses"

    translation: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    book: Mapped[str] = mapped_column(String(50), nullable=False)
    chapter: Mapped[int] = mapped_column(nullable=False)
    verse: Mapped[int] = mapped_column(nullable=False)
    text: Mapped[str] = mapped_column(nullable=False)
    # search_vector (migration 0009) is a DB-generated STORED tsvector
    # column (GENERATED ALWAYS AS (to_tsvector(...)) STORED), computed
    # from `text` at write time by Postgres itself. Computed(...) here
    # doesn't re-specify DDL (the raw-SQL migration already created the
    # real generated column; Alembic in this codebase never
    # autogenerates from models — see every migrations/versions/*.py) —
    # its purpose is telling SQLAlchemy's ORM to never include this
    # column in an INSERT/UPDATE's value list. Without it, a plain
    # `db.add(BibleVerse(...))` sends search_vector=NULL explicitly,
    # which Postgres rejects outright for a GENERATED ALWAYS column
    # ("cannot insert a non-DEFAULT value into column search_vector") —
    # confirmed live, not a hypothetical: this is exactly the error a
    # first test-fixture seed hit before this was added. Mapped at all
    # (rather than left off the model entirely) so
    # app/services/concordance.py can reference BibleVerse.search_vector
    # in queries.
    search_vector: Mapped[str | None] = mapped_column(TSVECTOR, Computed("to_tsvector('english', text)"), nullable=True)
