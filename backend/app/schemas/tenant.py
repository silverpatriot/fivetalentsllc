import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class TenantBase(BaseModel):
    slug: str = Field(max_length=63)
    name: str = Field(max_length=255)


class TenantCreate(TenantBase):
    clerk_org_id: str


class TenantRead(TenantBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    clerk_org_id: str
    plan_tier: str
    subscription_status: str
    stripe_customer_id: str | None
    stripe_subscription_id: str | None
    created_at: datetime


class TenantPublicRead(TenantBase):
    """The subset of a tenant safe to expose with NO auth at all — for the
    subdomain-routing middleware resolving `<slug>.kerygma.church` to
    branding before anyone has signed in. Deliberately excludes
    subscription_status, Stripe ids, and clerk_org_id: none of that is the
    access-control decision (the backend's own get_active_tenant_id is,
    off the verified Clerk session) and none of it should be learnable by
    hitting a slug that isn't yours.
    """

    model_config = ConfigDict(from_attributes=True)
