"""POST /media end-to-end: real Postgres, a real (self-signed) Clerk JWT,
and a real LocalDiskStorage writing into a tmp_path — only
app.services.transcription.transcribe_audio itself is mocked (no live
Groq/OpenAI spend needed to prove the plumbing), same "mock only the
external LLM/ASR call, keep everything else real" approach as
tests/test_generation_usage.py, including its convention of
monkeypatching a real async fake function in rather than
unittest.mock.patch.
"""
import io
import uuid
from typing import Iterator

import pytest
import sqlalchemy as sa
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import Engine
from fastapi.testclient import TestClient

from app.core import security
from app.main import app
from app.services.transcription import TranscriptionError, TranscriptionResult
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
def active_tenant(pg_engine: Engine) -> Iterator[dict]:
    tenant_id = uuid.uuid4()
    clerk_org_id = f"org_{tenant_id.hex[:16]}"
    with pg_engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO tenants (id, slug, name, clerk_org_id, subscription_status) "
                "VALUES (:id, :slug, :name, :org, 'active')"
            ),
            {"id": str(tenant_id), "slug": f"media-{tenant_id.hex[:8]}", "name": "Media Test Tenant", "org": clerk_org_id},
        )
    yield {"id": tenant_id, "clerk_org_id": clerk_org_id}
    with pg_engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM tenants WHERE id = :id"), {"id": str(tenant_id)})


@pytest.fixture
def auth_headers(rsa_keypair: tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey], active_tenant, monkeypatch):
    private_key, public_key = rsa_keypair
    monkeypatch.setattr(security, "_jwk_client", lambda: _FakeJWKClient(public_key))
    token = make_clerk_jwt(private_key, {"sub": "user_media_test", "o": {"id": active_tenant["clerk_org_id"], "rol": "admin"}})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def _local_storage_root(monkeypatch, tmp_path):
    """Real LocalDiskStorage, real file I/O — just redirected to a
    throwaway directory instead of the production /app/media mount."""
    import app.services.storage as storage_module

    monkeypatch.setattr(storage_module.settings, "media_storage_backend", "local")
    monkeypatch.setattr(storage_module.settings, "media_storage_root", str(tmp_path))
    return tmp_path


def test_upload_transcribes_successfully_and_records_usage(
    pg_engine: Engine, active_tenant, auth_headers, _local_storage_root, monkeypatch
):
    async def _fake_transcribe_audio(data: bytes, filename: str) -> TranscriptionResult:
        return TranscriptionResult(text="In the beginning was the Word.", duration_seconds=1500.0, source="groq")

    monkeypatch.setattr("app.api.media.transcribe_audio", _fake_transcribe_audio)
    monkeypatch.setattr("app.tasks.usage_reporting.report_usage_event.delay", lambda *a, **k: None)

    resp = client.post(
        "/media",
        files={"file": ("sermon.mp3", io.BytesIO(b"fake audio bytes"), "audio/mpeg")},
        headers=auth_headers,
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["transcription_status"] == "completed"
    assert body["transcript_text"] == "In the beginning was the Word."
    assert body["duration_seconds"] == 1500.0
    assert body["original_filename"] == "sermon.mp3"

    # The file is really on disk, under this tenant's own subdirectory —
    # not just a DB row claiming it was stored.
    stored_dir = _local_storage_root / body["tenant_id"]
    assert stored_dir.is_dir()
    assert any(f.read_bytes() == b"fake audio bytes" for f in stored_dir.iterdir())

    with pg_engine.begin() as conn:
        set_tenant(conn, active_tenant["id"])
        usage_rows = conn.execute(
            sa.text("SELECT event_type, quantity FROM usage_events WHERE tenant_id = :tid"),
            {"tid": str(active_tenant["id"])},
        ).fetchall()
    assert len(usage_rows) == 1
    assert usage_rows[0].event_type == "transcription_minute"
    assert float(usage_rows[0].quantity) == pytest.approx(25.0)  # 1500s / 60


def test_upload_survives_a_transcription_failure_without_losing_the_recording(
    pg_engine: Engine, active_tenant, auth_headers, _local_storage_root, monkeypatch
):
    async def _fake_transcribe_audio_failure(data: bytes, filename: str) -> TranscriptionResult:
        raise TranscriptionError("both providers down")

    monkeypatch.setattr("app.api.media.transcribe_audio", _fake_transcribe_audio_failure)
    monkeypatch.setattr("app.tasks.usage_reporting.report_usage_event.delay", lambda *a, **k: None)

    resp = client.post(
        "/media",
        files={"file": ("sermon.mp3", io.BytesIO(b"fake audio bytes"), "audio/mpeg")},
        headers=auth_headers,
    )

    # The upload itself succeeded — only transcription failed, which is a
    # normal terminal state (transcription_status), not an HTTP error.
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["transcription_status"] == "failed"
    assert body["transcript_text"] is None
    assert body["duration_seconds"] is None

    # The recording itself is NOT lost, even though transcription failed.
    stored_dir = _local_storage_root / body["tenant_id"]
    assert any(f.read_bytes() == b"fake audio bytes" for f in stored_dir.iterdir())

    with pg_engine.begin() as conn:
        set_tenant(conn, active_tenant["id"])
        usage_count = conn.execute(
            sa.text("SELECT count(*) FROM usage_events WHERE tenant_id = :tid"), {"tid": str(active_tenant["id"])}
        ).scalar_one()
    assert usage_count == 0  # nothing billable happened


def test_upload_rejects_file_over_the_size_limit(active_tenant, auth_headers, monkeypatch):
    import app.api.media as media_module

    monkeypatch.setattr(media_module.settings, "max_media_upload_size_bytes", 10)
    resp = client.post(
        "/media",
        files={"file": ("big.mp3", io.BytesIO(b"this is more than ten bytes"), "audio/mpeg")},
        headers=auth_headers,
    )
    assert resp.status_code == 413


def _upload_one(auth_headers, filename="sermon.mp3", sermon_id=None):
    data = {"sermon_id": str(sermon_id)} if sermon_id else {}
    return client.post(
        "/media",
        files={"file": (filename, io.BytesIO(b"fake audio bytes"), "audio/mpeg")},
        data=data,
        headers=auth_headers,
    )


def test_list_media_returns_only_this_tenants_recordings_newest_first(
    active_tenant, auth_headers, _local_storage_root, monkeypatch
):
    async def _fake_transcribe_audio(data: bytes, filename: str) -> TranscriptionResult:
        return TranscriptionResult(text="hello", duration_seconds=10.0, source="groq")

    monkeypatch.setattr("app.api.media.transcribe_audio", _fake_transcribe_audio)
    monkeypatch.setattr("app.tasks.usage_reporting.report_usage_event.delay", lambda *a, **k: None)

    first = _upload_one(auth_headers, filename="first.mp3")
    second = _upload_one(auth_headers, filename="second.mp3")
    assert first.status_code == 201 and second.status_code == 201

    resp = client.get("/media", headers=auth_headers)
    assert resp.status_code == 200
    filenames = [row["original_filename"] for row in resp.json()]
    # Newest first — "second" was uploaded after "first".
    assert filenames == ["second.mp3", "first.mp3"]


def test_get_media_audio_streams_back_the_original_bytes(
    active_tenant, auth_headers, _local_storage_root, monkeypatch
):
    async def _fake_transcribe_audio(data: bytes, filename: str) -> TranscriptionResult:
        return TranscriptionResult(text="hello", duration_seconds=10.0, source="groq")

    monkeypatch.setattr("app.api.media.transcribe_audio", _fake_transcribe_audio)
    monkeypatch.setattr("app.tasks.usage_reporting.report_usage_event.delay", lambda *a, **k: None)

    upload = _upload_one(auth_headers)
    media_id = upload.json()["id"]

    resp = client.get(f"/media/{media_id}/audio", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.content == b"fake audio bytes"
    assert resp.headers["content-type"] == "audio/mpeg"


def test_get_media_audio_404s_for_an_unknown_id(active_tenant, auth_headers):
    resp = client.get(f"/media/{uuid.uuid4()}/audio", headers=auth_headers)
    assert resp.status_code == 404


def test_delete_media_removes_the_row_and_the_file(active_tenant, auth_headers, _local_storage_root, monkeypatch):
    async def _fake_transcribe_audio(data: bytes, filename: str) -> TranscriptionResult:
        return TranscriptionResult(text="hello", duration_seconds=10.0, source="groq")

    monkeypatch.setattr("app.api.media.transcribe_audio", _fake_transcribe_audio)
    monkeypatch.setattr("app.tasks.usage_reporting.report_usage_event.delay", lambda *a, **k: None)

    upload = _upload_one(auth_headers)
    body = upload.json()
    stored_file = _local_storage_root / body["tenant_id"] / f"{body['id']}-sermon.mp3"
    assert stored_file.is_file()

    resp = client.delete(f"/media/{body['id']}", headers=auth_headers)
    assert resp.status_code == 204
    assert not stored_file.exists()

    resp = client.get("/media", headers=auth_headers)
    assert resp.json() == []


def test_delete_media_404s_for_an_unknown_id(active_tenant, auth_headers):
    resp = client.delete(f"/media/{uuid.uuid4()}", headers=auth_headers)
    assert resp.status_code == 404
