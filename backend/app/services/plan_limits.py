"""Per-plan-tier usage quotas — what actually differentiates
Free/Starter/Growth/Enterprise (see the flat monthly prices in
scripts/stripe_setup.py, and app/api/billing.py's activate_free_tier for
how a tenant lands on 'free' with no Stripe involved) beyond price alone.
AI generations included in the flat price before Stripe's metered
overage price (kerygma_ai_generation_overage) starts billing per-call —
see app/services/generation.py's _record_llm_call, the only caller of
is_ai_generation_within_quota, and app/tasks/usage_reporting.py's
`billable` flag, which this decides for every AI_GENERATION row.

Not applied to transcription_minutes: there is no transcription pipeline
built yet (app/models/media_file.py exists; nothing populates it), so
there is nothing to meter a quota against.

Only a SUCCEEDED generation counts against quota or can be billable — a
failed LLM call (an OpenRouter outage, exhausted retries) shouldn't cost
a pastor quota or money for producing nothing. See _record_llm_call.

Known race: this reads a count in its own short transaction, separate
from the transaction that then inserts the new usage_events row
(app/tasks/usage_reporting.record_usage_event, called right after this).
Two generations landing in the same instant for the same tenant could
both read "still within quota" and both post as included, undercounting
overage by at most one. Given this product's actual concurrency (one
pastor's browser, not a traffic spike), that's an acceptable, cheap
tradeoff over a locking scheme — revisit if usage patterns ever make that
not true.

Period boundary is calendar month (UTC), not the tenant's actual Stripe
billing-cycle anchor — tenants doesn't track current_period_start/end
yet (see app/models/tenant.py). Fine pre-launch, when overage isn't
being billed for real either; revisit both together.
"""
import datetime
import uuid

from sqlalchemy import func, select

from app.db.session import tenant_session_sync
from app.models.tenant import Tenant
from app.models.usage_event import UsageEvent, UsageEventType

# None = unlimited — Enterprise is a custom, contact-sales price that
# covers everything, no metered overage ever.
PLAN_TIER_MONTHLY_AI_GENERATIONS: dict[str, int | None] = {
    "free": 8,  # enough to seriously try 2-3 sermons, not run a church on
    "starter": 40,  # ~weekly sermons plus a realistic amount of regeneration
    "growth": 150,  # multiple campuses / a media team
    "enterprise": None,
}

# A tenant somehow missing a recognized plan_tier (shouldn't happen —
# Tenant.plan_tier's own server_default is 'free', already the smallest
# defined quota) gets the most restrictive real quota rather than
# crashing or silently defaulting to unlimited.
_FALLBACK_QUOTA = PLAN_TIER_MONTHLY_AI_GENERATIONS["free"]


def _current_period_start() -> datetime.datetime:
    now = datetime.datetime.now(datetime.timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def is_ai_generation_within_quota(tenant_id: uuid.UUID) -> bool:
    """True if the tenant's NEXT successful AI_GENERATION this calendar
    month would still be within its plan's included quota. Sync (Celery-
    task style DB access, like the rest of usage metering) — call via
    run_in_threadpool from async code, as _record_llm_call does."""
    with tenant_session_sync(tenant_id) as session:
        plan_tier = session.execute(select(Tenant.plan_tier).where(Tenant.id == tenant_id)).scalar_one_or_none()
        quota = PLAN_TIER_MONTHLY_AI_GENERATIONS.get(plan_tier or "", _FALLBACK_QUOTA)
        if quota is None:
            return True
        used = session.execute(
            select(func.count())
            .select_from(UsageEvent)
            .where(
                UsageEvent.tenant_id == tenant_id,
                UsageEvent.event_type == UsageEventType.AI_GENERATION,
                UsageEvent.outcome == "succeeded",
                UsageEvent.created_at >= _current_period_start(),
            )
        ).scalar_one()
        return used < quota
