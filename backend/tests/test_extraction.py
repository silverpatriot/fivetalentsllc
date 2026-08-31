"""app.services.extraction — real pypdf/python-docx parsing against
actual PDF/DOCX bytes built in-memory (not mocked: the whole point is
proving these libraries actually extract what a real file contains)."""
import io

import docx
import pytest

from app.services.extraction import ExtractionError, extract_text, guess_format


def _minimal_pdf_with_text(text: str) -> bytes:
    """A hand-built, minimal-but-valid single-page PDF containing `text`
    as its content stream — avoids pulling in a PDF-writing library
    (reportlab, etc.) as a test-only dependency just to produce a fixture
    pypdf can then read back."""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 4 0 R >> >> "
        b"/MediaBox [0 0 300 300] /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    stream_content = f"BT /F1 12 Tf 10 250 Td ({text}) Tj ET".encode()
    objects.append(b"<< /Length " + str(len(stream_content)).encode() + b" >>\nstream\n" + stream_content + b"\nendstream")

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_offset = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += b"trailer\n"
    out += f"<< /Size {len(objects) + 1} /Root 1 0 R >>\n".encode()
    out += f"startxref\n{xref_offset}\n%%EOF".encode()
    return bytes(out)


def _minimal_docx_with_paragraphs(paragraphs: list[str]) -> bytes:
    document = docx.Document()
    for p in paragraphs:
        document.add_paragraph(p)
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def test_guess_format_prefers_declared_content_type():
    assert guess_format("upload.bin", "application/pdf") == "pdf"
    assert (
        guess_format("upload.bin", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        == "docx"
    )
    assert guess_format("upload.bin", "text/plain") == "txt"


def test_guess_format_falls_back_to_extension_when_content_type_is_unhelpful():
    # Browsers/clients are inconsistent about setting this for .txt in
    # particular — often omitted or sent as application/octet-stream.
    assert guess_format("notes.txt", None) == "txt"
    assert guess_format("notes.txt", "application/octet-stream") == "txt"
    assert guess_format("sermon.pdf", "application/octet-stream") == "pdf"


def test_guess_format_rejects_unsupported_types():
    with pytest.raises(ExtractionError):
        guess_format("slides.pptx", "application/vnd.ms-powerpoint")


def test_extract_pdf_returns_real_text_from_the_content_stream():
    pdf_bytes = _minimal_pdf_with_text("Grace and truth came by Jesus Christ")
    text = extract_text(pdf_bytes, "pdf")
    assert "Grace and truth came by Jesus Christ" in text


def test_extract_pdf_corrupt_file_raises_extraction_error():
    with pytest.raises(ExtractionError):
        extract_text(b"this is not a pdf at all", "pdf")


def test_extract_docx_returns_paragraphs_joined():
    docx_bytes = _minimal_docx_with_paragraphs(
        ["In the beginning was the Word.", "", "And the Word was with God."]
    )
    text = extract_text(docx_bytes, "docx")
    assert "In the beginning was the Word." in text
    assert "And the Word was with God." in text


def test_extract_docx_corrupt_file_raises_extraction_error():
    with pytest.raises(ExtractionError):
        extract_text(b"not a real docx", "docx")


def test_extract_txt_decodes_utf8():
    text = extract_text("Blessed are the peacemakers — café, naïve.".encode("utf-8"), "txt")
    assert "peacemakers" in text
    assert "café" in text


def test_extract_txt_falls_back_gracefully_on_bad_encoding():
    # Not valid UTF-8 — must not raise, degrades via errors="replace".
    text = extract_text(b"\xff\xfe not valid utf-8", "txt")
    assert isinstance(text, str)
