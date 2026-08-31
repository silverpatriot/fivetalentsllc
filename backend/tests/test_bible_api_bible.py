"""app.services.bible's api.bible integration — real live calls, using the
BIBLE_API_KEY that's actually in .env. Skips itself if that key isn't
configured, same pattern as test_web_search.py.

The key that ended up working here came from a real back-and-forth: the
first value in .env didn't authenticate against api.scripture.api.bible
OR api.esv.org (confirmed via the raw AWS API Gateway 403
AccessDeniedException, both candidate hosts resolving to the same
backend); the "regenerated" value that followed was still the same key
with a leading character missing. This file exists specifically so a
future key rotation that breaks silently gets caught by the suite rather
than by a pastor seeing an empty scripture section.
"""
import pytest

from app.core.config import get_settings
from app.services import bible

settings = get_settings()

pytestmark = pytest.mark.skipif(not settings.bible_api_key, reason="BIBLE_API_KEY not configured")


async def test_fetch_passage_prefers_api_bible_and_returns_real_kjv_text():
    passage = await bible.fetch_passage("John 3:16", "kjv")
    assert passage is not None
    assert passage.source == "api.bible"
    assert "only begotten Son" in passage.text


async def test_fetch_passage_api_bible_handles_verse_ranges():
    passage = await bible.fetch_passage("Romans 8:28-30", "kjv")
    assert passage is not None
    assert passage.source == "api.bible"
    assert "work together for good" in passage.text
    assert "predestinate" in passage.text


async def test_private_fetch_from_api_bible_returns_none_on_clean_not_found():
    """The private per-source function's own raw behavior — a clean
    api.bible "no such passage" is None, not an exception. Cross-checking
    that against bible-api.com is fetch_passage's job (see the tests
    below), not this function's."""
    result = await bible._fetch_from_api_bible("Frobnitz 99:99", bible._API_BIBLE_IDS["kjv"])
    assert result is None


async def test_fetch_passage_falls_back_to_bible_api_com_on_api_bible_failure(monkeypatch):
    async def _boom(reference, bible_id):
        raise bible.ApiBibleError("simulated api.bible outage")

    monkeypatch.setattr(bible, "_fetch_from_api_bible", _boom)
    passage = await bible.fetch_passage("John 3:16", "kjv")
    assert passage is not None
    assert passage.source == "bible-api.com"
    assert "only begotten Son" in passage.text


async def test_fetch_passage_uses_bible_api_com_for_a_translation_api_bible_doesnt_map():
    # "bbe" (Bible in Basic English) isn't in _API_BIBLE_IDS — confirmed
    # live that bible-api.com serves it, so this should go straight there
    # rather than to api.bible.
    assert "bbe" not in bible._API_BIBLE_IDS
    passage = await bible.fetch_passage("John 3:16", "bbe")
    assert passage is not None
    assert passage.source == "bible-api.com"
    assert passage.translation == "bbe"


async def test_fetch_passage_cross_checks_a_clean_api_bible_not_found(monkeypatch):
    """The behavior this file exists to protect: if api.bible's /search
    cleanly says a real reference doesn't exist (simulated here — a
    parser edge case, not this specific reference actually being missing
    from api.bible), fetch_passage must not trust that alone. It has to
    cross-check bible-api.com and use ITS answer rather than reporting a
    real verse as unresolvable."""

    async def _clean_not_found(reference, bible_id):
        return None

    monkeypatch.setattr(bible, "_fetch_from_api_bible", _clean_not_found)
    passage = await bible.fetch_passage("John 3:16", "kjv")
    assert passage is not None
    assert passage.source == "bible-api.com"
    assert "only begotten Son" in passage.text


async def test_fetch_passage_returns_none_only_when_both_sources_agree_not_found():
    """A genuinely fake reference — real live calls to both sources,
    neither should find it, and only then does fetch_passage report None."""
    passage = await bible.fetch_passage("Frobnitz 99:99", "kjv")
    assert passage is None
