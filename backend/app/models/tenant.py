from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, UUIDPkMixin


class Tenant(Base, UUIDPkMixin, CreatedAtMixin):
    """The root of tenancy. Not itself RLS-scoped — everything else scopes
    to this table's id."""

    __tablename__ = "tenants"

    slug: Mapped[str] = mapped_column(String(63), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    clerk_org_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    plan_tier: Mapped[str] = mapped_column(String(50), nullable=False, server_default="free")
    stripe_customer_id: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    # pending -> active (checkout.session.completed) -> canceled
    # (customer.subscription.deleted). Set by migration 0003; a tenant row
    # created by the Clerk org-provisioning webhook exists in 'pending'
    # from the moment it's created, before Stripe Checkout ever happens.
    subscription_status: Mapped[str] = mapped_column(String(50), nullable=False, server_default="pending")
    # Migration 0011 — set once, by app/api/billing.py's
    # activate_free_tier, NOT tenants.created_at (see that migration's
    # docstring for why). Null for any tenant that never activated free.
    # app/services/plan_limits.py's has_cadence_access is the only reader.
    free_trial_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
