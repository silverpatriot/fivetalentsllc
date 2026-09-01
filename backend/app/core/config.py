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

    # --- OpenRouter (LLM gateway — Phase 3 Task 3) ---
    # Blank until a real key is added to .env; every call site checks for
    # this and raises a clear 503 rather than sending an unauthenticated
    # request to OpenRouter.
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    # Cheap/fast model for the outline pass, and a deliberately different,
    # stronger/pricier model for the full draft (Task 3: "don't use the
    # same model for both if cost matters at 250-user scale"). Both
    # confirmed present on OpenRouter's live /models endpoint before use
    # (not just trusted from the model slug string) — see the Phase 3
    # completion notes for that check. Configurable so a model swap later
    # doesn't need a code change.
    openrouter_outline_model: str = "qwen/qwen3.8-flash"
    openrouter_draft_model: str = "qwen/qwen3.8-27b"
    # Attribution headers OpenRouter asks callers to send (not secrets).
    openrouter_app_url: str = "https://kerygma.church"
    openrouter_app_title: str = "Kerygma"

    # --- Embeddings (Phase 4 Task 1: cadence matching) ---
    # The Phase 3 kickoff spec's stop line said "OpenRouter has no
    # embeddings endpoint" — checked again for real at the start of this
    # phase rather than trusting that note, and it's wrong: POST
    # {openrouter_base_url}/embeddings works live, proxying to OpenAI
    # (provider="OpenAI", is_byok=false — OpenRouter's own arrangement,
    # not a second key of ours) and returns exactly 1536 dims, matching
    # EMBEDDING_DIM in app/models/sermon_embedding.py already. No second
    # LLM-provider key needed after all.
    embedding_model: str = "openai/text-embedding-3-small"
    # Practical safeguard on the synchronous extract+chunk step in
    # app/api/documents.py — no object storage exists in this stack to
    # hand an oversized upload off to instead (see that module's
    # docstring), so this is the backstop against an unbounded upload
    # blocking the request indefinitely. 20MB comfortably covers a large
    # sermon manuscript or a lengthy PDF paper; revisit if that's not
    # true for real uploads once this ships.
    max_upload_size_bytes: int = 20 * 1024 * 1024

    # --- Bible text source (Phase 3 Task 3) ---
    # bible-api.com: free, no API key, and serves the public-domain KJV
    # text via ?translation=kjv — verified live (see Phase 3 completion
    # notes). ESV was considered and rejected for this phase: it requires
    # API registration, a daily verse-count cap, and redistribution
    # restrictions on quoted text, none of which fit "fetch scripture text
    # for arbitrary sermon passages, then quote it back to the user."
    # Revisit if a specific translation becomes a product requirement.
    bible_api_base_url: str = "https://bible-api.com"
    bible_translation: str = "kjv"

    # api.bible (api.scripture.api.bible) — preferred source when
    # configured, alongside bible-api.com; see app/services/bible.py's
    # module docstring for the layering. Confirmed live before wiring in.
    bible_api_key: str = ""
    api_bible_base_url: str = "https://api.scripture.api.bible"

    # How many of the tenant's past sermons to pull as cadence/voice
    # examples. Deliberately a plain recency query (ORDER BY created_at
    # DESC), NOT a pgvector similarity search over sermon_embeddings —
    # real semantic cadence-matching is explicitly deferred (see Phase 3
    # kickoff spec's stop line), since OpenRouter has no embeddings
    # endpoint and populating sermon_embeddings would need a second
    # LLM-provider key this phase doesn't have.
    cadence_example_count: int = 3

    # --- Tavily (live web search, folded into context assembly) ---
    # Confirmed live against api.tavily.com before use. Purely additive —
    # if blank, or if a search call fails, context assembly proceeds
    # without a web_context section rather than blocking generation.
    tavily_api_key: str = ""
    web_search_max_results: int = 3

    # --- Subdomain routing (Phase 3 Task 1) ---
    # The base domain tenant subdomains hang off (gracecommunity.<this>).
    # Used by the frontend to strip a request's Host header down to a
    # candidate tenant slug — see frontend/lib/tenant.ts.
    app_base_domain: str = "kerygma.church"

    # --- Whisper transcription: Groq primary, OpenAI fallback ---
    # See app/services/transcription.py's module docstring for the
    # layering rationale (same shape as bible.py's api.bible + bible-
    # api.com). Groq hosts whisper-large-v3-turbo — the cheapest, fastest
    # hosted Whisper option, no GPU infra of our own to run. OpenAI's own
    # Whisper API is the fallback for a genuine Groq outage/rejection.
    #
    # NEITHER confirmed live yet — both keys are blank until someone puts
    # real ones in .env; do that (and re-check the file-size caps noted
    # below against current provider docs) before trusting this beyond
    # local dev. Every other external integration in this codebase
    # (bible.py, openrouter.py, tavily) was held to "confirmed live
    # before use"; this one hasn't cleared that bar.
    groq_api_key: str = ""
    groq_base_url: str = "https://api.groq.com/openai/v1"
    whisper_model_groq: str = "whisper-large-v3-turbo"
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    whisper_model_openai: str = "whisper-1"
    # Both providers document a 25MB cap on a direct multipart upload
    # (unverified against a real account here — see above). A compressed
    # 25-minute sermon (Phase 5's target preached length) comfortably
    # fits under that (~12MB at 64kbps mp3); this is the backstop against
    # a raw WAV or an unusually long recording blowing past it — same
    # role as max_upload_size_bytes above, just for audio.
    max_media_upload_size_bytes: int = 25 * 1024 * 1024

    # --- Media storage (app/services/storage.py) ---
    # "local": a Docker-managed named volume, mounted into `backend` only
    # (see docker-compose.yml's media_storage volume) — the only
    # implementation that actually exists right now.
    # "google_drive" is reserved for a tenant mapping their own Drive
    # folder instead of using our disk — selecting it raises clearly
    # (GoogleDriveStorage) rather than pretending to work; see that
    # class's docstring for what real support would still need (OAuth,
    # a per-tenant encrypted refresh-token store, a folder picker, the
    # Drive API's own upload/download calls).
    media_storage_backend: str = "local"
    media_storage_root: str = "/app/media"

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # populated from env/`.env`
