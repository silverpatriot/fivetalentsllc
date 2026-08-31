"""app.services.web_search against the real Tavily API — TAVILY_API_KEY is
in .env, and a live call was confirmed working before this integration
was wired into context assembly (see the Phase 3 completion notes). Skips
itself if no key is configured rather than failing the whole suite.
"""
import pytest

from app.core.config import get_settings
from app.services import web_search

settings = get_settings()

pytestmark = pytest.mark.skipif(not settings.tavily_api_key, reason="TAVILY_API_KEY not configured")


async def test_search_context_returns_real_results():
    results = await web_search.search_context("Philippians 4:13 commentary", max_results=2)
    assert len(results) > 0
    for r in results:
        assert set(r.keys()) == {"title", "url", "content"}
        assert r["url"].startswith("http")


async def test_search_context_with_no_key_returns_empty_list(monkeypatch):
    monkeypatch.setattr(web_search.settings, "tavily_api_key", "")
    results = await web_search.search_context("anything")
    assert results == []
