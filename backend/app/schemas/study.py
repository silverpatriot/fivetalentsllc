from pydantic import BaseModel


class StudyQueryRequest(BaseModel):
    question: str


class StudyCitationRead(BaseModel):
    source_type: str  # "document" | "web" — frontend uses this to render the two groups distinctly
    label: str
    title: str
    excerpt: str
    document_id: str | None = None
    url: str | None = None


class StudyQueryResponse(BaseModel):
    answer: str
    citations: list[StudyCitationRead]
    used_own_documents: bool
    used_web_search: bool
