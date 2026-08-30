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

## Local development

```bash
cp .env.example .env   # fill in real values
docker compose up --build
```

Then, in another shell, apply migrations:

```bash
docker compose exec backend alembic upgrade head
```

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
