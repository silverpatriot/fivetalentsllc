"""The one ingestion entry point both corpora share (Phase 4 kickoff spec:
"build this once, generically... don't fork the pipeline"). Two callers:
  - app/services/generation.py, at sermon finalization (source=GENERATED)
  - app/api/documents.py, on file upload (source=UPLOADED, after
    app/services/extraction.py has already turned the file into plain text)

Both callers run inside an async request/task context with an
already-RLS-scoped AsyncSession, so ingest_text is async throughout —
chunking is cheap, synchronous CPU work (no reason to thread-pool it),
and only the embedding step is queued.

No object storage anywhere in this stack, and the backend/celery-worker
containers share no filesystem (confirmed before designing this) — so a
raw uploaded file's bytes can never reach the worker. The design instead
extracts+chunks synchronously, in-process, and only ever sends the
resulting chunk *text* across the Celery boundary, which serializes into
a task's JSON args fine. Consequence, and worth surfacing in the UI: an
uploaded file's original bytes are never persisted anywhere — processed
into chunks, then discarded. There is no way to re-download what was
originally uploaded, only the extracted text as embedded/stored.
"""
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document import Document
from app.services.chunking import chunk_text
from app.tasks.embeddings import embed_document_chunks


async def ingest_text(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    corpus_type: str,
    source: str,
    title: str,
    text: str,
    sermon_id: uuid.UUID | None = None,
    original_filename: str | None = None,
) -> Document | None:
    """Chunk `text`, create the `documents` row, queue embedding of its
    chunks. Returns None (and creates nothing) for blank/empty text —
    e.g. a sermon that somehow finalized with no content, or an uploaded
    file that extracted to nothing — rather than creating a documents row
    with zero chunks that would sit in 'processing' forever."""
    chunks = chunk_text(text)
    if not chunks:
        return None

    document = Document(
        tenant_id=tenant_id,
        corpus_type=corpus_type,
        source=source,
        sermon_id=sermon_id,
        title=title,
        original_filename=original_filename,
    )
    db.add(document)
    await db.flush()
    await db.refresh(document)

    embed_document_chunks.delay(str(document.id), str(tenant_id), [c.content for c in chunks])
    return document
