#!/usr/bin/env python3
"""One-time (but safe to re-run) bootstrap: creates the Stripe Products,
flat Prices, Billing Meters, and metered Prices this app needs.

NOT run by anything automatically — this creates real objects in whatever
Stripe account STRIPE_SECRET_KEY points at. Run it deliberately, once per
Stripe account (test mode first, then live mode when you're ready), and
paste the printed price/meter IDs into .env.

    cd backend
    python scripts/stripe_setup.py

Idempotent: every object is looked up by a stable `lookup_key` (Prices) or
`event_name` (Meters) before creating, so re-running this after a partial
failure, or after adding a new plan later, does not create duplicates.

THE THREE FLAT PRICES BELOW ARE PLACEHOLDERS.
$49 / $149 / $399 per month, straight from the Phase 2 spec, explicitly
flagged there as not a real business decision. Do not launch on these
without someone actually setting real prices. The two metered per-unit
rates ($0.10/transcription minute, $0.05/AI generation) are this script's
own placeholders, invented only so the Meters/Prices have *some* number to
be created with — they need the same real-pricing-decision treatment
before launch, and nothing about them came from the spec.
"""
import sys

import stripe

sys.path.insert(0, ".")  # run from backend/, matches this repo's other scripts/alembic

from app.core.config import get_settings  # noqa: E402

settings = get_settings()
stripe.api_key = settings.stripe_secret_key

FLAT_PLANS = [
    # (lookup_key, display name, unit_amount in cents, currency)
    ("kerygma_starter_monthly", "Kerygma Starter", 4900, "usd"),
    ("kerygma_growth_monthly", "Kerygma Growth", 14900, "usd"),
    ("kerygma_enterprise_monthly", "Kerygma Enterprise", 39900, "usd"),
]

# (meter event_name, meter display name, price lookup_key, price display
# name, unit_amount in cents per unit)
METERED_ADDONS = [
    (
        "transcription_minutes",
        "Transcription minutes",
        "kerygma_transcription_minute_overage",
        "Transcription minute overage",
        10,  # $0.10/minute — placeholder, see module docstring
    ),
    (
        "ai_generations",
        "AI generations",
        "kerygma_ai_generation_overage",
        "AI generation overage",
        5,  # $0.05/generation — placeholder, see module docstring
    ),
]


def _find_price_by_lookup_key(lookup_key: str) -> stripe.Price | None:
    result = stripe.Price.list(lookup_keys=[lookup_key], limit=1)
    return result.data[0] if result.data else None


def _find_meter_by_event_name(event_name: str) -> stripe.billing.Meter | None:
    # No server-side filter by event_name on this endpoint as of this
    # writing — list and filter client-side. Meter counts are small (a
    # handful per account), so this is fine.
    for meter in stripe.billing.Meter.list(limit=100).auto_paging_iter():
        if meter.event_name == event_name:
            return meter
    return None


def ensure_flat_price(lookup_key: str, name: str, unit_amount: int, currency: str) -> str:
    existing = _find_price_by_lookup_key(lookup_key)
    if existing:
        print(f"  [exists] {lookup_key} -> {existing.id}")
        return existing.id

    product = stripe.Product.create(name=name)
    price = stripe.Price.create(
        product=product.id,
        currency=currency,
        unit_amount=unit_amount,
        recurring={"interval": "month"},
        lookup_key=lookup_key,
    )
    print(f"  [created] {lookup_key} -> {price.id} (product {product.id})")
    return price.id


def ensure_metered_price(
    event_name: str, meter_name: str, price_lookup_key: str, price_name: str, unit_amount: int
) -> tuple[str, str]:
    meter = _find_meter_by_event_name(event_name)
    if meter:
        print(f"  [exists] meter {event_name} -> {meter.id}")
    else:
        meter = stripe.billing.Meter.create(
            display_name=meter_name,
            event_name=event_name,
            default_aggregation={"formula": "sum"},
            customer_mapping={"event_payload_key": "stripe_customer_id", "type": "by_id"},
            value_settings={"event_payload_key": "value"},
        )
        print(f"  [created] meter {event_name} -> {meter.id}")

    existing_price = _find_price_by_lookup_key(price_lookup_key)
    if existing_price:
        print(f"  [exists] {price_lookup_key} -> {existing_price.id}")
        return meter.id, existing_price.id

    product = stripe.Product.create(name=price_name)
    price = stripe.Price.create(
        product=product.id,
        currency="usd",
        unit_amount=unit_amount,
        recurring={"interval": "month", "usage_type": "metered", "meter": meter.id},
        lookup_key=price_lookup_key,
    )
    print(f"  [created] {price_lookup_key} -> {price.id} (product {product.id})")
    return meter.id, price.id


def main() -> None:
    if not settings.stripe_secret_key:
        print("STRIPE_SECRET_KEY is not set — nothing to do.", file=sys.stderr)
        sys.exit(1)

    print(f"Using Stripe key starting with: {settings.stripe_secret_key[:12]}...")
    if not settings.stripe_secret_key.startswith("sk_test_"):
        print(
            "WARNING: this does not look like a TEST-mode secret key (sk_test_...). "
            "Refusing to run against what looks like a live key.",
            file=sys.stderr,
        )
        sys.exit(1)

    print("\n--- Flat plan prices ---")
    plan_env_vars = {}
    for lookup_key, name, unit_amount, currency in FLAT_PLANS:
        price_id = ensure_flat_price(lookup_key, name, unit_amount, currency)
        plan_env_vars[lookup_key] = price_id

    print("\n--- Metered overage prices ---")
    meter_env_vars = {}
    for event_name, meter_name, price_lookup_key, price_name, unit_amount in METERED_ADDONS:
        meter_id, price_id = ensure_metered_price(
            event_name, meter_name, price_lookup_key, price_name, unit_amount
        )
        meter_env_vars[event_name] = (meter_id, price_id)

    print("\n--- Paste into .env ---")
    print(f"STRIPE_PRICE_STARTER={plan_env_vars['kerygma_starter_monthly']}")
    print(f"STRIPE_PRICE_GROWTH={plan_env_vars['kerygma_growth_monthly']}")
    print(f"STRIPE_PRICE_ENTERPRISE={plan_env_vars['kerygma_enterprise_monthly']}")
    print(f"STRIPE_METER_TRANSCRIPTION_MINUTES=transcription_minutes")
    print(f"STRIPE_PRICE_TRANSCRIPTION_MINUTES={meter_env_vars['transcription_minutes'][1]}")
    print(f"STRIPE_METER_AI_GENERATIONS=ai_generations")
    print(f"STRIPE_PRICE_AI_GENERATIONS={meter_env_vars['ai_generations'][1]}")
    print(
        "\nSTRIPE_METER_* values are the event_name strings (fixed, defined in this "
        "script), not IDs — that's what report_usage_event() sends events as."
    )


if __name__ == "__main__":
    main()
