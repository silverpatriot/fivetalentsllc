"""Concordance search — exact/stemmed word lookup across scripture
("every verse containing 'grace'"), NOT semantic similarity. Deliberately
lexical (Postgres tsvector/plainto_tsquery against bible_verses.search_vector,
migration 0009), not the pgvector/embedding pattern
app/services/reference_retrieval.py uses — a concordance query for "grace"
should return exact/stemmed occurrences of that word, not verses that are
merely topically similar (which would surface "mercy"/"kindness" verses
instead, the wrong tool for what a concordance is). No embedding call, no
EmbeddingError handling, no OpenRouter dependency anywhere in this file.

Tavily web-search supplement when local matches are thin, same "local
first, web only when thin, always separately labeled, never blended"
discipline app/services/study.py already established — reuses
search_context/WebSearchError from app/services/web_search.py as-is.
Orchestrated here (search_local + the thin-result Tavily decision) rather
than inside search_concordance itself, mirroring how study.py's
answer_question — not search_reference_corpus — owns that same decision;
search_concordance stays a pure DB-query helper.
"""
import dataclasses
import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bible_verse import BibleVerse
from app.services.web_search import WebSearchError, search_context

logger = logging.getLogger(__name__)

# Same threshold/precedent as app/services/study.py's
# MIN_OWN_RESULTS_BEFORE_WEB_SUPPLEMENT — defined locally rather than
# imported, since it governs a structurally different signal (local
# concordance match count, not a tenant's own-document count) even
# though the number happens to match.
MIN_LOCAL_RESULTS_BEFORE_WEB_SUPPLEMENT = 3


@dataclasses.dataclass
class ConcordanceMatch:
    translation: str
    book: str
    chapter: int
    verse: int
    text: str


@dataclasses.dataclass
class ConcordanceSearchResult:
    local_matches: list[ConcordanceMatch]
    web_results: list[dict[str, str]]
    used_web_search: bool


async def search_concordance(
    db: AsyncSession, query: str, translation: str = "kjv", limit: int = 25
) -> list[ConcordanceMatch]:
    """plainto_tsquery('english', ...) applies English stemming — a query
    for "believing" matches a verse containing "believe" — but not phrase
    or boolean (AND/OR/NOT) search; a websearch_to_tsquery/phraseto_tsquery
    swap covers that later if it turns out to matter, not a schema
    change."""
    tsquery = func.plainto_tsquery("english", query)
    stmt = (
        select(BibleVerse.translation, BibleVerse.book, BibleVerse.chapter, BibleVerse.verse, BibleVerse.text)
        .where(BibleVerse.translation == translation)
        .where(BibleVerse.search_vector.op("@@")(tsquery))
        .order_by(func.ts_rank(BibleVerse.search_vector, tsquery).desc())
        .limit(limit)
    )
    rows = (await db.execute(stmt)).all()
    return [
        ConcordanceMatch(translation=r.translation, book=r.book, chapter=r.chapter, verse=r.verse, text=r.text)
        for r in rows
    ]


async def search_concordance_with_web_fallback(
    db: AsyncSession, query: str, translation: str = "kjv", limit: int = 25
) -> ConcordanceSearchResult:
    local_matches = await search_concordance(db, query, translation, limit)

    web_results: list[dict[str, str]] = []
    used_web_search = False
    if len(local_matches) < MIN_LOCAL_RESULTS_BEFORE_WEB_SUPPLEMENT:
        try:
            web_results = await search_context(f"{query} bible verse concordance")
            used_web_search = bool(web_results)
        except WebSearchError:
            logger.warning("Web search failed for concordance query %r — proceeding with local matches only", query, exc_info=True)
            web_results = []

    return ConcordanceSearchResult(local_matches=local_matches, web_results=web_results, used_web_search=used_web_search)
