# Sermon Engine

Multi-tenant SaaS for churches: AI-assisted sermon generation, media
transcription, and clip generation.

## Stack

| Layer      | Choice                                                   |
|------------|-----------------------------------------------------------|
| Frontend   | Next.js 15 (App Router), React 19, Tailwind CSS v4, shadcn/ui |
| Backend    | FastAPI (Python 3.12), async, Pydantic v2                 |
| Auth       | Clerk (Organizations = tenants)                            |
| Billing    | Stripe — flat per-org tier + usage-based overage           |
| Data       | PostgreSQL 16 + pgvector, Redis 7                          |
| Jobs       | Celery + Redis                                             |
| Infra      | Docker Compose, behind an existing Cloudflare Tunnel        |

Neo4j is deferred to a later phase — deliberately not scaffolded.

## Multi-tenancy

Every tenant-scoped table is protected by Postgres Row-Level Security, not
just application-layer filtering. See
`backend/app/db/migrations/versions/0001_initial_schema_rls.py` for the
policies and `backend/tests/test_rls.py` for the proof (cross-tenant reads,
writes, and "someone forgot to set tenant context" are all covered).

The backend never accepts a tenant id from the client — `app/core/deps.py`
derives it from the verified Clerk session, and `app/db/session.py` sets it
as the Postgres session variable RLS policies check
(`app.current_tenant_id`), scoped to the current transaction only.

**Two Postgres roles, deliberately.** The official Postgres image makes
`POSTGRES_USER` a superuser, and superusers bypass RLS unconditionally —
`ENABLE`/`FORCE ROW LEVEL SECURITY` doesn't change that. So there are two
roles: `POSTGRES_USER` (admin, runs migrations only — `DATABASE_URL_SYNC`)
and `APP_DB_USER` (no superuser, no table ownership, DML-only via
migration `0002` — `DATABASE_URL`, what the backend and celery-worker
actually connect as). The app role is created by
`postgres/init/01-create-app-role.sh`, which only runs on a *first* boot of
an empty `postgres_data` volume — see below if you already have one.

## Local development

```bash
cp .env.example .env   # fill in real values, including APP_DB_USER/APP_DB_PASSWORD
docker compose up --build
```

Then, in another shell, apply migrations:

```bash
docker compose exec backend alembic upgrade head
```

**If you already ran this before the two-role split existed**: your
`postgres_data` volume predates `postgres/init/`, which only runs against
an empty volume, so the app role won't get created automatically. Easiest
fix for Phase 1 (no real data at stake yet):

```bash
docker compose down -v   # wipes the volume
docker compose up --build -d
docker compose exec backend alembic upgrade head
```

Also update your `.env` to match the new `.env.example` shape — add
`APP_DB_USER`/`APP_DB_PASSWORD`, and point `DATABASE_URL` (not
`DATABASE_URL_SYNC`) at the app role instead of `POSTGRES_USER`.

Backend: reachable only inside the Docker network at `http://backend:8000`
(not exposed to the host — see the comment in `docker-compose.yml`).
Frontend: http://localhost:3000.

### Running the RLS tests

Needs a real Postgres with migrations applied — it's a database security
test, not something worth mocking:

```bash
docker compose exec backend alembic upgrade head
docker compose exec backend pytest tests/test_rls.py -v
```

## Project layout

```
backend/
├── app/
│   ├── main.py         # FastAPI app
│   ├── core/           # config, Clerk JWT verification, request deps
│   ├── models/         # SQLAlchemy models
│   ├── schemas/        # Pydantic v2 schemas
│   ├── api/            # route modules
│   ├── tasks/          # Celery app + tasks
│   └── db/             # session/engine, Alembic migrations
├── tests/
└── requirements.txt
frontend/                # Next.js 15 app router
docker-compose.yml
.env.example
```

## Status

Phase 1: repo scaffold, Docker Compose, and the tenant schema + RLS are in
place. Auth and billing wiring are next, pending review of the RLS
approach above.
