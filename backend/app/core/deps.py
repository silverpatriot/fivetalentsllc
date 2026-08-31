"""FastAPI dependencies: verified caller -> tenant_id -> RLS-scoped session.

get_db is the one route handlers should depend on for anything touching a
tenant-scoped table. It never accepts tenant_id from the client — only from
get_current_tenant_id, which derives it from the verified Clerk token.

get_active_tenant_id additionally requires subscription_status == 'active'
— use it (not get_current_tenant_id) for any route serving actual product
functionality. get_current_tenant_id alone is for the handful of routes
that must work for a signed-up-but-not-yet-paid tenant too (starting
checkout, checking billing status).
"""
import uuid
from collections.abc import AsyncGenerator
from typing import Annotated, Any

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import extract_org_context, verify_clerk_jwt
from app.db.session import AsyncSessionLocal, set_tenant_context
from app.models import Tenant

_bearer = HTTPBearer(auto_error=True)


async def get_raw_db() -> AsyncGenerator[AsyncSession, None]:
    """A session with no tenant context set. Only for queries against
    tables that are not tenant-scoped (tenants, webhook_events, plus any
    future platform-admin tooling) — tenant-scoped tables are unreadable
    through this session by design (fail-closed RLS)."""
    async with AsyncSessionLocal() as session:
        yield session


async def get_current_claims(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(_bearer)],
) -> dict[str, Any]:
    return verify_clerk_jwt(credentials.credentials)


async def get_current_tenant(
    claims: Annotated[dict[str, Any], Depends(get_current_claims)],
    db: Annotated[AsyncSession, Depends(get_raw_db)],
) -> Tenant:
    """The caller's tenant row, regardless of subscription_status. Use
    get_current_tenant_id / get_active_tenant_id instead unless a route
    genuinely needs the whole row (e.g. to read plan_tier before it's
    active)."""
    org = extract_org_context(claims)
    if org is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Session is not scoped to an organization",
        )
    result = await db.execute(select(Tenant).where(Tenant.clerk_org_id == org.org_id))
    tenant = result.scalar_one_or_none()
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown tenant")
    return tenant


async def get_current_tenant_id(
    tenant: Annotated[Tenant, Depends(get_current_tenant)],
) -> uuid.UUID:
    return tenant.id


def require_active_subscription(tenant: Tenant) -> uuid.UUID:
    """The actual access-gate check — pure logic, no I/O, no `await`.
    Split out from get_active_tenant_id specifically so it's testable
    (tests/test_stripe_webhook_flow.py) without any event-loop machinery:
    calling it doesn't need asyncio.run() or pytest-asyncio at all, which
    sidesteps a real cross-event-loop issue that surfaced trying to
    exercise the async dependency function directly in a test module that
    also drives the app through a synchronous TestClient.

    Task 3 in the Phase 2 spec requires that a canceled/pending
    subscription blocks access, not just that the DB column reflects it —
    this is that gate.
    """
    if tenant.subscription_status != "active":
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=f"Subscription is not active (status: {tenant.subscription_status!r})",
        )
    return tenant.id


async def get_active_tenant_id(
    tenant: Annotated[Tenant, Depends(get_current_tenant)],
) -> uuid.UUID:
    """Same as get_current_tenant_id, but also enforces
    subscription_status == 'active' — see require_active_subscription."""
    return require_active_subscription(tenant)


async def get_db(
    tenant_id: Annotated[uuid.UUID, Depends(get_active_tenant_id)],
) -> AsyncGenerator[AsyncSession, None]:
    """RLS-scoped session for the current request's tenant. Requires an
    active subscription — see get_active_tenant_id."""
    async with AsyncSessionLocal() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_id)
            yield session
