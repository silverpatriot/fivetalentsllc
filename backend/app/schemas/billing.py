from pydantic import BaseModel

PLAN_TIERS = ("starter", "growth", "enterprise")


class CheckoutSessionCreate(BaseModel):
    plan_tier: str  # one of PLAN_TIERS


class CheckoutSessionRead(BaseModel):
    checkout_url: str


class PortalSessionRead(BaseModel):
    portal_url: str
