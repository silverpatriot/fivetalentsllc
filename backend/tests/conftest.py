"""Fixtures for the RLS proof tests.

Requires a real Postgres with migration 0001 already applied — this is
deliberately not mocked. RLS is a database feature; a test that doesn't
touch a real database with the real policies in place proves nothing.

    alembic upgrade head
    pytest tests/test_rls.py

Set TEST_DATABASE_URL to point at a disposable database if you don't want
this touching whatever DATABASE_URL_SYNC in .env points at. Falls back to
DATABASE_URL_SYNC otherwise.
"""
import os
import uuid
from collections.abc import Iterator

import pytest
import sqlalchemy as sa
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
