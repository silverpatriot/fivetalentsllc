"""Live web search (Tavily) folded into context assembly: recent
commentary/theological discussion/background relevant to the sermon's
passage or topic, alongside scripture text and cadence examples.

Confirmed live against api.tavily.com before wiring this in — a real
search for "Philippians 4:13 commentary" returned real commentary results
(Enduring Word, Precept Austin, etc.) with title/url/content.

Deliberately best-effort: no key configured, or a failed/rate-limited
call, degrades to "no web context" rather than blocking generation —
scripture text and cadence examples are the load-bearing parts of the
prompt; this is supplementary.
"""
import logging

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


class WebSearchError(RuntimeError):
    pass


async def search_context(query: str, max_results: int | None = None) -> list[dict[str, str]]:
    """Returns a list of {title, url, content} dicts, or [] if no API key
    is configured. Raises WebSearchError on a real API failure — callers
    (context_assembly.fetch_web_context) decide whether to swallow that."""
    if not settings.tavily_api_key:
        return []
    max_results = max_results or settings.web_search_max_results
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            "https://api.tavily.com/search",
            json={
                "api_key": settings.tavily_api_key,
                "query": query,
                "max_results": max_results,
                "search_depth": "basic",
            },
        )
    if resp.status_code >= 400:
        raise WebSearchError(f"Tavily {resp.status_code}: {resp.text[:500]}")
    data = resp.json()
    return [
        {
            "title": r.get("title", "") or "",
            "url": r.get("url", "") or "",
            "content": r.get("content", "") or "",
        }
        for r in data.get("results", [])
    ]
