import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.sermon import SermonFormat
from app.schemas.generation import CitationFlag


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
    outline: str | None
    status: str
    created_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class OutlineGenerateRequest(BaseModel):
    translation: str | None = Field(default=None, max_length=20)


class RevisionSummary(BaseModel):
    """One entry in a sermon's version history (Phase 8 Task 2). `id` is
    a real SermonRevision UUID for a past state, or the literal string
    "current" for the live sermon.content — that entry is synthesized
    from the Sermon row itself, never read from sermon_revisions (there
    is no revision row for the live content — see the Phase 8 Task 1
    audit for why that's the correct design, not a gap to fill)."""

    id: str
    instruction: str | None
    created_at: datetime
    is_current: bool


class RevisionDetail(RevisionSummary):
    content: str


class DiffSegment(BaseModel):
    op: str  # "equal" | "delete" | "insert"
    text: str


class RevisionCompareResponse(BaseModel):
    from_revision: RevisionSummary
    to_revision: RevisionSummary
    diff: list[DiffSegment]


class RestoreResponse(BaseModel):
    sermon: SermonRead
    # The revision snapshot THIS restore itself created (of the state
    # right before restoring) — echoed back so the frontend can refresh
    # its version list without a second round-trip, and so a restore is
    # visibly, not just internally, "provably recoverable".
    new_revision: RevisionSummary
    # Citation verification is re-run on the restored content as part of
    # THIS same request — not left for the frontend to remember to
    # re-fetch separately (see get_sermon_pdf/get_sermon_citations'
    # own docstrings for why nothing here is ever persisted/cached: the
    # same "recompute, never trust a stale value" property applies to a
    # restore's own response).
    citation_flags: list[CitationFlag]
