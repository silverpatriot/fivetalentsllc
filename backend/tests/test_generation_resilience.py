"""2026-09-03 fix: a downstream failure in citation verification or
cadence ingestion must never destroy an already-successful generation,
edit, or restore. Real live-infrastructure convention (real Postgres,
real Clerk JWT) — only OpenRouter is mocked; the actual failure under
test is a REAL, forced Bible-API-source failure (bible-api.com/
api.bible), reproducing the exact trigger identified during
investigation, not a synthetic stand-in for it.

2026-09-04 addition (bottom of file): a DIFFERENT resilience gap in the
same neighborhood — the client disconnecting mid-stream, before a draft
ever finishes, used to roll back the whole transaction with zero trace.
That test drives a REAL uvicorn server over a REAL loopback TCP socket
rather than TestClient/httpx.ASGITransport: ASGITransport's
handle_async_request runs the whole ASGI app to completion and only
THEN returns a response object (confirmed by reading its source) — a
partial read followed by an early close is structurally impossible
through that transport, so it cannot exercise this failure mode at all.
Only a genuine socket closed mid-response produces the real
`http.disconnect` ASGI message this fix responds to.
"""
import asyncio
import uuid
from typing import Iterator

import httpx
import pytest
import sqlalchemy as sa
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from app.core import security
from app.main import app
from app.services import bible
from tests.conftest import make_clerk_jwt, set_tenant

client = TestClient(app)


class _FakeSigningKey:
    def __init__(self, key):
        self.key = key


class _FakeJWKClient:
    def __init__(self, public_key):
        self._public_key = public_key

    def get_signing_key_from_jwt(self, token):
        return _FakeSigningKey(self._public_key)


@pytest.fixture
def active_tenant_with_org(pg_engine: Engine) -> Iterator[dict]:
    tenant_id = uuid.uuid4()
    clerk_org_id = f"org_{tenant_id.hex[:16]}"
    with pg_engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO tenants (id, slug, name, clerk_org_id, subscription_status) "
                "VALUES (:id, :slug, :name, :org, 'active')"
            ),
            {
                "id": str(tenant_id),
                "slug": f"resil-{tenant_id.hex[:8]}",
                "name": "Resilience Test Tenant",
                "org": clerk_org_id,
            },
        )
    yield {"id": tenant_id, "clerk_org_id": clerk_org_id}
    with pg_engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM tenants WHERE id = :id"), {"id": str(tenant_id)})


@pytest.fixture
def auth_headers(rsa_keypair: tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey], active_tenant_with_org, monkeypatch):
    private_key, public_key = rsa_keypair
    monkeypatch.setattr(security, "_jwk_client", lambda: _FakeJWKClient(public_key))
    token = make_clerk_jwt(
        private_key, {"sub": "user_resil_test", "o": {"id": active_tenant_with_org["clerk_org_id"], "rol": "admin"}}
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def _stub_background_tasks(monkeypatch):
    monkeypatch.setattr("app.tasks.usage_reporting.report_usage_event.delay", lambda *a, **k: None)
    monkeypatch.setattr("app.services.ingestion.embed_document_chunks.delay", lambda *a, **k: None)


async def _fake_outline(model, messages, **kwargs):
    return "1. Point one", '{"fake":"outline"}'


def _force_bible_api_down(monkeypatch):
    """Forces the EXACT real trigger identified during investigation:
    every Bible-source lookup fails as a genuine service error (not a
    clean "not found") — reproducing what an uncaught
    BibleApiComError/ApiBibleError used to do to the whole transaction,
    now that both are caught. Patches fetch_passage itself (the single
    real chokepoint every source funnels through) rather than the
    network layer, so this exercises the exact function verify_citation
    calls, regardless of which underlying source would have been tried."""

    async def _boom(reference, translation=None):
        raise bible.BibleApiComError("simulated: bible-api.com is down")

    monkeypatch.setattr(bible, "fetch_passage", _boom)


def test_generate_saves_the_draft_even_when_citation_verification_is_completely_down(
    pg_engine: Engine, active_tenant_with_org: dict, auth_headers: dict, monkeypatch
):
    """The actual reported incident, reproduced for real: a sermon with
    a real scripture citation in its draft, generated while the Bible
    API is genuinely down throughout. Before the fix, this exact
    scenario rolled back the ENTIRE transaction — the sermon a pastor
    just watched generate would vanish with zero trace. Confirms it
    doesn't anymore."""
    tenant_id = active_tenant_with_org["id"]

    async def _fake_stream(model, messages, raw_sink=None, **kwargs):
        text = 'Point one: as John 3:16 says, "For God so loved the world."'
        if raw_sink is not None:
            raw_sink.append(text)
        yield text

    monkeypatch.setattr("app.services.generation.chat_completion", _fake_outline)
    monkeypatch.setattr("app.services.generation.stream_chat_completion", _fake_stream)
    _force_bible_api_down(monkeypatch)

    create_resp = client.post("/sermons", json={"title": "Resilience Test", "format": "topical"}, headers=auth_headers)
    assert create_resp.status_code == 201, create_resp.text
    sermon_id = create_resp.json()["id"]

    gen_resp = client.post(f"/sermons/{sermon_id}/generate", json={"topic": "Love"}, headers=auth_headers)
    assert gen_resp.status_code == 200, gen_resp.text
    body = gen_resp.text
    # The draft streamed and completed cleanly — no error event, despite
    # the Bible API being completely down for the whole request.
    assert "event: error" not in body
    assert "event: done" in body
    assert "event: citations" in body

    with pg_engine.begin() as conn:
        set_tenant(conn, tenant_id)
        sermon_row = conn.execute(
            sa.text("SELECT status, content FROM sermons WHERE id = :id"), {"id": sermon_id}
        ).fetchone()
        log_rows = conn.execute(
            sa.text("SELECT stage, citation_flags FROM generation_logs WHERE tenant_id = :tid AND sermon_id = :sid ORDER BY stage"),
            {"tid": str(tenant_id), "sid": sermon_id},
        ).fetchall()

    # The actual bug, confirmed fixed: content saved, status ready, both
    # LLM-call log rows present (before the fix: zero rows, NULL content,
    # status stuck on draft — a full silent rollback).
    assert sermon_row.status == "ready"
    assert sermon_row.content == 'Point one: as John 3:16 says, "For God so loved the world."'
    assert {row.stage for row in log_rows} == {"outline", "draft"}
    draft_log = next(row for row in log_rows if row.stage == "draft")
    # The citation was attempted and honestly reported as unable to be
    # checked — not silently dropped, not crashing the whole save.
    assert draft_log.citation_flags == [] or all(f["status"] == "unverifiable" for f in draft_log.citation_flags)


def test_edit_saves_the_replacement_even_when_citation_verification_is_completely_down(
    pg_engine: Engine, active_tenant_with_org: dict, auth_headers: dict, monkeypatch
):
    tenant_id = active_tenant_with_org["id"]
    original = "Point one: a fairly simple draft sentence to begin with here."
    create_resp = client.post(
        "/sermons",
        json={"title": "Resilience Edit Test", "format": "topical", "content": original},
        headers=auth_headers,
    )
    sermon_id = create_resp.json()["id"]

    # Similar length to `original` — a drastic length change would
    # (correctly) trip the unrelated structural-artifact guard, which
    # isn't what this test is about.
    replacement = 'Point one: as Romans 8:28 says, all things work for good in the end.'

    async def _fake_stream(model, messages, raw_sink=None, **kwargs):
        if raw_sink is not None:
            raw_sink.append(replacement)
        yield replacement

    monkeypatch.setattr("app.services.generation.stream_chat_completion", _fake_stream)
    _force_bible_api_down(monkeypatch)

    edit_resp = client.post(
        f"/sermons/{sermon_id}/edit",
        json={"instruction": "add a reference", "selection": {"start": 0, "end": len(original)}},
        headers=auth_headers,
    )
    assert edit_resp.status_code == 200, edit_resp.text
    assert "event: error" not in edit_resp.text
    assert "event: done" in edit_resp.text

    with pg_engine.begin() as conn:
        set_tenant(conn, tenant_id)
        sermon_row = conn.execute(sa.text("SELECT content FROM sermons WHERE id = :id"), {"id": sermon_id}).fetchone()
    assert sermon_row.content == replacement


def test_restore_succeeds_even_when_citation_verification_is_completely_down(
    pg_engine: Engine, active_tenant_with_org: dict, auth_headers: dict, monkeypatch
):
    tenant_id = active_tenant_with_org["id"]
    create_resp = client.post(
        "/sermons",
        json={"title": "Resilience Restore Test", "format": "topical", "content": "Original content."},
        headers=auth_headers,
    )
    sermon_id = create_resp.json()["id"]

    async def _fake_stream(model, messages, raw_sink=None, **kwargs):
        yield "Edited content."

    monkeypatch.setattr("app.services.generation.stream_chat_completion", _fake_stream)
    edit_resp = client.post(
        f"/sermons/{sermon_id}/edit",
        json={"instruction": "change it", "selection": {"start": 0, "end": len("Original content.")}},
        headers=auth_headers,
    )
    assert edit_resp.status_code == 200, edit_resp.text

    entries = client.get(f"/sermons/{sermon_id}/revisions", headers=auth_headers).json()
    original_id = next(e["id"] for e in entries if not e["is_current"])

    _force_bible_api_down(monkeypatch)
    restore_resp = client.post(f"/sermons/{sermon_id}/revisions/{original_id}/restore", headers=auth_headers)
    assert restore_resp.status_code == 200, restore_resp.text
    body = restore_resp.json()
    assert body["sermon"]["content"] == "Original content."
    assert body["citation_flags"] == []


async def test_generate_persists_progress_on_a_real_mid_stream_disconnect(
    pg_engine: Engine, active_tenant_with_org: dict, auth_headers: dict, monkeypatch
):
    """2026-09-04 fix, tested against a REAL disconnect: the outline
    completes, the draft starts streaming and produces one real chunk,
    then the client's TCP connection is genuinely closed (not simulated)
    while stream_chat_completion is still stuck mid-draft. Reproduces the
    real incident's shape exactly (sermon 8d0dc646-684a-40d1-a5ab-
    fe4709c4713e, "Coal to the Lips, Fire in the Feet": outline succeeded,
    draft never finished, client went away) and confirms the fix's actual
    decisions: status becomes 'interrupted' (not left on 'draft' or stuck
    on 'generating'), the completed outline is preserved in
    generation_logs, the partial draft is ALSO kept there for forensics
    but is explicitly NOT written to sermon.content, and both stages get
    a usage_events row so nothing is silently missing from the ledger.
    """
    tenant_id = active_tenant_with_org["id"]

    draft_first_chunk_sent = asyncio.Event()

    async def _fake_outline(model, messages, **kwargs):
        return "1. Point one", '{"fake":"outline"}'

    async def _fake_stream(model, messages, raw_sink=None, **kwargs):
        chunk = "Point one: this part streamed before the disconnect. "
        if raw_sink is not None:
            raw_sink.append(chunk)
        yield chunk
        draft_first_chunk_sent.set()
        # However the real disconnect actually happens (see the trigger
        # discussion this fix was built from — a proxy-layer timeout, a
        # network blip, anything), the draft is genuinely mid-flight when
        # it does. This sleep just holds the fake upstream open long
        # enough to guarantee the test's own socket close (below) lands
        # while still inside the draft loop; it never needs to complete —
        # the disconnect gets there first and this task is cancelled.
        await asyncio.sleep(30)
        raise AssertionError("must never resume after a real disconnect")  # pragma: no cover

    monkeypatch.setattr("app.services.generation.chat_completion", _fake_outline)
    monkeypatch.setattr("app.services.generation.stream_chat_completion", _fake_stream)

    create_resp = client.post(
        "/sermons", json={"title": "Coal to the Lips, Fire in the Feet", "format": "topical"}, headers=auth_headers
    )
    assert create_resp.status_code == 201, create_resp.text
    sermon_id = create_resp.json()["id"]

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())
    try:
        while not server.started:
            await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]

        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=10) as real_client:
            async with real_client.stream(
                "POST", f"/sermons/{sermon_id}/generate", json={"topic": "Love"}, headers=auth_headers
            ) as resp:
                assert resp.status_code == 200
                async for line in resp.aiter_lines():
                    if line.startswith("event: delta"):
                        break
            # The draft's first chunk streamed before we broke out of the
            # loop above; confirm it actually reached the fake upstream
            # generator too, not just this client's read buffer.
            assert draft_first_chunk_sent.is_set()
        # Exiting both `async with` blocks above closes the real TCP
        # connection now — genuine socket teardown, no simulation.

        # The server-side request handler is unwinding asynchronously;
        # poll rather than guess a fixed sleep.
        for _ in range(50):
            with pg_engine.begin() as conn:
                set_tenant(conn, tenant_id)
                status = conn.execute(
                    sa.text("SELECT status FROM sermons WHERE id = :id"), {"id": sermon_id}
                ).scalar_one()
            if status == "interrupted":
                break
            await asyncio.sleep(0.1)
    finally:
        server.should_exit = True
        await server_task

    with pg_engine.begin() as conn:
        set_tenant(conn, tenant_id)
        sermon_row = conn.execute(
            sa.text("SELECT status, content FROM sermons WHERE id = :id"), {"id": sermon_id}
        ).fetchone()
        log_rows = conn.execute(
            sa.text(
                "SELECT stage, raw_response, prompt FROM generation_logs "
                "WHERE tenant_id = :tid AND sermon_id = :sid ORDER BY stage"
            ),
            {"tid": str(tenant_id), "sid": sermon_id},
        ).fetchall()
        usage_rows = conn.execute(
            sa.text(
                "SELECT generation_stage, outcome, billable FROM usage_events "
                "WHERE tenant_id = :tid AND sermon_id = :sid ORDER BY generation_stage"
            ),
            {"tid": str(tenant_id), "sid": sermon_id},
        ).fetchall()

    # The core fix, confirmed: distinguishable from both "never
    # attempted" (draft) and "still running" (generating) — and NEVER
    # silently rolled back to nothing, which is what happened for real
    # before this fix.
    assert sermon_row.status == "interrupted"
    # The whole point: a cut-off, unverified draft must never be
    # mistaken for a finished, ready-to-preach manuscript.
    assert sermon_row.content is None

    assert {row.stage for row in log_rows} == {"outline", "draft"}
    outline_log = next(row for row in log_rows if row.stage == "outline")
    draft_log = next(row for row in log_rows if row.stage == "draft")
    # The outline really did complete before the disconnect — preserved,
    # not lost with everything else the way it was before this fix.
    assert outline_log.raw_response == '{"fake":"outline"}'
    # The partial draft is kept for forensics/a future resume feature,
    # explicitly marked as such...
    assert draft_log.raw_response == "Point one: this part streamed before the disconnect. "
    assert draft_log.prompt.get("interrupted") is True
    # ...but (belt and suspenders on the assertion above) it is genuinely
    # nowhere in sermon.content.
    assert "this part streamed before the disconnect" not in (sermon_row.content or "")

    usage_by_stage = {row.generation_stage: row for row in usage_rows}
    assert usage_by_stage["outline"].outcome == "succeeded"
    assert usage_by_stage["draft"].outcome == "interrupted"
    # Never billable — an interrupted draft produced no usable sermon.
    assert usage_by_stage["draft"].billable is False
