"""Retrieval for the baseline reference corpus (cross-references,
commentary) — deliberately a SEPARATE function from
app.services.retrieval.similarity_search, not an overload of it, per an
explicit design decision: similarity_search reads document_chunks, which
IS tenant-scoped and RLS-enforced; reference_chunks has neither
tenant_id nor RLS, on purpose (see app/models/reference.py and migration
0007). Keeping these structurally separate — different function, this
module rather than retrieval.py — means a future edit to one can never
accidentally blur the tenant-isolation boundary into the other.

`db` here may be ANY session, including a tenant-scoped one with
app.current_tenant_id set — reference_chunks has RLS disabled, so it's
readable regardless of what tenant context (if any) exists on the
connection. app/services/study.py passes its normal request-scoped `db`
straight through with no special handling for exactly this reason.
"""
import dataclasses

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.reference import ReferenceChunk, ReferenceDocument


@dataclasses.dataclass
class ReferenceChunkResult:
    document_id: str
    title: str
    passage_reference: str | None
    content: str
    distance: float
    source_id: str | None = None


async def search_reference_corpus(
    db: AsyncSession, reference_type: str, query_vector: list[float], limit: int, source_id: str | None = None
) -> list[ReferenceChunkResult]:
    """source_id (migration 0008) narrows to one specific commentary when
    given, e.g. "adam-clarke" — meaningless for reference_type=
    cross_reference (there's only ever one cross-reference source), so
    callers there simply never pass it. Left unset, every matching row is
    returned regardless of source, which is exactly today's pre-0008
    behavior for any caller not yet updated to pick sources explicitly."""
    distance = ReferenceChunk.embedding.cosine_distance(query_vector).label("distance")
    stmt = (
        select(
            ReferenceChunk.reference_document_id,
            ReferenceChunk.content,
            distance,
            ReferenceDocument.title,
            ReferenceDocument.passage_reference,
            ReferenceDocument.source_id,
        )
        .join(ReferenceDocument, ReferenceDocument.id == ReferenceChunk.reference_document_id)
        .where(ReferenceChunk.reference_type == reference_type)
    )
    if source_id is not None:
        stmt = stmt.where(ReferenceDocument.source_id == source_id)
    stmt = stmt.order_by(distance).limit(limit)
    rows = (await db.execute(stmt)).all()
    return [
        ReferenceChunkResult(
            document_id=str(r.reference_document_id),
            title=r.title,
            passage_reference=r.passage_reference,
            content=r.content,
            distance=float(r.distance),
            source_id=r.source_id,
        )
        for r in rows
    ]
