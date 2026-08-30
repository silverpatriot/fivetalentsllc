"""Async engine/session plumbing, and the one function that makes RLS work:
`set_tenant_context`.

Every request-scoped session MUST call set_tenant_context (directly, or via
the app.core.deps.get_db dependency) before touching any tenant-scoped
table. Skipping it doesn't open the tenant up — with `FORCE ROW LEVEL
SECURITY` and a fail-closed policy (see the migration), an unset
app.current_tenant_id means every tenant-scoped query returns zero rows,
not "all rows". It's a broken query, not a leak.
"""
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings

settings = get_settings()

engine: AsyncEngine = create_async_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=10,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    expire_on_commit=False,
    autoflush=False,
)


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
    """Open a session already scoped to `tenant_id`, inside a transaction.

    Use this from Celery tasks and scripts. Request-scoped FastAPI code
    should use app.core.deps.get_db instead, which sources tenant_id from
    the verified Clerk session rather than a direct argument.
    """
    async with AsyncSessionLocal() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_id)
            yield session
