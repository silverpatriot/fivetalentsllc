"""Checkout (new subscription) and Customer Portal (self-serve plan
changes/cancellation) session creation. Both hand off to Stripe-hosted
pages — no custom payment form or billing-management UI, per Task 2.
"""
from typing import Annotated

import stripe
from fastapi import APIRouter, Depends, HTTPException, status

from app.core.config import get_settings
from app.core.deps import get_current_tenant
from app.models import Tenant
from app.schemas.billing import (
    PLAN_TIERS,
    CheckoutSessionCreate,
    CheckoutSessionRead,
    PortalSessionRead,
)

router = APIRouter(prefix="/billing", tags=["billing"])
settings = get_settings()

_PLAN_PRICE_IDS: dict[str, str] = {
    "starter": settings.stripe_price_starter,
    "growth": settings.stripe_price_growth,
    "enterprise": settings.stripe_price_enterprise,
}


@router.post("/checkout", response_model=CheckoutSessionRead)
async def create_checkout_session(
    body: CheckoutSessionCreate,
    tenant: Annotated[Tenant, Depends(get_current_tenant)],
) -> CheckoutSessionRead:
    """Starts the Task 3 signup flow's step 3. Deliberately depends on
    get_current_tenant, not get_active_tenant_id — a 'pending' tenant
    (the normal state at this point) must be able to reach this."""
    if body.plan_tier not in PLAN_TIERS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"plan_tier must be one of {PLAN_TIERS}",
        )
    price_id = _PLAN_PRICE_IDS[body.plan_tier]
    if not price_id:
        # Blocked until scripts/stripe_setup.py has actually been run
        # against a real Stripe account and the resulting price ID is in
        # .env — see Task 2 in the Phase 2 spec.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"No Stripe price configured for plan_tier={body.plan_tier!r}",
        )

    line_items = [{"price": price_id, "quantity": 1}]
    # Metered add-ons: no quantity — Stripe reports usage against these
    # via meter events (app/tasks/usage_reporting.py), not a Checkout
    # quantity.
    for meter_price in (
        settings.stripe_price_transcription_minutes,
        settings.stripe_price_ai_generations,
    ):
        if meter_price:
            line_items.append({"price": meter_price})

    stripe.api_key = settings.stripe_secret_key
    session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=line_items,
        # This, not stripe_customer_id (which doesn't exist yet — it's
        # set BY the checkout.session.completed webhook), is how that
        # webhook finds its way back to this tenant.
        client_reference_id=str(tenant.id),
        metadata={"plan_tier": body.plan_tier, "tenant_id": str(tenant.id)},
        success_url=f"{settings.frontend_url}/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{settings.frontend_url}/billing/canceled",
        customer_email=None,  # let Stripe collect it in Checkout; we have no email source of our own yet
    )
    if not session.url:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Stripe did not return a Checkout URL"
        )
    return CheckoutSessionRead(checkout_url=session.url)


@router.post("/portal", response_model=PortalSessionRead)
async def create_portal_session(
    tenant: Annotated[Tenant, Depends(get_current_tenant)],
) -> PortalSessionRead:
    if not tenant.stripe_customer_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Tenant has no Stripe customer yet — complete Checkout first",
        )
    stripe.api_key = settings.stripe_secret_key
    session = stripe.billing_portal.Session.create(
        customer=tenant.stripe_customer_id,
        return_url=f"{settings.frontend_url}/billing",
    )
    return PortalSessionRead(portal_url=session.url)
