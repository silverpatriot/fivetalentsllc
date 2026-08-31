"""Signature verification for both webhook sources, run through the ACTUAL
production verification functions (verify_stripe_webhook,
verify_clerk_webhook) — not a reimplementation of them. Pure HMAC checks,
no live Stripe/Clerk account needed: both providers' webhook signing is a
local cryptographic operation using only the shared secret, not something
that requires calling out to their APIs.
"""
import json

import pytest
from fastapi import HTTPException

from app.core.security import verify_clerk_webhook, verify_stripe_webhook
from tests.conftest import sign_stripe_payload, sign_svix_payload

STRIPE_SECRET = "whsec_test_stripe_1234567890abcdef"
CLERK_SECRET = "whsec_dGVzdHNlY3JldGtleWJ5dGVzZm9yc3ZpeA=="  # base64 payload, fake but well-formed


def test_valid_stripe_signature_verifies(monkeypatch):
    from app.core import config

    monkeypatch.setattr(config.get_settings(), "stripe_webhook_secret", STRIPE_SECRET)
    payload = json.dumps({"id": "evt_1", "object": "event", "type": "ping"}).encode()
    header = sign_stripe_payload(payload, STRIPE_SECRET)

    event = verify_stripe_webhook(payload, header)
    assert event.id == "evt_1"
    assert event.type == "ping"


def test_tampered_stripe_payload_is_rejected(monkeypatch):
    from app.core import config

    monkeypatch.setattr(config.get_settings(), "stripe_webhook_secret", STRIPE_SECRET)
    payload = json.dumps({"id": "evt_1", "object": "event", "type": "ping"}).encode()
    header = sign_stripe_payload(payload, STRIPE_SECRET)

    tampered_payload = json.dumps({"id": "evt_1", "object": "event", "type": "checkout.session.completed"}).encode()
    with pytest.raises(HTTPException) as exc_info:
        verify_stripe_webhook(tampered_payload, header)
    assert exc_info.value.status_code == 400


def test_stripe_wrong_secret_is_rejected(monkeypatch):
    from app.core import config

    monkeypatch.setattr(config.get_settings(), "stripe_webhook_secret", STRIPE_SECRET)
    payload = json.dumps({"id": "evt_1", "object": "event", "type": "ping"}).encode()
    header = sign_stripe_payload(payload, "whsec_a_completely_different_secret")

    with pytest.raises(HTTPException) as exc_info:
        verify_stripe_webhook(payload, header)
    assert exc_info.value.status_code == 400


def test_valid_svix_signature_verifies(monkeypatch):
    from app.core import config

    monkeypatch.setattr(config.get_settings(), "clerk_webhook_secret", CLERK_SECRET)
    payload = json.dumps({"type": "organization.created", "data": {"id": "org_1"}}).encode()
    headers = sign_svix_payload("msg_1", payload, CLERK_SECRET)

    event = verify_clerk_webhook(payload, headers)
    assert event["type"] == "organization.created"


def test_tampered_svix_payload_is_rejected(monkeypatch):
    from app.core import config

    monkeypatch.setattr(config.get_settings(), "clerk_webhook_secret", CLERK_SECRET)
    payload = json.dumps({"type": "organization.created", "data": {"id": "org_1"}}).encode()
    headers = sign_svix_payload("msg_1", payload, CLERK_SECRET)

    tampered = json.dumps({"type": "organization.deleted", "data": {"id": "org_1"}}).encode()
    with pytest.raises(HTTPException) as exc_info:
        verify_clerk_webhook(tampered, headers)
    assert exc_info.value.status_code == 400


def test_svix_wrong_secret_is_rejected(monkeypatch):
    from app.core import config

    monkeypatch.setattr(config.get_settings(), "clerk_webhook_secret", CLERK_SECRET)
    payload = json.dumps({"type": "organization.created", "data": {"id": "org_1"}}).encode()
    headers = sign_svix_payload("msg_1", payload, "whsec_YW5vdGhlcmZha2VzZWNyZXRieXRlcw==")

    with pytest.raises(HTTPException) as exc_info:
        verify_clerk_webhook(payload, headers)
    assert exc_info.value.status_code == 400
