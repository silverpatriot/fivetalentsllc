"""Phase 4 Task 1: the shared RAG pipeline — upload endpoint, ingestion,
and retrieval — against real Postgres/pgvector and real OpenRouter
embeddings (see tests/test_embeddings.py for why that's safe/cheap to
depend on). Celery's embedding task is run in-process via the
synchronous_embedding fixture (tests/conftest.py) rather than requiring a
live worker.
"""
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
from app.services.embeddings import embed_text
from app.services.ingestion import ingest_text
from app.services.retrieval import similarity_search
from tests.conftest import make_clerk_jwt, set_tenant

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
            {"id": str(tenant_id), "slug": f"doc-{tenant_id.hex[:8]}", "name": "Doc Test Tenant", "org": clerk_org_id},
        )
    yield {"id": tenant_id, "clerk_org_id": clerk_org_id}
    with pg_engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM tenants WHERE id = :id"), {"id": str(tenant_id)})


@pytest.fixture
def auth_headers(rsa_keypair: tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey], active_tenant, monkeypatch):
    private_key, public_key = rsa_keypair
    monkeypatch.setattr(security, "_jwk_client", lambda: _FakeJWKClient(public_key))
    token = make_clerk_jwt(private_key, {"sub": "user_doc_test", "o": {"id": active_tenant["clerk_org_id"], "rol": "admin"}})
    return {"Authorization": f"Bearer {token}"}


# ============================================================================
# Upload endpoint
# ============================================================================


def test_upload_txt_document_extracts_chunks_and_embeds_it(
    pg_engine: Engine, active_tenant: dict, auth_headers: dict, synchronous_embedding
):
    tenant_id = active_tenant["id"]
    content = b"Grace and truth came by Jesus Christ. " * 50  # long enough to force >1 chunk with a small size

    resp = client.post(
        "/documents",
        files={"file": ("my_notes.txt", io.BytesIO(content), "text/plain")},
        data={"corpus_type": "theology", "title": "My Notes"},
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    # The route's own request-scoped session has already committed by the
    # time client.post() returns (unlike the streaming generation
    # endpoint, an ordinary route's Depends(get_db) session is torn down
    # — committed — as part of producing this response) — safe to run the
    # captured embedding task now.
    synchronous_embedding()
    body = resp.json()
    assert body["status"] == "processing"  # snapshotted in the response before the task above ran
    assert body["corpus_type"] == "theology"
    assert body["source"] == "uploaded"
    assert body["original_filename"] == "my_notes.txt"

    with pg_engine.begin() as conn:
        set_tenant(conn, tenant_id)
        status = conn.execute(
            sa.text("SELECT status FROM documents WHERE id = :id"), {"id": body["id"]}
        ).scalar_one()
        assert status == "ready"
        chunks = conn.execute(
            sa.text("SELECT chunk_index, content, embedding FROM document_chunks WHERE document_id = :id ORDER BY chunk_index"),
            {"id": body["id"]},
        ).fetchall()
    assert len(chunks) >= 1
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))
    assert all("Grace and truth" in c.content for c in chunks)
    # pgvector returns the embedding as a string like "[0.1,0.2,...]" via
    # raw psycopg2 — just confirm it's there and non-trivial, not a
    # placeholder/zero vector.
    assert all(c.embedding is not None and len(c.embedding) > 100 for c in chunks)


def test_upload_rejects_unknown_corpus_type(active_tenant, auth_headers):
    resp = client.post(
        "/documents",
        files={"file": ("x.txt", io.BytesIO(b"hello"), "text/plain")},
        data={"corpus_type": "not-a-real-corpus"},
        headers=auth_headers,
    )
    assert resp.status_code == 400


def test_upload_rejects_file_over_the_size_limit(active_tenant, auth_headers, monkeypatch):
    import app.api.documents as documents_module

    monkeypatch.setattr(documents_module.settings, "max_upload_size_bytes", 10)
    resp = client.post(
        "/documents",
        files={"file": ("big.txt", io.BytesIO(b"this is more than ten bytes"), "text/plain")},
        data={"corpus_type": "theology"},
        headers=auth_headers,
    )
    assert resp.status_code == 413


def test_upload_unsupported_file_type_is_rejected(active_tenant, auth_headers):
    resp = client.post(
        "/documents",
        files={"file": ("slides.pptx", io.BytesIO(b"whatever"), "application/vnd.ms-powerpoint")},
        data={"corpus_type": "theology"},
        headers=auth_headers,
    )
    assert resp.status_code == 400


def test_list_documents_filters_by_corpus_type(pg_engine: Engine, active_tenant: dict, auth_headers: dict, synchronous_embedding):
    for i, corpus in enumerate(["theology", "cadence"]):
        client.post(
            "/documents",
            files={"file": (f"doc{i}.txt", io.BytesIO(b"some real content here " * 20), "text/plain")},
            data={"corpus_type": corpus},
            headers=auth_headers,
        )
    synchronous_embedding()
    resp = client.get("/documents", params={"corpus_type": "theology"}, headers=auth_headers)
    assert resp.status_code == 200
    docs = resp.json()
    assert len(docs) == 1
    assert docs[0]["corpus_type"] == "theology"


def test_delete_document_removes_it_and_its_chunks(pg_engine: Engine, active_tenant: dict, auth_headers: dict, synchronous_embedding):
    resp = client.post(
        "/documents",
        files={"file": ("d.txt", io.BytesIO(b"some real content here " * 20), "text/plain")},
        data={"corpus_type": "theology"},
        headers=auth_headers,
    )
    document_id = resp.json()["id"]
    synchronous_embedding()

    with pg_engine.begin() as conn:
        set_tenant(conn, active_tenant["id"])
        before = conn.execute(
            sa.text("SELECT count(*) FROM document_chunks WHERE document_id = :id"), {"id": document_id}
        ).scalar_one()
    assert before > 0  # otherwise this test would trivially pass no matter what delete does

    del_resp = client.delete(f"/documents/{document_id}", headers=auth_headers)
    assert del_resp.status_code == 204

    with pg_engine.begin() as conn:
        set_tenant(conn, active_tenant["id"])
        remaining = conn.execute(
            sa.text("SELECT count(*) FROM document_chunks WHERE document_id = :id"), {"id": document_id}
        ).scalar_one()
    assert remaining == 0  # ON DELETE CASCADE


# ============================================================================
# Retrieval: cross-tenant isolation, cold start, dedupe, exclusion
# ============================================================================


@pytest.fixture
def two_tenants_with_docs(pg_engine: Engine) -> Iterator[tuple[uuid.UUID, uuid.UUID]]:
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    with pg_engine.begin() as conn:
        conn.execute(
            sa.text("INSERT INTO tenants (id, slug, name, clerk_org_id) VALUES (:id, :slug, :name, :org)"),
            [
                {"id": str(tenant_a), "slug": f"iso-a-{tenant_a.hex[:8]}", "name": "A", "org": f"org_a_{tenant_a.hex[:8]}"},
                {"id": str(tenant_b), "slug": f"iso-b-{tenant_b.hex[:8]}", "name": "B", "org": f"org_b_{tenant_b.hex[:8]}"},
            ],
        )
    yield tenant_a, tenant_b
    with pg_engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM tenants WHERE id = :id_a OR id = :id_b"), {"id_a": str(tenant_a), "id_b": str(tenant_b)})


async def test_similarity_search_never_returns_another_tenants_chunk_even_at_zero_distance(
    two_tenants_with_docs, synchronous_embedding
):
    """Adversarial, not just by construction: tenant B's chunk is
    embedded from the EXACT SAME text as the query, so its cosine
    distance is ~0 — the best possible match, and one tenant A has
    nothing to compete with. If RLS were not actually filtering this
    query, tenant A's search would return tenant B's chunk. It must
    return nothing instead."""
    tenant_a, tenant_b = two_tenants_with_docs
    shared_text = "This extremely specific phrase about the transfiguration on the mountain only appears once."

    async with tenant_session(tenant_b) as db:
        doc = await ingest_text(
            db, tenant_b, corpus_type=CorpusType.THEOLOGY.value, source=DocumentSource.UPLOADED.value,
            title="Tenant B's doc", text=shared_text, original_filename="b.txt",
        )
        assert doc is not None
    synchronous_embedding()

    query_vector = await embed_text(shared_text)
    async with tenant_session(tenant_a) as db:
        results = await similarity_search(db, CorpusType.THEOLOGY.value, query_vector, limit=5)
    assert results == []


async def test_cadence_corpus_specifically_enforces_tenant_isolation(two_tenants_with_docs, synchronous_embedding):
    """Task 2's own explicit isolation requirement for the cadence corpus
    — same mechanism as the adversarial theology-corpus test above (RLS
    on document_chunks doesn't discriminate by corpus_type), proven again
    here directly against corpus_type='cadence' rather than left to be
    inferred from a different corpus's test."""
    tenant_a, tenant_b = two_tenants_with_docs
    shared_text = "A sermon on the raising of Lazarus and the power over death."

    async with tenant_session(tenant_b) as db:
        doc = await ingest_text(
            db, tenant_b, corpus_type=CorpusType.CADENCE.value, source=DocumentSource.GENERATED.value,
            title="Tenant B's sermon", text=shared_text,
        )
        assert doc is not None
    synchronous_embedding()

    query_vector = await embed_text(shared_text)
    async with tenant_session(tenant_a) as db:
        results = await similarity_search(db, CorpusType.CADENCE.value, query_vector, limit=5)
    assert results == []


async def test_similarity_search_cold_start_returns_empty_not_error(two_tenants_with_docs):
    tenant_a, _ = two_tenants_with_docs
    query_vector = await embed_text("anything at all")
    async with tenant_session(tenant_a) as db:
        results = await similarity_search(db, CorpusType.CADENCE.value, query_vector, limit=3)
    assert results == []


async def test_similarity_search_dedupe_by_document_returns_best_chunk_per_document(
    two_tenants_with_docs, synchronous_embedding
):
    from app.services.chunking import chunk_text

    tenant_a, _ = two_tenants_with_docs
    source_text = "On forgiveness and grace. " * 200
    chunks = chunk_text(source_text)
    assert len(chunks) > 1  # the whole point of this test

    async with tenant_session(tenant_a) as db:
        # One document, chunked into multiple pieces about the same
        # topic — dedupe_by_document must collapse this to ONE result,
        # its single closest chunk, not several.
        doc = await ingest_text(
            db, tenant_a, corpus_type=CorpusType.CADENCE.value, source=DocumentSource.GENERATED.value,
            title="Repeated Topic Sermon", text=source_text,
        )
        assert doc is not None
    synchronous_embedding()

    query_vector = await embed_text("forgiveness and grace")
    async with tenant_session(tenant_a) as db:
        deduped = await similarity_search(db, CorpusType.CADENCE.value, query_vector, limit=5, dedupe_by_document=True)
        raw = await similarity_search(db, CorpusType.CADENCE.value, query_vector, limit=5, dedupe_by_document=False)

    assert len(deduped) == 1
    assert len(raw) == len(chunks)


async def test_similarity_search_excludes_given_document_ids(two_tenants_with_docs, synchronous_embedding):
    tenant_a, _ = two_tenants_with_docs
    text = "A sermon on the parable of the sower and good soil."
    async with tenant_session(tenant_a) as db:
        doc = await ingest_text(
            db, tenant_a, corpus_type=CorpusType.CADENCE.value, source=DocumentSource.GENERATED.value,
            title="Sower Sermon", text=text,
        )
        assert doc is not None
    synchronous_embedding()

    query_vector = await embed_text(text)
    async with tenant_session(tenant_a) as db:
        with_exclusion = await similarity_search(
            db, CorpusType.CADENCE.value, query_vector, limit=5, exclude_document_ids=[str(doc.id)]
        )
        without_exclusion = await similarity_search(db, CorpusType.CADENCE.value, query_vector, limit=5)

    assert with_exclusion == []
    assert len(without_exclusion) == 1
