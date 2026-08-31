"""Webhook idempotency: claim an (source, external_event_id) pair exactly
once, atomically, so two concurrent deliveries of the same event can't
both process it.
"""
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def try_claim_event(
    db: AsyncSession, *, source: str, external_event_id: str, event_type: str
) -> bool:
    """Attempt to record this event as processed. Returns True if this
    call is the one that gets to process it (the INSERT landed), False if
    it's a duplicate delivery (a row for this (source, external_event_id)
    already existed — ON CONFLICT DO NOTHING means this call inserted
    nothing).

    Atomic via the table's UNIQUE (source, external_event_id) constraint
    — no separate "check, then insert" race window between two requests
    that arrive at nearly the same time (Stripe/Clerk retries can do
    this).
    """
    result = await db.execute(
        text(
            """
            INSERT INTO webhook_events (source, external_event_id, event_type)
            VALUES (:source, :external_event_id, :event_type)
            ON CONFLICT (source, external_event_id) DO NOTHING
            RETURNING id
            """
        ),
        {"source": source, "external_event_id": external_event_id, "event_type": event_type},
    )
    return result.first() is not None
