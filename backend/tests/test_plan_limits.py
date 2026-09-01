"""app.services.plan_limits — the actual product meaning behind
Free/Starter/Growth/Enterprise (Phase 5): a per-tier monthly sermon
quota, and cadence-matching access (permanent for paid tiers, a 30-day
trial window for Free). Exercised against real Postgres and real
usage_events/tenants rows, not mocked — this is pure DB-counting logic,
the same level test_reference_corpus.py and friends already test
data-shape things at.
"""
import datetime
import uuid
from collections.abc import Iterator

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine

from app.services.plan_limits import (
    CADENCE_TRIAL_DAYS,
    PLAN_TIER_MONTHLY_SERMONS,
    has_cadence_access,
    is_within_sermon_quota,
)
from tests.conftest import set_tenant


@pytest.fixture
def make_tenant(pg_engine: Engine) -> Iterator[callable]:
    """Factory for an 'active' tenant at a given plan_tier, optionally
    with a free_trial_started_at timestamp. Deleting the tenant row
    cascades to any usage_events created for it
    (usage_events_tenant_id_fkey ON DELETE CASCADE) — nothing else to
    clean up."""
    created_ids: list[uuid.UUID] = []

    def _make(plan_tier: str, *, free_trial_started_at: datetime.datetime | None = None) -> uuid.UUID:
        tenant_id = uuid.uuid4()
        with pg_engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO tenants (id, slug, name, clerk_org_id, subscription_status, plan_tier, free_trial_started_at) "
                    "VALUES (:id, :slug, :name, :org, 'active', :plan_tier, :trial_start)"
                ),
                {
                    "id": str(tenant_id),
                    "slug": f"quota-{tenant_id.hex[:8]}",
                    "name": "Quota Test Tenant",
                    "org": f"org_{tenant_id.hex[:16]}",
                    "plan_tier": plan_tier,
                    "trial_start": free_trial_started_at,
                },
            )
        created_ids.append(tenant_id)
        return tenant_id

    yield _make

    with pg_engine.begin() as conn:
        for tenant_id in created_ids:
            conn.execute(sa.text("DELETE FROM tenants WHERE id = :id"), {"id": str(tenant_id)})


def _insert_completed_sermons(pg_engine: Engine, tenant_id: uuid.UUID, count: int, *, outcome: str = "succeeded") -> None:
    """One DRAFT-stage usage_events row per completed sermon — the unit
    is_within_sermon_quota actually counts (not raw LLM calls)."""
    with pg_engine.begin() as conn:
        set_tenant(conn, tenant_id)
        for _ in range(count):
            conn.execute(
                sa.text(
                    "INSERT INTO usage_events (tenant_id, event_type, generation_stage, quantity, outcome, billable) "
                    "VALUES (:tid, 'ai_generation', 'draft', 1.0, :outcome, false)"
                ),
                {"tid": str(tenant_id), "outcome": outcome},
            )


# ============================================================================
# is_within_sermon_quota
# ============================================================================


def test_within_quota_for_a_fresh_free_tenant(pg_engine: Engine, make_tenant):
    tenant_id = make_tenant("free")
    assert is_within_sermon_quota(tenant_id) is True


def test_free_tenant_at_exactly_its_quota_is_not_within_quota(pg_engine: Engine, make_tenant):
    tenant_id = make_tenant("free")
    quota = PLAN_TIER_MONTHLY_SERMONS["free"]
    _insert_completed_sermons(pg_engine, tenant_id, quota)
    assert is_within_sermon_quota(tenant_id) is False


def test_free_tenant_one_below_quota_is_still_within_quota(pg_engine: Engine, make_tenant):
    tenant_id = make_tenant("free")
    quota = PLAN_TIER_MONTHLY_SERMONS["free"]
    _insert_completed_sermons(pg_engine, tenant_id, quota - 1)
    assert is_within_sermon_quota(tenant_id) is True


def test_failed_sermons_never_count_against_quota(pg_engine: Engine, make_tenant):
    tenant_id = make_tenant("free")
    quota = PLAN_TIER_MONTHLY_SERMONS["free"]
    # Twice the quota, but every one of them failed — a pastor shouldn't
    # be capped by generations that produced nothing.
    _insert_completed_sermons(pg_engine, tenant_id, quota * 2, outcome="failed")
    assert is_within_sermon_quota(tenant_id) is True


def test_enterprise_is_unlimited_regardless_of_usage(pg_engine: Engine, make_tenant):
    assert PLAN_TIER_MONTHLY_SERMONS["enterprise"] is None
    tenant_id = make_tenant("enterprise")
    _insert_completed_sermons(pg_engine, tenant_id, 10_000)
    assert is_within_sermon_quota(tenant_id) is True


def test_higher_tiers_get_larger_quotas(pg_engine: Engine, make_tenant):
    free, starter, growth = (
        PLAN_TIER_MONTHLY_SERMONS["free"],
        PLAN_TIER_MONTHLY_SERMONS["starter"],
        PLAN_TIER_MONTHLY_SERMONS["growth"],
    )
    assert free < starter < growth


def test_unrecognized_plan_tier_falls_back_to_the_free_quota_not_unlimited(pg_engine: Engine, make_tenant):
    # Shouldn't happen in practice (plan_tier's own DB default is
    # 'free'), but a typo/future tier name must fail closed, not open.
    tenant_id = make_tenant("some_future_tier_this_code_does_not_know_about")
    quota = PLAN_TIER_MONTHLY_SERMONS["free"]
    _insert_completed_sermons(pg_engine, tenant_id, quota)
    assert is_within_sermon_quota(tenant_id) is False


# ============================================================================
# has_cadence_access
# ============================================================================


def test_starter_growth_enterprise_have_permanent_cadence_access(pg_engine: Engine, make_tenant):
    for tier in ("starter", "growth", "enterprise"):
        tenant_id = make_tenant(tier)
        assert has_cadence_access(tenant_id) is True, tier


def test_free_tenant_within_trial_window_has_cadence_access(pg_engine: Engine, make_tenant):
    started = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)
    tenant_id = make_tenant("free", free_trial_started_at=started)
    assert has_cadence_access(tenant_id) is True


def test_free_tenant_near_the_end_of_the_trial_still_has_access(pg_engine: Engine, make_tenant):
    # A few minutes shy of the boundary, not exactly on it — `started` is
    # captured before the DB round-trip that make_tenant/has_cadence_access
    # do, so an exact-boundary timestamp is flaky (real elapsed time by
    # the time the check runs is always a little more than requested).
    started = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=CADENCE_TRIAL_DAYS, minutes=-5)
    tenant_id = make_tenant("free", free_trial_started_at=started)
    assert has_cadence_access(tenant_id) is True


def test_free_tenant_past_the_trial_window_loses_cadence_access(pg_engine: Engine, make_tenant):
    started = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=CADENCE_TRIAL_DAYS + 1)
    tenant_id = make_tenant("free", free_trial_started_at=started)
    assert has_cadence_access(tenant_id) is False


def test_free_tenant_that_never_activated_has_no_cadence_access(pg_engine: Engine, make_tenant):
    """free_trial_started_at is null — shouldn't happen in practice
    (activate_free_tier always sets it), but fail closed, not open."""
    tenant_id = make_tenant("free", free_trial_started_at=None)
    assert has_cadence_access(tenant_id) is False


def test_unrecognized_plan_tier_has_no_cadence_access(pg_engine: Engine, make_tenant):
    tenant_id = make_tenant("some_future_tier_this_code_does_not_know_about")
    assert has_cadence_access(tenant_id) is False
