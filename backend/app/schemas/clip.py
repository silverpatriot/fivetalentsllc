import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ClipCreate(BaseModel):
    media_file_id: uuid.UUID
    start_seconds: float
    end_seconds: float


class ClipRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    media_file_id: uuid.UUID
    start_seconds: float
    end_seconds: float
    status: str
    storage_path: str | None
    created_at: datetime
