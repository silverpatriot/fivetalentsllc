"""Embeds a document's chunks and stores them — the generic write side of
the Phase 4 RAG pipeline shared by both corpora. See
app/services/ingestion.py for the (also generic) call site and for why
this exists as a queued task at all: an uploaded file's raw bytes can't
reach this worker (no shared storage), so by the time this task runs the
document's text has already been extracted+chunked in-process by the
caller — this task only ever receives plain chunk text, embeds it, and
writes it.
"""
import logging
import uuid

from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.session import tenant_session_sync
from app.models.document import Document, DocumentChunk, DocumentStatus
from app.services.embeddings import EmbeddingError, embed_batch_sync
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=30)
def embed_document_chunks(self, document_id: str, tenant_id: str, chunk_texts: list[str]) -> None:
    tid = uuid.UUID(tenant_id)
    did = uuid.UUID(document_id)
    with tenant_session_sync(tid) as session:
        document = session.get(Document, did)
        if document is None:
            logger.warning("document %s not found (tenant %s) — skipping embedding", document_id, tenant_id)
            return

        try:
            vectors = embed_batch_sync(chunk_texts)
        except EmbeddingError as exc:
            if self.request.retries >= self.max_retries:
                # Final attempt exhausted — commit 'failed' so the
                # document doesn't sit in 'processing' forever with
                # nothing ever going to look at it again. Must NOT raise
                # here: tenant_session_sync's `with` block (via
                # session.begin()) rolls back on an exception leaving the
                # `with`, which would silently discard this status change
                # right when it matters most.
                logger.exception(
                    "Embedding failed for document %s after %d retries — marking failed",
                    document_id,
                    self.request.retries,
                )
                document.status = DocumentStatus.FAILED.value
                return
            logger.warning("Embedding failed for document %s — retrying", document_id, exc_info=True)
            raise self.retry(exc=exc) from exc

        # ON CONFLICT (document_id, chunk_index): defensive idempotency
        # for a task retried after a partial failure, not something this
        # single batched call is expected to hit in the success path.
        for chunk_index, (text, vector) in enumerate(zip(chunk_texts, vectors)):
            stmt = pg_insert(DocumentChunk).values(
                tenant_id=tid,
                corpus_type=document.corpus_type,
                document_id=did,
                chunk_index=chunk_index,
                content=text,
                embedding=vector,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[DocumentChunk.document_id, DocumentChunk.chunk_index],
                set_={"content": stmt.excluded.content, "embedding": stmt.excluded.embedding},
            )
            session.execute(stmt)

        document.status = DocumentStatus.READY.value
        # tenant_session_sync's `with` block commits this on clean exit.
