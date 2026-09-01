from pydantic import BaseModel


class ConcordanceVerseRead(BaseModel):
    reference: str  # e.g. "John 3:16", computed in the route
    text: str
    translation: str


class ConcordanceWebResultRead(BaseModel):
    title: str
    url: str
    content: str


class ConcordanceSearchResponse(BaseModel):
    query: str
    translation: str
    # Two separate lists, never one blended array with a source-type
    # discriminator — same "structurally separate, never blended"
    # principle as app/schemas/study.py's StudyCitationRead groups.
    local_matches: list[ConcordanceVerseRead]
    web_results: list[ConcordanceWebResultRead]
    used_web_search: bool
