"""fetch_passage_multi and GET /bible/* — same live-hit convention as
test_bible_api_bible.py: real calls against the actual BIBLE_API_KEY in
.env, no mocking. Skips itself if that key isn't configured.
"""
import uuid
from collections.abc import Iterator

import pytest
import sqlalchemy as sa
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from app.core import security
from app.core.config import get_settings
from app.main import app
from app.services import bible
from tests.conftest import make_clerk_jwt

client = TestClient(app)
settings = get_settings()

pytestmark = pytest.mark.skipif(not settings.bible_api_key, reason="BIBLE_API_KEY not configured")


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
            {"id": str(tenant_id), "slug": f"bible-compare-{tenant_id.hex[:8]}", "name": "Bible Compare Test Tenant", "org": clerk_org_id},
        )
    yield {"id": tenant_id, "clerk_org_id": clerk_org_id}
    with pg_engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM tenants WHERE id = :id"), {"id": str(tenant_id)})


@pytest.fixture
def auth_headers(rsa_keypair: tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey], active_tenant, monkeypatch):
    private_key, public_key = rsa_keypair
    monkeypatch.setattr(security, "_jwk_client", lambda: _FakeJWKClient(public_key))
    token = make_clerk_jwt(private_key, {"sub": "user_bible_compare_test", "o": {"id": active_tenant["clerk_org_id"], "rol": "admin"}})
    return {"Authorization": f"Bearer {token}"}


async def test_fetch_passage_multi_returns_real_distinct_text_per_translation():
    results = await bible.fetch_passage_multi("John 3:16", ["kjv", "web", "niv11"])
    assert set(results.keys()) == {"kjv", "web", "niv11"}
    assert all(p is not None for p in results.values())
    # KJV's "only begotten Son" vs. WEB/NIV's "one and only Son" wording —
    # a cheap sanity check that these are genuinely different translations'
    # text, not the same passage object returned three times.
    assert "only begotten Son" in results["kjv"].text
    assert "only begotten Son" not in results["web"].text
    assert "only begotten Son" not in results["niv11"].text


async def test_fetch_passage_multi_handles_a_translation_that_doesnt_resolve(monkeypatch):
    real_fetch_passage = bible.fetch_passage

    async def _fake_fetch(reference, translation=None):
        return None if translation == "kjv" else await real_fetch_passage(reference, translation)

    monkeypatch.setattr(bible, "fetch_passage", _fake_fetch)
    results = await bible.fetch_passage_multi("John 3:16", ["kjv", "web"])
    assert results["kjv"] is None
    assert results["web"] is not None


def test_get_bible_translations_returns_all_21(active_tenant, auth_headers):
    resp = client.get("/bible/translations", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["translations"]) == 21
    codes = {t["code"] for t in body["translations"]}
    assert codes == set(bible.API_BIBLE_IDS.keys())


def test_get_bible_compare_returns_all_requested_codes(active_tenant, auth_headers):
    resp = client.get(
        "/bible/compare", params={"reference": "John 3:16", "translations": "kjv,asv,web"}, headers=auth_headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["reference"] == "John 3:16"
    assert set(body["passages"].keys()) == {"kjv", "asv", "web"}
    assert all(body["passages"][code] is not None for code in ("kjv", "asv", "web"))


def test_get_bible_compare_rejects_blank_reference(active_tenant, auth_headers):
    resp = client.get("/bible/compare", params={"reference": "  ", "translations": "kjv"}, headers=auth_headers)
    assert resp.status_code == 400


def test_get_bible_compare_rejects_too_many_translations(active_tenant, auth_headers):
    too_many = ",".join(list(bible.API_BIBLE_IDS.keys())[:9])
    resp = client.get("/bible/compare", params={"reference": "John 3:16", "translations": too_many}, headers=auth_headers)
    assert resp.status_code == 400
