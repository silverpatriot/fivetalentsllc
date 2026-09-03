"""Phase 8 follow-up (2026-09-03): the "unverifiable" citation status.
Mocked transport (httpx.MockTransport, same established pattern
test_openrouter.py uses) rather than the real bible-api.com/api.bible —
the whole point here is deterministically forcing the exact service
failure that used to be uncaught, which "hit the real API" can't
reliably reproduce on demand.
"""
import httpx
import pytest

from app.services import bible


def _install_mock_transport(monkeypatch, handler):
    real_async_client = httpx.AsyncClient

    def _patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(bible.httpx, "AsyncClient", _patched)


async def test_fetch_from_bible_api_com_wraps_a_network_error(monkeypatch):
    """Confirms the actual gap this whole investigation started from:
    _fetch_from_bible_api_com used to have NO exception handling at
    all — any httpx error propagated raw and uncaught. The MockTransport
    handler raising directly is what a real connection failure looks
    like from httpx's own perspective — no need to fake the client."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated DNS/connection failure", request=request)

    _install_mock_transport(monkeypatch, handler)
    with pytest.raises(bible.BibleApiComError):
        await bible._fetch_from_bible_api_com("John 3:16", "kjv")


async def test_fetch_from_bible_api_com_wraps_an_unexpected_response_shape(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"nonsense": "no text field at all"})

    _install_mock_transport(monkeypatch, handler)
    with pytest.raises(bible.BibleApiComError):
        await bible._fetch_from_bible_api_com("John 3:16", "kjv")


async def test_fetch_from_bible_api_com_still_returns_none_for_a_clean_404(monkeypatch):
    """The fix must not change the ALREADY-correct behavior — a real
    404 is still an authoritative "no such passage", not an error."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    _install_mock_transport(monkeypatch, handler)
    result = await bible._fetch_from_bible_api_com("Frobnitz 99:99", "kjv")
    assert result is None


async def test_verify_citation_reports_unverifiable_when_source_unreachable(monkeypatch):
    """The new 4th status, exercised through verify_citation directly —
    NOT collapsed into invalid_reference (would misreport a service
    hiccup as a hallucinated verse) and NOT collapsed into verified/
    not_quoted (nothing was actually confirmed)."""

    async def _boom(reference, translation=None):
        raise bible.BibleApiComError("simulated bible-api.com outage")

    monkeypatch.setattr(bible, "fetch_passage", _boom)

    result = await bible.verify_citation("John 3:16", 'As John 3:16 says, "quote".')
    assert result["status"] == "unverifiable"
    assert result["quoted_text"] is None
    assert result["source_text"] is None
    assert "service issue" in result["detail"].lower()


async def test_verify_all_citations_does_not_raise_when_source_unreachable(monkeypatch):
    """verify_all_citations itself must not propagate the failure either
    — one unreachable-source citation among several must not take down
    the whole verification pass."""

    async def _boom(reference, translation=None):
        raise bible.ApiBibleError("simulated api.bible outage")

    monkeypatch.setattr(bible, "fetch_passage", _boom)

    flags = await bible.verify_all_citations('As John 3:16 says, "quote", and also Romans 8:28.')
    assert len(flags) == 2
    assert all(f["status"] == "unverifiable" for f in flags)
