import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class DocumentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    corpus_type: str
    source: str
    sermon_id: uuid.UUID | None
    title: str
    original_filename: str | None
    status: str
    created_at: datetime
    updated_at: datetime
