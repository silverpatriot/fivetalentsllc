"""verify_clerk_jwt against a self-signed test token — genuinely exercises
the signature-verification and claim-decoding logic (not a live Clerk
JWKS endpoint, which we don't have credentials for; see rsa_keypair /
make_clerk_jwt in conftest.py for exactly what this does and doesn't
prove).
"""
import jwt as pyjwt
import pytest
from fastapi import HTTPException

from app.core import security
from tests.conftest import make_clerk_jwt


class _FakeSigningKey:
    def __init__(self, key):
        self.key = key


class _FakeJWKClient:
    """Stands in for jwt.PyJWKClient — same interface, backed by our test
    keypair instead of a real network fetch against a live JWKS URL."""

    def __init__(self, public_key):
        self._public_key = public_key

    def get_signing_key_from_jwt(self, token):
        return _FakeSigningKey(self._public_key)


def test_valid_token_verifies_and_returns_claims(rsa_keypair, monkeypatch):
    private_key, public_key = rsa_keypair
    monkeypatch.setattr(security, "_jwk_client", lambda: _FakeJWKClient(public_key))

    token = make_clerk_jwt(private_key, {"sub": "user_123", "o": {"id": "org_abc", "rol": "admin"}})
    claims = security.verify_clerk_jwt(token)

    assert claims["sub"] == "user_123"
    assert claims["o"]["id"] == "org_abc"


def test_token_signed_with_wrong_key_is_rejected(rsa_keypair, monkeypatch):
    """A token signed with a DIFFERENT private key than the one our JWKS
    lookup returns — the actual forgery scenario JWT signing exists to
    prevent."""
    _correct_private, correct_public = rsa_keypair
    from cryptography.hazmat.primitives.asymmetric import rsa

    wrong_private = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    monkeypatch.setattr(security, "_jwk_client", lambda: _FakeJWKClient(correct_public))

    forged_token = make_clerk_jwt(wrong_private, {"sub": "attacker"})
    with pytest.raises(HTTPException) as exc_info:
        security.verify_clerk_jwt(forged_token)
    assert exc_info.value.status_code == 401


def test_expired_token_is_rejected(rsa_keypair, monkeypatch):
    private_key, public_key = rsa_keypair
    monkeypatch.setattr(security, "_jwk_client", lambda: _FakeJWKClient(public_key))

    import time

    expired = pyjwt.encode(
        {"sub": "user_123", "iat": int(time.time()) - 3600, "exp": int(time.time()) - 1800},
        private_key,
        algorithm="RS256",
        headers={"kid": "test-key-1"},
    )
    with pytest.raises(HTTPException) as exc_info:
        security.verify_clerk_jwt(expired)
    assert exc_info.value.status_code == 401
