"""Phase 4 Task 3: POST /study/query — real Postgres/pgvector, real
OpenRouter embeddings, real bracket-citation prompt construction. The
LLM chat-completion call itself is mocked (matching
tests/test_generation_usage.py's convention: no live LLM spend needed to
prove the plumbing — retrieval, citation construction, and the own-
documents/web-search distinction are what's under test, not OpenRouter's
actual answer quality). Tavily is exercised for real when it's expected
to fire, same as tests/test_web_search.py.
"""
import asyncio
import io
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
from app.models.document import CorpusType, DocumentSource
from app.services.ingestion import ingest_text
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
            {"id": str(tenant_id), "slug": f"study-{tenant_id.hex[:8]}", "name": "Study Test Tenant", "org": clerk_org_id},
        )
    yield {"id": tenant_id, "clerk_org_id": clerk_org_id}
    with pg_engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM tenants WHERE id = :id"), {"id": str(tenant_id)})


@pytest.fixture
def auth_headers(rsa_keypair: tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey], active_tenant, monkeypatch):
    private_key, public_key = rsa_keypair
    monkeypatch.setattr(security, "_jwk_client", lambda: _FakeJWKClient(public_key))
    token = make_clerk_jwt(private_key, {"sub": "user_study_test", "o": {"id": active_tenant["clerk_org_id"], "rol": "admin"}})
    return {"Authorization": f"Bearer {token}"}


async def _fake_chat_completion(model: str, messages: list[dict]) -> tuple[str, str]:
    # Echo the user message back so tests can assert on exactly what
    # context/citations the real retrieval+prompt-building step produced,
    # without depending on real LLM output.
    return messages[1]["content"], '{"fake":"study-response"}'


def test_query_with_own_documents_only_when_corpus_covers_it(
    pg_engine: Engine, active_tenant: dict, auth_headers: dict, synchronous_embedding, monkeypatch
):
    async def _web_boom(*a, **k):
        raise AssertionError("Tavily should not have been called")

    monkeypatch.setattr("app.services.study.chat_completion", _fake_chat_completion)
    # 4 documents >= MIN_OWN_RESULTS_BEFORE_WEB_SUPPLEMENT(3) — must NOT trigger Tavily.
    monkeypatch.setattr("app.services.study.search_context", _web_boom)

    for i in range(4):
        resp = client.post(
            "/documents",
            files={"file": (f"doc{i}.txt", io.BytesIO(f"Notes about grace and justification, entry {i}. ".encode() * 10), "text/plain")},
            data={"corpus_type": "theology", "title": f"Notes {i}"},
            headers=auth_headers,
        )
        assert resp.status_code == 201, resp.text
    synchronous_embedding()

    resp = client.post("/study/query", json={"question": "What is grace?"}, headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["used_own_documents"] is True
    assert body["used_web_search"] is False
    assert len(body["citations"]) == 4
    assert all(c["source_type"] == "document" for c in body["citations"])
    assert "YOUR DOCUMENTS" in body["answer"]  # the echoed prompt — proves real grounding content was built
    assert "grace" in body["answer"].lower()


def test_query_supplements_with_web_search_when_own_corpus_is_thin(
    pg_engine: Engine, active_tenant: dict, auth_headers: dict, synchronous_embedding, monkeypatch
):
    monkeypatch.setattr("app.services.study.chat_completion", _fake_chat_completion)

    resp = client.post("/study/query", json={"question": "What did Calvin teach about predestination?"}, headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["used_own_documents"] is False
    # Real Tavily call (TAVILY_API_KEY is configured in this environment
    # — see tests/test_web_search.py) — if it comes back empty this
    # assertion legitimately fails and that's worth knowing, not masking.
    assert body["used_web_search"] is True
    assert any(c["source_type"] == "web" for c in body["citations"])
    assert all(c["url"] for c in body["citations"] if c["source_type"] == "web")


def test_query_with_neither_source_available_does_not_call_the_llm_or_fabricate(
    active_tenant: dict, auth_headers: dict, monkeypatch
):
    async def _chat_boom(*a, **k):
        raise AssertionError("chat_completion must not be called when there is nothing to ground on")

    async def _no_web_results(*a, **k):
        return []

    monkeypatch.setattr("app.services.study.chat_completion", _chat_boom)
    monkeypatch.setattr("app.services.study.search_context", _no_web_results)

    resp = client.post(
        "/study/query", json={"question": "asdkjfhalksdjfhalksjdfh nonsense query"}, headers=auth_headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["used_own_documents"] is False
    assert body["used_web_search"] is False
    assert body["citations"] == []
    assert "couldn't find" in body["answer"].lower()


def test_query_rejects_blank_question(active_tenant, auth_headers):
    resp = client.post("/study/query", json={"question": "   "}, headers=auth_headers)
    assert resp.status_code == 400


def test_study_corpus_query_never_returns_another_tenants_document(
    pg_engine: Engine, active_tenant: dict, auth_headers: dict, synchronous_embedding, monkeypatch
):
    """Tenant isolation specifically through the /study/query endpoint,
    not just the underlying similarity_search function directly."""
    monkeypatch.setattr("app.services.study.chat_completion", _fake_chat_completion)

    other_tenant_id = uuid.uuid4()
    with pg_engine.begin() as conn:
        conn.execute(
            sa.text("INSERT INTO tenants (id, slug, name, clerk_org_id) VALUES (:id, :slug, :name, :org)"),
            {
                "id": str(other_tenant_id),
                "slug": f"other-{other_tenant_id.hex[:8]}",
                "name": "Other Tenant",
                "org": f"org_{other_tenant_id.hex[:16]}",
            },
        )
    try:
        async def _seed_other():
            async with tenant_session(other_tenant_id) as db:
                await ingest_text(
                    db, other_tenant_id, corpus_type=CorpusType.THEOLOGY.value, source=DocumentSource.UPLOADED.value,
                    title="Other Tenant's Secret Notes", text="A very specific secret teaching about the end times. " * 20,
                )

        asyncio.run(_seed_other())
        synchronous_embedding()

        resp = client.post(
            "/study/query", json={"question": "A very specific secret teaching about the end times"}, headers=auth_headers
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["used_own_documents"] is False
        assert not any(c["source_type"] == "document" for c in body["citations"])
    finally:
        with pg_engine.begin() as conn:
            conn.execute(sa.text("DELETE FROM tenants WHERE id = :id"), {"id": str(other_tenant_id)})
