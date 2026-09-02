"""Per-plan-tier limits — what actually differentiates
Free/Starter/Growth/Enterprise (see the flat monthly prices in
scripts/stripe_setup.py, and app/api/billing.py's activate_free_tier for
how a tenant lands on 'free' with no Stripe involved) beyond price
alone. Two independent things live here:

- Sermon quota (is_within_sermon_quota): how many COMPLETED sermons
  (one full outline+draft generation succeeding) a tenant gets per
  calendar month before Stripe's metered overage price
  (kerygma_ai_generation_overage) starts billing per additional one.
  Deliberately counts completed sermons, not each individual LLM call —
  see app/services/generation.py's _record_llm_call, the only caller.
  Regenerating a preaching outline for an already-generated sermon
  (app/services/generation.py's generate_outline_from_manuscript) never
  consumes a slot — that's a follow-up on a sermon already paid for, not
  a new one.

- Cadence access (has_cadence_access): whether a tenant can use
  cadence-matching (voice-from-past-sermons) at all right now. Starter/
  Growth/Enterprise: always yes. Free: yes for CADENCE_TRIAL_DAYS from
  free_trial_started_at, then no — they keep their sermon quota
  afterward, just lose voice-matching. See
  app/services/context_assembly.py's fetch_cadence_examples, the only
  caller.

  This is currently ONE gate, not two, even though the product plan is
  eventually two cadence SOURCES (AI-generated past sermons vs. real mp3
  transcripts, Starter+ vs. Growth+ respectively) — there is no
  transcript-sourced cadence data yet (no transcription pipeline is
  built; app/models/media_file.py exists, nothing populates it), so
  there's nothing to gate separately yet. has_cadence_access governs the
  only cadence source that actually exists today (AI-generated-sermon
  matching). When transcription ships, expect a second
  has_mp3_cadence_access (Growth+/Enterprise, or Free within the same
  30-day window) gating a transcript-sourced subset of the corpus
  specifically — don't conflate the two into one flag at that point.

Known race (both functions): each reads a count/timestamp in its own
short transaction, separate from whatever the caller does next. Two
generations landing in the same instant for the same tenant could both
read "still within quota." Given this product's actual concurrency (one
pastor's browser, not a traffic spike), that's an acceptable, cheap
tradeoff over a locking scheme — revisit if usage patterns ever make
that not true.

Sermon-quota period boundary is calendar month (UTC), not the tenant's
actual Stripe billing-cycle anchor — tenants doesn't track
current_period_start/end yet. Fine pre-launch, when overage isn't being
billed for real either; revisit both together.
"""
import datetime
import uuid

from sqlalchemy import func, select

from app.db.session import tenant_session_sync
from app.models.generation_log import GenerationStage
from app.models.tenant import Tenant
from app.models.usage_event import UsageEvent, UsageEventType

# Phase 6: a lightweight cost/abuse guardrail on per-sermon edit
# requests — NOT a billing feature (see EDIT/EDIT_LOCATE's billable=False
# in _record_llm_call). Unlike the sermon quota above, which is soft
# (still runs, just becomes billable overage), this hard-blocks a request
# once hit, before any LLM call is made — same spirit as rate-limiting,
# not quota enforcement. Counts BOTH stages (a failed locate that never
# reached the actual edit call still cost a real LLM call, so it still
# counts), regardless of outcome, for the life of the sermon — not a
# rolling window. 30 gives real headroom for a pastor iterating point-by-
# point over a whole sermon while bounding worst-case per-sermon spend;
# raising it is a one-line change, unlike walking back unbounded usage
# once it exists.
MAX_EDITS_PER_SERMON = 30

_EDIT_STAGES = (GenerationStage.EDIT, GenerationStage.EDIT_LOCATE)

# None = unlimited — Enterprise is a custom, contact-sales price that
# covers everything, no metered overage ever.
PLAN_TIER_MONTHLY_SERMONS: dict[str, int | None] = {
    "free": 4,  # one Sunday a week, no cushion — the forcing function to upgrade
    "starter": 10,  # weekly preaching plus real headroom for regeneration/extra services
    "growth": 25,  # multiple campuses / a media team
    "enterprise": None,
}

# A tenant somehow missing a recognized plan_tier (shouldn't happen —
# Tenant.plan_tier's own server_default is 'free', already the smallest
# defined quota) gets the most restrictive real quota rather than
# crashing or silently defaulting to unlimited.
_FALLBACK_QUOTA = PLAN_TIER_MONTHLY_SERMONS["free"]

# How long a Free tenant keeps full cadence-tool access after choosing
# free (app/api/billing.py's activate_free_tier stamps the clock this
# counts from) — see this module's docstring.
CADENCE_TRIAL_DAYS = 30

_TIERS_WITH_PERMANENT_CADENCE_ACCESS = {"starter", "growth", "enterprise"}


def _current_period_start() -> datetime.datetime:
    now = datetime.datetime.now(datetime.timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def is_within_sermon_quota(tenant_id: uuid.UUID) -> bool:
    """True if the tenant's NEXT completed sermon this calendar month
    would still be within its plan's included quota. Sync (Celery-task
    style DB access, like the rest of usage metering) — call via
    run_in_threadpool from async code, as _record_llm_call does.

    Counts successful DRAFT-stage usage_events rows: exactly one gets
    written per sermon that actually finishes (app/services/
    generation.py's _run), regardless of how many LLM calls (outline +
    draft, +outline_condense if a preaching outline is made later) that
    sermon involved — see this module's docstring.
    """
    with tenant_session_sync(tenant_id) as session:
        plan_tier = session.execute(select(Tenant.plan_tier).where(Tenant.id == tenant_id)).scalar_one_or_none()
        quota = PLAN_TIER_MONTHLY_SERMONS.get(plan_tier or "", _FALLBACK_QUOTA)
        if quota is None:
            return True
        used = session.execute(
            select(func.count())
            .select_from(UsageEvent)
            .where(
                UsageEvent.tenant_id == tenant_id,
                UsageEvent.event_type == UsageEventType.AI_GENERATION,
                UsageEvent.generation_stage == GenerationStage.DRAFT,
                UsageEvent.outcome == "succeeded",
                UsageEvent.created_at >= _current_period_start(),
            )
        ).scalar_one()
        return used < quota


def is_within_edit_cap(tenant_id: uuid.UUID, sermon_id: uuid.UUID) -> bool:
    """True if this sermon has made fewer than MAX_EDITS_PER_SERMON real
    edit-related LLM calls so far. Sync, same calling convention as
    is_within_sermon_quota — call via run_in_threadpool. Checked BEFORE
    the edit endpoint makes any LLM call (app/api/sermons.py), so hitting
    the cap costs nothing further; already-spent calls before the cap
    was hit are, correctly, not retroactively undone."""
    with tenant_session_sync(tenant_id) as session:
        used = session.execute(
            select(func.count())
            .select_from(UsageEvent)
            .where(
                UsageEvent.tenant_id == tenant_id,
                UsageEvent.sermon_id == sermon_id,
                UsageEvent.event_type == UsageEventType.AI_GENERATION,
                UsageEvent.generation_stage.in_(_EDIT_STAGES),
            )
        ).scalar_one()
        return used < MAX_EDITS_PER_SERMON


def has_cadence_access(tenant_id: uuid.UUID) -> bool:
    """True if this tenant can use cadence-matching (voice-from-past-
    sermons) right now — see this module's docstring for what "cadence
    access" currently covers (just the AI-generated-sermon source; there
    is no other one built yet)."""
    with tenant_session_sync(tenant_id) as session:
        row = session.execute(
            select(Tenant.plan_tier, Tenant.free_trial_started_at).where(Tenant.id == tenant_id)
        ).one_or_none()
        if row is None:
            return False
        plan_tier, free_trial_started_at = row
        if plan_tier in _TIERS_WITH_PERMANENT_CADENCE_ACCESS:
            return True
        if plan_tier == "free":
            if free_trial_started_at is None:
                # activate_free_tier always sets this — null here means
                # this tenant somehow never actually went through it
                # (shouldn't happen), not an entitled trial with no
                # start. Fail closed.
                return False
            elapsed = datetime.datetime.now(datetime.timezone.utc) - free_trial_started_at
            return elapsed <= datetime.timedelta(days=CADENCE_TRIAL_DAYS)
        return False
