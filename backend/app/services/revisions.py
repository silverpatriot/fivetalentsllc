"""Phase 8 Tasks 2-4: version history, compare, and restore for a
sermon's edit lineage. Built on top of the capture Phase 6 already
established and the regeneration-gap fix from the Phase 8 Task 1 audit
— this module only reads/writes sermon_revisions and sermon.content,
no new capture logic here.
"""
import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.sermon import Sermon
from app.models.sermon_revision import SermonRevision
from app.schemas.sermon import RevisionDetail, RevisionSummary
from app.services import bible

logger = logging.getLogger(__name__)


def _current_summary(sermon: Sermon) -> RevisionSummary:
    return RevisionSummary(id="current", instruction=None, created_at=sermon.updated_at, is_current=True)


def _row_summary(row: SermonRevision) -> RevisionSummary:
    return RevisionSummary(id=str(row.id), instruction=row.instruction, created_at=row.created_at, is_current=False)


async def list_revisions(db: AsyncSession, sermon: Sermon) -> list[RevisionSummary]:
    """Newest first. "current" is synthesized straight from the Sermon
    row, never read from sermon_revisions — there is no revision row
    for the live content (see the Phase 8 Task 1 audit for why that's
    correct, not a gap) — so it's always the first entry, ahead of even
    the most recent real revision row, whenever the sermon has content
    at all."""
    result = await db.execute(
        select(SermonRevision)
        .where(SermonRevision.sermon_id == sermon.id)
        .order_by(SermonRevision.created_at.desc())
    )
    summaries = [_row_summary(row) for row in result.scalars().all()]
    if sermon.content is not None:
        summaries.insert(0, _current_summary(sermon))
    return summaries


async def _resolve(db: AsyncSession, sermon: Sermon, revision_id: str) -> tuple[str, RevisionSummary] | None:
    """(content, summary) for "current" or a real revision id belonging
    to THIS sermon. None if it doesn't resolve to anything real — not
    found, malformed, or (RLS already prevents cross-TENANT access;
    this is the cross-SERMON check within one tenant) belongs to a
    different sermon."""
    if revision_id == "current":
        if sermon.content is None:
            return None
        return sermon.content, _current_summary(sermon)
    try:
        rid = uuid.UUID(revision_id)
    except ValueError:
        return None
    result = await db.execute(
        select(SermonRevision).where(SermonRevision.id == rid, SermonRevision.sermon_id == sermon.id)
    )
    row = result.scalar_one_or_none()
    if row is None:
        return None
    return row.content, _row_summary(row)


async def get_revision_detail(db: AsyncSession, sermon: Sermon, revision_id: str) -> RevisionDetail | None:
    resolved = await _resolve(db, sermon, revision_id)
    if resolved is None:
        return None
    content, summary = resolved
    return RevisionDetail(content=content, **summary.model_dump())


async def compare_revisions(
    db: AsyncSession, sermon: Sermon, from_id: str, to_id: str
) -> tuple[RevisionSummary, RevisionSummary, str, str] | None:
    from_resolved = await _resolve(db, sermon, from_id)
    to_resolved = await _resolve(db, sermon, to_id)
    if from_resolved is None or to_resolved is None:
        return None
    from_content, from_summary = from_resolved
    to_content, to_summary = to_resolved
    return from_summary, to_summary, from_content, to_content


async def restore_revision(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    sermon: Sermon,
    revision_id: str,
    translation: str | None = None,
) -> tuple[RevisionSummary, list[dict]] | None:
    """Restores `revision_id`'s content as the sermon's live content.
    Two things happen structurally HERE, not left for a caller to
    remember (same "provably recoverable" principle as the rest of this
    edit system):

    1. The state right before restoring is snapshotted first — a
       restore is never itself a one-way door, exactly like an edit or
       a regeneration isn't.
    2. Citations are re-verified against the newly-restored content —
       nothing here is ever cached/persisted (same property
       get_sermon_citations/get_sermon_pdf already rely on), so this is
       a real, fresh check of whatever is now actually live, not
       whatever the citations panel happened to show before restoring.

    Returns None if revision_id doesn't resolve (caller 404s). Restoring
    "current" onto itself is rejected by the caller before this is ever
    reached — see the route's own check.
    """
    resolved = await _resolve(db, sermon, revision_id)
    if resolved is None:
        return None
    restored_content, _ = resolved

    pre_restore_row = SermonRevision(
        tenant_id=tenant_id,
        sermon_id=sermon.id,
        content=sermon.content,
        instruction=f"(restored to revision {revision_id})",
    )
    db.add(pre_restore_row)
    await db.flush()

    sermon.content = restored_content
    sermon.status = "ready"
    await db.flush()

    # Same defensive guard as _run/_run_edit's own citation-verification
    # calls (2026-09-03) — the restore itself (content write + its own
    # undo-point snapshot, both already flushed above) must not be lost
    # because this downstream re-verification happened to break.
    try:
        citation_flags = await bible.verify_all_citations(restored_content, translation)
    except Exception:
        logger.exception(
            "Citation verification crashed outright for sermon %s's restore — the restore itself "
            "still succeeded, with no citation flags this pass.",
            sermon.id,
        )
        citation_flags = []
    return _row_summary(pre_restore_row), citation_flags
