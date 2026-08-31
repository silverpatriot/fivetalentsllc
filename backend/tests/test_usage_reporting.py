"""record_usage_event / report_usage_event: the Postgres write is real
(needs live Postgres, same as the rest of this suite); the Stripe API
call is mocked (no live Stripe account available here) — but what's
actually under test in the failure case is that a mocked Stripe failure
never loses or corrupts the Postgres row, which doesn't require the mock
to be real Stripe, only for stripe.billing.MeterEvent.create to raise the
same exception type real Stripe would.
"""
import uuid
from unittest.mock import MagicMock, patch

import pytest
import sqlalchemy as sa
import stripe
from celery.exceptions import Retry
from sqlalchemy import Engine

from app.models import UsageEventType
from app.tasks.usage_reporting import record_usage_event, report_usage_event
from tests.conftest import set_tenant


@pytest.fixture
def active_tenant(pg_engine: Engine):
    """A tenant with an active subscription and a Stripe customer — what
    report_usage_event needs to actually have something to report against."""
    tenant_id = uuid.uuid4()
    with pg_engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO tenants (id, slug, name, clerk_org_id, subscription_status, stripe_customer_id) "
                "VALUES (:id, :slug, :name, :org, 'active', :cus)"
            ),
            {
                "id": str(tenant_id),
                "slug": f"active-{tenant_id.hex[:8]}",
                "name": "Active Tenant",
                "org": f"org_{tenant_id.hex[:16]}",
                "cus": f"cus_{tenant_id.hex[:14]}",
            },
        )
    yield tenant_id
    with pg_engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM tenants WHERE id = :id"), {"id": str(tenant_id)})


def _usage_row(pg_engine: Engine, usage_event_id, tenant_id):
    # usage_events is RLS-protected (unlike tenants/webhook_events) —
    # context must be set before every query against it, same as
    # test_rls.py's helpers. Missing this is exactly what produced
    # "invalid input syntax for type uuid: \"\"": Phase 1's RLS policy is
    # deliberately hard-fail on missing/reverted context, not a silent
    # empty result.
    with pg_engine.begin() as conn:
        set_tenant(conn, tenant_id)
        return conn.execute(
            sa.text("SELECT stripe_usage_record_id FROM usage_events WHERE id = :id"),
            {"id": str(usage_event_id)},
        ).fetchone()


def test_record_usage_event_writes_before_any_stripe_call(pg_engine: Engine, active_tenant):
    """record_usage_event itself never touches Stripe — the row exists
    the moment this returns, regardless of what happens to reporting."""
    with patch("app.tasks.usage_reporting.report_usage_event.delay") as mock_delay:
        usage_event_id = record_usage_event(active_tenant, UsageEventType.AI_GENERATION, 3)

    row = _usage_row(pg_engine, usage_event_id, active_tenant)
    assert row is not None
    assert row.stripe_usage_record_id is None
    mock_delay.assert_called_once_with(str(usage_event_id), str(active_tenant))


def test_report_usage_event_success_marks_row_reported(pg_engine: Engine, active_tenant):
    with patch("app.tasks.usage_reporting.report_usage_event.delay"):
        usage_event_id = record_usage_event(active_tenant, UsageEventType.TRANSCRIPTION_MINUTE, 5)

    fake_meter_event = MagicMock(identifier=str(usage_event_id), id="mtr_evt_fake")
    with patch("stripe.billing.MeterEvent.create", return_value=fake_meter_event) as mock_create:
        report_usage_event.apply(args=(str(usage_event_id), str(active_tenant)), throw=True)

    mock_create.assert_called_once()
    call_kwargs = mock_create.call_args.kwargs
    assert call_kwargs["identifier"] == str(usage_event_id)  # our own dedup key sent to Stripe too

    row = _usage_row(pg_engine, usage_event_id, active_tenant)
    assert row.stripe_usage_record_id == str(usage_event_id)


def test_report_usage_event_stripe_failure_does_not_lose_the_row(pg_engine: Engine, active_tenant):
    """The whole point of writing to Postgres first: a Stripe outage
    during reporting must never mean 'we don't know this happened'."""
    with patch("app.tasks.usage_reporting.report_usage_event.delay"):
        usage_event_id = record_usage_event(active_tenant, UsageEventType.AI_GENERATION, 1)

    with patch("stripe.billing.MeterEvent.create", side_effect=stripe.APIConnectionError("simulated outage")):
        with pytest.raises(Retry):
            report_usage_event.apply(args=(str(usage_event_id), str(active_tenant)), throw=True)

    # Still there, still correct, still retryable — nothing was lost or
    # marked reported despite the Stripe call failing.
    row = _usage_row(pg_engine, usage_event_id, active_tenant)
    assert row is not None
    assert row.stripe_usage_record_id is None


def test_report_usage_event_is_idempotent_on_already_reported_row(pg_engine: Engine, active_tenant):
    """A retried task invocation for a row that got reported just before
    the failure was recorded (a real possibility with at-least-once task
    delivery) must not report it to Stripe a second time."""
    with patch("app.tasks.usage_reporting.report_usage_event.delay"):
        usage_event_id = record_usage_event(active_tenant, UsageEventType.AI_GENERATION, 1)

    with pg_engine.begin() as conn:
        set_tenant(conn, active_tenant)
        conn.execute(
            sa.text("UPDATE usage_events SET stripe_usage_record_id = :id WHERE id = :id2"),
            {"id": "already-reported-marker", "id2": str(usage_event_id)},
        )

    with patch("stripe.billing.MeterEvent.create") as mock_create:
        report_usage_event.apply(args=(str(usage_event_id), str(active_tenant)), throw=True)

    mock_create.assert_not_called()
