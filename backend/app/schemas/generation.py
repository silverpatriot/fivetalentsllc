from pydantic import BaseModel, Field, model_validator


class GenerateRequest(BaseModel):
    """Kicks off generation for an existing (draft) sermon. Exactly one of
    passage_reference/topic must be given — expository/textual sermons
    anchor to a passage, topical/narrative sermons often start from a
    topic instead; a custom format can supply either or both."""

    passage_reference: str | None = Field(default=None, max_length=200)
    topic: str | None = Field(default=None, max_length=500)
    translation: str | None = Field(default=None, max_length=20)

    @model_validator(mode="after")
    def _require_passage_or_topic(self) -> "GenerateRequest":
        if not self.passage_reference and not self.topic:
            raise ValueError("Provide at least one of passage_reference or topic")
        return self


class CitationFlag(BaseModel):
    """One scripture reference found in the model's draft, and the result
    of checking it against the actual Bible text source (Task 3: "don't
    trust the model to quote accurately from memory")."""

    reference: str
    status: str  # "verified" | "invalid_reference" | "quote_mismatch"
    quoted_text: str | None = None
    source_text: str | None = None
    detail: str


class EditSelection(BaseModel):
    """Character offsets into the sermon's CURRENT `content` at the time
    the pastor made their selection — start inclusive, end exclusive
    (Python slice semantics: content[start:end]). Sent by the frontend
    when the pastor highlighted text before typing an instruction; when
    absent, app/services/generation.py's _run_edit locates the target
    span itself (Task 1.1's confirmed design) instead of trusting the
    model to infer position from prose alone."""

    start: int = Field(ge=0)
    end: int = Field(gt=0)

    @model_validator(mode="after")
    def _end_after_start(self) -> "EditSelection":
        if self.end <= self.start:
            raise ValueError("end must be greater than start")
        return self


class EditRequest(BaseModel):
    """A natural-language edit instruction against an existing draft
    (Phase 6). `selection`, when given, makes the edit's scope exact and
    skips the locate step entirely — see EditSelection."""

    instruction: str = Field(min_length=1, max_length=2000)
    selection: EditSelection | None = None
    translation: str | None = Field(default=None, max_length=20)
