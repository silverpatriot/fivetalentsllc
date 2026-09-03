"""Sermon CRUD (Task 2) + the AI generation endpoint (Task 3).

Every route depends on get_db (app.core.deps) — RLS-scoped to the caller's
tenant and gated on subscription_status == 'active'. No route here ever
takes a tenant id from the client; it's derived from the verified Clerk
session, same as every other tenant-scoped route in this codebase.

created_by is left null on every sermon created through this router: there
is no Clerk-webhook-driven `users` table provisioning yet (only
organization.created -> tenants exists, see app/api/webhooks_clerk.py) —
that's a pre-existing gap from Phase 2, not something added or worked
around here. Building a users-provisioning path was out of scope for this
phase's task list; flagged in the Phase 3 completion notes.
"""
import re
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.core.deps import get_active_tenant_id, get_db
from app.models.sermon import Sermon
from app.schemas.generation import CitationFlag, EditRequest, GenerateRequest
from app.schemas.sermon import (
    DiffSegment,
    OutlineGenerateRequest,
    RestoreResponse,
    RevisionCompareResponse,
    RevisionDetail,
    RevisionSummary,
    SermonCreate,
    SermonRead,
    SermonUpdate,
)
from app.services import bible, revisions
from app.services.generation import edit_sermon_stream, generate_outline_from_manuscript, generate_sermon_stream
from app.services.openrouter import OpenRouterError
from app.services.pdf_export import render_sermon_pdf
from app.services.revision_diff import diff_words
from app.services.plan_limits import MAX_EDITS_PER_SERMON, is_within_edit_cap

router = APIRouter(prefix="/sermons", tags=["sermons"])


async def _get_owned_sermon(db: AsyncSession, sermon_id: uuid.UUID) -> Sermon:
    """RLS already prevents this from ever returning another tenant's row
    — a mismatched id looks identical (zero rows) whether it doesn't
    exist at all or belongs to someone else, which is the correct
    behavior (never confirm another tenant's sermon id exists)."""
    result = await db.execute(select(Sermon).where(Sermon.id == sermon_id))
    sermon = result.scalar_one_or_none()
    if sermon is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sermon not found")
    return sermon


@router.post("", response_model=SermonRead, status_code=status.HTTP_201_CREATED)
async def create_sermon(
    body: SermonCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    tenant_id: Annotated[uuid.UUID, Depends(get_active_tenant_id)],
) -> Sermon:
    # tenant_id must be set explicitly — RLS's WITH CHECK rejects an
    # insert with tenant_id left NULL (or any tenant_id other than the
    # current session's), it doesn't fill it in for you. Confirmed live:
    # omitting this raised "new row violates row-level security policy"
    # rather than silently inserting under the wrong tenant, which is the
    # correct failure mode but was still a real bug to catch here.
    # NOTE: content supplied directly here (rather than via generation or
    # the /documents upload pipeline) does NOT get ingested into the
    # cadence corpus — Task 2 defines exactly two ingestion paths
    # (finalized-in-Kerygma, and Task 1's file-upload endpoint), and a
    # bare JSON `content` field fits neither cleanly. A pastor wanting an
    # old manuscript in their cadence corpus has a real path for that:
    # upload it via POST /documents.
    sermon = Sermon(tenant_id=tenant_id, title=body.title, format=body.format, content=body.content)
    db.add(sermon)
    await db.flush()
    await db.refresh(sermon)
    return sermon


@router.get("", response_model=list[SermonRead])
async def list_sermons(db: Annotated[AsyncSession, Depends(get_db)]) -> list[Sermon]:
    result = await db.execute(select(Sermon).order_by(Sermon.created_at.desc()))
    return list(result.scalars().all())


@router.get("/{sermon_id}", response_model=SermonRead)
async def get_sermon(sermon_id: uuid.UUID, db: Annotated[AsyncSession, Depends(get_db)]) -> Sermon:
    return await _get_owned_sermon(db, sermon_id)


@router.patch("/{sermon_id}", response_model=SermonRead)
async def update_sermon(
    sermon_id: uuid.UUID, body: SermonUpdate, db: Annotated[AsyncSession, Depends(get_db)]
) -> Sermon:
    sermon = await _get_owned_sermon(db, sermon_id)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(sermon, field, value)
    await db.flush()
    await db.refresh(sermon)
    return sermon


@router.delete("/{sermon_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_sermon(sermon_id: uuid.UUID, db: Annotated[AsyncSession, Depends(get_db)]) -> None:
    sermon = await _get_owned_sermon(db, sermon_id)
    await db.delete(sermon)


@router.post("/{sermon_id}/generate")
async def generate_sermon(
    sermon_id: uuid.UUID,
    body: GenerateRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    tenant_id: Annotated[uuid.UUID, Depends(get_active_tenant_id)],
) -> StreamingResponse:
    """Kicks off outline -> draft generation for an existing sermon and
    streams the result back as Server-Sent Events (see
    app/services/generation.py for the event sequence).

    Only uses `db` (the request-scoped, dependency-injected session) for
    this upfront existence/ownership check, so a bad sermon_id 404s
    immediately rather than as a mid-stream SSE error. The actual
    generation work opens and owns its own session — see
    generate_sermon_stream's docstring for why a StreamingResponse body
    can't safely reuse a `Depends(get_db)` session.
    """
    await _get_owned_sermon(db, sermon_id)
    return StreamingResponse(
        generate_sermon_stream(tenant_id, sermon_id, body),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/{sermon_id}/edit")
async def edit_sermon(
    sermon_id: uuid.UUID,
    body: EditRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    tenant_id: Annotated[uuid.UUID, Depends(get_active_tenant_id)],
) -> StreamingResponse:
    """Phase 6: iterative, section-scoped editing of an existing draft,
    streamed back as SSE the same way /generate is — see
    app/services/generation.py's _run_edit for the event sequence
    (`target`, `delta`, `citations`, `done`/`error`).

    Every pre-flight check that doesn't need an LLM call happens HERE,
    against the request-scoped `db`, so a bad request fails fast with a
    real HTTP status rather than a mid-stream SSE error — same reasoning
    generate_sermon already follows for the sermon-existence check:
    - sermon exists and has a manuscript to edit
    - the edit cap (a cost/abuse guardrail, not billing — see
      plan_limits.is_within_edit_cap) isn't already exhausted, checked
      BEFORE any LLM call is made, not after
    - a given selection's offsets are actually valid against the CURRENT
      content (the generator re-validates this too, since its own
      session re-fetches content — this is the fast-fail copy of that
      same check)
    """
    sermon = await _get_owned_sermon(db, sermon_id)
    if sermon.content is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Generate a manuscript before editing it"
        )
    if body.selection is not None:
        sel = body.selection
        if sel.start < 0 or sel.end > len(sermon.content) or sel.start >= sel.end:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Selection does not match the current draft",
            )
    if not await run_in_threadpool(is_within_edit_cap, tenant_id, sermon_id):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"This sermon has reached its edit limit ({MAX_EDITS_PER_SERMON}) for now.",
        )
    return StreamingResponse(
        edit_sermon_stream(tenant_id, sermon_id, body),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/{sermon_id}/outline", response_model=SermonRead)
async def create_outline(
    sermon_id: uuid.UUID,
    body: OutlineGenerateRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    tenant_id: Annotated[uuid.UUID, Depends(get_active_tenant_id)],
) -> Sermon:
    """Condenses the sermon's already-generated manuscript into a
    persisted, preachable outline — see
    app/services/generation.py's generate_outline_from_manuscript.
    A plain JSON response, not SSE: a single fast LLM call, not the
    two-stage generate pipeline, so there's no reason for the
    StreamingResponse/own-session complexity `generate_sermon` needs.
    """
    sermon = await _get_owned_sermon(db, sermon_id)
    if sermon.content is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Generate a manuscript before creating an outline"
        )
    try:
        await generate_outline_from_manuscript(db, tenant_id, sermon, body.translation)
    except OpenRouterError as exc:
        # exc's own str() carries the raw upstream detail (server-side
        # logging only, via generate_outline_from_manuscript's
        # logger.exception) — user_message is what's safe to put in a
        # response a pastor actually sees.
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=exc.user_message) from exc
    await db.refresh(sermon)
    return sermon


@router.get("/{sermon_id}/citations", response_model=list[CitationFlag])
async def get_sermon_citations(
    sermon_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    translation: str | None = None,
) -> list[dict]:
    """Phase 7 Task 1: recomputes citation verification fresh against the
    sermon's CURRENT content — no LLM call (bible.verify_all_citations
    only calls the Bible text source and difflib, see its own docstring),
    no persistence, same "recompute rather than trust a cache" property
    generate/edit already rely on.

    Exists specifically because citation_flags was never a persisted
    column — /generate and /edit's SSE `citations` event is ephemeral,
    so a cold page load between generate/edit sessions otherwise has no
    citation data to show at all. The preach view (Task 1) needs this to
    highlight scripture quotes inline; confirmed real gap during the
    Phase 7 Task 1 design pass, not something already available to
    extend.
    """
    sermon = await _get_owned_sermon(db, sermon_id)
    if sermon.content is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Generate a manuscript first")
    return await bible.verify_all_citations(sermon.content, translation)


def _pdf_filename(title: str) -> str:
    """A Content-Disposition header value is attacker-adjacent here (the
    title is user-supplied) — CR/LF would let it inject extra headers,
    and quotes would break out of the filename="..." value. Collapsing to
    a conservative safe charset sidesteps both rather than trying to
    escape them correctly inline."""
    safe = re.sub(r"[^A-Za-z0-9 _-]", "", title).strip() or "sermon"
    return f"{safe}.pdf"


@router.get("/{sermon_id}/pdf")
async def get_sermon_pdf(sermon_id: uuid.UUID, db: Annotated[AsyncSession, Depends(get_db)]) -> Response:
    """Phase 7 Task 3: a real PDF, formatted using the preach view's
    typographic intent — see app/services/pdf_export.py. Recomputes
    citations fresh, same as GET /{sermon_id}/citations and for the same
    reason (no persisted citation_flags column) — used here to italicize
    scripture quotes in the exported PDF the same way the preach view
    highlights them on screen.
    """
    sermon = await _get_owned_sermon(db, sermon_id)
    if sermon.content is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Generate a manuscript first")
    citations = await bible.verify_all_citations(sermon.content)
    pdf_bytes = await render_sermon_pdf(sermon.title, sermon.content, citations)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{_pdf_filename(sermon.title)}"'},
    )


@router.get("/{sermon_id}/revisions", response_model=list[RevisionSummary])
async def list_sermon_revisions(sermon_id: uuid.UUID, db: Annotated[AsyncSession, Depends(get_db)]) -> list[RevisionSummary]:
    """Phase 8 Task 2. Newest first; "current" (synthesized from
    sermon.content, not a sermon_revisions row) leads the list whenever
    the sermon has content — see app/services/revisions.py."""
    sermon = await _get_owned_sermon(db, sermon_id)
    return await revisions.list_revisions(db, sermon)


# Declared BEFORE /{sermon_id}/revisions/{revision_id} deliberately —
# both are 2-segment paths under /revisions, and revision_id is a plain
# str (not a uuid.UUID path type, since it must also accept the literal
# "current"), so nothing stops it from swallowing "compare" as a
# revision_id if that route were matched first. FastAPI/Starlette match
# in declaration order for same-shape routes.
@router.get("/{sermon_id}/revisions/compare", response_model=RevisionCompareResponse)
async def compare_sermon_revisions(
    sermon_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    from_id: Annotated[str, Query()],
    to_id: Annotated[str, Query()],
) -> RevisionCompareResponse:
    """Phase 8 Task 3. from_id/to_id are each either "current" or a real
    revision UUID (as a string). Word-level diff — see
    app/services/revision_diff.py's module docstring for why word-level
    was chosen over line-level or sentence-level for sermon prose."""
    sermon = await _get_owned_sermon(db, sermon_id)
    resolved = await revisions.compare_revisions(db, sermon, from_id, to_id)
    if resolved is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="One or both revisions were not found")
    from_summary, to_summary, from_content, to_content = resolved
    diff = diff_words(from_content, to_content)
    return RevisionCompareResponse(
        from_revision=from_summary,
        to_revision=to_summary,
        diff=[DiffSegment(**seg) for seg in diff],
    )


@router.get("/{sermon_id}/revisions/{revision_id}", response_model=RevisionDetail)
async def get_sermon_revision(
    sermon_id: uuid.UUID, revision_id: str, db: Annotated[AsyncSession, Depends(get_db)]
) -> RevisionDetail:
    """Phase 8 Task 2: read-only full content of one past revision (or
    "current")."""
    sermon = await _get_owned_sermon(db, sermon_id)
    detail = await revisions.get_revision_detail(db, sermon, revision_id)
    if detail is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Revision not found")
    return detail


@router.post("/{sermon_id}/revisions/{revision_id}/restore", response_model=RestoreResponse)
async def restore_sermon_revision(
    sermon_id: uuid.UUID,
    revision_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    tenant_id: Annotated[uuid.UUID, Depends(get_active_tenant_id)],
) -> RestoreResponse:
    """Phase 8 Task 4. Rejects "current" here, before ever reaching
    revisions.restore_revision — restoring current onto itself is a
    meaningless no-op that would still burn a revision-row snapshot for
    nothing. Snapshot-before-overwrite and citation re-verification both
    happen inside restore_revision itself, not left to this route or the
    caller — see that function's own docstring."""
    if revision_id == "current":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="That's already the current version")
    sermon = await _get_owned_sermon(db, sermon_id)
    if sermon.content is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Generate a manuscript first")
    result = await revisions.restore_revision(db, tenant_id, sermon, revision_id)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Revision not found")
    new_revision, citation_flags = result
    # sermon.updated_at is server-computed (onupdate=func.now()) — refresh
    # so the response's timestamp (and any future list_revisions call's
    # "current" entry) reflects the real value, not a stale in-memory one.
    await db.refresh(sermon)
    return RestoreResponse(sermon=sermon, new_revision=new_revision, citation_flags=citation_flags)
