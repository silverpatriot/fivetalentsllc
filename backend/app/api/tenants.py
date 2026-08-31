"""Two tenant-lookup routes, deliberately with very different trust levels:

- GET /tenants/me: requires a verified Clerk session, returns the full
  TenantRead (including subscription_status) for the caller's own org.
  This is what the frontend dashboard layout calls to decide whether to
  redirect to /subscription-required — reusing get_current_tenant (the
  same dependency billing.py already uses), not reimplementing the
  active/pending check client-side. It deliberately depends on
  get_current_tenant, not get_active_tenant_id: a pending tenant must be
  able to call this to find out it's pending, not get a 402 instead of an
  answer.
- GET /tenants/by-slug/{slug}: NO auth at all, on purpose — the
  subdomain-routing middleware (frontend/middleware.ts) needs to resolve
  `<slug>.kerygma.church` to branding before anyone has signed in. Returns
  only TenantPublicRead (name/slug) — see that schema for why nothing else
  is safe to expose here.
"""
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_tenant, get_raw_db
from app.models import Tenant
from app.schemas.tenant import TenantPublicRead, TenantRead

router = APIRouter(prefix="/tenants", tags=["tenants"])


@router.get("/me", response_model=TenantRead)
async def get_my_tenant(tenant: Annotated[Tenant, Depends(get_current_tenant)]) -> Tenant:
    return tenant


@router.get("/by-slug/{slug}", response_model=TenantPublicRead)
async def get_tenant_by_slug(slug: str, db: Annotated[AsyncSession, Depends(get_raw_db)]) -> Tenant:
    result = await db.execute(select(Tenant).where(Tenant.slug == slug))
    tenant = result.scalar_one_or_none()
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown tenant")
    return tenant
