"""Orchestrates one sermon-generation request end to end: assemble
context -> outline (cheap/fast model) -> full draft (stronger model,
streamed to the frontend as it's produced) -> verify every scripture
citation the model used -> log both LLM calls and record a usage_events
row for each (see _record_llm_call) -> persist the result onto the
sermon.

Yields Server-Sent-Events frames (bytes) — see app/api/sermons.py, which
wraps this in a StreamingResponse.

Deliberately opens and owns its OWN database session/transaction for its
entire duration (via tenant_session), rather than accepting the
route-level, dependency-injected `db` from app.core.deps.get_db. FastAPI
tears down a `yield`-based dependency (commits/closes it) as soon as the
route handler returns its StreamingResponse object — BEFORE this
generator function has run a single line, since the body isn't actually
iterated until the ASGI response is being sent. Confirmed live: the first
query this function ran using a passed-in get_db session failed with
"unrecognized configuration parameter app.current_tenant_id" — the
transaction that set it had already been closed out from under it. This
generator owning its session end-to-end is the fix, not a workaround
layered on top of it.
"""
import json
import logging
import uuid
from collections.abc import AsyncIterator

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.core.config import get_settings
from app.db.session import tenant_session
from app.models.document import CorpusType, DocumentSource
from app.models.generation_log import GenerationLog, GenerationStage
from app.models.sermon import Sermon
from app.models.usage_event import UsageEventType
from app.schemas.generation import GenerateRequest
from app.services import bible
from app.services.context_assembly import (
    assemble_context,
    build_condense_outline_messages,
    build_draft_messages,
    build_outline_messages,
)
from app.services.ingestion import ingest_text
from app.services.openrouter import OpenRouterError, chat_completion, stream_chat_completion
from app.tasks.usage_reporting import record_usage_event

logger = logging.getLogger(__name__)
settings = get_settings()


def _sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


async def _record_llm_call(
    tenant_id: uuid.UUID, sermon_id: uuid.UUID, stage: GenerationStage, outcome: str
) -> None:
    """One usage_events row per real LLM call — outline and draft each
    record their own, independently, regardless of success/failure. This
    is the raw ledger of what actually happened (Phase 3 completion
    review's decision); it is NOT the billing decision — every row this
    writes is billable=False on purpose, so nothing here changes what a
    tenant is actually charged until that's decided separately. See
    app/models/usage_event.py and app/tasks/usage_reporting.py.

    A metering hiccup must never lose a generation that otherwise
    succeeded (or its error state) — same protective try/except as the
    original single end-of-generation call this replaced.
    """
    try:
        await run_in_threadpool(
            record_usage_event,
            tenant_id,
            UsageEventType.AI_GENERATION,
            1.0,
            sermon_id=sermon_id,
            generation_stage=stage,
            outcome=outcome,
            billable=False,
        )
    except Exception:
        logger.exception("Failed to record usage_events row for sermon %s (%s, %s)", sermon_id, stage.value, outcome)


async def generate_sermon_stream(
    tenant_id: uuid.UUID, sermon_id: uuid.UUID, request: GenerateRequest
) -> AsyncIterator[bytes]:
    async with tenant_session(tenant_id) as db:
        async for event in _run(db, tenant_id, sermon_id, request):
            yield event
        # Reaching here means every step below either completed or was
        # caught and handled (status reverted to 'draft', a partial
        # generation_logs row kept) — either way there's something worth
        # committing rather than losing. An uncaught exception instead
        # propagates out of `async with tenant_session`, which rolls the
        # whole transaction back — an unhandled bug loses this attempt
        # entirely rather than persisting a half-written state.


async def _run(
    db, tenant_id: uuid.UUID, sermon_id: uuid.UUID, request: GenerateRequest
) -> AsyncIterator[bytes]:
    result = await db.execute(select(Sermon).where(Sermon.id == sermon_id))
    sermon = result.scalar_one_or_none()
    if sermon is None:
        yield _sse("error", {"detail": "Sermon not found"})
        return

    sermon.status = "generating"
    await db.flush()

    try:
        ctx = await assemble_context(
            db, sermon, request.passage_reference, request.topic, request.translation
        )
    except Exception as exc:  # httpx/network errors reaching the Bible API, etc.
        logger.exception("Context assembly failed for sermon %s", sermon.id)
        yield _sse("error", {"detail": f"Could not assemble context: {exc}"})
        return

    yield _sse(
        "context",
        {
            "scripture_resolved": ctx.scripture is not None,
            "cadence_example_count": len(ctx.cadence_examples),
            "web_result_count": len(ctx.web_results),
        },
    )

    # --- Outline pass: cheap/fast model, not streamed (it's quick, and
    # the frontend only needs the final outline text, not its tokens). ---
    outline_messages = build_outline_messages(ctx)
    try:
        outline_text, outline_raw = await chat_completion(settings.openrouter_outline_model, outline_messages)
    except OpenRouterError as exc:
        logger.exception("Outline generation failed for sermon %s", sermon.id)
        sermon.status = "draft"
        await _record_llm_call(tenant_id, sermon.id, GenerationStage.OUTLINE, "failed")
        # logger.exception above has the raw upstream detail; the SSE
        # event a pastor's browser actually renders gets the sanitized
        # message instead — see OpenRouterError.user_message's docstring.
        yield _sse("error", {"detail": exc.user_message})
        return

    await _record_llm_call(tenant_id, sermon.id, GenerationStage.OUTLINE, "succeeded")

    db.add(
        GenerationLog(
            tenant_id=tenant_id,
            sermon_id=sermon.id,
            stage=GenerationStage.OUTLINE,
            model=settings.openrouter_outline_model,
            prompt={"messages": outline_messages},
            raw_response=outline_raw,
        )
    )
    await db.flush()
    yield _sse("outline", {"text": outline_text})

    # --- Draft pass: stronger model, streamed token-by-token so a 30+
    # second generation isn't silent. ---
    draft_messages = build_draft_messages(ctx, outline_text)
    draft_chunks: list[str] = []
    raw_lines: list[str] = []
    try:
        async for delta in stream_chat_completion(
            settings.openrouter_draft_model, draft_messages, raw_sink=raw_lines
        ):
            draft_chunks.append(delta)
            yield _sse("delta", {"text": delta})
    except OpenRouterError as exc:
        logger.exception("Draft generation failed for sermon %s", sermon.id)
        sermon.status = "draft"
        await _record_llm_call(tenant_id, sermon.id, GenerationStage.DRAFT, "failed")
        yield _sse("error", {"detail": exc.user_message})
        return

    await _record_llm_call(tenant_id, sermon.id, GenerationStage.DRAFT, "succeeded")

    draft_text = "".join(draft_chunks)

    # --- Trust and accuracy: verify every citation before it's shown,
    # never trust the model's memory of scripture text. ---
    citation_flags = await bible.verify_all_citations(draft_text, request.translation)
    flagged = [f for f in citation_flags if f["status"] != "verified"]
    if flagged:
        logger.warning(
            "Sermon %s draft has %d unverified/mismatched citation(s): %s",
            sermon.id,
            len(flagged),
            [f["reference"] for f in flagged],
        )

    db.add(
        GenerationLog(
            tenant_id=tenant_id,
            sermon_id=sermon.id,
            stage=GenerationStage.DRAFT,
            model=settings.openrouter_draft_model,
            prompt={"messages": draft_messages},
            raw_response="\n".join(raw_lines),
            citation_flags=citation_flags,
        )
    )
    await db.flush()

    sermon.content = draft_text
    sermon.status = "ready"
    await db.flush()

    # Finalization is exactly the trigger point Phase 4 specifies for
    # cadence-corpus ingestion. chunk_text is cheap/synchronous; the
    # actual embedding call is queued inside ingest_text (via
    # app/tasks/embeddings.py) — this response is already streaming back
    # to the pastor, and an extra OpenRouter round trip to embed the
    # sermon for FUTURE cadence-matching searches has no reason to hold
    # it up.
    await ingest_text(
        db,
        tenant_id,
        corpus_type=CorpusType.CADENCE.value,
        source=DocumentSource.GENERATED.value,
        title=sermon.title,
        text=draft_text,
        sermon_id=sermon.id,
    )

    yield _sse("citations", {"flags": citation_flags})

    # Usage metering already happened per-LLM-call above (_record_llm_call
    # at the outline and draft steps) — nothing left to record here.
    yield _sse("done", {"sermon_id": str(sermon.id), "status": sermon.status, "flagged_citation_count": len(flagged)})


async def generate_outline_from_manuscript(
    db: AsyncSession, tenant_id: uuid.UUID, sermon: Sermon, translation: str | None = None
) -> None:
    """On-demand: condenses an ALREADY-generated manuscript (sermon.content)
    into a persisted, preachable outline (sermon.outline) — distinct from
    _run's internal pre-draft outline pass above, which is never saved.
    Mutates `sermon` in place and flushes; the caller (app/api/sermons.py)
    is responsible for having already confirmed sermon.content is not
    None before calling this.

    Unlike generate_sermon_stream, this accepts the route's normal
    Depends(get_db) session directly rather than opening its own —
    that generator's whole reason for owning its own session is the
    StreamingResponse teardown-timing issue documented on its own
    docstring above; this is a single synchronous JSON response, so the
    injected session stays valid for the entire request, no workaround
    needed.
    """
    messages = build_condense_outline_messages(sermon.content)
    try:
        outline_text, raw = await chat_completion(settings.openrouter_outline_model, messages)
    except OpenRouterError:
        logger.exception("Outline condensing failed for sermon %s", sermon.id)
        await _record_llm_call(tenant_id, sermon.id, GenerationStage.OUTLINE_CONDENSE, "failed")
        raise

    await _record_llm_call(tenant_id, sermon.id, GenerationStage.OUTLINE_CONDENSE, "succeeded")

    # Post-process, not a model-trust step: the condensing prompt already
    # instructs the model to cite scripture by reference only (never
    # re-quote it from memory — see context_assembly._CONDENSE_SYSTEM_PROMPT),
    # and this appends the REAL verified text for each reference it cited,
    # deterministically, the same "never trust the model's memory of
    # scripture" principle app/services/bible.py's verify_citation exists
    # for elsewhere in this app.
    citation_flags = await bible.verify_all_citations(outline_text, translation)
    effective_translation = (translation or settings.bible_translation).upper()
    verified_lines = [
        f"{f['reference']} ({effective_translation}): {f['source_text']}" for f in citation_flags if f["source_text"]
    ]
    full_outline = outline_text
    if verified_lines:
        full_outline += "\n\n## Scripture Referenced\n" + "\n".join(verified_lines)

    db.add(
        GenerationLog(
            tenant_id=tenant_id,
            sermon_id=sermon.id,
            stage=GenerationStage.OUTLINE_CONDENSE,
            model=settings.openrouter_outline_model,
            prompt={"messages": messages},
            raw_response=raw,
            citation_flags=citation_flags,
        )
    )
    sermon.outline = full_outline
    await db.flush()
