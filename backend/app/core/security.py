"""Clerk JWT verification.

This verifies bearer tokens issued by Clerk and returns their claims. It
deliberately stops there — turning a verified `org_id` claim into a
`tenant_id` (and everything downstream: session/user provisioning, route
protection, Stripe) is auth/billing work for the next phase, not this one.
"""
from functools import lru_cache
from typing import Any

import jwt
from fastapi import HTTPException, status
from jwt import PyJWKClient

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
