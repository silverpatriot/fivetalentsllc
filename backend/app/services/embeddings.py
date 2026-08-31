"""Thin client for OpenRouter's embeddings endpoint.

Two entry points, deliberately parallel (same split as app/db/session.py's
tenant_session / tenant_session_sync, and for the same reason):
  - embed_text(): async, httpx.AsyncClient. Used inline during generation
    (app/services/context_assembly.py) to embed the *query* text for a
    cadence-matching search — this has to happen before the outline pass
    can proceed, so it's a real request-path call, not something to queue.
  - embed_text_sync(): sync, httpx.Client. Used from Celery tasks
    (app/tasks/embeddings.py) and the backfill script
    (scripts/backfill_embeddings.py), which are sync throughout this
    codebase (same reasoning as the sync Stripe SDK calls in
    app/tasks/usage_reporting.py — no asyncio.run() inside a worker task).

Despite the Phase 3 kickoff spec's note that "OpenRouter has no embeddings
endpoint," POST {base_url}/embeddings works live as of this phase — see
app/core/config.py's embedding_model comment for how that was confirmed.
It isn't listed in OpenRouter's public /models catalog (that catalog is
chat-model-shaped), but the endpoint itself accepts any valid embedding
model slug and OpenRouter proxies it — confirmed live returning
provider="OpenAI", is_byok=false, 1536-dim vectors for
"openai/text-embedding-3-small".
"""
from typing import Any

import httpx

from app.core.config import get_settings

settings = get_settings()


class EmbeddingError(RuntimeError):
    pass


def _require_api_key() -> str:
    if not settings.openrouter_api_key:
        raise EmbeddingError(
            "OPENROUTER_API_KEY is not configured — embedding generation is blocked until a real key is in .env"
        )
    return settings.openrouter_api_key


def _headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": settings.openrouter_app_url,
        "X-Title": settings.openrouter_app_title,
    }


def _body(input_: str | list[str]) -> dict[str, Any]:
    return {"model": settings.embedding_model, "input": input_}


def _extract_vector(data: dict[str, Any], raw: str) -> list[float]:
    try:
        return data["data"][0]["embedding"]
    except (KeyError, IndexError) as exc:
        raise EmbeddingError(f"Unexpected OpenRouter embeddings response shape: {raw[:2000]}") from exc


def _extract_vectors(data: dict[str, Any], raw: str, expected_count: int) -> list[list[float]]:
    """Batch variant — confirmed live that OpenRouter's embeddings
    endpoint accepts a list `input` and returns one row per item in
    `data`, each carrying its own `index`; sorted by that rather than
    trusted to come back in request order, since nothing in OpenRouter's
    docs promises that (there's no documentation for this endpoint at
    all — it isn't in their public /models catalog — so nothing about
    its response ordering is a documented guarantee)."""
    try:
        rows = sorted(data["data"], key=lambda row: row["index"])
        vectors = [row["embedding"] for row in rows]
    except (KeyError, TypeError) as exc:
        raise EmbeddingError(f"Unexpected OpenRouter embeddings response shape: {raw[:2000]}") from exc
    if len(vectors) != expected_count:
        raise EmbeddingError(
            f"Expected {expected_count} embeddings back, got {len(vectors)}: {raw[:2000]}"
        )
    return vectors


async def embed_text(text: str) -> list[float]:
    """Async — for the request-path cadence-matching query in
    context_assembly.py."""
    api_key = _require_api_key()
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{settings.openrouter_base_url}/embeddings",
            headers=_headers(api_key),
            json=_body(text),
        )
    if resp.status_code >= 400:
        raise EmbeddingError(f"OpenRouter {resp.status_code}: {resp.text[:2000]}")
    return _extract_vector(resp.json(), resp.text)


def embed_text_sync(text: str) -> list[float]:
    """Sync — for Celery tasks and the backfill script."""
    api_key = _require_api_key()
    with httpx.Client(timeout=30.0) as client:
        resp = client.post(
            f"{settings.openrouter_base_url}/embeddings",
            headers=_headers(api_key),
            json=_body(text),
        )
    if resp.status_code >= 400:
        raise EmbeddingError(f"OpenRouter {resp.status_code}: {resp.text[:2000]}")
    return _extract_vector(resp.json(), resp.text)


def embed_batch_sync(texts: list[str]) -> list[list[float]]:
    """Sync, batched — one HTTP call for N chunks rather than N calls.
    Used by app/tasks/embeddings.py to embed every chunk of a document in
    a single request. Confirmed live: OpenRouter's embeddings endpoint
    accepts a list `input` and returns one embedding per item."""
    if not texts:
        return []
    api_key = _require_api_key()
    with httpx.Client(timeout=60.0) as client:
        resp = client.post(
            f"{settings.openrouter_base_url}/embeddings",
            headers=_headers(api_key),
            json=_body(texts),
        )
    if resp.status_code >= 400:
        raise EmbeddingError(f"OpenRouter {resp.status_code}: {resp.text[:2000]}")
    return _extract_vectors(resp.json(), resp.text, expected_count=len(texts))
