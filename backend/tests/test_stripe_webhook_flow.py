"""End-to-end through the actual /webhooks/stripe route, proving Task 3's
signup-to-active flow and Task 3's explicit idempotency + access-gating
requirements. Same live-Postgres requirement as test_rls.py.
"""
import json

import pytest
import sqlalchemy as sa
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from app.core.config import get_settings
from app.core.deps import get_active_tenant_id
from app.main import app
from app.models import Tenant
from tests.conftest import sign_stripe_payload

client = TestClient(app)
settings = get_settings()

pytestmark = pytest.mark.skipif(
    not settings.stripe_webhook_secret, reason="STRIPE_WEBHOOK_SECRET not configured"
)


def _post_event(event_id: str, event_type: str, data_object: dict):
    body = json.dumps(
        {"id": event_id, "object": "event", "type": event_type, "data": {"object": data_object}}
    ).encode()
    header = sign_stripe_payload(body, settings.stripe_webhook_secret)
    return client.post("/webhooks/stripe", content=body, headers={"stripe-signature": header})


def _tenant_row(pg_engine: Engine, tenant_id) -> sa.Row:
    with pg_engine.begin() as conn:
        return conn.execute(
            sa.text(
                "SELECT subscription_status, stripe_customer_id, stripe_subscription_id, plan_tier "
                "FROM tenants WHERE id = :id"
            ),
            {"id": str(tenant_id)},
        ).fetchone()


def test_checkout_completed_activates_tenant(pg_engine: Engine, pending_tenant: dict):
    tenant_id = pending_tenant["id"]
    resp = _post_event(
        "evt_checkout_1",
        "checkout.session.completed",
        {
            "client_reference_id": str(tenant_id),
            "customer": "cus_test_123",
            "subscription": "sub_test_123",
            "metadata": {"plan_tier": "starter"},
        },
    )
    assert resp.status_code == 200, resp.text

    row = _tenant_row(pg_engine, tenant_id)
    assert row.subscription_status == "active"
    assert row.stripe_customer_id == "cus_test_123"
    assert row.stripe_subscription_id == "sub_test_123"
    assert row.plan_tier == "starter"


def test_duplicate_checkout_event_is_not_reprocessed(pg_engine: Engine, pending_tenant: dict):
    tenant_id = pending_tenant["id"]
    event_id = "evt_checkout_dup_1"
    first = _post_event(
        event_id,
        "checkout.session.completed",
        {
            "client_reference_id": str(tenant_id),
            "customer": "cus_test_first",
            "subscription": "sub_test_first",
            "metadata": {"plan_tier": "starter"},
        },
    )
    assert first.status_code == 200

    # Same event id, DIFFERENT payload (as if attacker or a bad retry
    # tried to reuse it to overwrite plan_tier) — if idempotency is
    # actually working, this must be skipped, not reapplied.
    second = _post_event(
        event_id,
        "checkout.session.completed",
        {
            "client_reference_id": str(tenant_id),
            "customer": "cus_test_second",
            "subscription": "sub_test_second",
            "metadata": {"plan_tier": "growth"},
        },
    )
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate, skipped"

    row = _tenant_row(pg_engine, tenant_id)
    # Still the FIRST event's values — proves the second was really
    # skipped, not just that the endpoint returned 200 for it.
    assert row.stripe_customer_id == "cus_test_first"
    assert row.plan_tier == "starter"


async def test_subscription_deleted_sets_canceled_and_actually_blocks_access(
    pg_engine: Engine, pending_tenant: dict
):
    tenant_id = pending_tenant["id"]
    activate = _post_event(
        "evt_checkout_2",
        "checkout.session.completed",
        {
            "client_reference_id": str(tenant_id),
            "customer": "cus_test_cancel",
            "subscription": "sub_test_cancel",
            "metadata": {"plan_tier": "growth"},
        },
    )
    assert activate.status_code == 200
    assert _tenant_row(pg_engine, tenant_id).subscription_status == "active"

    canceled = _post_event(
        "evt_sub_deleted_1", "customer.subscription.deleted", {"customer": "cus_test_cancel"}
    )
    assert canceled.status_code == 200
    row = _tenant_row(pg_engine, tenant_id)
    assert row.subscription_status == "canceled"

    # Not just "the column changed" — call the actual dependency
    # function that every product route depends on, against a Tenant
    # object reflecting this real DB state, and prove it rejects.
    tenant_obj = Tenant(id=tenant_id, subscription_status="canceled")
    with pytest.raises(HTTPException) as exc_info:
        await get_active_tenant_id(tenant=tenant_obj)
    assert exc_info.value.status_code == 402


def test_invalid_signature_is_rejected_and_changes_nothing(pg_engine: Engine, pending_tenant: dict):
    tenant_id = pending_tenant["id"]
    body = json.dumps(
        {
            "id": "evt_bad_sig",
            "object": "event",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "client_reference_id": str(tenant_id),
                    "customer": "cus_should_not_apply",
                    "subscription": "sub_should_not_apply",
                    "metadata": {"plan_tier": "enterprise"},
                }
            },
        }
    ).encode()
    header = sign_stripe_payload(body, "whsec_completely_wrong_secret")

    resp = client.post("/webhooks/stripe", content=body, headers={"stripe-signature": header})
    assert resp.status_code == 400

    row = _tenant_row(pg_engine, tenant_id)
    assert row.subscription_status == "pending"
    assert row.stripe_customer_id is None
