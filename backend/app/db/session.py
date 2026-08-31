"""Async engine/session plumbing, and the one function that makes RLS work:
`set_tenant_context`.

Every request-scoped session MUST call set_tenant_context (directly, or via
the app.core.deps.get_db dependency) before touching any tenant-scoped
table. Skipping it doesn't open the tenant up — with `FORCE ROW LEVEL
SECURITY` and no missing_ok on the policy's current_setting() call (see
the migration), an unset app.current_tenant_id raises a database error
rather than returning "all rows" — or "zero rows" either, deliberately:
silently-empty is a worse failure mode for a bug like this than a loud
one. Either way, it's a broken query, not a leak.
"""
import sys
import uuid
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager, contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.core.config import get_settings

settings = get_settings()

# Pooled asyncpg connections bind to whichever asyncio event loop was
# running when they were first opened. Under pytest, that's frequently
# NOT a single consistent loop across requests: Starlette's TestClient
# runs the ASGI app through AnyIO's BlockingPortal, and reusing a
# connection checked out under one loop from a later request that's
# running under a different one either raises ("attached to a different
# loop") or — confirmed directly, by calling a route handler outside
# pytest entirely and comparing — can silently produce wrong results with
# no exception at all. NullPool means every checkout opens a fresh
# connection and closes it right after use, so there's never a pooled
# connection old enough to be bound to some other, possibly-dead loop.
# Detected via sys.modules, not an env var: this can't be forgotten by a
# future test file, or accidentally left on/off by a missing setting —
# it's true exactly when the process is actually pytest, checked once,
# at the only place this engine gets constructed. Production processes
# (uvicorn, celery) never import pytest, so they get the pooled engine
# as before; nothing about this touches production behavior.
_TESTING = "pytest" in sys.modules

engine: AsyncEngine = create_async_engine(
    settings.database_url,
    pool_pre_ping=True,
    **({"poolclass": NullPool} if _TESTING else {"pool_size": 10, "max_overflow": 10}),
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    expire_on_commit=False,
    autoflush=False,
)

# Sync counterpart, same app role/credentials — for Celery tasks. Celery
# tasks are plain sync functions; driving the async engine from one means
# wrapping every task body in asyncio.run(), which is its own source of
# subtle bugs (event-loop-per-task, connection pool/loop mismatches).
# Simpler and more robust to just have a sync engine for sync callers,
# same pattern as database_url_app_sync for the test suite.
sync_engine: Engine = create_engine(settings.database_url_app_sync, pool_pre_ping=True)

SyncSessionLocal = sessionmaker(bind=sync_engine, expire_on_commit=False, autoflush=False)


async def set_tenant_context(session: AsyncSession, tenant_id: uuid.UUID) -> None:
    """Scope `session`'s current transaction to `tenant_id` via
    set_config(..., is_local=true) — the transaction-scoped equivalent of
    `SET LOCAL`.

    We use set_config() rather than a literal `SET LOCAL app.current_tenant_id
    = '<value>'` string specifically so the tenant id is a bound parameter,
    not string-interpolated SQL. `SET` does not support bind parameters in
    Postgres; `set_config()` is a normal function call and does.
    """
    await session.execute(
        text("SELECT set_config('app.current_tenant_id', :tenant_id, true)"),
        {"tenant_id": str(tenant_id)},
    )


@asynccontextmanager
async def tenant_session(tenant_id: uuid.UUID) -> AsyncGenerator[AsyncSession, None]:
    """Open an async session already scoped to `tenant_id`, inside a
    transaction. For async scripts. Celery tasks are sync — use
    tenant_session_sync instead. Request-scoped FastAPI code should use
    app.core.deps.get_db, which sources tenant_id from the verified Clerk
    session rather than a direct argument.
    """
    async with AsyncSessionLocal() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_id)
            yield session


def set_tenant_context_sync(session: Session, tenant_id: uuid.UUID) -> None:
    """Sync counterpart of set_tenant_context — see that function."""
    session.execute(
        text("SELECT set_config('app.current_tenant_id', :tenant_id, true)"),
        {"tenant_id": str(tenant_id)},
    )


@contextmanager
def tenant_session_sync(tenant_id: uuid.UUID) -> Generator[Session, None, None]:
    """Open a sync session already scoped to `tenant_id`, inside a
    transaction. Use this from Celery tasks (see app/tasks/usage_reporting.py)."""
    with SyncSessionLocal() as session, session.begin():
        set_tenant_context_sync(session, tenant_id)
        yield session
