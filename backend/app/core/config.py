"""App configuration, loaded from environment variables (.env in dev)."""
from functools import lru_cache

from pydantic import computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: str = "development"

    # --- database ---
    # database_url / database_url_sync are computed below from these parts,
    # not read directly from the environment — a full connection string
    # and its component username/password used to have to be kept in sync
    # by hand in .env, which is exactly the kind of drift that caused
    # migration 0002 to grant privileges to a role name that didn't match
    # what postgres/init/01-create-app-role.sh actually created. One
    # source of truth per credential now.
    postgres_host: str = "postgres"
    postgres_port: int = 5432
    postgres_db: str
    postgres_user: str  # admin/superuser — migrations only (DATABASE_URL_SYNC)
    postgres_password: str
    app_db_user: str = "sermon_engine_app"  # RLS-subject runtime role (DATABASE_URL)
    app_db_password: str

    @computed_field  # type: ignore[prop-decorator]
    @property
    def database_url(self) -> str:
        """Runtime (backend, celery-worker, tests): the non-superuser,
        RLS-subject app role."""
        return (
            f"postgresql+asyncpg://{self.app_db_user}:{self.app_db_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def database_url_sync(self) -> str:
        """Migrations only (Alembic): the admin/superuser role — needs DDL
        privileges app_db_user deliberately doesn't have."""
        return (
            f"postgresql+psycopg2://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def database_url_app_sync(self) -> str:
        """Sync-driver variant of database_url — same app role, same
        credentials, just psycopg2 instead of asyncpg. Used by
        tests/test_rls.py, which is synchronous by design (see its
        conftest.py). Using database_url_sync here instead — the admin
        role — was exactly the bug that made RLS look ineffective even
        after the role/grant fix landed: the test suite would've been
        connecting as a superuser, which bypasses RLS unconditionally
        regardless of anything the policies or grants say."""
        return (
            f"postgresql+psycopg2://{self.app_db_user}:{self.app_db_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    # --- redis / celery ---
    redis_url: str
    celery_broker_url: str
    celery_result_backend: str

    # --- clerk ---
    clerk_secret_key: str = ""
    clerk_publishable_key: str = ""
    clerk_jwks_url: str = ""
    # Separate from clerk_secret_key — this signs webhook deliveries
    # (Svix), not API requests. Get it from the Clerk Dashboard's webhook
    # endpoint config, not the API keys page.
    clerk_webhook_secret: str = ""

    # --- stripe ---
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_publishable_key: str = ""

    # Price/meter identifiers from Stripe — populated by running
    # scripts/stripe_setup.py against a real (test-mode) Stripe account.
    # Placeholders/blank until that's run; see that script and Task 2 in
    # the Phase 2 spec for why these can't be created by this codebase on
    # its own.
    stripe_price_starter: str = ""
    stripe_price_growth: str = ""
    stripe_price_enterprise: str = ""
    stripe_meter_transcription_minutes: str = ""
    stripe_meter_ai_generations: str = ""
    stripe_price_transcription_minutes: str = ""
    stripe_price_ai_generations: str = ""

    # --- cors ---
    backend_cors_origins: list[str] = ["http://localhost:3000"]

    # Publicly reachable frontend URL — used to build Stripe Checkout/
    # Portal success/cancel/return URLs. NOT the internal Docker-network
    # BACKEND_INTERNAL_URL frontend uses to reach the backend; this is the
    # other direction, the browser-facing address a user's browser (and
    # Stripe redirecting it) actually needs to load.
    frontend_url: str = "http://localhost:3000"

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # populated from env/`.env`
