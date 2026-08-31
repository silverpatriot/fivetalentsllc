"""Thin async client for OpenRouter's chat-completions API (OpenAI-
compatible shape). Not a full SDK — just what Task 3 needs: a
non-streaming call (outline pass) and a streaming call (draft pass), both
returning/yielding plain text plus giving the caller the raw response to
log (app/models/generation_log.py — "log what was actually sent to the
model and what came back").

No OPENROUTER_API_KEY is configured in this environment yet — every
function here raises RuntimeError up front rather than sending an
unauthenticated request, and nothing in this module has been exercised
against the real OpenRouter API. Reachability of the endpoint itself
(openrouter.ai) was confirmed (GET /models returns 200 with no auth), but
an actual chat completion has not been.
"""
import json
from collections.abc import AsyncIterator

import httpx

from app.core.config import get_settings

settings = get_settings()


class OpenRouterError(RuntimeError):
    pass


def _require_api_key() -> str:
    if not settings.openrouter_api_key:
        raise OpenRouterError(
            "OPENROUTER_API_KEY is not configured — generation is blocked until a real key is in .env"
        )
    return settings.openrouter_api_key


def _headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        # OpenRouter asks callers to identify themselves via these two
        # headers for its own leaderboard/analytics — not required for
        # requests to succeed, but requested in their docs.
        "HTTP-Referer": settings.openrouter_app_url,
        "X-Title": settings.openrouter_app_title,
    }


async def chat_completion(model: str, messages: list[dict[str, str]]) -> tuple[str, str]:
    """Non-streaming call. Returns (text, raw_response_json_str) — the raw
    string is what gets persisted to generation_logs verbatim."""
    api_key = _require_api_key()
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            f"{settings.openrouter_base_url}/chat/completions",
            headers=_headers(api_key),
            json={"model": model, "messages": messages, "stream": False},
        )
    if resp.status_code >= 400:
        raise OpenRouterError(f"OpenRouter {resp.status_code}: {resp.text[:2000]}")
    raw = resp.text
    data = resp.json()
    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as exc:
        raise OpenRouterError(f"Unexpected OpenRouter response shape: {raw[:2000]}") from exc
    return text, raw


async def stream_chat_completion(
    model: str, messages: list[dict[str, str]], raw_sink: list[str] | None = None
) -> AsyncIterator[str]:
    """Streaming call (SSE). Yields text deltas as they arrive.

    `raw_sink`, if given, gets every raw `data:` line appended to it as
    it's received — the caller (app/services/generation.py) uses this to
    build the full raw_response persisted to generation_logs, since the
    exact bytes the model returned are what "log what came back" (Task 3)
    means, not just the reassembled text.
    """
    api_key = _require_api_key()
    async with httpx.AsyncClient(timeout=None) as client:
        async with client.stream(
            "POST",
            f"{settings.openrouter_base_url}/chat/completions",
            headers=_headers(api_key),
            json={"model": model, "messages": messages, "stream": True},
        ) as resp:
            if resp.status_code >= 400:
                body = await resp.aread()
                raise OpenRouterError(f"OpenRouter {resp.status_code}: {body[:2000]!r}")
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                payload = line[len("data:") :].strip()
                if raw_sink is not None:
                    raw_sink.append(payload)
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                delta = chunk.get("choices", [{}])[0].get("delta", {}).get("content")
                if delta:
                    yield delta
