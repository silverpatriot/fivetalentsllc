"""Builds the structured LLM prompt for sermon generation: scripture text,
cadence examples, and format instructions kept as distinct, labeled
sections rather than concatenated into one blob (Task 3: "so the model can
weight them appropriately").

Cadence matching (Phase 4) is real pgvector cosine-similarity search
against the shared cadence corpus — see fetch_cadence_examples, and
app/services/retrieval.py for the generic search function both this and
the (separate, standalone) theology corpus call. The query vector is an
embedding of this NEW sermon's own title/topic/passage (the only text
that exists before generation runs); the past sermons whose *content*
embeds closest to that are surfaced as voice examples. That's a proxy for
"topically similar," not literally "same voice independent of topic" —
but it's what the kickoff spec's "replace recency with similarity search"
asks for, and topically-similar past sermons are a reasonable stand-in
for how this pastor tends to sound on a given kind of subject.

Ingestion into the cadence corpus (app/services/ingestion.py, queued via
Celery — see app/tasks/embeddings.py) happens at sermon finalization,
never inline here. A tenant with sermons that predate that trigger, that
never finished generating, or that simply hasn't finalized any sermon
yet, has no chunks in the cadence corpus for those sermons; the search
below naturally returns fewer (or zero) examples rather than erroring,
which is exactly the cold-start behavior _cadence_section already renders
as an explicit "no examples yet" instruction to the model, not a silent
gap.
"""
import dataclasses
import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.document import CorpusType, Document
from app.models.sermon import Sermon, SermonFormat
from app.services.bible import ScripturePassage, fetch_passage
from app.services.embeddings import EmbeddingError, embed_text
from app.services.retrieval import similarity_search
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
    # Cosine distance to the query vector (0 = identical, larger = less
    # similar). Not shown to the model — meaningless to an LLM as a bare
    # number in the prompt — but kept on the object for logging and for
    # tests/test_cadence_retrieval.py to assert against directly: the
    # titles still end up in generation_logs.prompt in this same
    # ascending-distance order, which is what a real generation is
    # checked against to confirm examples are the most-similar sermons,
    # not just the most recent.
    distance: float


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


def _cadence_query_text(sermon: Sermon, passage_reference: str | None, topic: str | None) -> str:
    """What this NEW sermon is embedded as, to search for topically/
    voice-similar past sermons against — see this module's docstring.
    Built from whatever's actually available before generation runs:
    title always exists (NOT NULL on the model); passage/topic are
    whichever the pastor gave for this request."""
    parts = [sermon.title, topic, passage_reference]
    return " — ".join(p for p in parts if p)


async def fetch_cadence_examples(
    db: AsyncSession,
    sermon: Sermon,
    passage_reference: str | None,
    topic: str | None,
    limit: int | None = None,
) -> list[CadenceExample]:
    """The tenant's own cadence-corpus documents (past sermons — generated
    in Kerygma or uploaded, per Task 2) whose content is most similar
    (pgvector cosine distance) to this new sermon's title/topic/passage —
    see this module's docstring for what "similar" means here and why.
    `db` is already RLS-scoped to the tenant (app.core.deps.get_db /
    app.db.session.tenant_session) — no explicit tenant_id filter needed,
    the same way every other query through this session works, and the
    same property tests/test_cadence_retrieval.py's isolation test relies
    on: a query embedding chosen to rank another tenant's document highest
    still can't see it, because RLS filters before ORDER BY ever runs.

    dedupe_by_document=True: three chunks from the same one past sermon
    isn't three voice examples, it's one sermon shown three times — see
    app/services/retrieval.py's docstring.

    Best-effort like fetch_web_context: an embeddings-API hiccup degrades
    to "no examples this time" (logged) rather than failing the whole
    generation over what's an enhancement, not a hard requirement.
    """
    limit = limit or settings.cadence_example_count
    query_text = _cadence_query_text(sermon, passage_reference, topic)
    try:
        query_vector = await embed_text(query_text)
    except EmbeddingError:
        logger.warning("Cadence-matching embedding failed for sermon %s — proceeding with no examples", sermon.id, exc_info=True)
        return []

    # A regeneration of an already-finalized sermon would otherwise be
    # able to match itself — it's already in its own tenant's cadence
    # corpus by the time a second /generate call runs.
    own_document = (
        await db.execute(select(Document.id).where(Document.sermon_id == sermon.id))
    ).scalar_one_or_none()
    exclude_ids = [str(own_document)] if own_document else None

    results = await similarity_search(
        db, CorpusType.CADENCE.value, query_vector, limit, dedupe_by_document=True, exclude_document_ids=exclude_ids
    )
    return [
        CadenceExample(title=r.document_title, excerpt=r.content[:4000], distance=r.distance) for r in results
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
    cadence_examples = await fetch_cadence_examples(db, sermon, passage_reference, topic)
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
