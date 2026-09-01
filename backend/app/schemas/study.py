from pydantic import BaseModel


class StudyQueryRequest(BaseModel):
    question: str
    # Which commentary source(s) to ground on, e.g. ["matthew-henry",
    # "adam-clarke"] — see app/models/reference.py's COMMENTARY_LABELS
    # for the full set. None (the default) queries whatever's been
    # ingested, unfiltered — see app/services/study.py's answer_question
    # docstring for exactly what that means.
    commentary_sources: list[str] | None = None


class StudyCitationRead(BaseModel):
    source_type: str  # "document" | "commentary" | "cross_reference" | "web" — frontend renders each as its own distinct group
    label: str
    title: str
    excerpt: str
    document_id: str | None = None
    url: str | None = None
    commentary_source: str | None = None  # which commentary (e.g. "adam-clarke") when source_type == "commentary"


class StudyQueryResponse(BaseModel):
    answer: str
    citations: list[StudyCitationRead]
    used_own_documents: bool
    used_web_search: bool


class CommentaryOption(BaseModel):
    id: str
    label: str


class CommentaryListResponse(BaseModel):
    commentaries: list[CommentaryOption]
