import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class MediaFileCreate(BaseModel):
    sermon_id: uuid.UUID | None = None
    original_filename: str = Field(max_length=500)
    storage_path: str = Field(max_length=1000)


class MediaFileRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    sermon_id: uuid.UUID | None
    original_filename: str
    storage_path: str
    duration_seconds: float | None
    transcription_status: str
    transcript_text: str | None
    created_at: datetime
