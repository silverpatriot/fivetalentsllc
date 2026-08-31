"""End-to-end through the real /sermons and /sermons/{id}/generate routes:
real Postgres, a real (self-signed) Clerk JWT verified by the real
verify_clerk_jwt, real citation verification against bible-api.com — only
the OpenRouter calls themselves are mocked (no live LLM spend needed to
prove the plumbing). This is the required "a generation call correctly
records a usage_events row" test from the Phase 3 spec, extended per the
Phase 3 completion review's usage-metering decision: one row per real LLM
call (outline, draft), independently, regardless of success/failure, all
billable=False (the actual billing decision is downstream, not made
here — see app/models/usage_event.py and app/services/generation.py).
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
                "slug": f"gen-{tenant_id.hex[:8]}",
                "name": "Generation Test Tenant",
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
        private_key, {"sub": "user_test_1", "o": {"id": active_tenant_with_org["clerk_org_id"], "rol": "admin"}}
    )
    return {"Authorization": f"Bearer {token}"}


async def _fake_chat_completion(model: str, messages: list[dict]) -> tuple[str, str]:
    return "1. Point one\n2. Point two\n3. Point three", '{"fake":"outline-response"}'


async def _fake_stream_chat_completion(model: str, messages: list[dict], raw_sink: list[str] | None = None):
    for chunk in ["In the beginning, ", "God created the heavens and the earth."]:
        if raw_sink is not None:
            raw_sink.append(chunk)
        yield chunk


def test_generate_records_usage_event_and_generation_logs(
    pg_engine: Engine, active_tenant_with_org: dict, auth_headers: dict, monkeypatch
):
    tenant_id = active_tenant_with_org["id"]
    monkeypatch.setattr("app.services.generation.chat_completion", _fake_chat_completion)
    monkeypatch.setattr("app.services.generation.stream_chat_completion", _fake_stream_chat_completion)
    # record_usage_event's own Postgres-write correctness is exercised for
    # real here; its Stripe-reporting half (report_usage_event, run via
    # Celery) is already covered by test_usage_reporting.py — not needed
    # to prove "a usage_events row gets written", so the enqueue is
    # stubbed rather than requiring a live worker/broker for this test.
    monkeypatch.setattr("app.tasks.usage_reporting.report_usage_event.delay", lambda *a, **k: None)
    # Same reasoning for Phase 4's cadence-corpus ingestion, which this
    # test's finalization step now also triggers — real chunking/
    # embedding of every sermon this test generates isn't what's under
    # test here (see tests/test_document_ingestion.py for that), and
    # would otherwise race the actual live celery-worker in this
    # environment for no reason this test cares about.
    monkeypatch.setattr("app.services.ingestion.embed_document_chunks.delay", lambda *a, **k: None)

    create_resp = client.post(
        "/sermons", json={"title": "On the Creation", "format": "expository"}, headers=auth_headers
    )
    assert create_resp.status_code == 201, create_resp.text
    sermon_id = create_resp.json()["id"]

    gen_resp = client.post(
        f"/sermons/{sermon_id}/generate",
        json={"passage_reference": "Genesis 1:1"},
        headers=auth_headers,
    )
    assert gen_resp.status_code == 200, gen_resp.text
    body = gen_resp.text
    assert "event: outline" in body
    assert "event: delta" in body
    assert "event: citations" in body
    assert "event: done" in body
    assert "event: error" not in body

    with pg_engine.begin() as conn:
        set_tenant(conn, tenant_id)
        usage_rows = conn.execute(
            sa.text(
                "SELECT event_type, quantity, sermon_id, generation_stage, outcome, billable "
                "FROM usage_events WHERE tenant_id = :tid ORDER BY generation_stage"
            ),
            {"tid": str(tenant_id)},
        ).fetchall()
        log_rows = conn.execute(
            sa.text("SELECT stage, model FROM generation_logs WHERE tenant_id = :tid ORDER BY stage"),
            {"tid": str(tenant_id)},
        ).fetchall()
        sermon_row = conn.execute(
            sa.text("SELECT status, content FROM sermons WHERE id = :id"), {"id": sermon_id}
        ).fetchone()

    # One row per real LLM call (outline, draft) — not one per generation
    # action — and every one of them billable=False: the raw ledger
    # records what happened, but not what to charge for (that's a
    # separate, not-yet-made decision).
    assert len(usage_rows) == 2
    for row in usage_rows:
        assert row.event_type == "ai_generation"
        assert float(row.quantity) == 1.0
        assert str(row.sermon_id) == sermon_id
        assert row.outcome == "succeeded"
        assert row.billable is False
    assert {row.generation_stage for row in usage_rows} == {"draft", "outline"}

    assert {row.stage for row in log_rows} == {"draft", "outline"}

    assert sermon_row.status == "ready"
    assert "God created the heavens" in sermon_row.content


def test_generate_records_a_failed_usage_event_when_outline_errors(
    pg_engine: Engine, active_tenant_with_org: dict, auth_headers: dict, monkeypatch
):
    """The core of the usage-metering decision: a failed LLM call still
    gets its own usage_events row (outcome='failed'), and nothing gets
    invented for the draft stage, which never ran."""
    tenant_id = active_tenant_with_org["id"]

    async def _boom(model, messages):
        from app.services.openrouter import OpenRouterError

        raise OpenRouterError("simulated outline failure")

    monkeypatch.setattr("app.services.generation.chat_completion", _boom)
    monkeypatch.setattr("app.tasks.usage_reporting.report_usage_event.delay", lambda *a, **k: None)

    create_resp = client.post("/sermons", json={"title": "Doomed", "format": "topical"}, headers=auth_headers)
    assert create_resp.status_code == 201, create_resp.text
    sermon_id = create_resp.json()["id"]

    gen_resp = client.post(
        f"/sermons/{sermon_id}/generate", json={"topic": "perseverance"}, headers=auth_headers
    )
    assert gen_resp.status_code == 200, gen_resp.text
    assert "event: error" in gen_resp.text

    with pg_engine.begin() as conn:
        set_tenant(conn, tenant_id)
        usage_rows = conn.execute(
            sa.text(
                "SELECT generation_stage, outcome, billable FROM usage_events WHERE tenant_id = :tid"
            ),
            {"tid": str(tenant_id)},
        ).fetchall()
        sermon_row = conn.execute(
            sa.text("SELECT status, content FROM sermons WHERE id = :id"), {"id": sermon_id}
        ).fetchone()

    assert len(usage_rows) == 1
    assert usage_rows[0].generation_stage == "outline"
    assert usage_rows[0].outcome == "failed"
    assert usage_rows[0].billable is False

    assert sermon_row.status == "draft"
    assert sermon_row.content is None


def test_generate_requires_active_subscription(pg_engine: Engine, rsa_keypair, monkeypatch):
    """get_active_tenant_id's 402 gate applies to /generate the same as
    every other product route — a pending tenant can't call it, exactly
    the "backend must still enforce it" requirement from Task 1."""
    tenant_id = uuid.uuid4()
    clerk_org_id = f"org_{tenant_id.hex[:16]}"
    with pg_engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO tenants (id, slug, name, clerk_org_id) VALUES (:id, :slug, :name, :org)"
            ),
            {"id": str(tenant_id), "slug": f"pend-{tenant_id.hex[:8]}", "name": "Pending", "org": clerk_org_id},
        )
    try:
        private_key, public_key = rsa_keypair
        monkeypatch.setattr(security, "_jwk_client", lambda: _FakeJWKClient(public_key))
        token = make_clerk_jwt(private_key, {"sub": "user_test_2", "o": {"id": clerk_org_id, "rol": "admin"}})
        headers = {"Authorization": f"Bearer {token}"}

        resp = client.post("/sermons", json={"title": "Blocked", "format": "topical"}, headers=headers)
        assert resp.status_code == 402
    finally:
        with pg_engine.begin() as conn:
            conn.execute(sa.text("DELETE FROM tenants WHERE id = :id"), {"id": str(tenant_id)})
