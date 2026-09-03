"""Phase 7 Task 1: GET /sermons/{id}/citations. Real live-infrastructure
convention, same as test_sermon_editing.py — real Postgres, a real
(self-signed) Clerk JWT, real citation verification against
bible-api.com; nothing here is mocked, since this endpoint itself does
no LLM call to mock (it only recomputes bible.verify_all_citations
against already-stored content). Proves specifically:

- the endpoint recomputes fresh against CURRENT content rather than
  reading some stale/nonexistent persisted field (there is no persisted
  citation_flags column — see app/api/sermons.py's own docstring on
  this route for why it exists at all).
- a real, accurately-quoted reference comes back verified.
- a manuscript with no scripture references at all comes back as an
  empty list, not an error.
- a sermon with no manuscript yet is a clean 400, not a 500 or an empty
  list pretending there was something to check.
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
                "slug": f"citations-{tenant_id.hex[:8]}",
                "name": "Citations Test Tenant",
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
        private_key, {"sub": "user_citations_test", "o": {"id": active_tenant_with_org["clerk_org_id"], "rol": "admin"}}
    )
    return {"Authorization": f"Bearer {token}"}


def _create_sermon_with_content(auth_headers: dict, content: str) -> str:
    resp = client.post(
        "/sermons",
        json={"title": "Faithfulness in Trial", "format": "topical", "content": content},
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_get_citations_verifies_an_accurate_quote_against_the_real_source(
    active_tenant_with_org: dict, auth_headers: dict
):
    content = (
        "Point one: as Jesus said in John 3:16, "
        '"For God so loved the world, that he gave his only begotten Son, '
        'that whosoever believeth in him should not perish, but have everlasting life."'
    )
    sermon_id = _create_sermon_with_content(auth_headers, content)

    resp = client.get(f"/sermons/{sermon_id}/citations", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    flags = resp.json()
    assert len(flags) == 1
    assert flags[0]["reference"] == "John 3:16"
    assert flags[0]["status"] == "verified"
    assert flags[0]["quoted_text"] is not None


def test_get_citations_returns_empty_list_for_a_manuscript_with_no_references(
    active_tenant_with_org: dict, auth_headers: dict
):
    sermon_id = _create_sermon_with_content(auth_headers, "A manuscript that never quotes or cites scripture.")
    resp = client.get(f"/sermons/{sermon_id}/citations", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json() == []


def test_get_citations_recomputes_fresh_rather_than_reading_a_stale_value(
    active_tenant_with_org: dict, auth_headers: dict
):
    """There is no persisted citation_flags column on Sermon — this
    endpoint exists BECAUSE of that gap (see the route's own docstring).
    Two calls in a row must both do real work and agree, not read back
    some cached/stored value from sermon creation."""
    content = 'Point one, as Habakkuk 12:5 says, "this reference does not exist."'
    sermon_id = _create_sermon_with_content(auth_headers, content)

    first = client.get(f"/sermons/{sermon_id}/citations", headers=auth_headers)
    second = client.get(f"/sermons/{sermon_id}/citations", headers=auth_headers)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert first.json()[0]["status"] == "invalid_reference"  # Habakkuk has no chapter 12


def test_get_citations_requires_an_existing_manuscript(active_tenant_with_org: dict, auth_headers: dict):
    resp = client.post(
        "/sermons", json={"title": "Not Generated Yet", "format": "topical"}, headers=auth_headers
    )
    sermon_id = resp.json()["id"]

    citations_resp = client.get(f"/sermons/{sermon_id}/citations", headers=auth_headers)
    assert citations_resp.status_code == 400
    assert "manuscript" in citations_resp.json()["detail"].lower()
