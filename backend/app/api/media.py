"""Upload + management endpoints for sermon audio ("Recordings" in the
frontend). POST persists the raw file (see app/services/storage.py)
FIRST, then transcribes it synchronously (Groq primary / OpenAI fallback
— app/services/transcription.py) before the request returns. GET ""
lists a tenant's recordings (optionally filtered to one sermon); GET
"/{id}/audio" streams the original bytes back for playback — unlike
app/api/documents.py, the raw file IS kept, so this is a real
MediaStorage.load() away, not a re-derivation; DELETE removes both the
DB row and the underlying file. Every route depends on get_db
(app.core.deps) — RLS-scoped to the caller's tenant and gated on
subscription_status == 'active', same as every other tenant-scoped route
in this codebase.

Transcription runs INLINE here, not via Celery — see
app/services/transcription.py's module docstring for why (no shared
storage between backend/celery-worker, and Groq's turbo model transcribes
well faster than realtime anyway).

Storage happens before transcription is even attempted, specifically so
a transcription failure never loses the tenant's original recording —
transcription_status ends up 'failed' with transcript_text/
duration_seconds left null, but storage_path is always set and the file
is always retrievable. This mirrors media_files.transcription_status's
own documented states ('pending -> processing -> completed | failed') —
a transcription failure is a normal terminal state to record, not
grounds to fail the whole upload with an HTTP error.
"""
import mimetypes
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.deps import get_active_tenant_id, get_db
from app.models.media_file import MediaFile
from app.models.usage_event import UsageEventType
from app.schemas.media_file import MediaFileRead
from app.services.storage import get_media_storage
from app.services.transcription import TranscriptionError, transcribe_audio
from app.tasks.usage_reporting import record_usage_event

router = APIRouter(prefix="/media", tags=["media"])
settings = get_settings()


async def _get_owned_media_file(db: AsyncSession, media_file_id: uuid.UUID) -> MediaFile:
    """RLS already prevents this from ever returning another tenant's row
    — same reasoning as app/api/sermons.py's _get_owned_sermon."""
    result = await db.execute(select(MediaFile).where(MediaFile.id == media_file_id))
    media_file = result.scalar_one_or_none()
    if media_file is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Recording not found")
    return media_file


@router.post("", response_model=MediaFileRead, status_code=status.HTTP_201_CREATED)
async def upload_media(
    db: Annotated[AsyncSession, Depends(get_db)],
    tenant_id: Annotated[uuid.UUID, Depends(get_active_tenant_id)],
    file: Annotated[UploadFile, File()],
    sermon_id: Annotated[uuid.UUID | None, Form()] = None,
) -> MediaFile:
    data = await file.read()
    if len(data) > settings.max_media_upload_size_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File exceeds the {settings.max_media_upload_size_bytes // (1024 * 1024)}MB upload limit",
        )

    filename = file.filename or "upload"
    # Generated here, not left to the id column's server_default, so the
    # storage path can be namespaced by it — save() happens before the
    # MediaFile row exists at all. An explicit id on the INSERT below
    # takes precedence over the column's server_default (only applies
    # when a column is omitted from the INSERT), so this doesn't fight
    # gen_random_uuid() — it just pre-empts it with a value we already
    # need before that INSERT can happen.
    media_file_id = uuid.uuid4()

    storage = get_media_storage()
    storage_path = storage.save(tenant_id, media_file_id, filename, data)

    try:
        result = await transcribe_audio(data, filename)
    except TranscriptionError:
        media_file = MediaFile(
            id=media_file_id,
            tenant_id=tenant_id,
            sermon_id=sermon_id,
            original_filename=filename,
            storage_path=storage_path,
            transcription_status="failed",
        )
    else:
        media_file = MediaFile(
            id=media_file_id,
            tenant_id=tenant_id,
            sermon_id=sermon_id,
            original_filename=filename,
            storage_path=storage_path,
            duration_seconds=result.duration_seconds,
            transcription_status="completed",
            transcript_text=result.text,
        )

    db.add(media_file)
    await db.flush()  # populate server-generated fields (created_at) before this returns

    if media_file.transcription_status == "completed":
        record_usage_event(tenant_id, UsageEventType.TRANSCRIPTION_MINUTE, media_file.duration_seconds / 60)

    return media_file


@router.get("", response_model=list[MediaFileRead])
async def list_media(
    db: Annotated[AsyncSession, Depends(get_db)], sermon_id: uuid.UUID | None = None
) -> list[MediaFile]:
    stmt = select(MediaFile).order_by(MediaFile.created_at.desc())
    if sermon_id is not None:
        stmt = stmt.where(MediaFile.sermon_id == sermon_id)
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.get("/{media_file_id}/audio")
async def get_media_audio(media_file_id: uuid.UUID, db: Annotated[AsyncSession, Depends(get_db)]) -> Response:
    """Streams the original uploaded bytes back for playback — unlike
    app/api/documents.py, the raw file is kept (see storage.py), so this
    is just a MediaStorage.load() away. Content-Type is guessed from the
    original filename (mimetypes, stdlib) since neither MediaFile nor
    MediaStorage records what was sent in the upload's own Content-Type
    header."""
    media_file = await _get_owned_media_file(db, media_file_id)
    storage = get_media_storage()
    try:
        data = storage.load(media_file.storage_path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Recording file is missing") from exc
    content_type = mimetypes.guess_type(media_file.original_filename)[0] or "application/octet-stream"
    return Response(content=data, media_type=content_type)


@router.delete("/{media_file_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_media(media_file_id: uuid.UUID, db: Annotated[AsyncSession, Depends(get_db)]) -> None:
    media_file = await _get_owned_media_file(db, media_file_id)
    storage = get_media_storage()
    storage.delete(media_file.storage_path)
    await db.delete(media_file)
