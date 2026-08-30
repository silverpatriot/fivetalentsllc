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
    subscription_status: str | None
    created_at: datetime
