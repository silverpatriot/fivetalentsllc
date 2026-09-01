from pydantic import BaseModel


class PassageRead(BaseModel):
    text: str
    translation: str
    source: str  # "api.bible" | "bible-api.com"


class BibleCompareResponse(BaseModel):
    reference: str
    # Keyed by translation code; a value of None means that translation
    # doesn't resolve this particular reference (a valid, non-error
    # outcome — see bible.fetch_passage's own docstring).
    passages: dict[str, PassageRead | None]


class TranslationOption(BaseModel):
    code: str
    label: str


class TranslationListResponse(BaseModel):
    translations: list[TranslationOption]
