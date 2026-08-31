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
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_active_tenant_id, get_db
from app.models.sermon import Sermon
from app.schemas.generation import GenerateRequest
from app.schemas.sermon import SermonCreate, SermonRead, SermonUpdate
from app.services.generation import generate_sermon_stream

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
