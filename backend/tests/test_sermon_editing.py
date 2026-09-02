"""Phase 6: iterative draft editing. Same live-infrastructure convention
as test_generation_usage.py — real Postgres, a real (self-signed) Clerk
JWT, real citation verification against bible-api.com; only the
OpenRouter calls themselves are mocked. Proves, specifically:

- section-scoped edits (both selection-based and locate-based) splice
  correctly, leaving the rest of the manuscript byte-for-byte untouched
  — the actual safety property Task 1.1 chose over full-draft rewrite.
- an edit introducing a hallucinated citation gets caught by the SAME
  verification pipeline original generation uses, not skipped or a
  weaker path (Task 1.2's explicit testing requirement).
- a locate span that can't be found in the real content is a clean,
  reported error rather than a silent edit against the wrong text.
- usage_events are recorded per real LLM call, non-billable, same
  discipline as every other stage (Task 1.3).
- the per-sermon edit cap hard-blocks before any LLM call is spent.
- sermon_revisions preserves the pre-edit content (Task 2's minimum
  viable recoverability).
"""
import uuid
from typing import Iterator

import pytest
import sqlalchemy as sa
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from app.core import security
from app.main import app
from app.services.plan_limits import MAX_EDITS_PER_SERMON
from tests.conftest import make_clerk_jwt, set_tenant

client = TestClient(app)

PARAGRAPH_1 = "Point one: God is faithful even in trial."
PARAGRAPH_2 = "Point two: we must trust in His timing, even when it is slow."
PARAGRAPH_3 = "Point three: community sustains us through hardship."
MANUSCRIPT = f"{PARAGRAPH_1}\n\n{PARAGRAPH_2}\n\n{PARAGRAPH_3}"


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
                "slug": f"edit-{tenant_id.hex[:8]}",
                "name": "Editing Test Tenant",
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
        private_key, {"sub": "user_edit_test", "o": {"id": active_tenant_with_org["clerk_org_id"], "rol": "admin"}}
    )
    return {"Authorization": f"Bearer {token}"}


def _create_sermon_with_content(auth_headers: dict, content: str = MANUSCRIPT) -> str:
    resp = client.post(
        "/sermons",
        json={"title": "Faithfulness in Trial", "format": "topical", "content": content},
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_edit_with_selection_splices_and_leaves_rest_untouched(
    pg_engine: Engine, active_tenant_with_org: dict, auth_headers: dict, monkeypatch
):
    tenant_id = active_tenant_with_org["id"]
    sermon_id = _create_sermon_with_content(auth_headers)
    start = MANUSCRIPT.index(PARAGRAPH_2)
    end = start + len(PARAGRAPH_2)

    replacement = "Point two, personally: I have trusted in His timing through my own hardest season."

    async def _fake_stream(model, messages, raw_sink=None):
        for chunk in [replacement[:20], replacement[20:]]:
            if raw_sink is not None:
                raw_sink.append(chunk)
            yield chunk

    monkeypatch.setattr("app.services.generation.stream_chat_completion", _fake_stream)
    monkeypatch.setattr("app.tasks.usage_reporting.report_usage_event.delay", lambda *a, **k: None)

    resp = client.post(
        f"/sermons/{sermon_id}/edit",
        json={"instruction": "make point 2 more personal", "selection": {"start": start, "end": end}},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.text
    assert "event: target" in body
    assert "event: delta" in body
    assert "event: citations" in body
    assert "event: done" in body
    assert "event: error" not in body

    with pg_engine.begin() as conn:
        set_tenant(conn, tenant_id)
        sermon_row = conn.execute(sa.text("SELECT content FROM sermons WHERE id = :id"), {"id": sermon_id}).fetchone()

    expected = f"{PARAGRAPH_1}\n\n{replacement}\n\n{PARAGRAPH_3}"
    assert sermon_row.content == expected
    # The actual safety property: paragraphs 1 and 3 are byte-for-byte
    # present, unchanged — not just "similar enough".
    assert PARAGRAPH_1 in sermon_row.content
    assert PARAGRAPH_3 in sermon_row.content
    assert PARAGRAPH_2 not in sermon_row.content  # the targeted span really was replaced


def test_edit_without_selection_uses_locate_then_splices(
    pg_engine: Engine, active_tenant_with_org: dict, auth_headers: dict, monkeypatch
):
    tenant_id = active_tenant_with_org["id"]
    sermon_id = _create_sermon_with_content(auth_headers)
    replacement = "Point three, expanded: real community means showing up, not just showing sympathy."

    async def _fake_locate(model, messages):
        return f"<<<TARGET>>>\n{PARAGRAPH_3}\n<<<END_TARGET>>>", '{"fake":"locate"}'

    async def _fake_stream(model, messages, raw_sink=None):
        for chunk in [replacement[:20], replacement[20:]]:
            if raw_sink is not None:
                raw_sink.append(chunk)
            yield chunk

    monkeypatch.setattr("app.services.generation.chat_completion", _fake_locate)
    monkeypatch.setattr("app.services.generation.stream_chat_completion", _fake_stream)
    monkeypatch.setattr("app.tasks.usage_reporting.report_usage_event.delay", lambda *a, **k: None)

    resp = client.post(
        f"/sermons/{sermon_id}/edit",
        json={"instruction": "expand on point three"},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    assert "event: target" in resp.text
    assert "event: error" not in resp.text

    with pg_engine.begin() as conn:
        set_tenant(conn, tenant_id)
        sermon_row = conn.execute(sa.text("SELECT content FROM sermons WHERE id = :id"), {"id": sermon_id}).fetchone()
        log_rows = conn.execute(
            sa.text("SELECT stage FROM generation_logs WHERE tenant_id = :tid AND sermon_id = :sid"),
            {"tid": str(tenant_id), "sid": sermon_id},
        ).fetchall()

    expected = f"{PARAGRAPH_1}\n\n{PARAGRAPH_2}\n\n{replacement}"
    assert sermon_row.content == expected
    assert {row.stage for row in log_rows} == {"edit_locate", "edit"}


def test_edit_locate_span_not_found_is_a_clean_error_not_a_silent_edit(
    pg_engine: Engine, active_tenant_with_org: dict, auth_headers: dict, monkeypatch
):
    """The locate model claiming a span that doesn't actually exist in
    the manuscript (paraphrased instead of quoting verbatim) must fail
    loudly — never fall back to editing the wrong text, or all of it."""
    tenant_id = active_tenant_with_org["id"]
    sermon_id = _create_sermon_with_content(auth_headers)

    async def _fake_locate(model, messages):
        return "<<<TARGET>>>\nThis sentence does not appear anywhere in the manuscript.\n<<<END_TARGET>>>", "{}"

    edit_call_count = {"n": 0}

    async def _fail_if_called(model, messages, raw_sink=None):
        edit_call_count["n"] += 1
        raise AssertionError("the edit/rewrite call must never run when the target span wasn't found")
        yield  # pragma: no cover - unreachable, keeps this an async generator

    monkeypatch.setattr("app.services.generation.chat_completion", _fake_locate)
    monkeypatch.setattr("app.services.generation.stream_chat_completion", _fail_if_called)
    monkeypatch.setattr("app.tasks.usage_reporting.report_usage_event.delay", lambda *a, **k: None)

    resp = client.post(
        f"/sermons/{sermon_id}/edit",
        json={"instruction": "change the part about hardship"},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    assert "event: error" in resp.text
    assert "event: target" not in resp.text
    assert edit_call_count["n"] == 0

    with pg_engine.begin() as conn:
        set_tenant(conn, tenant_id)
        sermon_row = conn.execute(
            sa.text("SELECT content, status FROM sermons WHERE id = :id"), {"id": sermon_id}
        ).fetchone()
        usage_rows = conn.execute(
            sa.text("SELECT generation_stage, outcome FROM usage_events WHERE tenant_id = :tid AND sermon_id = :sid"),
            {"tid": str(tenant_id), "sid": sermon_id},
        ).fetchall()

    # Content is completely untouched, and the sermon is back to a normal
    # state — not stuck mid-generation.
    assert sermon_row.content == MANUSCRIPT
    assert sermon_row.status == "ready"
    # The locate LLM call itself succeeded (it got a response) — the
    # failure is that the response didn't match real content, a separate,
    # application-level failure mode from an OpenRouter error.
    assert len(usage_rows) == 1
    assert usage_rows[0].generation_stage == "edit_locate"
    assert usage_rows[0].outcome == "succeeded"


def test_edit_introducing_a_hallucinated_citation_is_caught_by_verification(
    pg_engine: Engine, active_tenant_with_org: dict, auth_headers: dict, monkeypatch
):
    """The actual required proof from the Phase 6 spec: an edit that
    introduces a new, invented scripture reference must be caught by the
    SAME verification pipeline original generation uses — not skipped."""
    tenant_id = active_tenant_with_org["id"]
    sermon_id = _create_sermon_with_content(auth_headers)
    start = MANUSCRIPT.index(PARAGRAPH_2)
    end = start + len(PARAGRAPH_2)

    # "Zorblatt" matches the citation-reference shape (Capitalized Word +
    # chapter:verse) but is not a real book of the Bible — fetch_passage
    # will fail to resolve it against the real bible-api.com source,
    # exactly the "hallucinated reference" case verify_citation exists
    # to catch.
    replacement = "Point two, expanded: as it is written in Zorblatt 3:15, timing is never wasted."

    async def _fake_stream(model, messages, raw_sink=None):
        yield replacement

    monkeypatch.setattr("app.services.generation.stream_chat_completion", _fake_stream)
    monkeypatch.setattr("app.tasks.usage_reporting.report_usage_event.delay", lambda *a, **k: None)

    resp = client.post(
        f"/sermons/{sermon_id}/edit",
        json={"instruction": "add a supporting reference to point 2", "selection": {"start": start, "end": end}},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text

    with pg_engine.begin() as conn:
        set_tenant(conn, tenant_id)
        log_row = conn.execute(
            sa.text(
                "SELECT citation_flags FROM generation_logs WHERE tenant_id = :tid AND sermon_id = :sid "
                "AND stage = 'edit'"
            ),
            {"tid": str(tenant_id), "sid": sermon_id},
        ).fetchone()

    assert log_row is not None
    flags = log_row.citation_flags
    zorblatt = next(f for f in flags if f["reference"] == "Zorblatt 3:15")
    assert zorblatt["status"] == "invalid_reference"
    assert "done" in resp.text.split("event: ")[-1] or "flagged_citation_count" in resp.text


def test_edit_records_non_billable_usage_event(
    active_tenant_with_org: dict, auth_headers: dict, monkeypatch, pg_engine: Engine
):
    tenant_id = active_tenant_with_org["id"]
    sermon_id = _create_sermon_with_content(auth_headers)
    start = MANUSCRIPT.index(PARAGRAPH_1)
    end = start + len(PARAGRAPH_1)

    async def _fake_stream(model, messages, raw_sink=None):
        yield "Point one, revised."

    monkeypatch.setattr("app.services.generation.stream_chat_completion", _fake_stream)
    monkeypatch.setattr("app.tasks.usage_reporting.report_usage_event.delay", lambda *a, **k: None)

    resp = client.post(
        f"/sermons/{sermon_id}/edit",
        json={"instruction": "tighten point 1", "selection": {"start": start, "end": end}},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text

    with pg_engine.begin() as conn:
        set_tenant(conn, tenant_id)
        rows = conn.execute(
            sa.text(
                "SELECT event_type, quantity, outcome, billable FROM usage_events "
                "WHERE tenant_id = :tid AND sermon_id = :sid AND generation_stage = 'edit'"
            ),
            {"tid": str(tenant_id), "sid": sermon_id},
        ).fetchall()

    assert len(rows) == 1
    row = rows[0]
    assert row.event_type == "ai_generation"
    assert float(row.quantity) == 1.0
    assert row.outcome == "succeeded"
    assert row.billable is False  # Task 1.3: free follow-up, same as outline_condense


def test_edit_snapshots_pre_edit_content_into_sermon_revisions(
    active_tenant_with_org: dict, auth_headers: dict, monkeypatch, pg_engine: Engine
):
    tenant_id = active_tenant_with_org["id"]
    sermon_id = _create_sermon_with_content(auth_headers)
    start = MANUSCRIPT.index(PARAGRAPH_1)
    end = start + len(PARAGRAPH_1)
    instruction = "tighten point 1"

    async def _fake_stream(model, messages, raw_sink=None):
        yield "Point one, revised."

    monkeypatch.setattr("app.services.generation.stream_chat_completion", _fake_stream)
    monkeypatch.setattr("app.tasks.usage_reporting.report_usage_event.delay", lambda *a, **k: None)

    resp = client.post(
        f"/sermons/{sermon_id}/edit",
        json={"instruction": instruction, "selection": {"start": start, "end": end}},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text

    with pg_engine.begin() as conn:
        set_tenant(conn, tenant_id)
        rows = conn.execute(
            sa.text("SELECT content, instruction FROM sermon_revisions WHERE tenant_id = :tid AND sermon_id = :sid"),
            {"tid": str(tenant_id), "sid": sermon_id},
        ).fetchall()

    assert len(rows) == 1
    assert rows[0].content == MANUSCRIPT  # the state BEFORE this edit, fully recoverable
    assert rows[0].instruction == instruction


def test_edit_cap_blocks_before_any_llm_call_once_exhausted(
    active_tenant_with_org: dict, auth_headers: dict, monkeypatch, pg_engine: Engine
):
    tenant_id = active_tenant_with_org["id"]
    sermon_id = _create_sermon_with_content(auth_headers)

    with pg_engine.begin() as conn:
        set_tenant(conn, tenant_id)
        for _ in range(MAX_EDITS_PER_SERMON):
            conn.execute(
                sa.text(
                    "INSERT INTO usage_events (tenant_id, event_type, quantity, sermon_id, "
                    "generation_stage, outcome, billable) VALUES "
                    "(:tid, 'ai_generation', 1.0, :sid, 'edit', 'succeeded', false)"
                ),
                {"tid": str(tenant_id), "sid": sermon_id},
            )

    def _fail_if_called(*a, **k):
        raise AssertionError("no LLM call should happen once the edit cap is already exhausted")

    monkeypatch.setattr("app.services.generation.chat_completion", _fail_if_called)
    monkeypatch.setattr("app.services.generation.stream_chat_completion", _fail_if_called)

    resp = client.post(
        f"/sermons/{sermon_id}/edit",
        json={"instruction": "one more small tweak"},
        headers=auth_headers,
    )
    assert resp.status_code == 429, resp.text
    assert str(MAX_EDITS_PER_SERMON) in resp.json()["detail"]


def test_edit_requires_an_existing_manuscript(active_tenant_with_org: dict, auth_headers: dict):
    resp = client.post(
        "/sermons", json={"title": "Not Generated Yet", "format": "topical"}, headers=auth_headers
    )
    sermon_id = resp.json()["id"]

    edit_resp = client.post(f"/sermons/{sermon_id}/edit", json={"instruction": "anything"}, headers=auth_headers)
    assert edit_resp.status_code == 400
    assert "manuscript" in edit_resp.json()["detail"].lower()


def test_edit_rejects_a_selection_that_does_not_match_current_content(
    active_tenant_with_org: dict, auth_headers: dict
):
    sermon_id = _create_sermon_with_content(auth_headers)
    resp = client.post(
        f"/sermons/{sermon_id}/edit",
        json={"instruction": "x", "selection": {"start": 0, "end": len(MANUSCRIPT) + 500}},
        headers=auth_headers,
    )
    assert resp.status_code == 400
