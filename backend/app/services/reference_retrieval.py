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

hnsw.ef_search (below): confirmed live, right after the first
full-scale corpus ingestion (165,898 real commentary chunks), that
Postgres's DEFAULT ef_search (40) silently caps how many candidates the
HNSW index scan even considers, independent of `limit` — a query asking
for limit=500 got back 37 rows at the default, and the full 500 once
ef_search was raised to 1000. That's not a filter problem (migration
0014 already fixed the type-partitioning issue this module's docstring
used to warn about) — it's that ef_search under-provisions candidates
before ORDER BY/LIMIT ever gets applied. Every caller here scales
ef_search off its own requested `limit` rather than reaching for one
large global constant: a callsite always asking for 3-10 results (the
real Study-citation shape) stays fast; the rare caller asking for
hundreds (bounded tests included) automatically gets a wide-enough
candidate window to actually satisfy that limit. SET LOCAL, not SET —
scoped to the current transaction only (every caller here is already
inside one — app.core.deps.get_db and app.db.session.tenant_session
both wrap in session.begin()), so it can never leak onto a pooled
connection some unrelated later request picks up.
"""
import dataclasses

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.reference import ReferenceChunk, ReferenceDocument

# Floor and per-result multiplier for hnsw.ef_search, and a ceiling so a
# pathologically large `limit` can't make one query scan the whole
# index. 100 as a floor comfortably covers every real small-limit
# Study-citation call; the *4 multiplier is what made the limit=500 test
# case above return its full 500 in practice, not just "some more than
# 37."
_EF_SEARCH_FLOOR = 100
_EF_SEARCH_PER_LIMIT = 4
_EF_SEARCH_CEILING = 2000


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
    # SET doesn't accept a bound parameter for its value over the wire
    # (not a normal parse/bind-able statement) — safe to interpolate
    # directly here since ef_search is computed server-side from `limit`,
    # never user-supplied text.
    ef_search = max(_EF_SEARCH_FLOOR, min(limit * _EF_SEARCH_PER_LIMIT, _EF_SEARCH_CEILING))
    await db.execute(text(f"SET LOCAL hnsw.ef_search = {ef_search}"))

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
