"""tenants.free_trial_started_at

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-01

Phase 5's pricing redesign: Free tenants get every cadence-matching tool
(not just the AI-generated-sermon one that exists today) for 30 days from
when they actually land on the free plan, then lose cadence access
entirely (though they keep their ongoing per-month sermon quota — see
app/services/plan_limits.py's has_cadence_access). Set once, by
app/api/billing.py's activate_free_tier — NOT tenants.created_at, which
is stamped at Clerk org-provisioning time and could predate a tenant
actually choosing free by any amount of time (they might sit in
'pending' for a while first, or go straight to a paid Checkout and never
have this column mean anything at all — nullable, and simply unused for
any tenant that never took the free path).
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE tenants ADD COLUMN free_trial_started_at TIMESTAMPTZ")


def downgrade() -> None:
    op.execute("ALTER TABLE tenants DROP COLUMN free_trial_started_at")
