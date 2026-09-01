"""app.services.storage: LocalDiskStorage against a real (temp-directory)
filesystem — no mocking needed, this is genuinely just file I/O — plus
get_media_storage()'s backend-selection switch.
"""
import uuid

import pytest

from app.services import storage as storage_module
from app.services.storage import GoogleDriveStorage, LocalDiskStorage, get_media_storage


def test_save_then_load_roundtrips_the_same_bytes(tmp_path):
    disk = LocalDiskStorage(str(tmp_path))
    tenant_id, media_file_id = uuid.uuid4(), uuid.uuid4()

    storage_path = disk.save(tenant_id, media_file_id, "sermon.mp3", b"fake audio bytes")

    assert disk.load(storage_path) == b"fake audio bytes"


def test_save_namespaces_by_tenant_and_media_file_id(tmp_path):
    disk = LocalDiskStorage(str(tmp_path))
    tenant_id, media_file_id = uuid.uuid4(), uuid.uuid4()

    storage_path = disk.save(tenant_id, media_file_id, "sermon.mp3", b"data")

    assert storage_path.startswith(str(tenant_id))
    assert str(media_file_id) in storage_path


def test_save_sanitizes_a_path_traversal_filename(tmp_path):
    """A malicious/careless filename ("../../etc/passwd", or an absolute
    path) must never let save() write outside its own tenant
    subdirectory."""
    disk = LocalDiskStorage(str(tmp_path))
    tenant_id, media_file_id = uuid.uuid4(), uuid.uuid4()

    storage_path = disk.save(tenant_id, media_file_id, "../../../etc/passwd", b"data")

    assert ".." not in storage_path
    assert (tmp_path / storage_path).is_relative_to(tmp_path / str(tenant_id))


def test_two_uploads_of_the_same_filename_by_the_same_tenant_do_not_collide(tmp_path):
    disk = LocalDiskStorage(str(tmp_path))
    tenant_id = uuid.uuid4()

    path_a = disk.save(tenant_id, uuid.uuid4(), "sermon.mp3", b"first upload")
    path_b = disk.save(tenant_id, uuid.uuid4(), "sermon.mp3", b"second upload")

    assert path_a != path_b
    assert disk.load(path_a) == b"first upload"
    assert disk.load(path_b) == b"second upload"


def test_delete_removes_the_file(tmp_path):
    disk = LocalDiskStorage(str(tmp_path))
    storage_path = disk.save(uuid.uuid4(), uuid.uuid4(), "sermon.mp3", b"data")

    disk.delete(storage_path)

    with pytest.raises(FileNotFoundError):
        disk.load(storage_path)


def test_delete_on_an_already_missing_file_is_a_no_op(tmp_path):
    disk = LocalDiskStorage(str(tmp_path))
    disk.delete("tenant-x/nonexistent-file.mp3")  # must not raise


def test_get_media_storage_returns_local_disk_by_default(monkeypatch, tmp_path):
    monkeypatch.setattr(storage_module.settings, "media_storage_backend", "local")
    monkeypatch.setattr(storage_module.settings, "media_storage_root", str(tmp_path))

    backend = get_media_storage()

    assert isinstance(backend, LocalDiskStorage)


def test_get_media_storage_google_drive_raises_not_implemented(monkeypatch):
    monkeypatch.setattr(storage_module.settings, "media_storage_backend", "google_drive")

    with pytest.raises(NotImplementedError, match="isn't built yet"):
        get_media_storage()


def test_get_media_storage_unknown_backend_raises_value_error(monkeypatch):
    monkeypatch.setattr(storage_module.settings, "media_storage_backend", "s3")

    with pytest.raises(ValueError, match="Unknown MEDIA_STORAGE_BACKEND"):
        get_media_storage()
