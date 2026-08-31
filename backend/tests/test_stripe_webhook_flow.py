"""End-to-end through the actual /webhooks/stripe route, proving Task 3's
signup-to-active flow and Task 3's explicit idempotency + access-gating
requirements. Same live-Postgres requirement as test_rls.py.
"""
import json
import uuid

import pytest
import sqlalchemy as sa
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from app.core.config import get_settings
from app.core.deps import require_active_subscription
from app.main import app
from app.models import Tenant
from tests.conftest import sign_stripe_payload

client = TestClient(app)
settings = get_settings()

pytestmark = [
    pytest.mark.skipif(not settings.stripe_webhook_secret, reason="STRIPE_WEBHOOK_SECRET not configured"),
    # Every test in this file writes to webhook_events via the real
    # /webhooks/stripe route — see _clean_webhook_events in conftest.py
    # for why this can't be autouse globally.
    pytest.mark.usefixtures("_clean_webhook_events"),
]


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
        f"evt_checkout_{uuid.uuid4().hex}",
        "checkout.session.completed",
        {
            "client_reference_id": str(tenant_id),
            "customer": "cus_test_123",
            "subscription": "sub_test_123",
            "metadata": {"plan_tier": "starter"},
        },
    )
    assert resp.status_code == 200, resp.text
    # Not just the status code — "duplicate, skipped" is ALSO a 200. This
    # is exactly the assertion that was missing when stale webhook_events
    # rows from an earlier, un-wiped run made this test's fixed event id
    # look like a redelivery: the endpoint returned a legitimate 200 for
    # entirely the wrong reason, and only this check would have caught it
    # immediately instead of via a confusing downstream DB-state mismatch.
    assert resp.json()["status"] == "ok", resp.text

    row = _tenant_row(pg_engine, tenant_id)
    assert row.subscription_status == "active"
    assert row.stripe_customer_id == "cus_test_123"
    assert row.stripe_subscription_id == "sub_test_123"
    assert row.plan_tier == "starter"


def test_duplicate_checkout_event_is_not_reprocessed(pg_engine: Engine, pending_tenant: dict):
    tenant_id = pending_tenant["id"]
    # One fresh id per test run, reused for BOTH calls below — unique
    # across runs (so this run's "first" call can't collide with a stale
    # row from a previous one), but deliberately identical within this
    # test's own two calls, which is the actual thing under test.
    event_id = f"evt_checkout_dup_{uuid.uuid4().hex}"
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
    assert first.json()["status"] == "ok", first.text

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


def test_subscription_deleted_sets_canceled_and_actually_blocks_access(
    pg_engine: Engine, pending_tenant: dict
):
    tenant_id = pending_tenant["id"]
    activate = _post_event(
        f"evt_checkout_{uuid.uuid4().hex}",
        "checkout.session.completed",
        {
            "client_reference_id": str(tenant_id),
            "customer": "cus_test_cancel",
            "subscription": "sub_test_cancel",
            "metadata": {"plan_tier": "growth"},
        },
    )
    assert activate.status_code == 200
    assert activate.json()["status"] == "ok", activate.text
    assert _tenant_row(pg_engine, tenant_id).subscription_status == "active"

    canceled = _post_event(
        f"evt_sub_deleted_{uuid.uuid4().hex}", "customer.subscription.deleted", {"customer": "cus_test_cancel"}
    )
    assert canceled.status_code == 200
    assert canceled.json()["status"] == "ok", canceled.text
    row = _tenant_row(pg_engine, tenant_id)
    assert row.subscription_status == "canceled"

    # Not just "the column changed" — call the actual gate logic that
    # every product route sits behind, against a Tenant object reflecting
    # this real DB state, and prove it rejects.
    #
    # require_active_subscription, not the async get_active_tenant_id
    # dependency that wraps it: an earlier version of this test called
    # get_active_tenant_id directly via asyncio.run(), which turned out to
    # genuinely break — "attached to a different loop" / "Event loop is
    # closed" — because this file's other tests drive the app through one
    # module-level *synchronous* TestClient (with its own persistent
    # event loop/portal), and asyncio.run() spins up a separate,
    # throwaway loop each call. require_active_subscription is plain,
    # synchronous, I/O-free logic (get_active_tenant_id is only `async
    # def` for FastAPI's dependency-injection convention, not because it
    # awaits anything) — calling it directly needs no event loop at all,
    # which sidesteps the conflict rather than working around it.
    tenant_obj = Tenant(id=tenant_id, subscription_status="canceled")
    with pytest.raises(HTTPException) as exc_info:
        require_active_subscription(tenant_obj)
    assert exc_info.value.status_code == 402


def test_invalid_signature_is_rejected_and_changes_nothing(pg_engine: Engine, pending_tenant: dict):
    tenant_id = pending_tenant["id"]
    # Fixed id is fine here specifically (unlike the tests above): a bad
    # signature is rejected in verify_stripe_webhook, before the request
    # ever reaches try_claim_event — this event id is never written to
    # webhook_events at all, so it can't collide with a previous run's.
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
