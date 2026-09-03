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
import asyncio
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
from app.models.sermon_revision import SermonRevision
from app.models.usage_event import UsageEventType
from app.schemas.generation import EditRequest, GenerateRequest
from app.services import bible
from app.services.context_assembly import (
    assemble_context,
    build_condense_outline_messages,
    build_draft_messages,
    build_outline_messages,
)
from app.services.editing_prompts import build_edit_messages, build_locate_messages, extract_target_span
from app.services.ingestion import ingest_text
from app.services.openrouter import OpenRouterError, chat_completion, stream_chat_completion
from app.services.plan_limits import has_cadence_access, is_within_sermon_quota
from app.tasks.usage_reporting import record_usage_event

logger = logging.getLogger(__name__)
settings = get_settings()

# Citation statuses that mean "nothing wrong here" — used below to decide
# what counts as a flagged citation worth warning about / reporting via
# flagged_citation_count. `not_quoted` (bible.verify_citation) means
# "nothing was quoted, so nothing was checked" — genuinely fine, same as
# `verified`, and NOT the same thing as `invalid_reference`/
# `quote_mismatch`, which are real problems. Keeping this as one shared
# set (rather than repeating the != "verified" check per call site) is
# what a prior version of this file got wrong: `not_quoted` didn't exist
# yet when both filters below were written as `!= "verified"` alone.
_OK_CITATION_STATUSES = {"verified", "not_quoted"}

# Structural-artifact guard for edit replacements (2026-09-03 proposal,
# confirmed before building — see _check_replacement_structure). Provisional
# band: the ratio is logged on every call, pass or fail, specifically to
# gather real data before tightening/loosening this later rather than
# guessing again.
_EDIT_LENGTH_RATIO_MIN = 0.4
_EDIT_LENGTH_RATIO_MAX = 2.5

# A pastor explicitly asking for reformatting must not get flagged for
# doing exactly what they asked. This is a keyword allow-list, not real
# intent detection — a KNOWN, ACCEPTED LIMITATION (2026-09-03), not an
# oversight: an instruction that asks for restructuring without using one
# of these words — "make this flow as two thoughts", "give this some
# breathing room" — will still get incorrectly rejected as a false
# structural-artifact catch. Widening the list doesn't close this, only
# narrows it (there's always another phrasing). Accepted anyway because
# the alternative — no bypass at all — reliably breaks EVERY restructuring
# request, whereas this only breaks the ones that don't happen to use one
# of these words; catching the real bug this guards against (an
# UNREQUESTED structural change) was judged worth that residual gap. If a
# future session is asked to "fix" this, that's the actual open problem —
# there's no small keyword-list edit that resolves it, it needs either a
# real intent signal (e.g. asking the model itself) or accepting the
# tradeoff stays.
_RESTRUCTURE_KEYWORDS = ("paragraph", "split", "section break", "line break", "restructure")

# Phase 8 Task 1: tags a SermonRevision row created by a REGENERATION
# (see _run below) rather than a real edit — instruction is NOT NULL, so
# this is a fixed sentinel value rather than an empty/nullable field.
# Public (no leading underscore): the Phase 8 Task 2 version-history UI
# imports this directly to distinguish "regenerated" rows from real edit
# instructions, rather than duplicating the literal string in two places.
REGENERATION_INSTRUCTION_SENTINEL = "(sermon regenerated)"


def _sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


def _check_replacement_structure(target_span: str, replacement_text: str, instruction: str) -> str | None:
    """Returns a user-facing failure detail if the model's replacement
    looks like a structural artifact rather than a clean, scoped edit;
    None if it looks fine. Two independent signals, both computed against
    target_span (the exact original text being replaced) — same
    "provably not a bad edit" bar the target-span verification in
    _run_edit already applies to the INPUT side of this flow, applied
    here to the OUTPUT side:

    1. Length delta: len(replacement_text) / len(target_span) outside
       [_EDIT_LENGTH_RATIO_MIN, _EDIT_LENGTH_RATIO_MAX] signals runaway
       or truncated generation, not a scoped edit.
    2. New paragraph breaks: replacement_text has MORE "\n\n" breaks
       (this codebase's paragraph-boundary convention — see
       chunking.py's _SEPARATORS) than target_span did. Removed breaks
       are never flagged — consolidating/shortening is a legitimate,
       common edit. And this signal is skipped entirely when the
       instruction itself already asks for restructuring (see
       _RESTRUCTURE_KEYWORDS) — a real "split this into two paragraphs"
       request must not trip the exact guard built to catch the
       UNREQUESTED version of the same structural change.
    """
    ratio = len(replacement_text) / len(target_span)
    ratio_ok = _EDIT_LENGTH_RATIO_MIN <= ratio <= _EDIT_LENGTH_RATIO_MAX
    logger.info(
        "Edit replacement length ratio %.3f (band %.1fx-%.1fx): %s",
        ratio,
        _EDIT_LENGTH_RATIO_MIN,
        _EDIT_LENGTH_RATIO_MAX,
        "within band" if ratio_ok else "OUT OF BAND",
    )
    if not ratio_ok:
        return "The proposed edit came back a very different length than the selected text — please try again."

    target_breaks = target_span.count("\n\n")
    replacement_breaks = replacement_text.count("\n\n")
    requested_restructure = any(kw in instruction.lower() for kw in _RESTRUCTURE_KEYWORDS)
    if replacement_breaks > target_breaks and not requested_restructure:
        return (
            "The proposed edit introduced new paragraph structure that wasn't requested — please "
            "try again, or explicitly ask for the reformatting you want."
        )

    return None


# Phase 6 follow-up (2026-09-02): _run_edit's locate call, and each chunk
# of its edit/rewrite stream, can go completely silent — no SSE bytes at
# all — for far longer than "a few seconds". Confirmed live: a single
# locate call took up to ~90s against OpenRouter's shared rate-limited
# pool, well past what an idle intermediate connection (a proxy, a flaky
# client) might tolerate with nothing on the wire. _HEARTBEAT_INTERVAL_
# SECONDS is a plain module attribute (not a default parameter value) so
# a test can monkeypatch it — a default parameter's value is bound once,
# at function-definition time, and would ignore a later monkeypatch of
# the module constant; every call site below re-reads this name at call
# time instead.
_HEARTBEAT_INTERVAL_SECONDS = 15.0
_SSE_HEARTBEAT = b": keepalive\n\n"


async def _heartbeat_while_pending(task: "asyncio.Task", interval: float) -> AsyncIterator[bytes]:
    """Yields a raw SSE comment line (`: keepalive\n\n` — valid SSE
    syntax every real client silently ignores; EventSource never fires
    an event for a comment line) every `interval` seconds for as long as
    `task` is still running. Does NOT retrieve `task`'s result or
    exception — the caller awaits `task` itself once this generator is
    exhausted (awaiting an already-done task returns/raises immediately,
    no extra delay)."""
    while not task.done():
        _, pending = await asyncio.wait({task}, timeout=interval)
        if task in pending:
            yield _SSE_HEARTBEAT


async def _record_llm_call(
    tenant_id: uuid.UUID, sermon_id: uuid.UUID, stage: GenerationStage, outcome: str
) -> None:
    """One usage_events row per real LLM call — outline, draft, and
    outline_condense each record their own, independently, regardless of
    success/failure. This is the raw ledger of what actually happened
    (Phase 3 completion review's decision); billable is the actual
    billing decision (Phase 5), and only DRAFT rows ever carry it:

    A completed sermon (one DRAFT succeeding) is the metered unit — see
    app/services/plan_limits.py's is_within_sermon_quota. OUTLINE is a
    sub-step of that same sermon, not a second billable thing, and
    OUTLINE_CONDENSE (regenerating a preaching outline afterward) is a
    free follow-up on a sermon already accounted for — neither stage
    checks quota; both always record billable=False. A failed DRAFT is
    also never billable — it produced no sermon, so it can't be overage,
    and doesn't consume the tenant's quota either.

    A metering hiccup must never lose a generation that otherwise
    succeeded (or its error state) — same protective try/except as the
    original single end-of-generation call this replaced.
    """
    try:
        billable = False
        if stage == GenerationStage.DRAFT and outcome == "succeeded":
            billable = not await run_in_threadpool(is_within_sermon_quota, tenant_id)
        await run_in_threadpool(
            record_usage_event,
            tenant_id,
            UsageEventType.AI_GENERATION,
            1.0,
            sermon_id=sermon_id,
            generation_stage=stage,
            outcome=outcome,
            billable=billable,
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

    cadence_enabled = await run_in_threadpool(has_cadence_access, tenant_id)
    try:
        ctx = await assemble_context(
            db, sermon, request.passage_reference, request.topic, request.translation,
            cadence_enabled=cadence_enabled,
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
            "cadence_available": cadence_enabled,
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
    flagged = [f for f in citation_flags if f["status"] not in _OK_CITATION_STATUSES]
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

    # Phase 8 Task 1 fix: /generate has no guard against being called on
    # a sermon that already has content (unlike /edit and /outline, which
    # both check sermon.content is None first) — a regeneration used to
    # silently overwrite whatever was there, including already-edited/
    # refined content, with zero recovery trail. Mirrors _run_edit's own
    # snapshot-before-overwrite pattern exactly, skipped only when there
    # is genuinely nothing yet to snapshot (the real first-ever
    # generation for this sermon). REGENERATION_INSTRUCTION_SENTINEL
    # (not a real edit instruction) lets the version-history UI tell
    # these rows apart from real edit instructions.
    if sermon.content is not None:
        db.add(
            SermonRevision(
                tenant_id=tenant_id,
                sermon_id=sermon.id,
                content=sermon.content,
                instruction=REGENERATION_INSTRUCTION_SENTINEL,
            )
        )

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


async def edit_sermon_stream(
    tenant_id: uuid.UUID, sermon_id: uuid.UUID, request: EditRequest
) -> AsyncIterator[bytes]:
    """Phase 6: iterative, section-scoped editing of an already-generated
    draft. Same own-session-for-the-whole-generator reasoning as
    generate_sermon_stream (see its docstring) — StreamingResponse tears
    down a Depends(get_db) session before this body has run a line.
    """
    async with tenant_session(tenant_id) as db:
        async for event in _run_edit(db, tenant_id, sermon_id, request):
            yield event


async def _run_edit(
    db, tenant_id: uuid.UUID, sermon_id: uuid.UUID, request: EditRequest
) -> AsyncIterator[bytes]:
    """The safety property this whole flow is built around: the model
    NEVER sees or returns the full manuscript for a rewrite. It only ever
    gets back ONE exact target span (from the pastor's own text
    selection, or — when nothing's selected — a separate locate call that
    must reproduce that span verbatim from the real content). This
    function then splices the model's replacement into that exact span
    itself. Everything outside it is untouched by construction, not by
    prompting discipline — see the Phase 6 kickoff spec's Task 1.1
    checkpoint for why that distinction was the deciding factor over a
    simpler full-draft-rewrite-per-edit design.

    A span that can't be found, or isn't unique, in the current content
    is a clean, reported failure (SSE `error`) rather than a silent edit
    against the wrong text — "provably untouched" over "probably
    untouched" was the explicit bar set for this design.
    """
    result = await db.execute(select(Sermon).where(Sermon.id == sermon_id))
    sermon = result.scalar_one_or_none()
    if sermon is None:
        yield _sse("error", {"detail": "Sermon not found"})
        return
    if sermon.content is None:
        yield _sse("error", {"detail": "Generate a manuscript before editing it"})
        return

    original_content = sermon.content
    sermon.status = "generating"
    await db.flush()

    # --- Step 1: establish the target span. Selection-based (exact,
    # unambiguous by construction — the frontend's own offsets) skips the
    # locate call entirely; instruction-only falls back to it. ---
    if request.selection is not None:
        start, end = request.selection.start, request.selection.end
        if start < 0 or end > len(original_content) or start >= end:
            sermon.status = "ready"
            await db.flush()
            yield _sse(
                "error",
                {"detail": "That selection no longer matches the current draft — reload and try again."},
            )
            return
        target_span = original_content[start:end]
    else:
        locate_messages = build_locate_messages(original_content, request.instruction)
        locate_task = asyncio.ensure_future(
            chat_completion(settings.openrouter_outline_model, locate_messages)
        )
        async for heartbeat in _heartbeat_while_pending(locate_task, _HEARTBEAT_INTERVAL_SECONDS):
            yield heartbeat
        try:
            locate_raw_text, locate_raw = await locate_task
        except OpenRouterError as exc:
            logger.exception("Edit-locate failed for sermon %s", sermon.id)
            sermon.status = "ready"
            await _record_llm_call(tenant_id, sermon.id, GenerationStage.EDIT_LOCATE, "failed")
            await db.flush()
            yield _sse("error", {"detail": exc.user_message})
            return

        # The API call itself succeeded — outcome tracks call success,
        # same convention as DRAFT (a flagged citation doesn't make a
        # DRAFT call "failed" either). A span that doesn't parse or
        # doesn't match the real content is a separate, application-level
        # failure, reported below, not retried and not recorded as an
        # OpenRouter failure it wasn't.
        await _record_llm_call(tenant_id, sermon.id, GenerationStage.EDIT_LOCATE, "succeeded")
        db.add(
            GenerationLog(
                tenant_id=tenant_id,
                sermon_id=sermon.id,
                stage=GenerationStage.EDIT_LOCATE,
                model=settings.openrouter_outline_model,
                prompt={"messages": locate_messages},
                raw_response=locate_raw,
            )
        )
        await db.flush()

        target_span = extract_target_span(locate_raw_text)
        occurrences = original_content.count(target_span) if target_span else 0
        if not target_span or occurrences != 1:
            sermon.status = "ready"
            await db.flush()
            detail = (
                "Couldn't pinpoint that part of the draft automatically — try selecting the exact "
                "text you want changed and asking again."
                if occurrences == 0
                else "That instruction matched multiple places in the draft — try selecting the "
                "exact text you want changed, or be more specific."
            )
            yield _sse("error", {"detail": detail})
            return

        start = original_content.find(target_span)
        end = start + len(target_span)

    yield _sse("target", {"start": start, "end": end, "text": target_span})

    # --- Step 2: the scoped rewrite itself, streamed like the original
    # draft pass — replacement text ONLY, never the whole manuscript. ---
    edit_messages = build_edit_messages(original_content, target_span, request.instruction)
    replacement_chunks: list[str] = []
    raw_lines: list[str] = []
    # Heartbeat-protected per-chunk, not just a plain `async for` — a
    # slow start (nothing until the model's first token arrives) is the
    # same silent-gap shape as the locate call above, and an unusually
    # slow gap between tokens gets the same protection for free, at no
    # cost when tokens are actually flowing normally.
    stream_iter = stream_chat_completion(
        settings.openrouter_draft_model, edit_messages, raw_sink=raw_lines
    ).__aiter__()
    try:
        while True:
            next_task = asyncio.ensure_future(stream_iter.__anext__())
            async for heartbeat in _heartbeat_while_pending(next_task, _HEARTBEAT_INTERVAL_SECONDS):
                yield heartbeat
            try:
                delta = await next_task
            except StopAsyncIteration:
                break
            replacement_chunks.append(delta)
            yield _sse("delta", {"text": delta})
    except OpenRouterError as exc:
        logger.exception("Edit failed for sermon %s", sermon.id)
        sermon.status = "ready"
        await _record_llm_call(tenant_id, sermon.id, GenerationStage.EDIT, "failed")
        await db.flush()
        yield _sse("error", {"detail": exc.user_message})
        return

    await _record_llm_call(tenant_id, sermon.id, GenerationStage.EDIT, "succeeded")

    replacement_text = "".join(replacement_chunks)

    # Structural-artifact guard: the EDIT call itself succeeded (recorded
    # above, same as a locate call that returns a span not present in the
    # real content — separate, application-level failure) but its OUTPUT
    # isn't trustworthy. Reject with a clean SSE error rather than splice
    # it in — the replacement has already been streamed to the client via
    # `delta` events by this point, so a silent retry can't undo what the
    # UI already rendered; telling the pastor plainly and letting them
    # re-ask is the same UX the locate-failure paths above already use.
    structural_issue = _check_replacement_structure(target_span, replacement_text, request.instruction)
    if structural_issue:
        logger.warning(
            "Sermon %s edit replacement rejected by structural-artifact guard: %s",
            sermon.id,
            structural_issue,
        )
        sermon.status = "ready"
        db.add(
            GenerationLog(
                tenant_id=tenant_id,
                sermon_id=sermon.id,
                stage=GenerationStage.EDIT,
                model=settings.openrouter_draft_model,
                prompt={"messages": edit_messages},
                raw_response="\n".join(raw_lines),
            )
        )
        await db.flush()
        yield _sse("error", {"detail": structural_issue})
        return

    new_content = original_content[:start] + replacement_text + original_content[end:]

    # --- Step 3: verify EVERY citation in the resulting draft, full text
    # — same call, same place in the flow, as original generation. An
    # edit that introduces a new (possibly hallucinated) reference gets
    # caught exactly like one would in a fresh draft; one anywhere else
    # in the untouched text is re-confirmed too, cheaply (no LLM cost). ---
    citation_flags = await bible.verify_all_citations(new_content, request.translation)
    flagged = [f for f in citation_flags if f["status"] not in _OK_CITATION_STATUSES]
    if flagged:
        logger.warning(
            "Sermon %s edit has %d unverified/mismatched citation(s): %s",
            sermon.id,
            len(flagged),
            [f["reference"] for f in flagged],
        )

    db.add(
        GenerationLog(
            tenant_id=tenant_id,
            sermon_id=sermon.id,
            stage=GenerationStage.EDIT,
            model=settings.openrouter_draft_model,
            prompt={"messages": edit_messages},
            raw_response="\n".join(raw_lines),
            citation_flags=citation_flags,
        )
    )

    # Snapshot BEFORE overwriting — migration 0015's minimum-viable
    # recoverability (Task 2): the pre-edit content is never lost, even
    # though only sermon.content itself is ever the "live" version.
    db.add(
        SermonRevision(
            tenant_id=tenant_id,
            sermon_id=sermon.id,
            content=original_content,
            instruction=request.instruction,
        )
    )

    sermon.content = new_content
    sermon.status = "ready"
    await db.flush()

    yield _sse("citations", {"flags": citation_flags})
    yield _sse(
        "done",
        {
            "sermon_id": str(sermon.id),
            "status": sermon.status,
            "content": new_content,
            "flagged_citation_count": len(flagged),
        },
    )
