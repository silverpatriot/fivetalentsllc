"""Concordance search endpoint — exact/stemmed word lookup across
scripture. See app/services/concordance.py for why this is lexical
(Postgres tsvector), not the semantic-similarity retrieval every other
search feature in this app uses.
"""
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.schemas.concordance import ConcordanceSearchResponse, ConcordanceVerseRead, ConcordanceWebResultRead
from app.services.concordance import search_concordance_with_web_fallback

router = APIRouter(prefix="/concordance", tags=["concordance"])


@router.get("/search", response_model=ConcordanceSearchResponse)
async def search_concordance_route(
    q: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    translation: str = "kjv",
    limit: int = 25,
) -> ConcordanceSearchResponse:
    if not q.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="q must not be blank")

    result = await search_concordance_with_web_fallback(db, q, translation, limit)
    return ConcordanceSearchResponse(
        query=q,
        translation=translation,
        local_matches=[
            ConcordanceVerseRead(reference=f"{m.book} {m.chapter}:{m.verse}", text=m.text, translation=m.translation)
            for m in result.local_matches
        ],
        web_results=[
            ConcordanceWebResultRead(title=r["title"], url=r["url"], content=r["content"]) for r in result.web_results
        ],
        used_web_search=result.used_web_search,
    )
