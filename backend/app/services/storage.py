"""Where uploaded sermon audio actually lives once the request that
accepted it has returned — distinct from app/services/transcription.py,
which only needs the raw bytes for exactly as long as it takes to call
Groq/OpenAI.

One backend implemented today: local disk, under a Docker-managed named
volume (docker-compose.yml's media_storage volume, mounted into `backend`
only — no other container touches uploaded audio directly, same reasoning
as app/services/ingestion.py's "no shared storage with celery-worker").
MEDIA_STORAGE_BACKEND (app/core/config.py) is a plain config switch, not a
plugin registry, on purpose — get_media_storage() is the only seam a
second backend needs to slot into, and app/api/media.py is the only
caller that would need to change for one.

Google Drive is the anticipated second backend — a tenant maps their own
Drive folder instead of using our disk. GoogleDriveStorage below is a
deliberate stub, not a real integration: doing this for real needs a
Google Cloud OAuth consent screen, a per-tenant encrypted refresh-token
store, a folder-picker UI, and the Drive API's own upload/download calls,
none of which exist yet. Selecting it via MEDIA_STORAGE_BACKEND raises
clearly instead of silently falling back to local disk or pretending to
work — same "fail loud on missing config" convention as
OpenRouterError/TranscriptionError for a missing API key.

Why this stub exists at all, not just "maybe someday": Kerygma's stored
recordings/uploads are the intended future content source for a separate,
still-unbuilt social-clip-generation SaaS — clip generation was
deliberately cut from Kerygma's own scope (see git history,
"Drop clip generation from scope — moving to Cluos, a separate SaaS").
Local disk under a Docker volume scoped to this backend container won't
be reachable from that separate product's deployment, so a shared-access
backend (Drive, object storage, or an internal API) will eventually be
needed — Drive is simply the one sketched out here as a seam. This is
not scope creep or dead code left over from clip generation being
removed; no active integration work is happening now, and none should
start until that second product is actually being built.
"""
import os
import uuid
from abc import ABC, abstractmethod

from app.core.config import get_settings

settings = get_settings()


class MediaStorage(ABC):
    @abstractmethod
    def save(self, tenant_id: uuid.UUID, media_file_id: uuid.UUID, filename: str, data: bytes) -> str:
        """Persist `data`; return the storage_path to save on the
        MediaFile row (opaque to callers — only ever passed back into
        load()/delete() on the same backend, never parsed or displayed)."""

    @abstractmethod
    def load(self, storage_path: str) -> bytes:
        """Fetch back exactly what save() wrote, given the storage_path
        it returned."""

    @abstractmethod
    def delete(self, storage_path: str) -> None:
        """Best-effort delete; a no-op (not an error) if the path is
        already gone."""


def _safe_filename(filename: str) -> str:
    """Strip any path component a client might send — a full Windows
    path, "../.." traversal — down to just the basename, so save() can
    never be tricked into writing outside its own tenant subdirectory."""
    return os.path.basename(filename) or "upload"


class LocalDiskStorage(MediaStorage):
    """One subdirectory per tenant under MEDIA_STORAGE_ROOT, one file per
    MediaFile row, prefixed with its own id so two uploads of the same
    filename by the same tenant never collide."""

    def __init__(self, root: str) -> None:
        self._root = root

    def _relative_path(self, tenant_id: uuid.UUID, media_file_id: uuid.UUID, filename: str) -> str:
        return os.path.join(str(tenant_id), f"{media_file_id}-{_safe_filename(filename)}")

    def save(self, tenant_id: uuid.UUID, media_file_id: uuid.UUID, filename: str, data: bytes) -> str:
        relative_path = self._relative_path(tenant_id, media_file_id, filename)
        absolute_path = os.path.join(self._root, relative_path)
        os.makedirs(os.path.dirname(absolute_path), exist_ok=True)
        with open(absolute_path, "wb") as f:
            f.write(data)
        return relative_path

    def load(self, storage_path: str) -> bytes:
        with open(os.path.join(self._root, storage_path), "rb") as f:
            return f.read()

    def delete(self, storage_path: str) -> None:
        try:
            os.remove(os.path.join(self._root, storage_path))
        except FileNotFoundError:
            pass


class GoogleDriveStorage(MediaStorage):
    """Not implemented — see module docstring. Constructing this (i.e.
    selecting MEDIA_STORAGE_BACKEND=google_drive) fails immediately and
    specifically, rather than at the first save() call."""

    def __init__(self) -> None:
        raise NotImplementedError(
            "Google Drive storage isn't built yet — it needs OAuth, a per-tenant "
            "encrypted refresh-token store, a folder-picker UI, and the Drive "
            "API's own upload/download calls. Set MEDIA_STORAGE_BACKEND=local "
            "until this is implemented."
        )

    def save(self, tenant_id: uuid.UUID, media_file_id: uuid.UUID, filename: str, data: bytes) -> str:
        raise NotImplementedError

    def load(self, storage_path: str) -> bytes:
        raise NotImplementedError

    def delete(self, storage_path: str) -> None:
        raise NotImplementedError


def get_media_storage() -> MediaStorage:
    if settings.media_storage_backend == "local":
        return LocalDiskStorage(settings.media_storage_root)
    if settings.media_storage_backend == "google_drive":
        return GoogleDriveStorage()
    raise ValueError(f"Unknown MEDIA_STORAGE_BACKEND: {settings.media_storage_backend!r}")
