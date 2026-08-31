"""Clerk JWT verification, plus webhook signature verification for both
Clerk (Svix-signed) and Stripe.
"""
import dataclasses
import json
from functools import lru_cache
from typing import Any

import jwt
import stripe
from fastapi import HTTPException, status
from jwt import PyJWKClient
from svix.webhooks import Webhook, WebhookVerificationError

from app.core.config import get_settings

settings = get_settings()


@lru_cache
def _jwk_client() -> PyJWKClient:
    if not settings.clerk_jwks_url:
        raise RuntimeError("CLERK_JWKS_URL is not configured")
    # PyJWKClient caches keys internally and re-fetches on an unknown kid,
    # so this is safe to call per-request.
    return PyJWKClient(settings.clerk_jwks_url, cache_keys=True)


def verify_clerk_jwt(token: str) -> dict[str, Any]:
    """Verify a Clerk session token and return its claims.

    Raises HTTPException(401) on any verification failure — expired,
    wrong signature, wrong audience, malformed, etc. Never returns claims
    for a token that didn't verify.
    """
    try:
        signing_key = _jwk_client().get_signing_key_from_jwt(token)
        claims: dict[str, Any] = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            options={"require": ["exp", "iat"]},
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired session token",
        ) from exc
    return claims


@dataclasses.dataclass(frozen=True)
class OrgContext:
    org_id: str
    org_role: str | None
    org_slug: str | None


def extract_org_context(claims: dict[str, Any]) -> OrgContext | None:
    """Pull organization identity out of a verified Clerk claims dict.

    As of the current Clerk session token format (v2, checked against
    Clerk's docs — see backend/app/core/security.py history/PR for the
    date; this is exactly the kind of detail that silently rots), org data
    lives under a compact nested `o` claim to keep the JWT small:

        {"o": {"id": "org_...", "rol": "admin", "slg": "org-slug"}}

    NOT flat `org_id` / `org_role` / `org_slug` top-level claims — that
    was the *previous* shape and is what you'd get by guessing from
    memory instead of checking. This function accepts either shape
    defensively (some Clerk instances/older SDKs may still emit the flat
    form), but the nested `o` form is what a current Clerk app actually
    sends by default.

    Returns None if the token has no active organization at all (a valid,
    verified session that just isn't in an org context) — callers must
    treat that as "no tenant", not as an error in this function.
    """
    o = claims.get("o")
    if isinstance(o, dict) and o.get("id"):
        role = o.get("rol")
        if isinstance(role, str) and role.startswith("org:"):
            role = role.removeprefix("org:")
        return OrgContext(org_id=o["id"], org_role=role, org_slug=o.get("slg"))

    # Defensive fallback for the older flat claim shape.
    org_id = claims.get("org_id")
    if org_id:
        role = claims.get("org_role")
        if isinstance(role, str) and role.startswith("org:"):
            role = role.removeprefix("org:")
        return OrgContext(org_id=org_id, org_role=role, org_slug=claims.get("org_slug"))

    return None


def verify_clerk_webhook(payload: bytes, headers: dict[str, str]) -> dict[str, Any]:
    """Verify a Clerk webhook request (Clerk delivers via Svix) and return
    the parsed event body. Raises HTTPException(400) on any verification
    failure. Never returns a body for a request that didn't verify."""
    if not settings.clerk_webhook_secret:
        raise RuntimeError("CLERK_WEBHOOK_SECRET is not configured")
    try:
        wh = Webhook(settings.clerk_webhook_secret)
        # verify() only verifies (raises on failure) — it returns None,
        # not the parsed body, unlike stripe.Webhook.construct_event.
        # json.loads only happens after verify() succeeds, so an invalid
        # signature never reaches JSON parsing at all.
        wh.verify(payload, headers)
    except WebhookVerificationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid webhook signature",
        ) from exc
    return json.loads(payload)  # type: ignore[no-any-return]


def verify_stripe_webhook(payload: bytes, sig_header: str) -> stripe.Event:
    """Verify a Stripe webhook request and return the parsed Event. Raises
    HTTPException(400) on any verification failure — bad signature, no
    signature header, payload tampering, wrong secret. Never returns an
    event for a request that didn't verify."""
    if not settings.stripe_webhook_secret:
        raise RuntimeError("STRIPE_WEBHOOK_SECRET is not configured")
    try:
        return stripe.Webhook.construct_event(
            payload, sig_header, settings.stripe_webhook_secret
        )
    except (ValueError, stripe.SignatureVerificationError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid webhook signature",
        ) from exc
