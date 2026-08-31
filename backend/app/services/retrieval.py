"""The one similarity-search function both corpora share (Phase 4 kickoff
spec's Task 1: "a generic similarity-search function: given a query... and
a corpus type + tenant, return the top-N most relevant chunks"). Tenant
scoping comes entirely from RLS on the session passed in (same as every
other query in this codebase) — this function never takes or needs a
tenant_id parameter itself.
"""
import dataclasses

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document import Document, DocumentChunk


@dataclasses.dataclass
class ChunkResult:
    document_id: str
    document_title: str
    content: str
    distance: float


async def similarity_search(
    db: AsyncSession,
    corpus_type: str,
    query_vector: list[float],
    limit: int,
    *,
    dedupe_by_document: bool = False,
    exclude_document_ids: list[str] | None = None,
) -> list[ChunkResult]:
    """Top-`limit` chunks in `corpus_type` by cosine distance to
    `query_vector`.

    dedupe_by_document=True returns at most one chunk — its single best
    match — per source document, via Postgres DISTINCT ON. Cadence
    matching wants this (three chunks from the SAME one past sermon isn't
    three voice examples, it's one sermon shown three times, and biases
    the model toward whatever happened to chunk finely); a theology-corpus
    question in Task 3 generally does NOT — the top five most relevant
    passages might legitimately all come from one document, and
    excluding four of them because they share a document_id would be
    throwing away real signal. Default False: raw top-N chunks,
    regardless of which document(s) they came from.

    exclude_document_ids: generic exclusion, not corpus-specific — e.g.
    cadence-matching on a sermon being regenerated (already finalized
    once, so already ingested into its own tenant's cadence corpus)
    excludes that sermon's own document so it can't match itself.
    """
    distance = DocumentChunk.embedding.cosine_distance(query_vector).label("distance")
    exclusion = DocumentChunk.document_id.notin_(exclude_document_ids) if exclude_document_ids else True

    if dedupe_by_document:
        # DISTINCT ON (document_id) ORDER BY document_id, distance: for
        # each document, keep only its single closest chunk. Then the
        # outer query re-ranks THOSE by distance globally and takes the
        # top N — DISTINCT ON's own ordering is per-group, not global.
        inner = (
            select(
                DocumentChunk.document_id,
                DocumentChunk.content,
                distance,
            )
            .where(DocumentChunk.corpus_type == corpus_type, exclusion)
            .distinct(DocumentChunk.document_id)
            .order_by(DocumentChunk.document_id, distance)
            .subquery()
        )
        stmt = (
            select(inner.c.document_id, inner.c.content, inner.c.distance, Document.title)
            .join(Document, Document.id == inner.c.document_id)
            .order_by(inner.c.distance)
            .limit(limit)
        )
        rows = (await db.execute(stmt)).all()
        return [
            ChunkResult(document_id=str(r.document_id), document_title=r.title, content=r.content, distance=float(r.distance))
            for r in rows
        ]

    stmt = (
        select(DocumentChunk.document_id, DocumentChunk.content, distance, Document.title)
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(DocumentChunk.corpus_type == corpus_type, exclusion)
        .order_by(distance)
        .limit(limit)
    )
    rows = (await db.execute(stmt)).all()
    return [
        ChunkResult(document_id=str(r.document_id), document_title=r.title, content=r.content, distance=float(r.distance))
        for r in rows
    ]
