"""The baseline reference corpus (approved baseline-corpus proposal) —
real Postgres, real OpenRouter embeddings. scripts/ingest_baseline_corpus.py
itself is not exercised here (it's a standalone, deliberately-run admin
script, same category as scripts/stripe_setup.py — not imported/run by
the test suite, and its two live data sources, openbible.info and
bible.helloao.org, are exercised by actually running it, not by tests);
what's under test is the retrieval side and the specific property this
whole design exists for: this content is NOT gated per tenant.

Seeds its own rows directly (real embeddings, not mocked) rather than
depending on whatever the script happens to have ingested — self-
contained and repeatable regardless of whether ingest_baseline_corpus.py
has ever been run in this environment.
"""
import asyncio
import uuid
from collections.abc import Iterator

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine

from app.db.session import tenant_session
from app.models.reference import ReferenceChunk, ReferenceDocument, ReferenceType
from app.services.embeddings import embed_text
from app.services.reference_retrieval import search_reference_corpus


@pytest.fixture
def seeded_reference_corpus(pg_engine: Engine) -> Iterator[dict]:
    """One commentary entry and one cross-reference entry, with real
    embeddings, inserted directly (not via the ingestion script)."""
    # Distinct, unmistakably-test-only passage_reference values —
    # otherwise this collides with the UNIQUE(reference_type,
    # passage_reference) constraint against any real data
    # scripts/ingest_baseline_corpus.py has already loaded (confirmed
    # the hard way: "John 3" collided with real Matthew Henry commentary
    # ingested live during this same session).
    marker = uuid.uuid4().hex[:8]
    commentary_ref = f"TEST-{marker}-John 3"
    xref_ref = f"TEST-{marker}-John 3:16"

    commentary_text = (
        "Nicodemus came to Jesus by night, timid and uncertain, yet genuinely seeking the truth "
        "about the kingdom of God and what it means to be born again."
    )
    xref_text = "Cross-references for John 3:16:\n- Romans 5:8 (90 votes)\n- 1 John 4:9 (70 votes)"

    async def _seed():
        # ORM-level inserts (ReferenceChunk.embedding is mapped as
        # pgvector.sqlalchemy.Vector), not raw sa.text() with a manual
        # ::vector cast — the latter hits a real, previously-documented
        # SQLAlchemy text() parsing footgun in this codebase: a bind
        # parameter immediately followed by `::` isn't recognized as a
        # parameter at all (confirmed live: it passed ":embedding::vector"
        # through to asyncpg literally, syntax error). The ORM path
        # avoids the whole problem — no manual cast needed.
        async with tenant_session(uuid.uuid4()) as db:
            commentary_vector = await embed_text(commentary_text)
            xref_vector = await embed_text(xref_text)

            commentary_doc = ReferenceDocument(
                reference_type=ReferenceType.COMMENTARY.value, title="Test: John 3", passage_reference=commentary_ref
            )
            db.add(commentary_doc)
            await db.flush()
            db.add(
                ReferenceChunk(
                    reference_type=ReferenceType.COMMENTARY.value,
                    reference_document_id=commentary_doc.id,
                    chunk_index=0,
                    content=commentary_text,
                    embedding=commentary_vector,
                )
            )

            xref_doc = ReferenceDocument(
                reference_type=ReferenceType.CROSS_REFERENCE.value,
                title="Test xref: John 3:16",
                passage_reference=xref_ref,
            )
            db.add(xref_doc)
            await db.flush()
            db.add(
                ReferenceChunk(
                    reference_type=ReferenceType.CROSS_REFERENCE.value,
                    reference_document_id=xref_doc.id,
                    chunk_index=0,
                    content=xref_text,
                    embedding=xref_vector,
                )
            )
            await db.flush()
            return {"commentary_doc_id": str(commentary_doc.id), "xref_doc_id": str(xref_doc.id)}

    ids = asyncio.run(_seed())
    yield ids
    with pg_engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM reference_documents WHERE id = :id1 OR id = :id2"),
                     {"id1": ids["commentary_doc_id"], "id2": ids["xref_doc_id"]})


async def test_search_reference_corpus_returns_relevant_commentary(seeded_reference_corpus):
    query_vector = await embed_text("Who was Nicodemus and why did he visit Jesus at night?")
    async with tenant_session(uuid.uuid4()) as db:
        results = await search_reference_corpus(db, ReferenceType.COMMENTARY.value, query_vector, limit=3)
    assert any(r.document_id == seeded_reference_corpus["commentary_doc_id"] for r in results)


async def test_search_reference_corpus_filters_by_reference_type(seeded_reference_corpus):
    """A commentary query must not surface a cross-reference entry, even
    an unrelated one that happens to be the only other row present."""
    query_vector = await embed_text("Who was Nicodemus and why did he visit Jesus at night?")
    async with tenant_session(uuid.uuid4()) as db:
        results = await search_reference_corpus(db, ReferenceType.COMMENTARY.value, query_vector, limit=10)
    assert not any(r.document_id == seeded_reference_corpus["xref_doc_id"] for r in results)


async def test_search_reference_corpus_is_not_gated_by_tenant_context(seeded_reference_corpus):
    """The whole point of this design: two DIFFERENT, unrelated random
    tenant contexts both see the exact same reference-corpus results —
    this content is genuinely global, not scoped to whichever tenant
    happens to be asking. If this table had RLS enabled (even
    accidentally, e.g. a copy-pasted migration pattern), this would
    return [] for at least one of these, since neither tenant_id was
    ever associated with these rows (there is no tenant_id column at
    all)."""
    query_vector = await embed_text("Who was Nicodemus and why did he visit Jesus at night?")

    async with tenant_session(uuid.uuid4()) as db:
        results_a = await search_reference_corpus(db, ReferenceType.COMMENTARY.value, query_vector, limit=3)
    async with tenant_session(uuid.uuid4()) as db:
        results_b = await search_reference_corpus(db, ReferenceType.COMMENTARY.value, query_vector, limit=3)

    assert results_a
    assert [r.document_id for r in results_a] == [r.document_id for r in results_b]


async def test_reference_documents_and_chunks_have_no_tenant_id_column(pg_engine: Engine):
    """A schema-level guarantee, not just a behavioral one — confirms
    the design decision actually landed in the database, not just that
    no test happens to have triggered a leak."""
    with pg_engine.begin() as conn:
        for table in ("reference_documents", "reference_chunks"):
            columns = conn.execute(
                sa.text("SELECT column_name FROM information_schema.columns WHERE table_name = :t"), {"t": table}
            ).scalars().all()
            assert "tenant_id" not in columns, f"{table} must not have a tenant_id column"

            rls_enabled = conn.execute(
                sa.text("SELECT relrowsecurity FROM pg_class WHERE relname = :t"), {"t": table}
            ).scalar_one()
            assert rls_enabled is False, f"{table} must not have RLS enabled"
