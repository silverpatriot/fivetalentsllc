"""app.services.context_assembly against a real Postgres (fixture past
sermons, ingested into the real cadence corpus via app.services.ingestion
— real chunking, real OpenRouter embeddings) and a real bible-api.com
lookup (no mocking — see test_bible_service.py for why that's safe to
depend on live). This is the required "context assembly produces a
correctly structured prompt given a real set of past sermons and a real
scripture lookup" test, now against Phase 4's RAG-based cadence matching
rather than Phase 3's plain recency query.
"""
import asyncio
import uuid

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy import Engine, select

from app.db.session import tenant_session
from app.models.document import CorpusType, DocumentSource
from app.models.sermon import Sermon, SermonFormat
from app.services.context_assembly import assemble_context, to_prompt_sections
from app.services.ingestion import ingest_text
from tests.conftest import set_tenant

try:
    _BIBLE_API_UP = httpx.get("https://bible-api.com/john+3:16", timeout=5).status_code == 200
except httpx.HTTPError:
    _BIBLE_API_UP = False

pytestmark = pytest.mark.skipif(not _BIBLE_API_UP, reason="bible-api.com not reachable from this environment")


@pytest.fixture
def tenant_with_past_sermons(pg_engine: Engine, synchronous_embedding):
    """A tenant with two past sermons, ingested for real into the
    cadence corpus (real chunking, real embeddings — this fixture calls
    the actual pipeline, not a shortcut), plus one fresh empty-content
    sermon standing in for the draft currently being generated —
    assemble_context must pull the former as examples and never the
    latter (which isn't even ingested yet)."""
    tenant_id = uuid.uuid4()
    with pg_engine.begin() as conn:
        conn.execute(
            sa.text("INSERT INTO tenants (id, slug, name, clerk_org_id) VALUES (:id, :slug, :name, :org)"),
            {
                "id": str(tenant_id),
                "slug": f"ctx-{tenant_id.hex[:8]}",
                "name": "Context Test Tenant",
                "org": f"org_{tenant_id.hex[:16]}",
            },
        )

    async def _seed():
        async with tenant_session(tenant_id) as db:
            for title, content in [
                ("On Perseverance", "Beloved, life is hard, but grace is harder still. " * 20),
                ("The Prodigal Returns", "There was a man who had two sons. " * 20),
            ]:
                sermon = Sermon(tenant_id=tenant_id, title=title, format=SermonFormat.TOPICAL, content=content)
                db.add(sermon)
                await db.flush()
                await db.refresh(sermon)
                await ingest_text(
                    db, tenant_id, corpus_type=CorpusType.CADENCE.value, source=DocumentSource.GENERATED.value,
                    title=title, text=content, sermon_id=sermon.id,
                )
            current = Sermon(tenant_id=tenant_id, title="On the Love of God", format=SermonFormat.EXPOSITORY)
            db.add(current)
            await db.flush()
            await db.refresh(current)
            return current.id

    current_id = asyncio.run(_seed())
    synchronous_embedding()  # run only after _seed()'s transaction has committed — see conftest.py

    yield tenant_id, current_id

    with pg_engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM tenants WHERE id = :id"), {"id": str(tenant_id)})


async def test_assemble_context_structures_scripture_cadence_and_format_separately(
    tenant_with_past_sermons,
):
    tenant_id, current_sermon_id = tenant_with_past_sermons

    async with tenant_session(tenant_id) as db:
        result = await db.execute(select(Sermon).where(Sermon.id == current_sermon_id))
        sermon = result.scalar_one()
        ctx = await assemble_context(db, sermon, passage_reference="John 3:16", topic=None, translation="kjv")

    # Scripture: real, verified KJV text — not fabricated, not skipped.
    assert ctx.scripture is not None
    assert "only begotten Son" in ctx.scripture.text

    # Cadence: the two ingested past sermons, via real similarity search —
    # never the current (not-yet-ingested) one, and never a third result
    # that doesn't exist.
    assert len(ctx.cadence_examples) == 2
    titles = {ex.title for ex in ctx.cadence_examples}
    assert titles == {"On Perseverance", "The Prodigal Returns"}
    # Real cosine distances (0..2 range) were actually computed, not a
    # placeholder — this is what proves it's a real similarity search.
    assert all(0 <= ex.distance <= 2 for ex in ctx.cadence_examples)

    assert ctx.format == SermonFormat.EXPOSITORY

    sections = to_prompt_sections(ctx)
    # The things Task 3 (Phase 3) asked to be kept distinguishable, not
    # concatenated into one blob.
    assert set(["scripture", "cadence_examples", "format_instructions", "web_context"]) <= sections.keys()
    assert "only begotten Son" in sections["scripture"]
    assert "Beloved, life is hard" in sections["cadence_examples"]
    assert "Prodigal" in sections["cadence_examples"] or "two sons" in sections["cadence_examples"]
    assert "verse-by-verse" in sections["format_instructions"].lower()
    # Sections must actually be separate strings, not one merged string —
    # scripture text shouldn't leak into the format-instructions section.
    assert "only begotten Son" not in sections["format_instructions"]


async def test_cadence_examples_reflect_similarity_not_just_presence(pg_engine: Engine, synchronous_embedding):
    """The actual "most-similar, not just recent" property: a sermon
    about a clearly different subject, created MORE recently than a
    topically-close one, must still rank behind it. Under the old
    recency-based query this would have failed (the newer one always
    wins); under real similarity search it must not."""
    tenant_id = uuid.uuid4()
    with pg_engine.begin() as conn:
        conn.execute(
            sa.text("INSERT INTO tenants (id, slug, name, clerk_org_id) VALUES (:id, :slug, :name, :org)"),
            {"id": str(tenant_id), "slug": f"sim-{tenant_id.hex[:8]}", "name": "Sim Tenant", "org": f"org_{tenant_id.hex[:16]}"},
        )

    async def _seed():
        async with tenant_session(tenant_id) as db:
            # Older, topically close to the new sermon's subject.
            close = Sermon(
                tenant_id=tenant_id, title="On Forgiveness", format=SermonFormat.TOPICAL,
                content="Forgiveness sets us free from bitterness and restores relationships. " * 15,
            )
            db.add(close)
            await db.flush()
            await db.refresh(close)
            await ingest_text(
                db, tenant_id, corpus_type=CorpusType.CADENCE.value, source=DocumentSource.GENERATED.value,
                title=close.title, text=close.content, sermon_id=close.id,
            )

            # Newer (inserted after, so created_at is later), but on a
            # completely unrelated subject — recency would have preferred
            # this one; similarity must not.
            unrelated = Sermon(
                tenant_id=tenant_id, title="Church Building Fund Update", format=SermonFormat.TOPICAL,
                content="This quarter's capital campaign raised funds for the new roof and parking lot. " * 15,
            )
            db.add(unrelated)
            await db.flush()
            await db.refresh(unrelated)
            await ingest_text(
                db, tenant_id, corpus_type=CorpusType.CADENCE.value, source=DocumentSource.GENERATED.value,
                title=unrelated.title, text=unrelated.content, sermon_id=unrelated.id,
            )

    async def _run():
        # Deliberately a SEPARATE tenant_session/transaction from _seed()
        # — the embedding task in between (synchronous_embedding, run
        # from the sync caller below) reads the just-ingested documents
        # over a completely different (sync) connection, which can't see
        # _seed()'s writes until that transaction has actually committed.
        # See conftest.py's synchronous_embedding docstring.
        async with tenant_session(tenant_id) as db:
            new_sermon = Sermon(tenant_id=tenant_id, title="Learning to Forgive Others", format=SermonFormat.TOPICAL)
            db.add(new_sermon)
            await db.flush()
            await db.refresh(new_sermon)
            return await assemble_context(db, new_sermon, passage_reference=None, topic="forgiveness", translation="kjv")

    try:
        await _seed()
        synchronous_embedding()
        ctx = await _run()
        assert len(ctx.cadence_examples) == 2
        # Most similar first.
        assert ctx.cadence_examples[0].title == "On Forgiveness"
        assert ctx.cadence_examples[0].distance < ctx.cadence_examples[1].distance
    finally:
        with pg_engine.begin() as conn:
            conn.execute(sa.text("DELETE FROM tenants WHERE id = :id"), {"id": str(tenant_id)})


async def test_assemble_context_with_no_past_sermons_falls_back_gracefully(pg_engine: Engine):
    tenant_id = uuid.uuid4()
    with pg_engine.begin() as conn:
        conn.execute(
            sa.text("INSERT INTO tenants (id, slug, name, clerk_org_id) VALUES (:id, :slug, :name, :org)"),
            {
                "id": str(tenant_id),
                "slug": f"empty-{tenant_id.hex[:8]}",
                "name": "Empty Tenant",
                "org": f"org_{tenant_id.hex[:16]}",
            },
        )
    try:
        async with tenant_session(tenant_id) as db:
            sermon = Sermon(tenant_id=tenant_id, title="First Sermon Ever", format=SermonFormat.TOPICAL)
            db.add(sermon)
            await db.flush()
            ctx = await assemble_context(db, sermon, passage_reference=None, topic="hope", translation="kjv")

        assert ctx.cadence_examples == []
        assert ctx.scripture is None
        sections = to_prompt_sections(ctx)
        assert "No past sermons" in sections["cadence_examples"]
        assert "topic-only" in sections["scripture"] or "No specific passage" in sections["scripture"]
    finally:
        with pg_engine.begin() as conn:
            conn.execute(sa.text("DELETE FROM tenants WHERE id = :id"), {"id": str(tenant_id)})
