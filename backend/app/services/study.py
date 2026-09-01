"""Phase 4 Task 3: the theology/study corpus's query interface — a
question in, a grounded answer with citations out. Deliberately separate
from app/services/context_assembly.py (sermon generation's prompt
builder): this is a different feature with a different prompt shape,
even though both share the same anti-hallucination discipline and the
same underlying retrieval (app/services/retrieval.py's similarity_search,
app/services/reference_retrieval.py's search_reference_corpus) and
web-search (app/services/web_search.py) infrastructure — reused, not
reimplemented, per the kickoff spec's "propose this connection rather
than building a parallel system."

Four distinct kinds of grounding material, each surfaced separately —
never blended:
  - YOUR DOCUMENTS: the tenant's own uploaded theology-corpus chunks.
    Private, trusted.
  - COMMENTARY: the baseline reference corpus (app/models/reference.py) —
    public-domain/CC-licensed, identical for every tenant, not RLS-
    scoped. Trusted, curated, but NOT the pastor's own material — always
    attempted (unlike Tavily below), since it's a stable, vetted source
    rather than a live, unverified one. Since migration 0008, multiple
    distinct commentaries can be ingested (app/models/reference.py's
    COMMENTARY_LABELS) — each gets its OWN labeled section below, never
    blended with another commentary, same discipline this module already
    applied to commentary-vs-cross-reference-vs-web.
  - CROSS-REFERENCES: same baseline reference corpus, the other
    reference_type.
  - WEB SEARCH RESULTS: live Tavily search, unverified, supplementary —
    consulted when the tenant's own corpus is thin (the original Task 3
    spec's framing) OR when the selected commentary source(s) came back
    thin (a later addition, same reasoning: a real gap in curated
    material is still a gap worth supplementing, whichever kind of local
    material was sparse).

Grounding discipline, stated plainly rather than overclaimed: the
citation LIST returned alongside the answer is exactly real — the actual
retrieved chunks, verbatim, with real document titles/ids (or real
Tavily URLs). The answer TEXT is LLM-generated and instructed to stick to
that material and cite it by bracket number, but — unlike scripture
citations in sermon generation, which are checked against bible-api.com
after the fact — there is no deterministic verification that the
generated prose accurately reflects what a given bracket number's source
actually says. That's an inherent property of RAG question-answering,
not a shortcut taken here; if a stronger guarantee is ever needed, the
next step would be a verification pass, same shape as
app/services/bible.py's verify_all_citations.
"""
import dataclasses
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.document import CorpusType
from app.models.reference import COMMENTARY_LABELS, ReferenceType
from app.services.embeddings import EmbeddingError, embed_text
from app.services.openrouter import OpenRouterError, chat_completion
from app.services.reference_retrieval import ReferenceChunkResult, search_reference_corpus
from app.services.retrieval import ChunkResult, similarity_search
from app.services.web_search import WebSearchError, search_context

logger = logging.getLogger(__name__)
settings = get_settings()

# How many of the tenant's own chunks to retrieve, and the threshold
# below which Tavily is also consulted — "the corpus doesn't cover
# something" is read here as "retrieval came back thin", not attempted
# via a distance-quality cutoff: with no real usage data yet to tune a
# cosine-distance threshold against, a count-based rule is the honest
# starting point, not a guess dressed up as precision.
OWN_CORPUS_RESULT_COUNT = 6
MIN_OWN_RESULTS_BEFORE_WEB_SUPPLEMENT = 3
# The baseline corpus is small and always attempted, so a smaller limit
# than the tenant's own corpus keeps the prompt from being dominated by
# it — commentary/cross-references are grounding, not the main answer.
REFERENCE_CORPUS_RESULT_COUNT = 3

# search_reference_corpus (like similarity_search) has no relevance
# floor of its own — it always returns its top-K, however poor a match,
# which is fine for a tenant's own corpus (naturally sparse if they
# haven't uploaded much) but wrong for a corpus that's now ALWAYS
# queried: an unrelated question would otherwise always surface "the
# least-bad" commentary/cross-reference entry as if it were relevant.
# Confirmed live, not guessed: genuinely relevant matches against a real
# ingested slice scored cosine distance ~0.26-0.32; two clearly unrelated
# queries ("nonsense query", "best pizza toppings") both scored
# ~0.77-0.81 regardless of how different they were from each other. 0.5
# sits cleanly in the gap.
MAX_REFERENCE_DISTANCE = 0.5


@dataclasses.dataclass
class StudyCitation:
    source_type: str  # "document" | "commentary" | "cross_reference" | "web"
    label: str  # what a citation bracket like [1] refers back to
    title: str  # document title, reference passage, or the web result's page title
    excerpt: str
    document_id: str | None = None
    url: str | None = None
    # Which specific commentary this citation came from (e.g.
    # "adam-clarke") — only meaningful when source_type == "commentary".
    commentary_source: str | None = None


@dataclasses.dataclass
class StudyAnswer:
    answer: str
    citations: list[StudyCitation]
    used_own_documents: bool
    used_web_search: bool


_SYSTEM_PROMPT = (
    "You are a theological research assistant helping a pastor study a topic, passage, or "
    "question using ONLY the material provided below — never your own outside/training "
    "knowledge. Several distinct kinds of material may be given: YOUR DOCUMENTS (the pastor's "
    "own uploaded reference material — private, trusted), one or more historical COMMENTARY "
    "sources (each labeled below by its author — a shared, public-domain reference library, "
    "trusted, but not the pastor's own), CROSS-REFERENCES (the same shared library), and WEB "
    "SEARCH RESULTS (live web search, unverified, supplementary). Never present one as if it "
    "were another, and never blend two different commentary authors' views together as if they "
    "were one voice. Cite every claim using the bracketed number matching its source below, "
    "e.g. \"...as Calvin argues [2]\". If the material given does not actually answer the "
    "question, say so plainly rather than guessing or filling the gap from memory."
)


def _numbered_section(header: str, items: list[str], start: int, empty_note: str) -> tuple[str, int]:
    if not items:
        return f"## {header}\n{empty_note}", start
    lines = [f"## {header}"]
    n = start
    for item in items:
        lines.append(f"[{n}] {item}")
        n += 1
    return "\n".join(lines), n


def _build_messages(
    question: str,
    doc_results: list[ChunkResult],
    commentary_results_by_source: dict[str, list[ReferenceChunkResult]],
    xref_results: list[ReferenceChunkResult],
    web_results: list[dict[str, str]],
) -> list[dict[str, str]]:
    doc_items = [f"{r.document_title}: {r.content}" for r in doc_results]
    doc_section, n = _numbered_section(
        "YOUR DOCUMENTS", doc_items, start=1, empty_note="No matching content found in your uploaded documents."
    )

    # One section PER commentary source actually retrieved — never one
    # blended "commentary" section once more than one source is in play.
    commentary_sections: list[str] = []
    for source_id, results in commentary_results_by_source.items():
        label = COMMENTARY_LABELS.get(source_id, source_id)
        items = [f"{r.title}: {r.content}" for r in results]
        section, n = _numbered_section(
            f"{label.upper()}'S COMMENTARY", items, start=n, empty_note=f"No matching commentary found from {label}."
        )
        commentary_sections.append(section)

    xref_items = [f"{r.title}: {r.content}" for r in xref_results]
    xref_section, n = _numbered_section("CROSS-REFERENCES", xref_items, start=n, empty_note="None retrieved for this question.")
    web_items = [f"{r['title']} ({r['url']}): {r['content']}" for r in web_results]
    web_section, _ = _numbered_section("WEB SEARCH RESULTS", web_items, start=n, empty_note="Not used for this question.")

    sections = "\n\n".join([doc_section, *commentary_sections, xref_section, web_section])
    user = (
        f"## QUESTION\n{question}\n\n{sections}\n\n"
        "Answer using only the material above, citing sources by their bracket number."
    )
    return [{"role": "system", "content": _SYSTEM_PROMPT}, {"role": "user", "content": user}]


def _citations_for(
    doc_results: list[ChunkResult],
    commentary_results_by_source: dict[str, list[ReferenceChunkResult]],
    xref_results: list[ReferenceChunkResult],
    web_results: list[dict[str, str]],
) -> list[StudyCitation]:
    citations = [
        StudyCitation(
            source_type="document", label=f"[{i}]", title=r.document_title, document_id=r.document_id,
            excerpt=r.content[:1000],
        )
        for i, r in enumerate(doc_results, start=1)
    ]

    for source_id, results in commentary_results_by_source.items():
        n = len(citations) + 1
        citations += [
            StudyCitation(
                source_type="commentary", label=f"[{i}]", title=r.title, excerpt=r.content[:1000],
                commentary_source=source_id,
            )
            for i, r in enumerate(results, start=n)
        ]

    n = len(citations) + 1
    citations += [
        StudyCitation(source_type="cross_reference", label=f"[{i}]", title=r.title, excerpt=r.content[:1000])
        for i, r in enumerate(xref_results, start=n)
    ]
    n = len(citations) + 1
    citations += [
        StudyCitation(source_type="web", label=f"[{i}]", title=r["title"], url=r["url"], excerpt=r["content"][:1000])
        for i, r in enumerate(web_results, start=n)
    ]
    return citations


async def answer_question(
    db: AsyncSession, question: str, commentary_sources: list[str] | None = None
) -> StudyAnswer:
    """`db` is already RLS-scoped to the caller's tenant (app.core.deps.get_db)
    for the tenant's own theology-corpus documents; the same session is
    passed straight through to search_reference_corpus unmodified, since
    reference_chunks has no tenant scoping to apply in the first place —
    see app/services/reference_retrieval.py.

    commentary_sources selects which specific commentary(ies) to query
    (e.g. ["matthew-henry", "adam-clarke"]) — see
    app/models/reference.py's COMMENTARY_LABELS for the full set. None
    (the default) runs a single unfiltered query across whatever
    commentary rows exist, same as this function's behavior before
    migration 0008 introduced multiple commentaries — the results are
    still grouped by their actual source_id before being handed to
    _build_messages/_citations_for, so even the unfiltered case never
    blends two different commentaries into one undifferentiated section
    if more than one happens to be ingested.
    """
    try:
        query_vector = await embed_text(question)
        doc_results = await similarity_search(
            db, CorpusType.THEOLOGY.value, query_vector, limit=OWN_CORPUS_RESULT_COUNT
        )

        commentary_results_by_source: dict[str, list[ReferenceChunkResult]] = {}
        if commentary_sources:
            for source in commentary_sources:
                filtered = [
                    r
                    for r in await search_reference_corpus(
                        db, ReferenceType.COMMENTARY.value, query_vector, limit=REFERENCE_CORPUS_RESULT_COUNT,
                        source_id=source,
                    )
                    if r.distance <= MAX_REFERENCE_DISTANCE
                ]
                commentary_results_by_source[source] = filtered
        else:
            unfiltered = [
                r
                for r in await search_reference_corpus(
                    db, ReferenceType.COMMENTARY.value, query_vector, limit=REFERENCE_CORPUS_RESULT_COUNT
                )
                if r.distance <= MAX_REFERENCE_DISTANCE
            ]
            for r in unfiltered:
                commentary_results_by_source.setdefault(r.source_id or "matthew-henry", []).append(r)

        xref_results = [
            r
            for r in await search_reference_corpus(
                db, ReferenceType.CROSS_REFERENCE.value, query_vector, limit=REFERENCE_CORPUS_RESULT_COUNT
            )
            if r.distance <= MAX_REFERENCE_DISTANCE
        ]
    except EmbeddingError:
        logger.warning("Study-corpus embedding failed for question %r — proceeding with no retrieved results", question, exc_info=True)
        doc_results, commentary_results_by_source, xref_results = [], {}, []

    total_commentary_results = sum(len(v) for v in commentary_results_by_source.values())

    web_results: list[dict[str, str]] = []
    if (
        len(doc_results) < MIN_OWN_RESULTS_BEFORE_WEB_SUPPLEMENT
        or total_commentary_results < MIN_OWN_RESULTS_BEFORE_WEB_SUPPLEMENT
    ):
        try:
            web_results = await search_context(question)
        except WebSearchError:
            logger.warning("Web search failed for study question %r — proceeding without it", question, exc_info=True)
            web_results = []

    if not doc_results and not total_commentary_results and not xref_results and not web_results:
        return StudyAnswer(
            answer=(
                "I couldn't find anything in your uploaded documents, the reference library, or via "
                "web search to answer this. Try uploading relevant material to your study library, or "
                "rephrasing the question."
            ),
            citations=[],
            used_own_documents=False,
            used_web_search=False,
        )

    messages = _build_messages(question, doc_results, commentary_results_by_source, xref_results, web_results)
    try:
        answer_text, _raw = await chat_completion(settings.openrouter_draft_model, messages)
    except OpenRouterError:
        logger.exception("Study answer generation failed for question %r", question)
        raise

    return StudyAnswer(
        answer=answer_text,
        citations=_citations_for(doc_results, commentary_results_by_source, xref_results, web_results),
        used_own_documents=bool(doc_results),
        used_web_search=bool(web_results),
    )
