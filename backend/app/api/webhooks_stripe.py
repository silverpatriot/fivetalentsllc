"""Stripe webhook: checkout.session.completed / invoice.paid /
customer.subscription.updated / customer.subscription.deleted.

Every handler here is idempotent (see try_claim_event) and updates only
`tenants`, which has no RLS — these run through get_raw_db, not a
tenant-scoped session, since which tenant an event belongs to is exactly
what each handler has to work out first.
"""
import logging
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_raw_db
from app.core.idempotency import try_claim_event
from app.core.security import verify_stripe_webhook
from app.models import Tenant

router = APIRouter()
logger = logging.getLogger(__name__)

# Stripe subscription.status values -> our simplified tenants.subscription_status.
# Deliberately coarse for this phase: past_due/incomplete/paused aren't yet
# distinct product states (no dunning/grace-period UX exists), so they're
# treated as still-active rather than invented into new states nothing
# else in the app knows how to handle. Revisit when that UX exists.
_STRIPE_STATUS_MAP: dict[str, str] = {
    "active": "active",
    "trialing": "active",
    "past_due": "active",
    "unpaid": "canceled",
    "canceled": "canceled",
    "incomplete": "pending",
    "incomplete_expired": "canceled",
    "paused": "canceled",
}


@router.post("/webhooks/stripe", status_code=status.HTTP_200_OK)
async def stripe_webhook(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_raw_db)],
    stripe_signature: Annotated[str | None, Header(alias="stripe-signature")] = None,
) -> dict[str, str]:
    payload = await request.body()
    if not stripe_signature:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing Stripe-Signature header")

    event = verify_stripe_webhook(payload, stripe_signature)

    async with db.begin():
        should_process = await try_claim_event(
            db, source="stripe", external_event_id=event.id, event_type=event.type
        )
        if not should_process:
            logger.info("Skipping duplicate Stripe webhook delivery: %s", event.id)
            return {"status": "duplicate, skipped"}

        obj: dict[str, Any] = event.data.object  # type: ignore[assignment]

        if event.type == "checkout.session.completed":
            await _handle_checkout_completed(db, obj)
        elif event.type == "invoice.paid":
            await _handle_invoice_paid(db, obj)
        elif event.type == "customer.subscription.updated":
            await _handle_subscription_updated(db, obj)
        elif event.type == "customer.subscription.deleted":
            await _handle_subscription_deleted(db, obj)
        else:
            logger.info("Ignoring unhandled Stripe webhook event type: %s", event.type)

    return {"status": "ok"}


async def _handle_checkout_completed(db: AsyncSession, session_obj: dict) -> None:
    tenant_id_raw = session_obj.get("client_reference_id")
    customer_id = session_obj.get("customer")
    subscription_id = session_obj.get("subscription")
    plan_tier = (session_obj.get("metadata") or {}).get("plan_tier")

    if not (tenant_id_raw and customer_id and subscription_id and plan_tier):
        # client_reference_id and metadata.plan_tier are both set by us
        # when creating the Checkout Session (app/api/billing.py) — their
        # absence means a misconfigured session, not a legitimate event
        # to quietly skip.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="checkout.session.completed missing client_reference_id/customer/subscription/plan_tier",
        )

    result = await db.execute(
        update(Tenant)
        .where(Tenant.id == uuid.UUID(tenant_id_raw))
        .values(
            stripe_customer_id=customer_id,
            stripe_subscription_id=subscription_id,
            subscription_status="active",
            plan_tier=plan_tier,
        )
    )
    if result.rowcount == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No tenant found for client_reference_id {tenant_id_raw!r}",
        )


async def _handle_invoice_paid(db: AsyncSession, invoice_obj: dict) -> None:
    customer_id = invoice_obj.get("customer")
    if not customer_id:
        return
    # Self-healing: a successful renewal payment means the subscription is
    # in good standing, regardless of what transient status it may have
    # carried before (e.g. recovering from past_due).
    await db.execute(
        update(Tenant).where(Tenant.stripe_customer_id == customer_id).values(subscription_status="active")
    )


async def _handle_subscription_updated(db: AsyncSession, sub_obj: dict) -> None:
    customer_id = sub_obj.get("customer")
    stripe_status = sub_obj.get("status")
    if not (customer_id and stripe_status):
        return
    mapped_status = _STRIPE_STATUS_MAP.get(stripe_status)
    if mapped_status is None:
        logger.warning("Unrecognized Stripe subscription status %r — leaving tenant status unchanged", stripe_status)
        return
    await db.execute(
        update(Tenant)
        .where(Tenant.stripe_customer_id == customer_id)
        .values(subscription_status=mapped_status)
    )


async def _handle_subscription_deleted(db: AsyncSession, sub_obj: dict) -> None:
    customer_id = sub_obj.get("customer")
    if not customer_id:
        return
    await db.execute(
        update(Tenant).where(Tenant.stripe_customer_id == customer_id).values(subscription_status="canceled")
    )
