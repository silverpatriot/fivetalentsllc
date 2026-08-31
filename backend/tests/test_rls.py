"""Proves tenant isolation at the database level.

Every test here operates on `sermons`, which is representative of every
tenant-scoped table (all six get the identical policy from migration 0001).
If these pass, the isolation guarantee holds regardless of what a route
handler's Python code does or forgets to do — a compromised or buggy app
server still can't cross tenant boundaries as long as it authenticates as
this role and set_tenant_context runs.
"""
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine

from tests.conftest import set_tenant


def test_default_query_only_sees_own_tenant(pg_engine: Engine, two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    tenant_a, tenant_b = two_tenants

    with pg_engine.begin() as conn:
        set_tenant(conn, tenant_a)
        rows = conn.execute(sa.text("SELECT title FROM sermons")).fetchall()

    titles = {row[0] for row in rows}
    assert titles == {"Tenant A's sermon"}


def test_malicious_query_by_explicit_id_cannot_bypass(
    pg_engine: Engine, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """Even a query that names tenant B's row by its exact primary key
    should not be able to retrieve it while scoped to tenant A — proving
    the policy applies regardless of the query shape, not just to an
    un-filtered SELECT *."""
    tenant_a, tenant_b = two_tenants

    with pg_engine.begin() as conn:
        set_tenant(conn, tenant_b)
        b_sermon_id = conn.execute(
            sa.text("SELECT id FROM sermons WHERE tenant_id = :tid"), {"tid": str(tenant_b)}
        ).scalar_one()

    with pg_engine.begin() as conn:
        set_tenant(conn, tenant_a)
        row = conn.execute(
            sa.text("SELECT * FROM sermons WHERE id = :id"), {"id": str(b_sermon_id)}
        ).fetchone()

    assert row is None


def test_malicious_negated_filter_cannot_bypass(
    pg_engine: Engine, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """A query that explicitly asks for rows NOT belonging to the current
    tenant should still return nothing — the RLS predicate is ANDed onto
    every query, it doesn't just add a default filter that a WHERE clause
    can override."""
    tenant_a, _tenant_b = two_tenants

    with pg_engine.begin() as conn:
        set_tenant(conn, tenant_a)
        rows = conn.execute(
            sa.text("SELECT * FROM sermons WHERE tenant_id != :tid"), {"tid": str(tenant_a)}
        ).fetchall()

    assert rows == []


def test_cross_tenant_insert_is_rejected(
    pg_engine: Engine, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """WITH CHECK, not just USING: a session scoped to tenant A cannot
    insert a row claiming to belong to tenant B."""
    tenant_a, tenant_b = two_tenants

    with pytest.raises(sa.exc.DBAPIError):
        with pg_engine.begin() as conn:
            set_tenant(conn, tenant_a)
            conn.execute(
                sa.text(
                    "INSERT INTO sermons (tenant_id, title, format) "
                    "VALUES (:tid, 'forged', 'topical')"
                ),
                {"tid": str(tenant_b)},
            )


def test_missing_tenant_context_raises_instead_of_leaking(
    pg_engine: Engine, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """A transaction that never calls set_tenant_context at all — the
    'someone forgot to wire it up' bug — must never see all tenants' rows.

    It raises rather than silently returning zero rows: deliberate, see
    migration 0001's docstring. Both are safe (neither ever leaks another
    tenant's data); a loud failure is more operable than one that looks
    like ordinary empty state.
    """
    with pytest.raises(sa.exc.DBAPIError):
        with pg_engine.begin() as conn:
            conn.execute(sa.text("SELECT * FROM sermons")).fetchall()


def test_isolation_holds_on_a_second_table(
    pg_engine: Engine, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """Spot-check that the same policy shape was actually applied to
    another tenant-scoped table, not just `sermons`."""
    tenant_a, tenant_b = two_tenants

    with pg_engine.begin() as conn:
        set_tenant(conn, tenant_a)
        conn.execute(
            sa.text(
                "INSERT INTO usage_events (tenant_id, event_type, quantity) "
                "VALUES (:tid, 'ai_generation', 1)"
            ),
            {"tid": str(tenant_a)},
        )
    with pg_engine.begin() as conn:
        set_tenant(conn, tenant_b)
        conn.execute(
            sa.text(
                "INSERT INTO usage_events (tenant_id, event_type, quantity) "
                "VALUES (:tid, 'ai_generation', 1)"
            ),
            {"tid": str(tenant_b)},
        )

    with pg_engine.begin() as conn:
        set_tenant(conn, tenant_a)
        rows = conn.execute(sa.text("SELECT tenant_id FROM usage_events")).fetchall()

    assert {row[0] for row in rows} == {tenant_a}
