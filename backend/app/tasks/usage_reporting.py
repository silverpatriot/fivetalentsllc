"""Usage metering: usage_events (Postgres) is the source of truth; Stripe
meter events are a downstream report of it, never the other way around.

Two entry points:
  - record_usage_event(): call this synchronously from wherever usage
    actually happens (a transcription completing, an AI generation
    completing). It only writes to Postgres and — if the row is
    `billable` — enqueues a task; it never calls Stripe itself, so it's
    safe to call from a request path.
  - sweep_unreported_usage(): a celery-beat periodic safety net that
    catches anything record_usage_event's queued task didn't get to
    (worker was down, task was lost, a future backfill script wrote
    usage_events directly, etc.) — belt and suspenders on top of the
    on-event path, not a replacement for it.

Not every row written here is meant to be reported to Stripe — see
`billable` on the model and record_usage_event's docstring. Phase 3
records one row per real LLM call (outline, draft) regardless of
success/failure, as `billable=False`, specifically so the raw ledger of
"what actually happened" and the separate, not-yet-decided question of
"what should a church be charged for" don't get conflated at write time.
"""
import logging
import uuid

import stripe
from sqlalchemy import create_engine, select, text

from app.core.config import get_settings
from app.db.session import tenant_session_sync
from app.models import Tenant, UsageEvent, UsageEventType
from app.models.generation_log import GenerationStage
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)
settings = get_settings()

# Deliberately the ADMIN (superuser) connection, and deliberately the only
# place in this codebase that is. sweep_unreported_usage is a genuine
# cross-tenant platform job — it has to look across every tenant to find
# unreported usage, which is exactly what RLS exists to prevent for
# anything else. Do not copy this pattern for request-scoped or
# per-tenant code; use tenant_session_sync there.
_sweep_engine = create_engine(settings.database_url_sync, pool_pre_ping=True)

_METER_EVENT_NAMES: dict[UsageEventType, str] = {
    UsageEventType.TRANSCRIPTION_MINUTE: settings.stripe_meter_transcription_minutes,
    UsageEventType.AI_GENERATION: settings.stripe_meter_ai_generations,
}


def record_usage_event(
    tenant_id: uuid.UUID,
    event_type: UsageEventType,
    quantity: float,
    *,
    sermon_id: uuid.UUID | None = None,
    generation_stage: GenerationStage | None = None,
    outcome: str | None = None,
    billable: bool = True,
) -> uuid.UUID:
    """Write the record of what happened, then — only if `billable` —
    queue its Stripe report. Never calls Stripe directly. Safe to call
    inline from a request/task path.

    billable=False (used for Phase 3's per-LLM-call AI_GENERATION rows)
    skips the enqueue entirely: there is deliberately no Stripe-reporting
    path for these yet, not even a delayed one via sweep_unreported_usage
    — see this module's docstring and app/models/usage_event.py.
    """
    with tenant_session_sync(tenant_id) as session:
        usage_event = UsageEvent(
            tenant_id=tenant_id,
            event_type=event_type,
            quantity=quantity,
            sermon_id=sermon_id,
            generation_stage=generation_stage,
            outcome=outcome,
            billable=billable,
        )
        session.add(usage_event)
        session.flush()
        usage_event_id = usage_event.id

    if billable:
        report_usage_event.delay(str(usage_event_id), str(tenant_id))
    return usage_event_id


@celery_app.task(bind=True, max_retries=5, default_retry_delay=60)
def report_usage_event(self, usage_event_id: str, tenant_id: str) -> None:
    """Report exactly one usage_events row to Stripe as a meter event.

    Idempotent two ways: stripe_usage_record_id already set means we
    already reported this row, so it's a no-op; and the meter event's
    `identifier` is set to our own usage_event id, so even if we somehow
    called Stripe twice for the same row, Stripe itself de-duplicates by
    identifier.
    """
    tid = uuid.UUID(tenant_id)
    with tenant_session_sync(tid) as session:
        usage_event = session.get(UsageEvent, uuid.UUID(usage_event_id))
        if usage_event is None:
            logger.warning("usage_event %s not found (tenant %s) — skipping", usage_event_id, tenant_id)
            return
        if usage_event.stripe_usage_record_id is not None:
            logger.info("usage_event %s already reported — skipping", usage_event_id)
            return
        if not usage_event.billable:
            # Defense in depth — record_usage_event already skips
            # enqueueing this task for a non-billable row, and the sweep
            # query below excludes them too. This guard is for the case
            # neither of those applies: something calls this task
            # directly (a retry queued before billable existed, a manual
            # invocation, a future call site that forgets the check).
            logger.info("usage_event %s is not billable — skipping Stripe report", usage_event_id)
            return

        tenant = session.execute(select(Tenant).where(Tenant.id == tid)).scalar_one_or_none()
        if tenant is None or not tenant.stripe_customer_id:
            # Not necessarily an error: usage could theoretically be
            # recorded before Checkout completes. Leave stripe_usage_record_id
            # unset so sweep_unreported_usage picks it up once the tenant
            # has a stripe_customer_id.
            logger.info(
                "tenant %s has no stripe_customer_id yet — deferring usage_event %s", tenant_id, usage_event_id
            )
            return

        meter_event_name = _METER_EVENT_NAMES.get(usage_event.event_type)
        if not meter_event_name:
            logger.error(
                "No Stripe meter configured for %s — blocked on scripts/stripe_setup.py "
                "having been run and STRIPE_METER_* set in .env",
                usage_event.event_type,
            )
            return

        stripe.api_key = settings.stripe_secret_key
        try:
            meter_event = stripe.billing.MeterEvent.create(
                event_name=meter_event_name,
                payload={
                    "stripe_customer_id": tenant.stripe_customer_id,
                    "value": str(usage_event.quantity),
                },
                identifier=str(usage_event.id),
            )
        except stripe.StripeError as exc:
            # The Postgres row is already committed and safe regardless
            # of what happens here — that's the whole point of writing it
            # first. Retry; nothing about "what happened" is at risk.
            raise self.retry(exc=exc) from exc

        usage_event.stripe_usage_record_id = meter_event.identifier or meter_event.id
        # tenant_session_sync's `with` block commits this on clean exit.


@celery_app.task
def sweep_unreported_usage(limit: int = 500) -> dict[str, int]:
    """celery-beat periodic task — see module docstring. Excludes
    billable=false rows on purpose — those are Phase 3's per-LLM-call
    ledger entries with no Stripe-reporting path yet; this sweep existing
    to catch anything record_usage_event's own enqueue missed must not
    become a second way for them to get reported anyway."""
    with _sweep_engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT id, tenant_id FROM usage_events "
                "WHERE stripe_usage_record_id IS NULL AND billable = true ORDER BY created_at LIMIT :limit"
            ),
            {"limit": limit},
        ).fetchall()

    for row in rows:
        report_usage_event.delay(str(row.id), str(row.tenant_id))

    return {"queued": len(rows)}
