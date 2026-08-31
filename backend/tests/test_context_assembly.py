"""app.services.context_assembly against a real Postgres (fixture past
sermons, inserted directly like every other RLS test in this suite) and a
real bible-api.com lookup (no mocking — see test_bible_service.py for why
that's safe to depend on live). This is the required "context assembly
produces a correctly structured prompt given a mocked/fixture set of past
sermons and a real scripture lookup" test from the Phase 3 spec.
"""
import uuid

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy import Engine, select

from app.db.session import tenant_session
from app.models.sermon import Sermon, SermonFormat
from app.services.context_assembly import assemble_context, to_prompt_sections
from tests.conftest import set_tenant

try:
    _BIBLE_API_UP = httpx.get("https://bible-api.com/john+3:16", timeout=5).status_code == 200
except httpx.HTTPError:
    _BIBLE_API_UP = False

pytestmark = pytest.mark.skipif(not _BIBLE_API_UP, reason="bible-api.com not reachable from this environment")


@pytest.fixture
def tenant_with_past_sermons(pg_engine: Engine):
    """A tenant, two past sermons with real content (the cadence-example
    fixture set), and one fresh empty-content sermon standing in for the
    draft currently being generated — assemble_context must pull the
    former as examples and never the latter."""
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
    with pg_engine.begin() as conn:
        set_tenant(conn, tenant_id)
        conn.execute(
            sa.text(
                "INSERT INTO sermons (tenant_id, title, format, content) "
                "VALUES (:tid, :title, :fmt, :content)"
            ),
            [
                {
                    "tid": str(tenant_id),
                    "title": "On Perseverance",
                    "fmt": "topical",
                    "content": "Beloved, life is hard, but grace is harder still. " * 20,
                },
                {
                    "tid": str(tenant_id),
                    "title": "The Prodigal Returns",
                    "fmt": "narrative",
                    "content": "There was a man who had two sons. " * 20,
                },
            ],
        )
        current = conn.execute(
            sa.text(
                "INSERT INTO sermons (tenant_id, title, format) "
                "VALUES (:tid, 'On the Love of God', 'expository') RETURNING id"
            ),
            {"tid": str(tenant_id)},
        ).scalar_one()

    yield tenant_id, current

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

    # Cadence: exactly the two past sermons with content, never the
    # current (empty-content) one, and never "most recent 1" only.
    assert len(ctx.cadence_examples) == 2
    titles = {ex.title for ex in ctx.cadence_examples}
    assert titles == {"On Perseverance", "The Prodigal Returns"}

    assert ctx.format == SermonFormat.EXPOSITORY

    sections = to_prompt_sections(ctx)
    # The things Task 3 asked to be kept distinguishable, not concatenated
    # into one blob (web_context added when TAVILY_API_KEY support was
    # folded into context assembly).
    assert set(["scripture", "cadence_examples", "format_instructions", "web_context"]) <= sections.keys()
    assert "only begotten Son" in sections["scripture"]
    assert "Beloved, life is hard" in sections["cadence_examples"]
    assert "Prodigal" in sections["cadence_examples"] or "two sons" in sections["cadence_examples"]
    assert "verse-by-verse" in sections["format_instructions"].lower()
    # Sections must actually be separate strings, not one merged string —
    # scripture text shouldn't leak into the format-instructions section.
    assert "only begotten Son" not in sections["format_instructions"]


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
