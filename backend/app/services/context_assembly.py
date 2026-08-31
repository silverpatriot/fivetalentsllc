"""Builds the structured LLM prompt for sermon generation: scripture text,
cadence examples, and format instructions kept as distinct, labeled
sections rather than concatenated into one blob (Task 3: "so the model can
weight them appropriately").

Cadence matching here is a plain recency query over the tenant's own past
sermons (ORDER BY created_at DESC LIMIT N) — NOT a pgvector similarity
search over sermon_embeddings, even though that table exists from Phase 1.
Real semantic cadence-matching is deferred alongside clip generation (see
the Phase 3 kickoff spec's stop line, and the decision recorded in this
phase's completion report): OpenRouter has no embeddings endpoint, so
populating sermon_embeddings would need a second LLM-provider key this
phase was never given. Recency is a reasonable stand-in for "how does this
pastor usually sound" until that lands for real.
"""
import dataclasses
import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.sermon import Sermon, SermonFormat
from app.services.bible import ScripturePassage, fetch_passage
from app.services.web_search import WebSearchError, search_context

logger = logging.getLogger(__name__)
settings = get_settings()

_FORMAT_INSTRUCTIONS: dict[SermonFormat, str] = {
    SermonFormat.EXPOSITORY: (
        "Expository: work verse-by-verse through the scripture text above. "
        "Draw the main points directly from the passage's own structure and "
        "flow, not from an outside topical outline."
    ),
    SermonFormat.TOPICAL: (
        "Topical: organize the sermon around the stated topic. Draw supporting "
        "scripture from across the Bible as needed, and cite every reference "
        "you use clearly and exactly (e.g. \"Romans 8:28\") so it can be checked."
    ),
    SermonFormat.NARRATIVE: (
        "Narrative: tell the passage or topic as a story arc — setup, tension, "
        "turn, resolution — rather than a list of doctrinal points."
    ),
    SermonFormat.TEXTUAL: (
        "Textual: draw the sermon's main divisions directly from the words and "
        "grammatical structure of a short passage (a phrase or single verse), "
        "rather than its broader narrative or topic."
    ),
    SermonFormat.CUSTOM: (
        "Custom: no fixed structural template — follow the pastor's stated "
        "topic/passage and title as directly as possible."
    ),
}


@dataclasses.dataclass
class CadenceExample:
    title: str
    excerpt: str


@dataclasses.dataclass
class WebResult:
    title: str
    url: str
    content: str


@dataclasses.dataclass
class AssembledContext:
    title: str
    format: SermonFormat
    passage_reference: str | None
    topic: str | None
    scripture: ScripturePassage | None
    cadence_examples: list[CadenceExample]
    web_results: list[WebResult]


async def fetch_cadence_examples(
    db: AsyncSession, exclude_sermon_id: uuid.UUID, limit: int | None = None
) -> list[CadenceExample]:
    """Most recent other sermons for the current tenant that have content
    to learn a voice from. `db` is already RLS-scoped to the tenant (see
    app.core.deps.get_db) — no explicit tenant_id filter needed here, the
    same way every other query through this session works."""
    limit = limit or settings.cadence_example_count
    result = await db.execute(
        select(Sermon)
        .where(Sermon.id != exclude_sermon_id, Sermon.content.isnot(None), Sermon.content != "")
        .order_by(Sermon.created_at.desc())
        .limit(limit)
    )
    sermons = result.scalars().all()
    return [
        CadenceExample(title=s.title, excerpt=(s.content or "")[:4000])
        for s in sermons
    ]


async def fetch_web_context(passage_reference: str | None, topic: str | None) -> list[WebResult]:
    """Live web search (Tavily) for commentary/theological background on
    the sermon's passage/topic. Best-effort: no key configured, or the
    call failing, returns [] rather than blocking generation — see
    app/services/web_search.py.
    """
    query_parts = [p for p in (passage_reference, topic) if p]
    if not query_parts:
        return []
    query = " ".join(query_parts) + " commentary theological background"
    try:
        raw_results = await search_context(query)
    except WebSearchError:
        logger.warning("Web search failed for query %r — proceeding without web context", query, exc_info=True)
        return []
    return [WebResult(title=r["title"], url=r["url"], content=r["content"]) for r in raw_results]


async def assemble_context(
    db: AsyncSession,
    sermon: Sermon,
    passage_reference: str | None,
    topic: str | None,
    translation: str | None = None,
) -> AssembledContext:
    scripture = await fetch_passage(passage_reference, translation) if passage_reference else None
    cadence_examples = await fetch_cadence_examples(db, exclude_sermon_id=sermon.id)
    web_results = await fetch_web_context(passage_reference, topic)
    return AssembledContext(
        title=sermon.title,
        format=sermon.format,
        passage_reference=passage_reference,
        topic=topic,
        scripture=scripture,
        cadence_examples=cadence_examples,
        web_results=web_results,
    )


def _scripture_section(ctx: AssembledContext) -> str:
    if ctx.scripture is not None:
        return (
            f"Reference: {ctx.scripture.reference} ({ctx.scripture.translation.upper()})\n\n"
            f"{ctx.scripture.text}\n\n"
            "This is the exact, verified text. Quote it verbatim if you quote it at all — "
            "do not paraphrase it and present the paraphrase as a quotation."
        )
    if ctx.passage_reference:
        return (
            f"The pastor specified a passage reference of {ctx.passage_reference!r}, but it "
            "did not resolve against the Bible text source — treat it as unverified and say so "
            "rather than inventing text for it."
        )
    return (
        "No specific passage was given for this sermon (topic-only). Do not fabricate a "
        "specific verse quotation from memory; if you cite scripture, cite it by reference only "
        "unless you are certain of the exact wording."
    )


def _cadence_section(ctx: AssembledContext) -> str:
    if not ctx.cadence_examples:
        return (
            "No past sermons are available yet for this tenant. Write in a clear, direct, "
            "pastoral voice — plain language, not academic."
        )
    parts = [
        "These are excerpts from the pastor's own past sermons. Match their voice, vocabulary, "
        "sentence rhythm, and level of formality — do NOT reuse their content or illustrations, "
        "only their style."
    ]
    for i, ex in enumerate(ctx.cadence_examples, start=1):
        parts.append(f'--- Example {i}: "{ex.title}" ---\n{ex.excerpt}')
    return "\n\n".join(parts)


def _format_section(ctx: AssembledContext) -> str:
    return _FORMAT_INSTRUCTIONS[ctx.format]


def _web_section(ctx: AssembledContext) -> str:
    if not ctx.web_results:
        return "No supplementary web context was retrieved for this sermon."
    parts = [
        "The following are excerpts from a live web search — background, commentary, or discussion from "
        "outside sources. They are NOT scripture and are NOT independently verified the way the SCRIPTURE "
        "section is. Use them only for context or illustration, and if you draw on one directly, attribute "
        "it by name (e.g. \"as one commentator notes...\") rather than presenting it as your own thought or "
        "as scripture. Never treat text here as the source for a scripture quotation — scripture wording "
        "only comes from the SCRIPTURE section; any quotation you attribute to a Bible reference is checked "
        "against the real text regardless of what these sources say."
    ]
    for r in ctx.web_results:
        parts.append(f"--- {r.title} ({r.url}) ---\n{r.content[:1500]}")
    return "\n\n".join(parts)


def to_prompt_sections(ctx: AssembledContext) -> dict[str, str]:
    """The structured sections, kept separate — this exact dict is what
    gets persisted to generation_logs.prompt for review, and what the
    *_messages builders below turn into the actual LLM message list."""
    return {
        "scripture": _scripture_section(ctx),
        "cadence_examples": _cadence_section(ctx),
        "format_instructions": _format_section(ctx),
        "web_context": _web_section(ctx),
        "title": ctx.title,
        "topic": ctx.topic or "",
    }


_SYSTEM_PROMPT = (
    "You are a homiletics assistant helping a pastor prepare a sermon for their own "
    "congregation. You will be given the sermon's title, its intended format, the exact "
    "scripture text (already verified — never contradict or alter it), excerpts from "
    "the pastor's own past sermons to match their voice, and supplementary web context. "
    "Sections below are separated and labeled deliberately; weight them accordingly: "
    "SCRIPTURE is authoritative source text, CADENCE EXAMPLES are a style reference only, "
    "FORMAT INSTRUCTIONS govern structure, and SUPPLEMENTARY WEB CONTEXT is unverified "
    "outside material for background/illustration only — attribute it if you use it, and "
    "never let it substitute for or override the verified SCRIPTURE text."
)


def build_outline_messages(ctx: AssembledContext) -> list[dict[str, str]]:
    sections = to_prompt_sections(ctx)
    user = (
        f"## TITLE\n{sections['title']}\n\n"
        f"## TOPIC\n{sections['topic'] or '(none given — see scripture/format below)'}\n\n"
        f"## SCRIPTURE\n{sections['scripture']}\n\n"
        f"## CADENCE EXAMPLES\n{sections['cadence_examples']}\n\n"
        f"## FORMAT INSTRUCTIONS\n{sections['format_instructions']}\n\n"
        f"## SUPPLEMENTARY WEB CONTEXT\n{sections['web_context']}\n\n"
        "Produce a short outline for this sermon: 3-6 main points, each with a one-sentence "
        "summary. No full manuscript text yet — outline only."
    )
    return [{"role": "system", "content": _SYSTEM_PROMPT}, {"role": "user", "content": user}]


def build_draft_messages(ctx: AssembledContext, outline_text: str) -> list[dict[str, str]]:
    sections = to_prompt_sections(ctx)
    user = (
        f"## TITLE\n{sections['title']}\n\n"
        f"## TOPIC\n{sections['topic'] or '(none given — see scripture/format below)'}\n\n"
        f"## SCRIPTURE\n{sections['scripture']}\n\n"
        f"## CADENCE EXAMPLES\n{sections['cadence_examples']}\n\n"
        f"## FORMAT INSTRUCTIONS\n{sections['format_instructions']}\n\n"
        f"## SUPPLEMENTARY WEB CONTEXT\n{sections['web_context']}\n\n"
        f"## APPROVED OUTLINE\n{outline_text}\n\n"
        "Write the full sermon manuscript following this outline. Whenever you cite scripture, "
        "cite the reference exactly (e.g. \"Romans 8:28\") so it can be checked against the "
        "source text — never quote a verse from memory without also giving its reference. If "
        "you draw on the supplementary web context, attribute it by name rather than presenting "
        "it as scripture or as your own original thought."
    )
    return [{"role": "system", "content": _SYSTEM_PROMPT}, {"role": "user", "content": user}]
