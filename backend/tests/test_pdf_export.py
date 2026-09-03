"""Phase 7 Task 3: PDF export. Real live-infrastructure convention, same
as test_sermon_citations.py — real Postgres, a real (self-signed) Clerk
JWT, real citation verification against bible-api.com; nothing mocked
(this endpoint makes no LLM call either). Proves:

- estimate_delivery_minutes (Phase 7 Task 2's shared calculation,
  mirrored in frontend/lib/timing.ts) is sound for a known word count.
- the PDF endpoint returns a real, parseable PDF (via pypdf, already a
  dependency — reading the bytes back out and checking the actual
  extracted text is a real "is this readable" check, not just "the
  response didn't error").
- the title, manuscript content, and a verified scripture quote all
  appear in the extracted text.
- Content-Disposition carries a sane filename; a title with characters
  that would corrupt or inject into that header gets sanitized instead.
- a sermon with no manuscript yet is a clean 400, not a 500.
"""
import io
import uuid
from typing import Iterator

import pytest
import sqlalchemy as sa
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from pypdf import PdfReader
from sqlalchemy import Engine

from app.api.sermons import _pdf_filename
from app.core import security
from app.main import app
from app.services.pdf_export import estimate_delivery_minutes
from tests.conftest import make_clerk_jwt

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
                "slug": f"pdf-{tenant_id.hex[:8]}",
                "name": "PDF Test Tenant",
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
        private_key, {"sub": "user_pdf_test", "o": {"id": active_tenant_with_org["clerk_org_id"], "rol": "admin"}}
    )
    return {"Authorization": f"Bearer {token}"}


def _create_sermon_with_content(auth_headers: dict, content: str, title: str = "Faithfulness in Trial") -> str:
    resp = client.post(
        "/sermons",
        json={"title": title, "format": "topical", "content": content},
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_estimate_delivery_minutes_matches_the_shared_target_pace():
    # Exactly 260 words at PREACHING_WORDS_PER_MINUTE (130) is exactly 2
    # minutes — a known, hand-checkable word count, not an approximation.
    content = " ".join(["word"] * 260)
    assert estimate_delivery_minutes(content) == 2


def test_estimate_delivery_minutes_rounds_rather_than_truncates():
    # 195 words / 130 wpm = 1.5 minutes exactly -> banker's/round-half rules
    # aside, Python's round(1.5) rounds to even (2) — just confirm it's not
    # naively truncated down to 1.
    content = " ".join(["word"] * 195)
    assert estimate_delivery_minutes(content) == 2


def test_pdf_export_contains_title_content_and_a_verified_quote(
    active_tenant_with_org: dict, auth_headers: dict
):
    content = (
        "Introduction: a message about steadfast faithfulness in hardship.\n\n"
        'Point one: as Paul writes in Romans 8:28, "And we know that all things work together for '
        'good to them that love God, to them who are the called according to his purpose."'
    )
    sermon_id = _create_sermon_with_content(auth_headers, content, title="All Things for Good")

    resp = client.get(f"/sermons/{sermon_id}/pdf", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/pdf"
    assert 'filename="All Things for Good.pdf"' in resp.headers["content-disposition"]

    reader = PdfReader(io.BytesIO(resp.content))
    assert len(reader.pages) >= 1
    # Collapsed to single-spaced: pypdf's extract_text() inserts a
    # newline wherever the PDF itself visually line-wraps, which doesn't
    # necessarily land on the same word boundaries as the source text —
    # real formatting, not something to assert on here, so it shouldn't
    # break a plain substring check.
    text = " ".join(page.extract_text() for page in reader.pages).replace("\n", " ")
    assert "All Things for Good" in text
    assert "steadfast faithfulness in hardship" in text
    assert "Romans 8:28" in text
    assert "all things work together for good" in text
    # The timing-estimate line (Task 2) is printed in the PDF too.
    assert "at an average pace" in text


def test_pdf_export_requires_an_existing_manuscript(active_tenant_with_org: dict, auth_headers: dict):
    resp = client.post(
        "/sermons", json={"title": "Not Generated Yet", "format": "topical"}, headers=auth_headers
    )
    sermon_id = resp.json()["id"]

    pdf_resp = client.get(f"/sermons/{sermon_id}/pdf", headers=auth_headers)
    assert pdf_resp.status_code == 400
    assert "manuscript" in pdf_resp.json()["detail"].lower()


def test_pdf_filename_sanitizes_header_unsafe_characters():
    # Real risk: a title is user-supplied and goes straight into an HTTP
    # header value — CR/LF could inject a second header, quotes could
    # break out of filename="...". Both must be stripped, not escaped.
    assert _pdf_filename('Evil"\r\nX-Injected: yes') == "EvilX-Injected yes.pdf"
    assert _pdf_filename("") == "sermon.pdf"
    assert _pdf_filename("   ") == "sermon.pdf"
    assert _pdf_filename("All Things for Good") == "All Things for Good.pdf"
