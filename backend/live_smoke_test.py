"""Run INSIDE the backend container: exercises generate_sermon_stream for
real, against the live OpenRouter API and live bible-api.com — no mocks.
Not part of the pytest suite (costs real LLM spend); a one-off manual
verification for the Phase 3 completion report.
"""
import asyncio
import uuid

import sqlalchemy as sa

from app.core.config import get_settings
from app.db.session import sync_engine
from app.models.sermon import SermonFormat
from app.schemas.generation import GenerateRequest
from app.services.generation import generate_sermon_stream

settings = get_settings()


async def main() -> None:
    tenant_id = uuid.uuid4()
    clerk_org_id = f"org_{tenant_id.hex[:16]}"

    with sync_engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO tenants (id, slug, name, clerk_org_id, subscription_status) "
                "VALUES (:id, :slug, :name, :org, 'active')"
            ),
            {"id": str(tenant_id), "slug": f"smoke-{tenant_id.hex[:8]}", "name": "Smoke Test", "org": clerk_org_id},
        )
        conn.execute(sa.text("SELECT set_config('app.current_tenant_id', :tid, false)"), {"tid": str(tenant_id)})
        sermon_id = conn.execute(
            sa.text(
                "INSERT INTO sermons (tenant_id, title, format) "
                "VALUES (:tid, 'On Contentment', 'topical') RETURNING id"
            ),
            {"tid": str(tenant_id)},
        ).scalar_one()

    print(f"tenant_id={tenant_id} sermon_id={sermon_id}")
    print(f"outline model={settings.openrouter_outline_model} draft model={settings.openrouter_draft_model}")
    print("--- streaming events ---")

    request = GenerateRequest(passage_reference="Philippians 4:13", translation="kjv")
    full_text_chunks = []
    async for event_bytes in generate_sermon_stream(tenant_id, sermon_id, request):
        text = event_bytes.decode()
        full_text_chunks.append(text)
        # Print a trimmed line per event, not the whole draft token stream.
        first_line = text.splitlines()[0] if text.splitlines() else ""
        if first_line == "event: delta":
            print(".", end="", flush=True)
        else:
            print("\n" + text.strip()[:300])

    print("\n--- final DB state ---")
    with sync_engine.begin() as conn:
        conn.execute(sa.text("SELECT set_config('app.current_tenant_id', :tid, false)"), {"tid": str(tenant_id)})
        sermon_row = conn.execute(
            sa.text("SELECT status, length(content) as content_len FROM sermons WHERE id = :id"),
            {"id": str(sermon_id)},
        ).fetchone()
        usage_rows = conn.execute(
            sa.text(
                "SELECT event_type, quantity, generation_stage, outcome, billable "
                "FROM usage_events WHERE tenant_id = :tid ORDER BY generation_stage"
            ),
            {"tid": str(tenant_id)},
        ).fetchall()
        log_rows = conn.execute(
            sa.text("SELECT stage, model, length(raw_response) as raw_len FROM generation_logs WHERE tenant_id = :tid"),
            {"tid": str(tenant_id)},
        ).fetchall()
    print("sermon:", sermon_row)
    print("usage_events:", usage_rows)
    print("generation_logs:", log_rows)

    with sync_engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM tenants WHERE id = :id"), {"id": str(tenant_id)})
    print("\ncleaned up.")


asyncio.run(main())
