"""POST /billing/activate-free — the free-tier path with no Stripe
involved at all (see app/api/billing.py's activate_free_tier). Real
Postgres, real routes, a real self-signed Clerk JWT — same pattern as
test_tenants.py, whose active_tenant/pending_tenant fixtures this file
would otherwise duplicate.
"""
import uuid
from collections.abc import Iterator

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


def _auth_headers(private_key: rsa.RSAPrivateKey, claims: dict) -> dict[str, str]:
    token = make_clerk_jwt(private_key, claims)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def active_starter_tenant(pg_engine: Engine) -> Iterator[dict]:
    tenant_id = uuid.uuid4()
    clerk_org_id = f"org_{tenant_id.hex[:16]}"
    with pg_engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO tenants (id, slug, name, clerk_org_id, plan_tier, subscription_status) "
                "VALUES (:id, :slug, :name, :org, 'starter', 'active')"
            ),
            {
                "id": str(tenant_id),
                "slug": f"paid-{tenant_id.hex[:8]}",
                "name": "Already Paying Church",
                "org": clerk_org_id,
            },
        )
    yield {"id": tenant_id, "clerk_org_id": clerk_org_id}
    with pg_engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM tenants WHERE id = :id"), {"id": str(tenant_id)})


def test_activate_free_moves_a_pending_tenant_to_active_free(pending_tenant: dict, rsa_keypair, monkeypatch):
    private_key, public_key = rsa_keypair
    monkeypatch.setattr(security, "_jwk_client", lambda: _FakeJWKClient(public_key))
    headers = _auth_headers(private_key, {"sub": "user_1", "o": {"id": pending_tenant["clerk_org_id"], "rol": "admin"}})

    resp = client.post("/billing/activate-free", headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["plan_tier"] == "free"
    assert body["subscription_status"] == "active"
    assert body["stripe_customer_id"] is None  # no Stripe object was ever created for this
    assert body["free_trial_started_at"] is not None  # starts the 30-day cadence-access clock


def test_activate_free_refuses_an_already_active_paid_tenant(active_starter_tenant: dict, rsa_keypair, monkeypatch):
    """The one thing this endpoint must never do: quietly downgrade a
    paying tenant to free."""
    private_key, public_key = rsa_keypair
    monkeypatch.setattr(security, "_jwk_client", lambda: _FakeJWKClient(public_key))
    headers = _auth_headers(
        private_key, {"sub": "user_2", "o": {"id": active_starter_tenant["clerk_org_id"], "rol": "admin"}}
    )

    resp = client.post("/billing/activate-free", headers=headers)

    assert resp.status_code == 400
    assert "already" in resp.json()["detail"].lower()
