"""GET /tenants/me and GET /tenants/by-slug/{slug} — real Postgres, real
routes through the actual app, a real self-signed Clerk JWT verified by
the real verify_clerk_jwt (only the JWKS network fetch is stubbed — same
pattern as test_clerk_jwt.py / test_generation_usage.py).

/tenants/me deliberately depends on get_current_tenant, not
get_active_tenant_id — a pending tenant must be able to call it to find
out it's pending, not get a 402 instead of an answer. That's the specific
behavior under test here, alongside /tenants/by-slug/{slug}'s much
narrower trust level (no auth at all, but also far less to expose — see
TenantPublicRead's docstring for why subscription_status/Stripe
ids/clerk_org_id must never appear in that response even though the row
underneath has them).
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
    """Stands in for jwt.PyJWKClient — same interface, backed by our test
    keypair instead of a real network fetch against a live JWKS URL."""

    def __init__(self, public_key):
        self._public_key = public_key

    def get_signing_key_from_jwt(self, token):
        return _FakeSigningKey(self._public_key)


def _auth_headers(private_key: rsa.RSAPrivateKey, claims: dict) -> dict[str, str]:
    token = make_clerk_jwt(private_key, claims)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def active_tenant(pg_engine: Engine) -> Iterator[dict]:
    """Every column populated with a non-default, distinguishable value
    (real-looking Stripe ids, plan_tier='starter', not the 'free'/'pending'
    server defaults) — so a field-by-field assertion actually proves the
    route returns/withholds the right data, rather than passing by
    accident against a default that would look the same either way."""
    tenant_id = uuid.uuid4()
    clerk_org_id = f"org_{tenant_id.hex[:16]}"
    slug = f"active-{tenant_id.hex[:8]}"
    with pg_engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO tenants (id, slug, name, clerk_org_id, plan_tier, "
                "subscription_status, stripe_customer_id, stripe_subscription_id) "
                "VALUES (:id, :slug, :name, :org, :plan, 'active', :cust, :sub)"
            ),
            {
                "id": str(tenant_id),
                "slug": slug,
                "name": "Grace Community",
                "org": clerk_org_id,
                "plan": "starter",
                "cust": f"cus_{tenant_id.hex[:14]}",
                "sub": f"sub_{tenant_id.hex[:14]}",
            },
        )
    yield {"id": tenant_id, "clerk_org_id": clerk_org_id, "slug": slug}
    with pg_engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM tenants WHERE id = :id"), {"id": str(tenant_id)})


# ============================================================================
# GET /tenants/me
# ============================================================================


def test_get_my_tenant_returns_tenant_for_a_pending_session(pending_tenant: dict, rsa_keypair, monkeypatch):
    """The core behavior this route exists for: a 'pending' tenant (the
    normal state right after signup, before Stripe Checkout) gets a real
    answer, not a 402 — get_active_tenant_id's gate does NOT apply here."""
    private_key, public_key = rsa_keypair
    monkeypatch.setattr(security, "_jwk_client", lambda: _FakeJWKClient(public_key))
    headers = _auth_headers(private_key, {"sub": "user_1", "o": {"id": pending_tenant["clerk_org_id"], "rol": "admin"}})

    resp = client.get("/tenants/me", headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == str(pending_tenant["id"])
    assert body["clerk_org_id"] == pending_tenant["clerk_org_id"]
    assert body["name"] == "Pending Tenant"
    assert body["subscription_status"] == "pending"
    assert body["plan_tier"] == "free"
    assert body["stripe_customer_id"] is None
    assert body["stripe_subscription_id"] is None
    assert "created_at" in body


def test_get_my_tenant_returns_every_field_once_active(active_tenant: dict, rsa_keypair, monkeypatch):
    private_key, public_key = rsa_keypair
    monkeypatch.setattr(security, "_jwk_client", lambda: _FakeJWKClient(public_key))
    headers = _auth_headers(private_key, {"sub": "user_2", "o": {"id": active_tenant["clerk_org_id"], "rol": "admin"}})

    resp = client.get("/tenants/me", headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == str(active_tenant["id"])
    assert body["slug"] == active_tenant["slug"]
    assert body["name"] == "Grace Community"
    assert body["plan_tier"] == "starter"
    assert body["subscription_status"] == "active"
    assert body["stripe_customer_id"].startswith("cus_")
    assert body["stripe_subscription_id"].startswith("sub_")


def test_get_my_tenant_without_organization_in_session_is_403(rsa_keypair, monkeypatch):
    """A verified session that just isn't in an org context — e.g. a
    solo-account Clerk user who hasn't created/joined an organization
    yet. extract_org_context returns None for this; get_current_tenant
    must not treat it as an authentication failure (401) or a missing-
    tenant-row failure (404) — it's neither."""
    private_key, public_key = rsa_keypair
    monkeypatch.setattr(security, "_jwk_client", lambda: _FakeJWKClient(public_key))
    headers = _auth_headers(private_key, {"sub": "user_3"})  # no "o" claim at all

    resp = client.get("/tenants/me", headers=headers)

    assert resp.status_code == 403
    assert "organization" in resp.json()["detail"].lower()


def test_get_my_tenant_for_unknown_org_is_404(rsa_keypair, monkeypatch):
    """A verified session with an active org, but no tenants row exists
    for it yet — the momentary gap right after /create-organization,
    before Clerk's organization.created webhook has landed."""
    private_key, public_key = rsa_keypair
    monkeypatch.setattr(security, "_jwk_client", lambda: _FakeJWKClient(public_key))
    headers = _auth_headers(private_key, {"sub": "user_4", "o": {"id": "org_never_provisioned", "rol": "admin"}})

    resp = client.get("/tenants/me", headers=headers)

    assert resp.status_code == 404


def test_get_my_tenant_requires_authentication():
    """No Authorization header at all — never reaches get_current_tenant,
    let alone the database."""
    resp = client.get("/tenants/me")
    assert resp.status_code == 403


# ============================================================================
# GET /tenants/by-slug/{slug}
# ============================================================================


def test_get_tenant_by_slug_returns_only_the_public_subset(active_tenant: dict):
    """No Authorization header — this route is deliberately reachable
    pre-auth (subdomain-routing branding). The row underneath has
    subscription_status='active' and real Stripe ids; none of that may
    appear in the response — see TenantPublicRead's docstring."""
    resp = client.get(f"/tenants/by-slug/{active_tenant['slug']}")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {"slug": active_tenant["slug"], "name": "Grace Community"}
    assert "subscription_status" not in body
    assert "stripe_customer_id" not in body
    assert "stripe_subscription_id" not in body
    assert "clerk_org_id" not in body
    assert "id" not in body


def test_get_tenant_by_slug_unknown_slug_is_404():
    resp = client.get("/tenants/by-slug/no-such-church-exists")
    assert resp.status_code == 404
