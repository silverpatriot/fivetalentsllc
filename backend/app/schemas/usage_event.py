import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models.usage_event import UsageEventType


class UsageEventCreate(BaseModel):
    event_type: UsageEventType
    quantity: float


class UsageEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    event_type: UsageEventType
    quantity: float
    stripe_usage_record_id: str | None
    created_at: datetime
