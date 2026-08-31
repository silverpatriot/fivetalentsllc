"""Upload endpoint for the shared RAG pipeline (Phase 4 Task 1) — PDF,
DOCX, or plain text, into either corpus (corpus_type in the form body).
Every route depends on get_db (app.core.deps) — RLS-scoped to the
caller's tenant and gated on subscription_status == 'active', same as
every other tenant-scoped route in this codebase.

Extraction+chunking happen HERE, synchronously, before any Celery task is
queued — see app/services/ingestion.py's docstring for exactly why (no
shared storage between this process and the celery-worker container, so
a raw file's bytes can never reach a background task; only chunk text
can). The original uploaded file is never persisted anywhere once this
request returns — processed into chunks, then discarded. Surface that in
the frontend upload UI so a pastor doesn't expect to re-download
whatever they uploaded.
"""
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.deps import get_active_tenant_id, get_db
from app.models.document import CorpusType, Document, DocumentSource
from app.schemas.document import DocumentRead
from app.services.extraction import ExtractionError, extract_text, guess_format
from app.services.ingestion import ingest_text

router = APIRouter(prefix="/documents", tags=["documents"])
settings = get_settings()

_VALID_CORPUS_TYPES = {c.value for c in CorpusType}


@router.post("", response_model=DocumentRead, status_code=status.HTTP_201_CREATED)
async def upload_document(
    db: Annotated[AsyncSession, Depends(get_db)],
    tenant_id: Annotated[uuid.UUID, Depends(get_active_tenant_id)],
    file: Annotated[UploadFile, File()],
    corpus_type: Annotated[str, Form()],
    title: Annotated[str | None, Form()] = None,
) -> Document:
    if corpus_type not in _VALID_CORPUS_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"corpus_type must be one of {sorted(_VALID_CORPUS_TYPES)}",
        )

    data = await file.read()
    if len(data) > settings.max_upload_size_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File exceeds the {settings.max_upload_size_bytes // (1024 * 1024)}MB upload limit",
        )

    filename = file.filename or "upload"
    try:
        fmt = guess_format(filename, file.content_type)
        text = extract_text(data, fmt)
    except ExtractionError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    document = await ingest_text(
        db,
        tenant_id,
        corpus_type=corpus_type,
        source=DocumentSource.UPLOADED.value,
        title=title or filename,
        text=text,
        original_filename=filename,
    )
    if document is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No extractable text found in this file",
        )
    return document


@router.get("", response_model=list[DocumentRead])
async def list_documents(
    db: Annotated[AsyncSession, Depends(get_db)], corpus_type: str | None = None
) -> list[Document]:
    stmt = select(Document).order_by(Document.created_at.desc())
    if corpus_type is not None:
        stmt = stmt.where(Document.corpus_type == corpus_type)
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(document_id: uuid.UUID, db: Annotated[AsyncSession, Depends(get_db)]) -> None:
    """RLS already prevents this from ever affecting another tenant's
    document — see app/api/sermons.py's _get_owned_sermon for the same
    reasoning applied here."""
    result = await db.execute(select(Document).where(Document.id == document_id))
    document = result.scalar_one_or_none()
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    await db.delete(document)
