"""End-to-end through the actual /webhooks/clerk route (FastAPI
TestClient -> real signature verification -> real Postgres), proving the
Task 3 provisioning trigger: organization.created creates a tenants row,
nothing else does.

Needs a real Postgres with all migrations applied — same requirement as
test_rls.py. Signs with whatever CLERK_WEBHOOK_SECRET is actually
configured in the environment (not a fixed test secret), since this goes
through the real running app's verification, not a monkeypatched one.
"""
import json
import uuid

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from app.core.config import get_settings
from app.main import app
from tests.conftest import sign_svix_payload

client = TestClient(app)
settings = get_settings()

pytestmark = [
    pytest.mark.skipif(not settings.clerk_webhook_secret, reason="CLERK_WEBHOOK_SECRET not configured"),
    # Every test in this file writes to webhook_events via the real
    # /webhooks/clerk route — see _clean_webhook_events in conftest.py
    # for why this can't be autouse globally.
    pytest.mark.usefixtures("_clean_webhook_events"),
]


def _org_created_payload(org_id: str, slug: str, name: str) -> bytes:
    return json.dumps({"type": "organization.created", "data": {"id": org_id, "slug": slug, "name": name}}).encode()


def _cleanup_tenant(pg_engine: Engine, clerk_org_id: str) -> None:
    with pg_engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM tenants WHERE clerk_org_id = :org"), {"org": clerk_org_id})


def test_organization_created_creates_pending_tenant(pg_engine: Engine):
    unique = uuid.uuid4().hex[:12]
    org_id, slug, name = f"org_test_{unique}", f"test-church-{unique}", "Test Church"
    payload = _org_created_payload(org_id, slug, name)
    headers = sign_svix_payload(f"msg_{unique}", payload, settings.clerk_webhook_secret)

    try:
        resp = client.post("/webhooks/clerk", content=payload, headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "ok", resp.text  # not "duplicate, skipped" — see test_stripe_webhook_flow.py

        with pg_engine.begin() as conn:
            row = conn.execute(
                sa.text("SELECT slug, name, subscription_status, stripe_customer_id FROM tenants WHERE clerk_org_id = :org"),
                {"org": org_id},
            ).fetchone()
        assert row is not None
        assert row.slug == slug
        assert row.name == name
        assert row.subscription_status == "pending"
        assert row.stripe_customer_id is None
    finally:
        _cleanup_tenant(pg_engine, org_id)


def test_duplicate_delivery_creates_tenant_only_once(pg_engine: Engine):
    unique = uuid.uuid4().hex[:12]
    org_id, slug, name = f"org_test_{unique}", f"test-church-{unique}", "Test Church"
    payload = _org_created_payload(org_id, slug, name)
    msg_id = f"msg_{unique}"
    headers = sign_svix_payload(msg_id, payload, settings.clerk_webhook_secret)

    try:
        first = client.post("/webhooks/clerk", content=payload, headers=headers)
        assert first.status_code == 200
        # Same svix-id (same msg_id/timestamp/signature) — a genuine
        # redelivery of the same logical event, not a new one.
        second = client.post("/webhooks/clerk", content=payload, headers=headers)
        assert second.status_code == 200
        assert second.json()["status"] == "duplicate, skipped"

        with pg_engine.begin() as conn:
            count = conn.execute(
                sa.text("SELECT count(*) FROM tenants WHERE clerk_org_id = :org"), {"org": org_id}
            ).scalar_one()
        assert count == 1
    finally:
        _cleanup_tenant(pg_engine, org_id)


def test_invalid_signature_is_rejected_and_creates_nothing(pg_engine: Engine):
    unique = uuid.uuid4().hex[:12]
    org_id, slug, name = f"org_test_{unique}", f"test-church-{unique}", "Test Church"
    payload = _org_created_payload(org_id, slug, name)
    # Signed with the wrong secret.
    headers = sign_svix_payload(f"msg_{unique}", payload, "whsec_d3JvbmdzZWNyZXRieXRlc2hlcmU=")

    resp = client.post("/webhooks/clerk", content=payload, headers=headers)
    assert resp.status_code == 400

    with pg_engine.begin() as conn:
        count = conn.execute(
            sa.text("SELECT count(*) FROM tenants WHERE clerk_org_id = :org"), {"org": org_id}
        ).scalar_one()
    assert count == 0
