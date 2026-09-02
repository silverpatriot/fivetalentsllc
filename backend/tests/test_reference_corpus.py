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


@pytest.fixture
def seeded_multi_commentary(pg_engine: Engine) -> Iterator[dict]:
    """Two DIFFERENT commentaries (source_id), sharing the same
    passage_reference — the exact scenario migration 0008's widened
    UNIQUE(reference_type, passage_reference, source_id) constraint
    exists to allow. Proves both (a) the constraint actually permits this
    (the insert would fail outright against the old 2-column constraint)
    and (b) source_id-filtered search returns only the matching one."""
    marker = uuid.uuid4().hex[:8]
    shared_ref = f"TEST-{marker}-John 3"
    henry_text = "Matthew Henry: Nicodemus came by night, cautious but sincere in his inquiry."
    clarke_text = "Adam Clarke: The Pharisee's nocturnal visit reflects both fear and genuine curiosity."

    async def _seed():
        async with tenant_session(uuid.uuid4()) as db:
            henry_vector = await embed_text(henry_text)
            clarke_vector = await embed_text(clarke_text)

            henry_doc = ReferenceDocument(
                reference_type=ReferenceType.COMMENTARY.value, title=f"Matthew Henry: {shared_ref}",
                passage_reference=shared_ref, source_id="matthew-henry",
            )
            db.add(henry_doc)
            await db.flush()
            db.add(
                ReferenceChunk(
                    reference_type=ReferenceType.COMMENTARY.value, reference_document_id=henry_doc.id,
                    chunk_index=0, content=henry_text, embedding=henry_vector,
                )
            )

            clarke_doc = ReferenceDocument(
                reference_type=ReferenceType.COMMENTARY.value, title=f"Adam Clarke: {shared_ref}",
                passage_reference=shared_ref, source_id="adam-clarke",
            )
            db.add(clarke_doc)
            await db.flush()
            db.add(
                ReferenceChunk(
                    reference_type=ReferenceType.COMMENTARY.value, reference_document_id=clarke_doc.id,
                    chunk_index=0, content=clarke_text, embedding=clarke_vector,
                )
            )
            await db.flush()
            return {
                "henry_doc_id": str(henry_doc.id),
                "clarke_doc_id": str(clarke_doc.id),
                "henry_text": henry_text,
                "clarke_text": clarke_text,
            }

    ids = asyncio.run(_seed())
    yield ids
    with pg_engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM reference_documents WHERE id = :id1 OR id = :id2"),
                     {"id1": ids["henry_doc_id"], "id2": ids["clarke_doc_id"]})


async def test_two_commentaries_can_share_a_passage_reference(seeded_multi_commentary):
    """The fixture itself succeeding (no unique-constraint violation on
    insert) is the assertion that matters most here — this would raise
    on the OLD 2-column constraint. Also confirm both rows are genuinely
    there under their own distinct source_id.

    Queries with each row's OWN seeded text, not a generic paraphrase —
    guarantees ~0 cosine distance to itself and so a top rank regardless
    of how large or topically adjacent the rest of the corpus gets. A
    shared generic query ("Nicodemus visiting at night") used to do this
    job; it broke the moment this environment ingested real Matthew
    Henry/Adam Clarke commentary on John 3 (the actual Nicodemus
    passage) at full scale — real, more specific content legitimately
    outranked this fixture's one-liner, which is the retrieval working
    correctly, not a bug to route around with a wider search."""
    async with tenant_session(uuid.uuid4()) as db:
        henry_results = await search_reference_corpus(
            db, ReferenceType.COMMENTARY.value, await embed_text(seeded_multi_commentary["henry_text"]), limit=10,
            source_id="matthew-henry",
        )
        clarke_results = await search_reference_corpus(
            db, ReferenceType.COMMENTARY.value, await embed_text(seeded_multi_commentary["clarke_text"]), limit=10,
            source_id="adam-clarke",
        )
    assert any(r.document_id == seeded_multi_commentary["henry_doc_id"] for r in henry_results)
    assert any(r.document_id == seeded_multi_commentary["clarke_doc_id"] for r in clarke_results)


async def test_search_reference_corpus_source_id_filter_excludes_other_sources(seeded_multi_commentary):
    query_vector = await embed_text("Nicodemus visiting at night")
    async with tenant_session(uuid.uuid4()) as db:
        henry_only = await search_reference_corpus(
            db, ReferenceType.COMMENTARY.value, query_vector, limit=10, source_id="matthew-henry"
        )
    assert not any(r.document_id == seeded_multi_commentary["clarke_doc_id"] for r in henry_only)
    assert all(r.source_id == "matthew-henry" for r in henry_only if r.document_id == seeded_multi_commentary["henry_doc_id"])


async def test_search_reference_corpus_no_source_id_filter_returns_both(seeded_multi_commentary):
    """source_id=None (the default) — no filter applied — must return
    both, matching pre-0008 unfiltered behavior.

    Two separate self-matching queries, not one shared generic phrase —
    see test_two_commentaries_can_share_a_passage_reference's docstring
    for why a generic query stopped being reliable once this environment
    ingested the real, full-scale baseline corpus (real commentary on
    the actual Nicodemus passage now legitimately outranks a synthetic
    one-liner for anything short of an enormous, slow ANN search). Each
    query still runs with source_id unset, so this still exercises the
    real thing under test — that leaving source_id off doesn't drop
    either source from the corpus, not that one shared query happens to
    surface both at once."""
    async with tenant_session(uuid.uuid4()) as db:
        henry_side = await search_reference_corpus(
            db, ReferenceType.COMMENTARY.value, await embed_text(seeded_multi_commentary["henry_text"]), limit=10
        )
        clarke_side = await search_reference_corpus(
            db, ReferenceType.COMMENTARY.value, await embed_text(seeded_multi_commentary["clarke_text"]), limit=10
        )
    assert any(r.document_id == seeded_multi_commentary["henry_doc_id"] for r in henry_side)
    assert any(r.document_id == seeded_multi_commentary["clarke_doc_id"] for r in clarke_side)


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
