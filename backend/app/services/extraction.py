"""Text extraction for uploaded documents (Phase 4 Task 1). Library-based
per format, not hand-rolled parsing — pypdf for PDF, python-docx for
DOCX, plain decode for .txt. PDF/DOCX/txt is the confirmed complete
format list for now; anything else is out of scope until it's actually
needed (see the kickoff spec's "don't over-build format support
speculatively").

Runs synchronously, in the upload request itself — see
app/services/ingestion.py's docstring for why (no shared storage between
the backend and celery-worker containers, so the raw file can't cross
into a background task; only the extracted chunk *text* needs to, and
text serializes into a Celery task's args fine).
"""
import io

import docx
from pypdf import PdfReader


class ExtractionError(ValueError):
    pass


_SUPPORTED_CONTENT_TYPES = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "text/plain": "txt",
}


def guess_format(filename: str, content_type: str | None) -> str:
    """Prefer the declared content-type; fall back to the file extension
    (browsers/clients are inconsistent about setting multipart part
    content-types correctly, especially for .txt)."""
    if content_type in _SUPPORTED_CONTENT_TYPES:
        return _SUPPORTED_CONTENT_TYPES[content_type]
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext in ("pdf", "docx", "txt"):
        return ext
    raise ExtractionError(
        f"Unsupported file type (filename={filename!r}, content_type={content_type!r}). "
        "Supported: PDF, DOCX, plain text."
    )


def extract_text(data: bytes, fmt: str) -> str:
    if fmt == "pdf":
        return _extract_pdf(data)
    if fmt == "docx":
        return _extract_docx(data)
    if fmt == "txt":
        return _extract_txt(data)
    raise ExtractionError(f"Unsupported format: {fmt!r}")


def _extract_pdf(data: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(data))
        return "\n\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as exc:  # pypdf raises several distinct exception types for a corrupt/encrypted file
        raise ExtractionError(f"Could not read PDF: {exc}") from exc


def _extract_docx(data: bytes) -> str:
    try:
        document = docx.Document(io.BytesIO(data))
        return "\n\n".join(p.text for p in document.paragraphs if p.text.strip())
    except Exception as exc:
        raise ExtractionError(f"Could not read DOCX: {exc}") from exc


def _extract_txt(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        # Best-effort fallback for a file that isn't actually UTF-8 (an
        # older Word export saved as .txt with a Windows codepage, say)
        # — replacing undecodable bytes beats rejecting the whole upload
        # outright for what's very likely still mostly-readable text.
        return data.decode("utf-8", errors="replace")
