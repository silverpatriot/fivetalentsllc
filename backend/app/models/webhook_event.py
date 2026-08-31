from sqlalchemy import String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, UUIDPkMixin


class WebhookEvent(Base, UUIDPkMixin, CreatedAtMixin):
    """Idempotency record for inbound webhooks (Stripe, Clerk/Svix — both
    redeliver, and the same event can arrive more than once).

    Not tenant-scoped, no RLS: webhook processing happens before we
    necessarily know which tenant a given event belongs to (e.g. Clerk's
    organization.created fires *before* any tenants row exists for it).

    Idempotency check is a single atomic INSERT ... ON CONFLICT DO NOTHING
    on (source, external_event_id) — see app/api/webhooks_stripe.py and
    webhooks_clerk.py. Whichever concurrent delivery's INSERT actually
    lands is the one that processes the event; the other sees a conflict
    and skips, with no race window.
    """

    __tablename__ = "webhook_events"
    __table_args__ = (UniqueConstraint("source", "external_event_id", name="uq_webhook_events_source_event"),)

    source: Mapped[str] = mapped_column(String(20), nullable=False)  # 'stripe' | 'clerk'
    external_event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
