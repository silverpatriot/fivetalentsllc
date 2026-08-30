"""FastAPI dependencies: verified caller -> tenant_id -> RLS-scoped session.

get_db is the one route handlers should depend on for anything touching a
tenant-scoped table. It never accepts tenant_id from the client — only from
get_current_tenant_id, which derives it from the verified Clerk token.
"""
import uuid
from collections.abc import AsyncGenerator
from typing import Annotated, Any

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import verify_clerk_jwt
from app.db.session import AsyncSessionLocal, set_tenant_context
from app.models import Tenant

_bearer = HTTPBearer(auto_error=True)


async def get_raw_db() -> AsyncGenerator[AsyncSession, None]:
    """A session with no tenant context set. Only for queries against
    tables that are not tenant-scoped (tenants itself, plus any future
    platform-admin tooling) — tenant-scoped tables are unreadable through
    this session by design (fail-closed RLS)."""
    async with AsyncSessionLocal() as session:
        yield session


async def get_current_claims(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(_bearer)],
) -> dict[str, Any]:
    return verify_clerk_jwt(credentials.credentials)


async def get_current_tenant_id(
    claims: Annotated[dict[str, Any], Depends(get_current_claims)],
    db: Annotated[AsyncSession, Depends(get_raw_db)],
) -> uuid.UUID:
    org_id = claims.get("org_id")
    if not org_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Session is not scoped to an organization",
        )
    result = await db.execute(select(Tenant.id).where(Tenant.clerk_org_id == org_id))
    tenant_id = result.scalar_one_or_none()
    if tenant_id is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown tenant")
    return tenant_id


async def get_db(
    tenant_id: Annotated[uuid.UUID, Depends(get_current_tenant_id)],
) -> AsyncGenerator[AsyncSession, None]:
    """RLS-scoped session for the current request's tenant."""
    async with AsyncSessionLocal() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_id)
            yield session
