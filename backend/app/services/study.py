"""Phase 4 Task 3: the theology/study corpus's query interface — a
question in, a grounded answer with citations out. Deliberately separate
from app/services/context_assembly.py (sermon generation's prompt
builder): this is a different feature with a different prompt shape,
even though both share the same anti-hallucination discipline and the
same underlying retrieval (app/services/retrieval.py's similarity_search)
and web-search (app/services/web_search.py) infrastructure — reused, not
reimplemented, per the kickoff spec's "propose this connection rather
than building a parallel system."

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
from app.services.embeddings import EmbeddingError, embed_text
from app.services.openrouter import OpenRouterError, chat_completion
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


@dataclasses.dataclass
class StudyCitation:
    source_type: str  # "document" | "web"
    label: str  # what a citation bracket like [1] refers back to
    title: str  # document title, or the web result's page title
    excerpt: str
    document_id: str | None = None
    url: str | None = None


@dataclasses.dataclass
class StudyAnswer:
    answer: str
    citations: list[StudyCitation]
    used_own_documents: bool
    used_web_search: bool


_SYSTEM_PROMPT = (
    "You are a theological research assistant helping a pastor study a topic, passage, or "
    "question using ONLY the material provided below — never your own outside/training "
    "knowledge. Two distinct kinds of material may be given: YOUR DOCUMENTS (the pastor's own "
    "uploaded reference material — private, trusted) and WEB SEARCH RESULTS (live web search, "
    "unverified, supplementary). Never present one as if it were the other. Cite every claim "
    "using the bracketed number matching its source below, e.g. \"...as Calvin argues [2]\". "
    "If the material given does not actually answer the question, say so plainly rather than "
    "guessing or filling the gap from memory."
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
    question: str, doc_results: list[ChunkResult], web_results: list[dict[str, str]]
) -> list[dict[str, str]]:
    doc_items = [f"{r.document_title}: {r.content}" for r in doc_results]
    doc_section, next_n = _numbered_section(
        "YOUR DOCUMENTS", doc_items, start=1, empty_note="No matching content found in your uploaded documents."
    )
    web_items = [f"{r['title']} ({r['url']}): {r['content']}" for r in web_results]
    web_section, _ = _numbered_section(
        "WEB SEARCH RESULTS", web_items, start=next_n, empty_note="Not used for this question."
    )
    user = f"## QUESTION\n{question}\n\n{doc_section}\n\n{web_section}\n\nAnswer using only the material above, citing sources by their bracket number."
    return [{"role": "system", "content": _SYSTEM_PROMPT}, {"role": "user", "content": user}]


def _citations_for(doc_results: list[ChunkResult], web_results: list[dict[str, str]]) -> list[StudyCitation]:
    citations = [
        StudyCitation(
            source_type="document", label=f"[{i}]", title=r.document_title, document_id=r.document_id,
            excerpt=r.content[:1000],
        )
        for i, r in enumerate(doc_results, start=1)
    ]
    start = len(doc_results) + 1
    citations += [
        StudyCitation(source_type="web", label=f"[{i}]", title=r["title"], url=r["url"], excerpt=r["content"][:1000])
        for i, r in enumerate(web_results, start=start)
    ]
    return citations


async def answer_question(db: AsyncSession, question: str) -> StudyAnswer:
    """`db` is already RLS-scoped to the caller's tenant (app.core.deps.get_db)
    — retrieval only ever sees this tenant's own theology-corpus documents,
    same as every other query in this codebase."""
    try:
        query_vector = await embed_text(question)
        doc_results = await similarity_search(
            db, CorpusType.THEOLOGY.value, query_vector, limit=OWN_CORPUS_RESULT_COUNT
        )
    except EmbeddingError:
        logger.warning("Study-corpus embedding failed for question %r — proceeding with no own-document results", question, exc_info=True)
        doc_results = []

    web_results: list[dict[str, str]] = []
    if len(doc_results) < MIN_OWN_RESULTS_BEFORE_WEB_SUPPLEMENT:
        try:
            web_results = await search_context(question)
        except WebSearchError:
            logger.warning("Web search failed for study question %r — proceeding without it", question, exc_info=True)
            web_results = []

    if not doc_results and not web_results:
        return StudyAnswer(
            answer=(
                "I couldn't find anything in your uploaded documents or via web search to answer "
                "this. Try uploading relevant material to your study library, or rephrasing the question."
            ),
            citations=[],
            used_own_documents=False,
            used_web_search=False,
        )

    messages = _build_messages(question, doc_results, web_results)
    try:
        answer_text, _raw = await chat_completion(settings.openrouter_draft_model, messages)
    except OpenRouterError as exc:
        logger.exception("Study answer generation failed for question %r", question)
        raise

    return StudyAnswer(
        answer=answer_text,
        citations=_citations_for(doc_results, web_results),
        used_own_documents=bool(doc_results),
        used_web_search=bool(web_results),
    )
