"""Phase 4 Task 3: the theology/study corpus's query endpoint. Upload
into this corpus reuses app/api/documents.py's existing pipeline as-is
(POST /documents with corpus_type="theology") — nothing new needed there,
per the kickoff spec's "using Task 1's upload pipeline."
"""
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.models.reference import COMMENTARY_LABELS, ReferenceDocument, ReferenceType
from app.schemas.study import CommentaryListResponse, CommentaryOption, StudyQueryRequest, StudyQueryResponse
from app.services.openrouter import OpenRouterError
from app.services.study import answer_question

router = APIRouter(prefix="/study", tags=["study"])


@router.get("/commentaries", response_model=CommentaryListResponse)
async def list_commentaries(db: Annotated[AsyncSession, Depends(get_db)]) -> CommentaryListResponse:
    """Only what's actually been ingested in this environment — not the
    full 7-id catalog COMMENTARY_LABELS knows about, so the picker never
    offers a commentary that would silently return empty results because
    nobody's run the ingest script for it yet."""
    stmt = (
        select(ReferenceDocument.source_id)
        .where(ReferenceDocument.reference_type == ReferenceType.COMMENTARY.value, ReferenceDocument.source_id.is_not(None))
        .distinct()
    )
    source_ids = [row[0] for row in (await db.execute(stmt)).all()]
    return CommentaryListResponse(
        commentaries=[
            CommentaryOption(id=sid, label=COMMENTARY_LABELS.get(sid, sid)) for sid in sorted(source_ids)
        ]
    )


@router.post("/query", response_model=StudyQueryResponse)
async def query_study_corpus(
    body: StudyQueryRequest, db: Annotated[AsyncSession, Depends(get_db)]
) -> StudyQueryResponse:
    if not body.question.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="question must not be blank")
    try:
        result = await answer_question(db, body.question, commentary_sources=body.commentary_sources)
    except OpenRouterError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    return StudyQueryResponse(
        answer=result.answer,
        citations=[
            {
                "source_type": c.source_type,
                "label": c.label,
                "title": c.title,
                "excerpt": c.excerpt,
                "document_id": c.document_id,
                "url": c.url,
                "commentary_source": c.commentary_source,
            }
            for c in result.citations
        ],
        used_own_documents=result.used_own_documents,
        used_web_search=result.used_web_search,
    )
