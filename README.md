# Kerygma

Multi-tenant SaaS for churches: AI-assisted sermon generation, media
transcription, and clip generation. (Repo/package names still say
`sermon-engine`/`sermon_engine` internally — only customer-facing surfaces
use the Kerygma name.)

## Stack

| Layer      | Choice                                                   |
|------------|-----------------------------------------------------------|
| Frontend   | Next.js 15 (App Router), React 19, Tailwind CSS v4, shadcn/ui |
| Backend    | FastAPI (Python 3.12), async, Pydantic v2                 |
| Auth       | Clerk (Organizations = tenants)                            |
| Billing    | Stripe — flat per-org tier + usage-based overage (Billing Meters) |
| Data       | PostgreSQL 16 + pgvector, Redis 7                          |
| Jobs       | Celery + Redis (worker + beat)                             |
| Infra      | Docker Compose, behind an existing Cloudflare Tunnel        |

Neo4j is deferred to a later phase — deliberately not scaffolded.

## Multi-tenancy

Every tenant-scoped table is protected by Postgres Row-Level Security, not
just application-layer filtering. See
`backend/app/db/migrations/versions/0001_initial_schema_rls.py` for the
policies and `backend/tests/test_rls.py` for the proof (cross-tenant reads,
writes, and "someone forgot to set tenant context" are all covered).

The backend never accepts a tenant id from the client — `app/core/deps.py`
derives it from the verified Clerk session (`extract_org_context` — the
current Clerk session token nests org data under a compact `o` claim,
`o.id`/`o.rol`/`o.slg`, not flat `org_id`/`org_role`; checked against
Clerk's docs, not assumed), and `app/db/session.py` sets it as the
Postgres session variable RLS policies check (`app.current_tenant_id`),
scoped to the current transaction only.

**Two Postgres roles, deliberately.** The official Postgres image makes
`POSTGRES_USER` a superuser, and superusers bypass RLS unconditionally —
`ENABLE`/`FORCE ROW LEVEL SECURITY` doesn't change that. So there are two
roles: `POSTGRES_USER` (admin, runs migrations only — `DATABASE_URL_SYNC`)
and `APP_DB_USER` (no superuser, no table ownership, DML-only via
migration `0002` — `DATABASE_URL`, what the backend/celery-worker/tests
actually connect as). The app role is created by
`postgres/init/01-create-app-role.sh`, which only runs on a *first* boot of
an empty `postgres_data` volume — see below if you already have one.

## Roles: Clerk vs. our own

Clerk ships two default roles (`org:admin`, `org:member`). Our product
needs four (`admin`/`pastor`/`editor`/`viewer`) — Clerk supports custom
roles matching that, but only for free in development; production custom
roles need Clerk's paid B2B Authentication add-on. Decided against that
for now: Clerk's `org:admin`/`org:member` only gate coarse org management
(who can invite members, manage billing), and the finer four-role
distinction lives entirely in our own `users.role` column, managed by our
own (future) UI — not synced from Clerk. Revisit if/when the add-on cost
is worth it.

## Billing

Hybrid: three flat monthly Stripe Prices (Starter/Growth/Enterprise —
**$49/$149/$399 are explicit placeholders**, not a real pricing decision)
plus two Stripe Billing Meters for overage (transcription minutes, AI
generations). Built directly on Stripe's Billing Meters API rather than
Metronome (the platform Stripe now steers new usage-based integrations
toward) — Metronome is aimed at prepaid credits/enterprise
contracts/dimensional pricing, overkill for two simple counters; revisit
if pricing gets materially more complex.

- `backend/scripts/stripe_setup.py` — idempotent bootstrap script that
  creates the Products/Prices/Meters. **Not run automatically** — it
  creates real objects in whatever Stripe account `STRIPE_SECRET_KEY`
  points at. Run it once (test mode first), then paste the printed IDs
  into `.env`.
- `backend/app/api/billing.py` — Checkout Session + Customer Portal
  creation (both Stripe-hosted, no custom payment/billing UI).
- `backend/app/api/webhooks_stripe.py` — signature-verified, idempotent
  (via `webhook_events`, keyed on Stripe's event id) handling of
  `checkout.session.completed`, `invoice.paid`,
  `customer.subscription.updated`, `customer.subscription.deleted`.
- `backend/app/tasks/usage_reporting.py` — `usage_events` (Postgres) is
  the source of truth; reporting to Stripe happens after the write, in
  the background (Celery), never synchronously in a request path. A
  Stripe outage during reporting can't lose the record of what happened —
  see `backend/tests/test_usage_reporting.py`.
- `backend/app/core/deps.py`'s `get_active_tenant_id` is the actual access
  gate: `subscription_status != 'active'` → `402 Payment Required`. A
  canceled subscription blocks the dependency every product route will
  depend on, not just a DB column.

### Webhooks are proxied through Next.js, not exposed directly

Only `frontend` is exposed to the host (see `docker-compose.yml`), and
Cloudflare Tunnel config is out of scope to change. So Stripe/Clerk can't
reach the backend directly — `frontend/app/api/webhooks/{stripe,clerk}/route.ts`
forward the raw request (byte-for-byte body, exact signature headers) to
the backend over the internal network. Configure webhook endpoint URLs in
the Stripe/Clerk dashboards as `https://<domain>/api/webhooks/stripe` and
`.../api/webhooks/clerk` — not the backend's own `/webhooks/*` paths.

## Local development

```bash
cp .env.example .env   # fill in real values — see that file's comments
docker compose up --build
```

Then, in another shell, apply migrations:

```bash
docker compose exec backend alembic upgrade head
```

**If you already ran this before the two-role split existed**: your
`postgres_data` volume predates `postgres/init/`, which only runs against
an empty volume, so the app role won't get created automatically. Easiest
fix while this is still pre-production (no real data at stake):

```bash
docker compose down -v   # wipes the volume
docker compose up --build -d
docker compose exec backend alembic upgrade head
```

Backend: reachable only inside the Docker network at `http://backend:8000`.
Frontend: http://localhost:3000.

### Running the tests

Most of this suite needs a real Postgres with migrations applied — same
reasoning as Phase 1: RLS, webhook idempotency, and access gating are
database-level guarantees, and a test that doesn't touch a real database
proves nothing about them.

```bash
docker compose exec backend alembic upgrade head
docker compose exec backend pytest tests/ -v
```

A subset needs no database at all — `test_org_context.py`,
`test_clerk_jwt.py`, `test_webhook_crypto.py` — pure logic and signature
verification against self-signed test payloads, runnable anywhere:

```bash
docker compose exec backend pytest tests/test_org_context.py tests/test_clerk_jwt.py tests/test_webhook_crypto.py -v
```

`test_clerk_webhook_flow.py` and `test_stripe_webhook_flow.py`
additionally skip themselves (not fail) if `CLERK_WEBHOOK_SECRET` /
`STRIPE_WEBHOOK_SECRET` aren't set in `.env` — they sign test payloads
with whatever secret is actually configured, so there's something for the
real verification code to check against.

## Project layout

```
backend/
├── app/
│   ├── main.py         # FastAPI app
│   ├── core/           # config, Clerk JWT + webhook verification, request deps, idempotency
│   ├── models/         # SQLAlchemy models
│   ├── schemas/        # Pydantic v2 schemas
│   ├── api/            # route modules (health, billing, webhooks_clerk, webhooks_stripe)
│   ├── tasks/          # Celery app + usage-reporting tasks
│   └── db/             # session/engine (async + sync), Alembic migrations
├── scripts/             # stripe_setup.py — one-time, not run automatically
├── tests/
└── requirements.txt
frontend/                # Next.js 15 app router, Clerk-wired
postgres/init/           # first-boot role provisioning (see "Two Postgres roles" above)
docker-compose.yml
.env.example
```

## Status

Phase 1 (repo scaffold, Docker Compose, tenant schema + RLS) and Phase 2
(Clerk auth, Stripe billing, webhooks, usage metering) are built. See the
Phase 2 completion report in conversation history for exactly what was
verified against a live Postgres/self-signed crypto vs. what's still
blocked on real Clerk/Stripe test-mode credentials.
