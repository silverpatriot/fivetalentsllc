"""Clerk webhook: organization.created -> tenants row.

This is the ONLY provisioning path for tenants rows in normal operation —
never a manual insert, never triggered from the frontend. See Task 3 in
the Phase 2 spec.
"""
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_raw_db
from app.core.idempotency import try_claim_event
from app.core.security import verify_clerk_webhook
from app.models import Tenant

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/webhooks/clerk", status_code=status.HTTP_200_OK)
async def clerk_webhook(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_raw_db)],
    svix_id: Annotated[str | None, Header(alias="svix-id")] = None,
    svix_timestamp: Annotated[str | None, Header(alias="svix-timestamp")] = None,
    svix_signature: Annotated[str | None, Header(alias="svix-signature")] = None,
) -> dict[str, str]:
    payload = await request.body()
    if not (svix_id and svix_timestamp and svix_signature):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing Svix headers")

    event = verify_clerk_webhook(
        payload,
        {
            "svix-id": svix_id,
            "svix-timestamp": svix_timestamp,
            "svix-signature": svix_signature,
        },
    )

    event_type = event.get("type", "")

    async with db.begin():
        # svix-id, not anything in the body: it's the actual unique-per-
        # delivery identifier. A retried delivery of the same logical
        # event reuses the same svix-id; org data.id is the ORG's id, not
        # the event's, and is shared across e.g. organization.created and
        # organization.updated for the same org — using it for dedup
        # would falsely treat those as duplicates of each other.
        should_process = await try_claim_event(
            db, source="clerk", external_event_id=svix_id, event_type=event_type
        )
        if not should_process:
            logger.info("Skipping duplicate Clerk webhook delivery: %s", svix_id)
            return {"status": "duplicate, skipped"}

        if event_type == "organization.created":
            await _handle_organization_created(db, event.get("data", {}))
        else:
            logger.info("Ignoring unhandled Clerk webhook event type: %s", event_type)

    return {"status": "ok"}


async def _handle_organization_created(db: AsyncSession, data: dict) -> None:
    org_id = data.get("id")
    slug = data.get("slug")
    name = data.get("name")
    if not (org_id and slug and name):
        # Don't silently no-op on a malformed payload — this is the only
        # provisioning path for tenants; a swallowed error here means a
        # church signs up and nothing happens.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="organization.created payload missing id/slug/name",
        )

    tenant = Tenant(clerk_org_id=org_id, slug=slug, name=name)
    db.add(tenant)
    try:
        await db.flush()
    except IntegrityError as exc:
        # Same org provisioned twice (e.g. a non-idempotent retry that
        # slipped past try_claim_event some other way, or a manual
        # re-send) — clerk_org_id and slug are both UNIQUE, so this is a
        # conflict, not silent double-provisioning.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Tenant already exists for this organization",
        ) from exc
