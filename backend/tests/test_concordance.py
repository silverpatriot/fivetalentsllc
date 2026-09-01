"""Concordance search — real Postgres tsvector/GIN, no embeddings (much
cheaper fixture than test_reference_corpus.py's, since search_concordance
has no embedding call at all — see app/services/concordance.py). Seeds
its own BibleVerse rows directly under a unique, unmistakably-test-only
translation code, same collision-avoidance lesson test_reference_corpus.py
documents (a marker-based value, not real data, so this never depends on
or collides with whatever scripts/ingest_bible_verses.py has actually
loaded in this environment).
"""
import uuid
from collections.abc import Iterator

import pytest
import sqlalchemy as sa
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from app.core import security
from app.db.session import tenant_session
from app.main import app
from app.models.bible_verse import BibleVerse
from app.services.concordance import search_concordance, search_concordance_with_web_fallback
from tests.conftest import make_clerk_jwt

client = TestClient(app)


class _FakeSigningKey:
    def __init__(self, key):
        self.key = key


class _FakeJWKClient:
    def __init__(self, public_key):
        self._public_key = public_key

    def get_signing_key_from_jwt(self, token):
        return _FakeSigningKey(self._public_key)


@pytest.fixture
def active_tenant(pg_engine: Engine) -> Iterator[dict]:
    tenant_id = uuid.uuid4()
    clerk_org_id = f"org_{tenant_id.hex[:16]}"
    with pg_engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO tenants (id, slug, name, clerk_org_id, subscription_status) "
                "VALUES (:id, :slug, :name, :org, 'active')"
            ),
            {"id": str(tenant_id), "slug": f"concordance-{tenant_id.hex[:8]}", "name": "Concordance Test Tenant", "org": clerk_org_id},
        )
    yield {"id": tenant_id, "clerk_org_id": clerk_org_id}
    with pg_engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM tenants WHERE id = :id"), {"id": str(tenant_id)})


@pytest.fixture
def auth_headers(rsa_keypair: tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey], active_tenant, monkeypatch):
    private_key, public_key = rsa_keypair
    monkeypatch.setattr(security, "_jwk_client", lambda: _FakeJWKClient(public_key))
    token = make_clerk_jwt(private_key, {"sub": "user_concordance_test", "o": {"id": active_tenant["clerk_org_id"], "rol": "admin"}})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def seeded_verses(pg_engine: Engine) -> Iterator[dict]:
    marker = uuid.uuid4().hex[:8]
    translation = f"test-{marker}"

    async def _seed():
        async with tenant_session(uuid.uuid4()) as db:
            rows = [
                BibleVerse(translation=translation, book="JHN", chapter=3, verse=16,
                           text="For God so loved the world that he gave grace abundantly through his Son."),
                BibleVerse(translation=translation, book="EPH", chapter=2, verse=8,
                           text="For by grace are ye saved through faith, and that not of yourselves."),
                BibleVerse(translation=translation, book="ROM", chapter=5, verse=1,
                           text="Being justified by faith, we have peace with God, believing in his promises."),
                BibleVerse(translation=translation, book="GEN", chapter=1, verse=1,
                           text="In the beginning God created the heaven and the earth."),
            ]
            db.add_all(rows)
            await db.flush()
            return [str(r.id) for r in rows]

    import asyncio
    ids = asyncio.run(_seed())
    yield {"translation": translation, "ids": ids}
    with pg_engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM bible_verses WHERE translation = :t"), {"t": translation})


async def test_search_concordance_finds_exact_word_match(seeded_verses):
    async with tenant_session(uuid.uuid4()) as db:
        results = await search_concordance(db, "grace", translation=seeded_verses["translation"])
    refs = {(r.book, r.chapter, r.verse) for r in results}
    assert ("JHN", 3, 16) in refs
    assert ("EPH", 2, 8) in refs
    assert ("GEN", 1, 1) not in refs  # doesn't contain "grace" at all


async def test_search_concordance_applies_english_stemming(seeded_verses):
    """The concrete behavior distinguishing a concordance from a naive
    LIKE '%word%' search: a query for "believing" should also match a
    verse containing "believe" via to_tsvector('english', ...)'s
    stemming, not just an exact substring."""
    async with tenant_session(uuid.uuid4()) as db:
        results = await search_concordance(db, "believe", translation=seeded_verses["translation"])
    refs = {(r.book, r.chapter, r.verse) for r in results}
    assert ("ROM", 5, 1) in refs  # contains "believing", stemmed match for "believe"


async def test_search_concordance_scoped_to_translation(seeded_verses):
    """A query against a different translation code entirely must never
    surface this fixture's rows — translation is a hard filter, not a
    ranking signal."""
    async with tenant_session(uuid.uuid4()) as db:
        other_translation_results = await search_concordance(db, "grace", translation="kjv")
    other_refs = {(r.book, r.chapter, r.verse) for r in other_translation_results}
    assert ("JHN", 3, 16) not in other_refs
    assert ("EPH", 2, 8) not in other_refs

    async with tenant_session(uuid.uuid4()) as db:
        own_translation_results = await search_concordance(db, "grace", translation=seeded_verses["translation"])
    assert len(own_translation_results) == 2  # JHN 3:16 and EPH 2:8, confirmed above


async def test_web_fallback_fires_when_local_matches_are_thin(seeded_verses, monkeypatch):
    async def _fake_web_results(*a, **k):
        return [{"title": "Some site", "url": "https://example.com", "content": "About faithfulness"}]

    monkeypatch.setattr("app.services.concordance.search_context", _fake_web_results)
    async with tenant_session(uuid.uuid4()) as db:
        # "xylophone" shares no stem with any seeded verse (confirmed —
        # unlike an earlier draft of this test that used "faithfulness",
        # which turned out to legitimately stem-match "faith" in two of
        # the seeded verses and wasn't actually testing the thin-results
        # path at all) — genuinely 0 local matches, well below the threshold.
        result = await search_concordance_with_web_fallback(db, "xylophone", translation=seeded_verses["translation"])
    assert len(result.local_matches) == 0
    assert result.used_web_search is True
    assert len(result.web_results) == 1


async def test_web_fallback_does_not_fire_when_local_matches_are_sufficient(pg_engine: Engine, monkeypatch):
    """Proves "local first" — Tavily must NOT be called when the local
    corpus already covers the query well enough."""

    async def _web_boom(*a, **k):
        raise AssertionError("Tavily should not have been called")

    monkeypatch.setattr("app.services.concordance.search_context", _web_boom)

    marker = uuid.uuid4().hex[:8]
    translation = f"test-{marker}"

    # This test is itself `async def` (pytest-asyncio already provides a
    # running loop here) — await the seed directly rather than
    # asyncio.run()'ing a nested coroutine, which fails outright from
    # inside an already-running loop. seeded_verses' fixture (a plain
    # `def`, no loop running yet at fixture-setup time) is the place
    # asyncio.run() is the right tool — see that fixture above.
    async with tenant_session(uuid.uuid4()) as db:
        db.add_all(
            [
                BibleVerse(translation=translation, book="X", chapter=1, verse=i, text=f"Grace entry number {i}.")
                for i in range(1, 5)
            ]
        )
        await db.flush()

    try:
        async with tenant_session(uuid.uuid4()) as db:
            result = await search_concordance_with_web_fallback(db, "grace", translation=translation)
        assert len(result.local_matches) == 4
        assert result.used_web_search is False
        assert result.web_results == []
    finally:
        with pg_engine.begin() as conn:
            conn.execute(sa.text("DELETE FROM bible_verses WHERE translation = :t"), {"t": translation})


def test_get_concordance_search_returns_expected_shape(seeded_verses, active_tenant, auth_headers, monkeypatch):
    async def _no_web(*a, **k):
        return []

    monkeypatch.setattr("app.services.concordance.search_context", _no_web)
    resp = client.get(
        "/concordance/search", params={"q": "grace", "translation": seeded_verses["translation"]}, headers=auth_headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["query"] == "grace"
    assert len(body["local_matches"]) == 2
    assert all(m["reference"] and m["text"] and m["translation"] == seeded_verses["translation"] for m in body["local_matches"])
    assert body["web_results"] == []
    assert body["used_web_search"] is False


def test_get_concordance_search_rejects_blank_query(active_tenant, auth_headers):
    resp = client.get("/concordance/search", params={"q": "  "}, headers=auth_headers)
    assert resp.status_code == 400
