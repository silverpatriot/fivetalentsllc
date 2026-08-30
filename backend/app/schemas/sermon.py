import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.sermon import SermonFormat


class SermonCreate(BaseModel):
    title: str = Field(max_length=500)
    format: SermonFormat
    content: str | None = None


class SermonUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=500)
    content: str | None = None
    status: str | None = None


class SermonRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    title: str
    format: SermonFormat
    content: str | None
    status: str
    created_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
