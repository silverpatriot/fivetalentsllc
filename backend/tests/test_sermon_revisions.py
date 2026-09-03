"""Phase 8 Tasks 2-4: version history, compare, and restore. Real
live-infrastructure convention, same as every other generation test —
real Postgres, a real (self-signed) Clerk JWT, real citation
verification against bible-api.com; only OpenRouter calls are mocked
(no live LLM spend needed to prove the plumbing — see
test_generation_usage.py for the same convention).

Proves specifically:
- the Phase 8 Task 1 regeneration-snapshot fix: a real first generation
  creates no revision row, a real edit and a real regeneration both do,
  and the regeneration's row is tagged with the sentinel instruction so
  the UI can tell it apart from a real edit instruction.
- the version list is ordered newest-first with "current" synthesized
  from live sermon.content, never a sermon_revisions row itself.
- compare produces a real word-level diff, not just two full texts.
- restore actually restores content, is ITSELF recoverable (creates its
  own snapshot of the pre-restore state), and re-verifies citations
  against the newly-restored content rather than leaving the previous
  citations panel stale.
- tenant isolation holds for sermon_revisions at the RLS level, same
  rigor as test_rls.py applies to the original six tables.
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
from app.services.generation import REGENERATION_INSTRUCTION_SENTINEL
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
                "slug": f"rev-{tenant_id.hex[:8]}",
                "name": "Revisions Test Tenant",
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
        private_key, {"sub": "user_rev_test", "o": {"id": active_tenant_with_org["clerk_org_id"], "rol": "admin"}}
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def _stub_background_tasks(monkeypatch):
    # Same reasoning as test_generation_usage.py: neither Stripe usage
    # reporting nor cadence-corpus ingestion is what these tests are
    # about, and both would otherwise need a live Celery worker.
    monkeypatch.setattr("app.tasks.usage_reporting.report_usage_event.delay", lambda *a, **k: None)
    monkeypatch.setattr("app.services.ingestion.embed_document_chunks.delay", lambda *a, **k: None)


async def _fake_outline(model, messages, **kwargs):
    return "1. Point one\n2. Point two", '{"fake":"outline"}'


def _create_sermon(auth_headers: dict, title: str = "On Contentment") -> str:
    resp = client.post("/sermons", json={"title": title, "format": "topical"}, headers=auth_headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _generate(auth_headers: dict, sermon_id: str, draft_text: str, monkeypatch) -> None:
    async def _fake_stream(model, messages, raw_sink=None, **kwargs):
        if raw_sink is not None:
            raw_sink.append(draft_text)
        yield draft_text

    monkeypatch.setattr("app.services.generation.chat_completion", _fake_outline)
    monkeypatch.setattr("app.services.generation.stream_chat_completion", _fake_stream)
    resp = client.post(
        f"/sermons/{sermon_id}/generate",
        json={"topic": "Contentment"},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    assert "event: error" not in resp.text


def _edit(auth_headers: dict, sermon_id: str, instruction: str, replacement: str, start: int, end: int, monkeypatch) -> None:
    async def _fake_stream(model, messages, raw_sink=None, **kwargs):
        if raw_sink is not None:
            raw_sink.append(replacement)
        yield replacement

    monkeypatch.setattr("app.services.generation.stream_chat_completion", _fake_stream)
    resp = client.post(
        f"/sermons/{sermon_id}/edit",
        json={"instruction": instruction, "selection": {"start": start, "end": end}},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    assert "event: error" not in resp.text


def test_full_lifecycle_produces_correctly_ordered_and_labeled_history(
    active_tenant_with_org: dict, auth_headers: dict, monkeypatch
):
    sermon_id = _create_sermon(auth_headers)

    draft1 = "Point one: contentment is learned, not innate."
    _generate(auth_headers, sermon_id, draft1, monkeypatch)

    # Edit A, on the freshly-generated draft.
    edit_a_replacement = "Point one, personally: I have had to learn contentment the hard way."
    _edit(auth_headers, sermon_id, "make it personal", edit_a_replacement, 0, len(draft1), monkeypatch)

    # Regeneration — overwrites the edited content, no selection involved.
    draft2 = "Point one: contentment in Philippians 4 is a learned discipline, not a feeling."
    _generate(auth_headers, sermon_id, draft2, monkeypatch)

    # Edit B, on the regenerated draft.
    edit_b_replacement = "Point one: true contentment, per Philippians 4, is trained, not felt."
    _edit(auth_headers, sermon_id, "tighten this", edit_b_replacement, 0, len(draft2), monkeypatch)

    list_resp = client.get(f"/sermons/{sermon_id}/revisions", headers=auth_headers)
    assert list_resp.status_code == 200, list_resp.text
    entries = list_resp.json()

    # 1 "current" + 3 real revision rows (edit A's pre-edit snapshot,
    # the regeneration's pre-overwrite snapshot, edit B's pre-edit
    # snapshot) — critically NOT 4: the very first generation created no
    # row at all, exactly the Phase 8 Task 1 audit's finding.
    assert len(entries) == 4
    assert entries[0]["is_current"] is True
    assert entries[0]["id"] == "current"
    assert entries[0]["instruction"] is None

    # Newest-first: edit B's snapshot (draft2, pre-edit-B), then the
    # regeneration's snapshot (edit_a_replacement, pre-regeneration),
    # then edit A's snapshot (draft1, pre-edit-A).
    assert entries[1]["instruction"] == "tighten this"
    assert entries[2]["instruction"] == REGENERATION_INSTRUCTION_SENTINEL
    assert entries[3]["instruction"] == "make it personal"

    # Full content is fetchable read-only for any past entry.
    detail_resp = client.get(f"/sermons/{sermon_id}/revisions/{entries[3]['id']}", headers=auth_headers)
    assert detail_resp.status_code == 200, detail_resp.text
    assert detail_resp.json()["content"] == draft1

    detail_regen_resp = client.get(f"/sermons/{sermon_id}/revisions/{entries[2]['id']}", headers=auth_headers)
    assert detail_regen_resp.json()["content"] == edit_a_replacement

    current_resp = client.get(f"/sermons/{sermon_id}/revisions/current", headers=auth_headers)
    assert current_resp.json()["content"] == edit_b_replacement


def test_compare_produces_a_real_word_level_diff_not_full_texts(
    active_tenant_with_org: dict, auth_headers: dict, monkeypatch
):
    sermon_id = _create_sermon(auth_headers)
    draft = "The quick brown fox jumps over the lazy dog."
    _generate(auth_headers, sermon_id, draft, monkeypatch)
    replacement = "The quick brown fox leaps over the sleepy dog."
    _edit(auth_headers, sermon_id, "vary the verbs", replacement, 0, len(draft), monkeypatch)

    entries = client.get(f"/sermons/{sermon_id}/revisions", headers=auth_headers).json()
    old_id = next(e["id"] for e in entries if not e["is_current"])

    resp = client.get(
        f"/sermons/{sermon_id}/revisions/compare",
        params={"from_id": old_id, "to_id": "current"},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    diff = body["diff"]

    # Reconstructs both texts exactly from the diff ops — proves this is
    # a real diff, not a lossy approximation.
    old_reconstructed = "".join(seg["text"] for seg in diff if seg["op"] in ("equal", "delete"))
    new_reconstructed = "".join(seg["text"] for seg in diff if seg["op"] in ("equal", "insert"))
    assert old_reconstructed == draft
    assert new_reconstructed == replacement

    # Only the two changed words are flagged — a full-text diff (or a
    # naive "two paragraphs differ" line-level diff) would instead mark
    # the whole sentence as one big replace.
    deletes = {seg["text"].strip() for seg in diff if seg["op"] == "delete"}
    inserts = {seg["text"].strip() for seg in diff if seg["op"] == "insert"}
    assert deletes == {"jumps", "lazy"}
    assert inserts == {"leaps", "sleepy"}
    equal_text = "".join(seg["text"] for seg in diff if seg["op"] == "equal")
    assert "quick brown fox" in equal_text
    assert "dog." in equal_text

    # Comparing a revision to itself: entirely "equal", nothing changed.
    self_resp = client.get(
        f"/sermons/{sermon_id}/revisions/compare",
        params={"from_id": "current", "to_id": "current"},
        headers=auth_headers,
    )
    self_diff = self_resp.json()["diff"]
    assert all(seg["op"] == "equal" for seg in self_diff)
    assert "".join(seg["text"] for seg in self_diff) == replacement


def test_restore_reverts_content_and_re_verifies_citations(
    pg_engine: Engine, active_tenant_with_org: dict, auth_headers: dict, monkeypatch
):
    tenant_id = active_tenant_with_org["id"]
    sermon_id = _create_sermon(auth_headers)

    # draft1 has a real, verifiable quote; the edited version replaces it
    # with a hallucinated reference — restoring draft1 back should make
    # the citation panel correctly clean again, proving re-verification
    # runs against the RESTORED content, not whatever was flagged before.
    draft1 = 'Point one: as John 3:16 says, "For God so loved the world, that he gave his only begotten Son."'
    _generate(auth_headers, sermon_id, draft1, monkeypatch)
    bad_replacement = 'Point one: as Zorblatt 9:9 says, "this reference does not exist."'
    _edit(auth_headers, sermon_id, "add a different reference", bad_replacement, 0, len(draft1), monkeypatch)

    entries = client.get(f"/sermons/{sermon_id}/revisions", headers=auth_headers).json()
    draft1_revision_id = next(e["id"] for e in entries if not e["is_current"])

    restore_resp = client.post(f"/sermons/{sermon_id}/revisions/{draft1_revision_id}/restore", headers=auth_headers)
    assert restore_resp.status_code == 200, restore_resp.text
    body = restore_resp.json()
    assert body["sermon"]["content"] == draft1
    assert body["new_revision"]["is_current"] is False
    citation_refs = {f["reference"]: f["status"] for f in body["citation_flags"]}
    assert citation_refs == {"John 3:16": "verified"}

    with pg_engine.begin() as conn:
        set_tenant(conn, tenant_id)
        sermon_row = conn.execute(sa.text("SELECT content FROM sermons WHERE id = :id"), {"id": sermon_id}).fetchone()
    assert sermon_row.content == draft1

    # Restore is itself recoverable: the pre-restore state (bad_replacement)
    # was snapshotted, so restoring to THAT snapshot undoes the restore.
    entries_after = client.get(f"/sermons/{sermon_id}/revisions", headers=auth_headers).json()
    assert len(entries_after) == 3  # current(draft1) + pre-edit snapshot(draft1) + pre-restore snapshot(bad_replacement)
    pre_restore_entry = next(e for e in entries_after if e["id"] == body["new_revision"]["id"])
    assert "(restored to revision" in pre_restore_entry["instruction"]

    undo_resp = client.post(f"/sermons/{sermon_id}/revisions/{pre_restore_entry['id']}/restore", headers=auth_headers)
    assert undo_resp.status_code == 200, undo_resp.text
    assert undo_resp.json()["sermon"]["content"] == bad_replacement


def test_restore_rejects_current_and_unknown_revision(active_tenant_with_org: dict, auth_headers: dict, monkeypatch):
    sermon_id = _create_sermon(auth_headers)
    _generate(auth_headers, sermon_id, "Some content.", monkeypatch)

    current_resp = client.post(f"/sermons/{sermon_id}/revisions/current/restore", headers=auth_headers)
    assert current_resp.status_code == 400

    missing_resp = client.post(f"/sermons/{sermon_id}/revisions/{uuid.uuid4()}/restore", headers=auth_headers)
    assert missing_resp.status_code == 404


def test_first_generation_creates_no_revision_row(active_tenant_with_org: dict, auth_headers: dict, monkeypatch):
    """The Phase 8 Task 1 audit finding, proven directly: a sermon with
    exactly one generation and zero edits/regenerations has zero
    sermon_revisions rows — the version list's only entry is the
    synthesized "current" one."""
    sermon_id = _create_sermon(auth_headers)
    _generate(auth_headers, sermon_id, "A single, never-edited draft.", monkeypatch)

    entries = client.get(f"/sermons/{sermon_id}/revisions", headers=auth_headers).json()
    assert len(entries) == 1
    assert entries[0]["is_current"] is True


def test_revision_endpoints_enforce_tenant_isolation_via_rls(
    pg_engine: Engine, two_tenants: tuple[uuid.UUID, uuid.UUID]
):
    """Same rigor test_rls.py applies to the original six tables,
    extended to sermon_revisions specifically (added later, in migration
    0015 — not one of the six that file already covers)."""
    tenant_a, tenant_b = two_tenants

    with pg_engine.begin() as conn:
        set_tenant(conn, tenant_a)
        sermon_a_id = conn.execute(sa.text("SELECT id FROM sermons WHERE tenant_id = :tid"), {"tid": str(tenant_a)}).scalar_one()
        conn.execute(
            sa.text(
                "INSERT INTO sermon_revisions (tenant_id, sermon_id, content, instruction) "
                "VALUES (:tid, :sid, 'Tenant A revision content', 'an edit')"
            ),
            {"tid": str(tenant_a), "sid": str(sermon_a_id)},
        )

    # Tenant B's context can't see tenant A's revision row at all, even
    # querying by its own real id.
    with pg_engine.begin() as conn:
        set_tenant(conn, tenant_a)
        revision_id = conn.execute(
            sa.text("SELECT id FROM sermon_revisions WHERE sermon_id = :sid"), {"sid": str(sermon_a_id)}
        ).scalar_one()

    with pg_engine.begin() as conn:
        set_tenant(conn, tenant_b)
        row = conn.execute(sa.text("SELECT * FROM sermon_revisions WHERE id = :id"), {"id": str(revision_id)}).fetchone()
    assert row is None

    with pg_engine.begin() as conn:
        set_tenant(conn, tenant_b)
        all_visible = conn.execute(sa.text("SELECT * FROM sermon_revisions")).fetchall()
    assert all_visible == []
