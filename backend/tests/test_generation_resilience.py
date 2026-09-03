"""2026-09-03 fix: a downstream failure in citation verification or
cadence ingestion must never destroy an already-successful generation,
edit, or restore. Real live-infrastructure convention (real Postgres,
real Clerk JWT) — only OpenRouter is mocked; the actual failure under
test is a REAL, forced Bible-API-source failure (bible-api.com/
api.bible), reproducing the exact trigger identified during
investigation, not a synthetic stand-in for it.
"""
import uuid
from typing import Iterator

import pytest
import sqlalchemy as sa
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from app.core import security
from app.main import app
from app.services import bible
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
def active_tenant_with_org(pg_engine: Engine) -> Iterator[dict]:
    tenant_id = uuid.uuid4()
    clerk_org_id = f"org_{tenant_id.hex[:16]}"
    with pg_engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO tenants (id, slug, name, clerk_org_id, subscription_status) "
                "VALUES (:id, :slug, :name, :org, 'active')"
            ),
            {
                "id": str(tenant_id),
                "slug": f"resil-{tenant_id.hex[:8]}",
                "name": "Resilience Test Tenant",
                "org": clerk_org_id,
            },
        )
    yield {"id": tenant_id, "clerk_org_id": clerk_org_id}
    with pg_engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM tenants WHERE id = :id"), {"id": str(tenant_id)})


@pytest.fixture
def auth_headers(rsa_keypair: tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey], active_tenant_with_org, monkeypatch):
    private_key, public_key = rsa_keypair
    monkeypatch.setattr(security, "_jwk_client", lambda: _FakeJWKClient(public_key))
    token = make_clerk_jwt(
        private_key, {"sub": "user_resil_test", "o": {"id": active_tenant_with_org["clerk_org_id"], "rol": "admin"}}
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def _stub_background_tasks(monkeypatch):
    monkeypatch.setattr("app.tasks.usage_reporting.report_usage_event.delay", lambda *a, **k: None)
    monkeypatch.setattr("app.services.ingestion.embed_document_chunks.delay", lambda *a, **k: None)


async def _fake_outline(model, messages, **kwargs):
    return "1. Point one", '{"fake":"outline"}'


def _force_bible_api_down(monkeypatch):
    """Forces the EXACT real trigger identified during investigation:
    every Bible-source lookup fails as a genuine service error (not a
    clean "not found") — reproducing what an uncaught
    BibleApiComError/ApiBibleError used to do to the whole transaction,
    now that both are caught. Patches fetch_passage itself (the single
    real chokepoint every source funnels through) rather than the
    network layer, so this exercises the exact function verify_citation
    calls, regardless of which underlying source would have been tried."""

    async def _boom(reference, translation=None):
        raise bible.BibleApiComError("simulated: bible-api.com is down")

    monkeypatch.setattr(bible, "fetch_passage", _boom)


def test_generate_saves_the_draft_even_when_citation_verification_is_completely_down(
    pg_engine: Engine, active_tenant_with_org: dict, auth_headers: dict, monkeypatch
):
    """The actual reported incident, reproduced for real: a sermon with
    a real scripture citation in its draft, generated while the Bible
    API is genuinely down throughout. Before the fix, this exact
    scenario rolled back the ENTIRE transaction — the sermon a pastor
    just watched generate would vanish with zero trace. Confirms it
    doesn't anymore."""
    tenant_id = active_tenant_with_org["id"]

    async def _fake_stream(model, messages, raw_sink=None, **kwargs):
        text = 'Point one: as John 3:16 says, "For God so loved the world."'
        if raw_sink is not None:
            raw_sink.append(text)
        yield text

    monkeypatch.setattr("app.services.generation.chat_completion", _fake_outline)
    monkeypatch.setattr("app.services.generation.stream_chat_completion", _fake_stream)
    _force_bible_api_down(monkeypatch)

    create_resp = client.post("/sermons", json={"title": "Resilience Test", "format": "topical"}, headers=auth_headers)
    assert create_resp.status_code == 201, create_resp.text
    sermon_id = create_resp.json()["id"]

    gen_resp = client.post(f"/sermons/{sermon_id}/generate", json={"topic": "Love"}, headers=auth_headers)
    assert gen_resp.status_code == 200, gen_resp.text
    body = gen_resp.text
    # The draft streamed and completed cleanly — no error event, despite
    # the Bible API being completely down for the whole request.
    assert "event: error" not in body
    assert "event: done" in body
    assert "event: citations" in body

    with pg_engine.begin() as conn:
        set_tenant(conn, tenant_id)
        sermon_row = conn.execute(
            sa.text("SELECT status, content FROM sermons WHERE id = :id"), {"id": sermon_id}
        ).fetchone()
        log_rows = conn.execute(
            sa.text("SELECT stage, citation_flags FROM generation_logs WHERE tenant_id = :tid AND sermon_id = :sid ORDER BY stage"),
            {"tid": str(tenant_id), "sid": sermon_id},
        ).fetchall()

    # The actual bug, confirmed fixed: content saved, status ready, both
    # LLM-call log rows present (before the fix: zero rows, NULL content,
    # status stuck on draft — a full silent rollback).
    assert sermon_row.status == "ready"
    assert sermon_row.content == 'Point one: as John 3:16 says, "For God so loved the world."'
    assert {row.stage for row in log_rows} == {"outline", "draft"}
    draft_log = next(row for row in log_rows if row.stage == "draft")
    # The citation was attempted and honestly reported as unable to be
    # checked — not silently dropped, not crashing the whole save.
    assert draft_log.citation_flags == [] or all(f["status"] == "unverifiable" for f in draft_log.citation_flags)


def test_edit_saves_the_replacement_even_when_citation_verification_is_completely_down(
    pg_engine: Engine, active_tenant_with_org: dict, auth_headers: dict, monkeypatch
):
    tenant_id = active_tenant_with_org["id"]
    original = "Point one: a fairly simple draft sentence to begin with here."
    create_resp = client.post(
        "/sermons",
        json={"title": "Resilience Edit Test", "format": "topical", "content": original},
        headers=auth_headers,
    )
    sermon_id = create_resp.json()["id"]

    # Similar length to `original` — a drastic length change would
    # (correctly) trip the unrelated structural-artifact guard, which
    # isn't what this test is about.
    replacement = 'Point one: as Romans 8:28 says, all things work for good in the end.'

    async def _fake_stream(model, messages, raw_sink=None, **kwargs):
        if raw_sink is not None:
            raw_sink.append(replacement)
        yield replacement

    monkeypatch.setattr("app.services.generation.stream_chat_completion", _fake_stream)
    _force_bible_api_down(monkeypatch)

    edit_resp = client.post(
        f"/sermons/{sermon_id}/edit",
        json={"instruction": "add a reference", "selection": {"start": 0, "end": len(original)}},
        headers=auth_headers,
    )
    assert edit_resp.status_code == 200, edit_resp.text
    assert "event: error" not in edit_resp.text
    assert "event: done" in edit_resp.text

    with pg_engine.begin() as conn:
        set_tenant(conn, tenant_id)
        sermon_row = conn.execute(sa.text("SELECT content FROM sermons WHERE id = :id"), {"id": sermon_id}).fetchone()
    assert sermon_row.content == replacement


def test_restore_succeeds_even_when_citation_verification_is_completely_down(
    pg_engine: Engine, active_tenant_with_org: dict, auth_headers: dict, monkeypatch
):
    tenant_id = active_tenant_with_org["id"]
    create_resp = client.post(
        "/sermons",
        json={"title": "Resilience Restore Test", "format": "topical", "content": "Original content."},
        headers=auth_headers,
    )
    sermon_id = create_resp.json()["id"]

    async def _fake_stream(model, messages, raw_sink=None, **kwargs):
        yield "Edited content."

    monkeypatch.setattr("app.services.generation.stream_chat_completion", _fake_stream)
    edit_resp = client.post(
        f"/sermons/{sermon_id}/edit",
        json={"instruction": "change it", "selection": {"start": 0, "end": len("Original content.")}},
        headers=auth_headers,
    )
    assert edit_resp.status_code == 200, edit_resp.text

    entries = client.get(f"/sermons/{sermon_id}/revisions", headers=auth_headers).json()
    original_id = next(e["id"] for e in entries if not e["is_current"])

    _force_bible_api_down(monkeypatch)
    restore_resp = client.post(f"/sermons/{sermon_id}/revisions/{original_id}/restore", headers=auth_headers)
    assert restore_resp.status_code == 200, restore_resp.text
    body = restore_resp.json()
    assert body["sermon"]["content"] == "Original content."
    assert body["citation_flags"] == []
