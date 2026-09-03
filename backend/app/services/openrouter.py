"""Thin async client for OpenRouter's chat-completions API (OpenAI-
compatible shape). Not a full SDK — just what Task 3 needs: a
non-streaming call (outline pass) and a streaming call (draft pass), both
returning/yielding plain text plus giving the caller the raw response to
log (app/models/generation_log.py — "log what was actually sent to the
model and what came back").

Live against the real OpenRouter API (openrouter_api_key set in .env,
Phase 3+). 429 ("Provider returned error... temporarily rate-limited
upstream") turns out to be routine, not exceptional — the configured
models run through OpenRouter's shared, non-BYOK provider pool, which
gets rate-limited under load independent of anything this app does. Both
calls below retry through that automatically (see _RETRYABLE_STATUSES);
only a caller-visible OpenRouterError after retries are exhausted, or a
non-retryable failure, needs handling upstream.
"""
import asyncio
import json
import logging
from collections.abc import AsyncIterator

import httpx

from app.core.config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)

# 3 attempts (1 initial + 2 retries), 1s/2s backoff between them — enough
# to ride out the "shared pool momentarily saturated" case OpenRouter's
# own error text describes ("retry shortly"), without a pastor watching a
# blank screen for too long on a call that's genuinely going to fail.
# Retry-After (seconds), when the upstream sends one, wins over the
# backoff schedule.
_MAX_ATTEMPTS = 3
_BASE_BACKOFF_SECONDS = 1.0
_RETRYABLE_STATUSES = {429, 502, 503, 504}


class OpenRouterError(RuntimeError):
    """`str(exc)` (used in logger.exception calls) carries the full detail,
    including the raw upstream response body — useful server-side, but
    NOT safe to show a pastor as-is: OpenRouter's error payloads have
    included provider-internal detail (an account user_id, provider
    routing metadata) that's meaningless to a customer and looks like a
    leak of internal system detail if rendered verbatim. `user_message`
    is what a caller should actually put in an API response or SSE
    `error` event — see app/api/sermons.py and app/services/generation.py.
    """

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code

    @property
    def user_message(self) -> str:
        if self.status_code == 429:
            return (
                "The AI service is at capacity right now. This usually clears within a minute "
                "or two — please try again shortly."
            )
        return "AI generation failed unexpectedly. Please try again — if it keeps happening, let us know."


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


def _retry_delay(attempt: int, headers: httpx.Headers | None) -> float:
    """Prefer the upstream's own Retry-After (seconds) when it sends one;
    exponential backoff (1s, 2s, 4s, ...) otherwise. `attempt` is
    1-indexed (the attempt that just failed)."""
    retry_after = headers.get("retry-after") if headers is not None else None
    if retry_after:
        try:
            return float(retry_after)
        except ValueError:
            pass
    return _BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))


async def _notify_retry(
    retry_queue: "asyncio.Queue[dict] | None", attempt: int, delay: float, *, status_code: int | None, reason: str
) -> None:
    """Pushes a plain data dict (never SSE-encoded bytes — this module
    stays agnostic of the app's own SSE event shape; app/services/
    generation.py owns that encoding when it drains the queue) describing
    a retry that's about to happen. 2026-09-03: this is what makes a
    real OpenRouter retry sequence VISIBLE to the pastor instead of an
    unexplained silent "Revising…"/heartbeat-only wait — confirmed live
    that the latter reads as "broken" during genuine (if temporary)
    upstream capacity trouble, not just theoretically."""
    if retry_queue is not None:
        await retry_queue.put(
            {"attempt": attempt, "max_attempts": _MAX_ATTEMPTS, "delay_seconds": delay, "status_code": status_code, "reason": reason}
        )


async def chat_completion(
    model: str, messages: list[dict[str, str]], retry_queue: "asyncio.Queue[dict] | None" = None
) -> tuple[str, str]:
    """Non-streaming call. Returns (text, raw_response_json_str) — the raw
    string is what gets persisted to generation_logs verbatim.

    `retry_queue`, if given, receives one dict (see _notify_retry) per
    retry — optional and purely additive, every existing caller that
    doesn't pass one behaves exactly as before."""
    api_key = _require_api_key()
    attempt = 0
    while True:
        attempt += 1
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(
                    f"{settings.openrouter_base_url}/chat/completions",
                    headers=_headers(api_key),
                    json={"model": model, "messages": messages, "stream": False},
                )
        except httpx.TransportError as exc:
            if attempt < _MAX_ATTEMPTS:
                delay = _retry_delay(attempt, None)
                logger.warning("OpenRouter network error (attempt %d/%d): %s — retrying", attempt, _MAX_ATTEMPTS, exc)
                await _notify_retry(retry_queue, attempt, delay, status_code=None, reason="network_error")
                await asyncio.sleep(delay)
                continue
            raise OpenRouterError(f"Network error calling OpenRouter: {exc}") from exc

        if resp.status_code in _RETRYABLE_STATUSES and attempt < _MAX_ATTEMPTS:
            delay = _retry_delay(attempt, resp.headers)
            logger.warning(
                "OpenRouter %s (attempt %d/%d) — retrying in %.1fs", resp.status_code, attempt, _MAX_ATTEMPTS, delay
            )
            await _notify_retry(retry_queue, attempt, delay, status_code=resp.status_code, reason="upstream_error")
            await asyncio.sleep(delay)
            continue

        if resp.status_code >= 400:
            raise OpenRouterError(f"OpenRouter {resp.status_code}: {resp.text[:2000]}", status_code=resp.status_code)

        raw = resp.text
        data = resp.json()
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as exc:
            raise OpenRouterError(f"Unexpected OpenRouter response shape: {raw[:2000]}") from exc
        return text, raw


async def stream_chat_completion(
    model: str,
    messages: list[dict[str, str]],
    raw_sink: list[str] | None = None,
    retry_queue: "asyncio.Queue[dict] | None" = None,
) -> AsyncIterator[str]:
    """Streaming call (SSE). Yields text deltas as they arrive.

    `raw_sink`, if given, gets every raw `data:` line appended to it as
    it's received — the caller (app/services/generation.py) uses this to
    build the full raw_response persisted to generation_logs, since the
    exact bytes the model returned are what "log what came back" (Task 3)
    means, not just the reassembled text.

    `retry_queue`, if given, receives one dict (see _notify_retry) per
    retry — optional and purely additive, every existing caller that
    doesn't pass one behaves exactly as before.

    Retries (see module docstring) only happen before the first delta of
    an attempt has been yielded — the retryable-status check runs right
    after the response headers arrive, strictly before any body is read.
    Once even one delta has reached the caller, a mid-stream failure
    raises instead of retrying: the caller (generation.py) has already
    forwarded that partial content on, so silently restarting would
    duplicate it rather than cleanly recover.
    """
    api_key = _require_api_key()
    attempt = 0
    while True:
        attempt += 1
        yielded_any = False
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream(
                    "POST",
                    f"{settings.openrouter_base_url}/chat/completions",
                    headers=_headers(api_key),
                    json={"model": model, "messages": messages, "stream": True},
                ) as resp:
                    if resp.status_code in _RETRYABLE_STATUSES and attempt < _MAX_ATTEMPTS:
                        delay = _retry_delay(attempt, resp.headers)
                        logger.warning(
                            "OpenRouter %s (attempt %d/%d) — retrying in %.1fs",
                            resp.status_code, attempt, _MAX_ATTEMPTS, delay,
                        )
                        await _notify_retry(
                            retry_queue, attempt, delay, status_code=resp.status_code, reason="upstream_error"
                        )
                        await asyncio.sleep(delay)
                        continue
                    if resp.status_code >= 400:
                        body = await resp.aread()
                        raise OpenRouterError(
                            f"OpenRouter {resp.status_code}: {body[:2000]!r}", status_code=resp.status_code
                        )
                    async for line in resp.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        payload = line[len("data:") :].strip()
                        if raw_sink is not None:
                            raw_sink.append(payload)
                        if payload == "[DONE]":
                            return
                        try:
                            chunk = json.loads(payload)
                        except json.JSONDecodeError:
                            continue
                        delta = chunk.get("choices", [{}])[0].get("delta", {}).get("content")
                        if delta:
                            yielded_any = True
                            yield delta
                    return
        except httpx.TransportError as exc:
            if not yielded_any and attempt < _MAX_ATTEMPTS:
                delay = _retry_delay(attempt, None)
                logger.warning("OpenRouter network error (attempt %d/%d): %s — retrying", attempt, _MAX_ATTEMPTS, exc)
                await _notify_retry(retry_queue, attempt, delay, status_code=None, reason="network_error")
                await asyncio.sleep(delay)
                continue
            raise OpenRouterError(f"Network error calling OpenRouter: {exc}") from exc
