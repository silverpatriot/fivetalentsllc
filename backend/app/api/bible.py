"""Multiple-translation lookup for the comparison view (Phase 5's UI gap
list: "multiple versions, comparisons"). Deliberately separate from
app/services/study.py and app/api/sermons.py — this is a read-only
scripture lookup, not RAG or generation, and reuses bible.py's existing
fetch_passage/fetch_passage_multi unmodified rather than adding a second
fetch codepath.
"""
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.schemas.bible import BibleCompareResponse, PassageRead, TranslationListResponse, TranslationOption
from app.services.bible import API_BIBLE_IDS, TRANSLATION_LABELS, fetch_passage_multi

router = APIRouter(prefix="/bible", tags=["bible"])

# 21 translations are available (API_BIBLE_IDS), but each is 2 live HTTP
# round-trips (fetch_passage -> _fetch_from_api_bible's /search then
# /passages/{id}) — asyncio.gather-ing too many of those per request is
# real latency/rate-limit exposure with no product benefit (nobody reads
# a 21-column comparison grid). Capped well above any sane UI default
# (Phase 5's frontend defaults to 5 preselected) while still allowing a
# pastor to add a few more before hitting the wall.
_MAX_TRANSLATIONS_PER_COMPARE = 8


@router.get("/translations", response_model=TranslationListResponse)
async def list_translations(_db: Annotated[AsyncSession, Depends(get_db)]) -> TranslationListResponse:
    return TranslationListResponse(
        translations=[
            TranslationOption(code=code, label=TRANSLATION_LABELS[code]) for code in API_BIBLE_IDS
        ]
    )


@router.get("/compare", response_model=BibleCompareResponse)
async def compare_translations(
    reference: str,
    translations: str,
    _db: Annotated[AsyncSession, Depends(get_db)],
) -> BibleCompareResponse:
    if not reference.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="reference must not be blank")

    # Dedupe while preserving order (a repeated code in the query string
    # shouldn't double-fetch), drop blanks from a trailing/stray comma.
    codes = list(dict.fromkeys(t.strip().lower() for t in translations.split(",") if t.strip()))
    if not codes:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="translations must not be blank")
    if len(codes) > _MAX_TRANSLATIONS_PER_COMPARE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"at most {_MAX_TRANSLATIONS_PER_COMPARE} translations per comparison",
        )

    results = await fetch_passage_multi(reference, codes)
    passages: dict[str, PassageRead | None] = {
        code: (PassageRead(text=p.text, translation=p.translation, source=p.source) if p is not None else None)
        for code, p in results.items()
    }
    return BibleCompareResponse(reference=reference, passages=passages)
