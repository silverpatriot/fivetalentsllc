"""app.services.plan_limits — the actual product meaning behind
Free/Starter/Growth/Enterprise's included monthly AI-generation quota
(Phase 5). Exercised against real Postgres and real usage_events rows,
not mocked — this is pure DB-counting logic, the same level
test_reference_corpus.py and friends already test data-shape things at.
"""
import uuid
from collections.abc import Iterator

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine

from app.services.plan_limits import PLAN_TIER_MONTHLY_AI_GENERATIONS, is_ai_generation_within_quota
from tests.conftest import set_tenant


@pytest.fixture
def make_tenant(pg_engine: Engine) -> Iterator[callable]:
    """Factory for an 'active' tenant at a given plan_tier. Deleting the
    tenant row cascades to any usage_events created for it
    (usage_events_tenant_id_fkey ON DELETE CASCADE) — nothing else to
    clean up."""
    created_ids: list[uuid.UUID] = []

    def _make(plan_tier: str) -> uuid.UUID:
        tenant_id = uuid.uuid4()
        with pg_engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO tenants (id, slug, name, clerk_org_id, subscription_status, plan_tier) "
                    "VALUES (:id, :slug, :name, :org, 'active', :plan_tier)"
                ),
                {
                    "id": str(tenant_id),
                    "slug": f"quota-{tenant_id.hex[:8]}",
                    "name": "Quota Test Tenant",
                    "org": f"org_{tenant_id.hex[:16]}",
                    "plan_tier": plan_tier,
                },
            )
        created_ids.append(tenant_id)
        return tenant_id

    yield _make

    with pg_engine.begin() as conn:
        for tenant_id in created_ids:
            conn.execute(sa.text("DELETE FROM tenants WHERE id = :id"), {"id": str(tenant_id)})


def _insert_ai_generation_events(pg_engine: Engine, tenant_id: uuid.UUID, count: int, *, outcome: str = "succeeded") -> None:
    with pg_engine.begin() as conn:
        set_tenant(conn, tenant_id)
        for _ in range(count):
            conn.execute(
                sa.text(
                    "INSERT INTO usage_events (tenant_id, event_type, quantity, outcome, billable) "
                    "VALUES (:tid, 'ai_generation', 1.0, :outcome, false)"
                ),
                {"tid": str(tenant_id), "outcome": outcome},
            )


def test_within_quota_for_a_fresh_free_tenant(pg_engine: Engine, make_tenant):
    tenant_id = make_tenant("free")
    assert is_ai_generation_within_quota(tenant_id) is True


def test_free_tenant_at_exactly_its_quota_is_not_within_quota(pg_engine: Engine, make_tenant):
    tenant_id = make_tenant("free")
    quota = PLAN_TIER_MONTHLY_AI_GENERATIONS["free"]
    _insert_ai_generation_events(pg_engine, tenant_id, quota)
    assert is_ai_generation_within_quota(tenant_id) is False


def test_free_tenant_one_below_quota_is_still_within_quota(pg_engine: Engine, make_tenant):
    tenant_id = make_tenant("free")
    quota = PLAN_TIER_MONTHLY_AI_GENERATIONS["free"]
    _insert_ai_generation_events(pg_engine, tenant_id, quota - 1)
    assert is_ai_generation_within_quota(tenant_id) is True


def test_failed_calls_never_count_against_quota(pg_engine: Engine, make_tenant):
    tenant_id = make_tenant("free")
    quota = PLAN_TIER_MONTHLY_AI_GENERATIONS["free"]
    # Twice the quota, but every one of them failed — a pastor shouldn't
    # be capped by generations that produced nothing.
    _insert_ai_generation_events(pg_engine, tenant_id, quota * 2, outcome="failed")
    assert is_ai_generation_within_quota(tenant_id) is True


def test_enterprise_is_unlimited_regardless_of_usage(pg_engine: Engine, make_tenant):
    assert PLAN_TIER_MONTHLY_AI_GENERATIONS["enterprise"] is None
    tenant_id = make_tenant("enterprise")
    _insert_ai_generation_events(pg_engine, tenant_id, 10_000)
    assert is_ai_generation_within_quota(tenant_id) is True


def test_higher_tiers_get_larger_quotas(pg_engine: Engine, make_tenant):
    free, starter, growth = (
        PLAN_TIER_MONTHLY_AI_GENERATIONS["free"],
        PLAN_TIER_MONTHLY_AI_GENERATIONS["starter"],
        PLAN_TIER_MONTHLY_AI_GENERATIONS["growth"],
    )
    assert free < starter < growth


def test_unrecognized_plan_tier_falls_back_to_the_free_quota_not_unlimited(pg_engine: Engine, make_tenant):
    # Shouldn't happen in practice (plan_tier's own DB default is
    # 'free'), but a typo/future tier name must fail closed, not open.
    tenant_id = make_tenant("some_future_tier_this_code_does_not_know_about")
    quota = PLAN_TIER_MONTHLY_AI_GENERATIONS["free"]
    _insert_ai_generation_events(pg_engine, tenant_id, quota)
    assert is_ai_generation_within_quota(tenant_id) is False
