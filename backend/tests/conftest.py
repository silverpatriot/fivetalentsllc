"""Shared fixtures: the Phase 1 RLS fixtures below, plus Phase 2 additions
— signed test payloads for Stripe/Clerk webhooks and a self-signed JWT for
Clerk session token verification. All require a real Postgres with
migrations applied — see the Phase 1 docstring below, still accurate.

    alembic upgrade head
    pytest tests/

Set TEST_DATABASE_URL to point at a disposable database if you don't want
this touching whatever DATABASE_URL_SYNC in .env points at. Falls back to
DATABASE_URL_SYNC otherwise.
"""
import base64
import hashlib
import hmac
import json
import os
import time
import uuid
from collections.abc import Iterator

import jwt
import pytest
import sqlalchemy as sa
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import Connection, Engine

from app.core.config import get_settings


def _database_url() -> str:
    # database_url_app_sync, NOT database_url_sync — the latter is the
    # admin/superuser role (migrations only). Connecting the RLS test
    # suite through it would make every policy look ineffective, because
    # superusers bypass RLS unconditionally, independent of the policies
    # or grants being tested.
    return os.environ.get("TEST_DATABASE_URL") or get_settings().database_url_app_sync


@pytest.fixture(scope="session")
def pg_engine() -> Iterator[Engine]:
    engine = sa.create_engine(_database_url())
    yield engine
    engine.dispose()


def set_tenant(conn: Connection, tenant_id: uuid.UUID) -> None:
    """Mirrors app.db.session.set_tenant_context, but sync + raw for tests.

    For the "no context set at all" case, tests simply don't call this on a
    fresh connection — that's the actual failure mode being tested, not a
    context set to some sentinel value.
    """
    conn.execute(
        sa.text("SELECT set_config('app.current_tenant_id', :tid, true)"),
        {"tid": str(tenant_id)},
    )


@pytest.fixture
def two_tenants(pg_engine: Engine) -> Iterator[tuple[uuid.UUID, uuid.UUID]]:
    """Two real tenant rows, each with one sermon, inserted under their own
    tenant context (proving inserts work the same way reads do). Cleaned up
    via cascade delete on the tenant rows themselves."""
    tenant_a_id = uuid.uuid4()
    tenant_b_id = uuid.uuid4()

    with pg_engine.begin() as conn:
        # tenants has no RLS — it's the tenancy root, inserted with no context.
        conn.execute(
            sa.text(
                "INSERT INTO tenants (id, slug, name, clerk_org_id) "
                "VALUES (:id, :slug, :name, :org)"
            ),
            [
                {"id": str(tenant_a_id), "slug": f"tenant-a-{tenant_a_id.hex[:8]}",
                 "name": "Tenant A", "org": f"org_a_{tenant_a_id.hex[:8]}"},
                {"id": str(tenant_b_id), "slug": f"tenant-b-{tenant_b_id.hex[:8]}",
                 "name": "Tenant B", "org": f"org_b_{tenant_b_id.hex[:8]}"},
            ],
        )

    for tenant_id, title in [(tenant_a_id, "Tenant A's sermon"), (tenant_b_id, "Tenant B's sermon")]:
        with pg_engine.begin() as conn:
            set_tenant(conn, tenant_id)
            conn.execute(
                sa.text(
                    "INSERT INTO sermons (tenant_id, title, format) "
                    "VALUES (:tid, :title, 'topical')"
                ),
                {"tid": str(tenant_id), "title": title},
            )

    yield tenant_a_id, tenant_b_id

    with pg_engine.begin() as conn:
        # No tenant context needed: DELETE on `tenants` isn't RLS-scoped,
        # and FK ON DELETE CASCADE isn't subject to the children's RLS
        # policies either — it's an internal referential-integrity action.
        conn.execute(
            # Two plain scalar params, not an array bind — a scalar
            # compared directly to a uuid column gets its type inferred
            # correctly with no cast needed (same as every other bind
            # param in this file). An array bind needed an explicit
            # ::uuid[] cast, and a bind parameter immediately followed by
            # `::` isn't recognized as a parameter at all by SQLAlchemy's
            # text() parser — it requires a non-word, non-colon character
            # right after the name. Simplest fix: don't use an array here.
            sa.text("DELETE FROM tenants WHERE id = :id_a OR id = :id_b"),
            {"id_a": str(tenant_a_id), "id_b": str(tenant_b_id)},
        )


# ============================================================================
# Phase 2: signed test payloads. These implement the SAME signing schemes
# Stripe/Svix use client-side, so that verify_stripe_webhook /
# verify_clerk_webhook — the actual production verification code — can be
# exercised for real, both accepting a validly-signed payload and
# rejecting a tampered one, without needing a live Stripe/Clerk account.
# The signing math itself is a secondary source (not what's under test);
# what's under test is our verification code accepting/rejecting correctly.
# ============================================================================


def sign_stripe_payload(payload: bytes, secret: str, *, timestamp: int | None = None) -> str:
    """Build a Stripe-Signature header value the way Stripe itself does:
    t=<unix ts>,v1=<hex hmac-sha256 of "{ts}.{payload}">."""
    ts = timestamp if timestamp is not None else int(time.time())
    signed_payload = f"{ts}.".encode() + payload
    sig = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


def sign_svix_payload(
    msg_id: str, payload: bytes, secret: str, *, timestamp: int | None = None
) -> dict[str, str]:
    """Build the three svix-* headers the way Svix (Clerk's webhook
    delivery provider) itself does. Secret is the whsec_... value as
    given by Clerk; decode past the prefix, base64-decode the rest."""
    ts = timestamp if timestamp is not None else int(time.time())
    secret_bytes = base64.b64decode(secret.removeprefix("whsec_"))
    signed_content = f"{msg_id}.{ts}.".encode() + payload
    sig = base64.b64encode(hmac.new(secret_bytes, signed_content, hashlib.sha256).digest()).decode()
    return {
        "svix-id": msg_id,
        "svix-timestamp": str(ts),
        "svix-signature": f"v1,{sig}",
    }


@pytest.fixture(scope="session")
def rsa_keypair() -> tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


def make_clerk_jwt(
    private_key: rsa.RSAPrivateKey, claims: dict, *, kid: str = "test-key-1"
) -> str:
    """Sign a JWT shaped like a Clerk session token, using our own test
    keypair — not Clerk's real one, which we don't have. This tests
    verify_clerk_jwt's verification logic (signature check, claim
    extraction) genuinely; it can't test that Clerk's real JWKS endpoint
    is reachable or serves what we expect, which is the one part of this
    that a real Clerk test account is unavoidably needed to prove.
    """
    now = int(time.time())
    payload = {"iat": now, "exp": now + 300, **claims}
    return jwt.encode(payload, private_key, algorithm="RS256", headers={"kid": kid})


@pytest.fixture
def pending_tenant(pg_engine: Engine) -> Iterator[dict]:
    """A tenant row in the state the Clerk org-provisioning webhook
    creates one in: 'pending', no Stripe fields yet."""
    tenant_id = uuid.uuid4()
    clerk_org_id = f"org_{tenant_id.hex[:16]}"
    with pg_engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO tenants (id, slug, name, clerk_org_id) "
                "VALUES (:id, :slug, :name, :org)"
            ),
            {
                "id": str(tenant_id),
                "slug": f"pending-{tenant_id.hex[:8]}",
                "name": "Pending Tenant",
                "org": clerk_org_id,
            },
        )
    yield {"id": tenant_id, "clerk_org_id": clerk_org_id}
    with pg_engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM tenants WHERE id = :id"), {"id": str(tenant_id)})


@pytest.fixture
def stripe_event_factory():
    """Build a JSON payload shaped like a real stripe.Event, with a fresh
    event id and current timestamp — signed via sign_stripe_payload for
    the actual HTTP test."""

    def _make(event_type: str, data_object: dict, event_id: str | None = None) -> bytes:
        body = {
            "id": event_id or f"evt_test_{uuid.uuid4().hex}",
            "object": "event",
            "type": event_type,
            "data": {"object": data_object},
        }
        return json.dumps(body).encode()

    return _make


# NOTE: an earlier version of this file had an autouse fixture here that
# disposed app.db.session.engine's pool after every test, to stop stale
# asyncpg connections from a closed event loop leaking into the next
# test. It didn't fully work — test_duplicate_checkout_event_is_not_reprocessed
# makes two requests inside ONE test, and disposal between tests never
# ran between those two calls, so the collision could still happen
# within a single test. The actual fix is upstream, in
# app/db/session.py: the engine is constructed with NullPool whenever
# the process is pytest, so there's never a pooled connection to begin
# with, regardless of how many requests one test makes or which HTTP
# test client style it uses. Nothing to do here anymore — that's rather
# the point.


@pytest.fixture
def _clean_webhook_events(pg_engine: Engine) -> Iterator[None]:
    """webhook_events is the idempotency ledger (source, external_event_id)
    — see app/core/idempotency.py. Nothing was ever cleaning it up between
    test runs, and several tests use fixed, hardcoded event ids
    ("evt_checkout_1", etc.) rather than fresh ones. Against a database
    that isn't wiped between runs (true for most of this project's local
    dev workflow), a SECOND run finds those ids already claimed from the
    FIRST run and correctly (by design) treats them as duplicate
    deliveries — try_claim_event returns False, the handler never runs,
    and the webhook still returns 200. That's not a bug in the app; it's
    stale fixture data defeating the exact mechanism the idempotency
    tests are trying to prove.

    NOT autouse=True: that applied it to every test in the suite,
    including test_org_context.py/test_clerk_jwt.py/test_webhook_crypto.py,
    which are deliberately runnable with zero database — pytest.py wants
    pg_engine (a live Postgres connection) to even set up, so autouse
    broke that property for tests that never touch webhook_events at all.
    Instead: test_stripe_webhook_flow.py and test_clerk_webhook_flow.py —
    the only files that write to webhook_events — pull this in via a
    module-level `pytestmark = [..., pytest.mark.usefixtures(...)]`, which
    applies it to every test *in those files* automatically, so a new
    test added there still can't forget it, without forcing a DB
    dependency onto files that were never meant to have one.
    """
    with pg_engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM webhook_events"))
    yield
