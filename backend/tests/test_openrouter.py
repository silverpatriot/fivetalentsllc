"""app.services.openrouter's retry/backoff behavior and user-facing error
sanitization. The transport is mocked here (httpx.MockTransport — already
part of httpx, no new dependency) rather than hitting the real API,
since deterministically forcing OpenRouter to return a 429 isn't possible
the way test_embeddings.py exercises the real embeddings endpoint for
real. What generation.py does with a real chat_completion/
stream_chat_completion call is covered end-to-end elsewhere
(test_generation_usage.py), with those two functions mocked out entirely
at that layer instead — this file is the one place the HTTP-level retry
logic itself gets exercised.
"""
import httpx
import pytest

from app.services import openrouter as openrouter_module
from app.services.openrouter import OpenRouterError, chat_completion, stream_chat_completion


def _install_mock_transport(monkeypatch, handler):
    """Redirects every httpx.AsyncClient this module constructs through a
    MockTransport instead of a real network call."""
    real_async_client = httpx.AsyncClient

    def _patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(openrouter_module.httpx, "AsyncClient", _patched)


def _install_fast_sleep(monkeypatch):
    """No real waiting in tests — records what it was asked to sleep for
    so retry-after/backoff selection can be asserted on directly."""
    sleeps: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(openrouter_module.asyncio, "sleep", _fake_sleep)
    return sleeps


def _ok_json_response(text: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})


def _rate_limited_response(*, retry_after: str | None = None) -> httpx.Response:
    headers = {"retry-after": retry_after} if retry_after else {}
    return httpx.Response(
        429,
        headers=headers,
        json={"error": {"message": "Provider returned error", "code": 429, "metadata": {"user_id": "user_abc123"}}},
    )


async def test_chat_completion_retries_a_429_and_succeeds(monkeypatch):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return _rate_limited_response() if len(calls) == 1 else _ok_json_response("Second attempt worked.")

    _install_mock_transport(monkeypatch, handler)
    sleeps = _install_fast_sleep(monkeypatch)

    text, _raw = await chat_completion("some/model", [{"role": "user", "content": "hi"}])

    assert text == "Second attempt worked."
    assert len(calls) == 2
    assert len(sleeps) == 1  # one retry wait, no more


async def test_chat_completion_prefers_retry_after_header_over_backoff_schedule(monkeypatch):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return _rate_limited_response(retry_after="7") if len(calls) == 1 else _ok_json_response("ok")

    _install_mock_transport(monkeypatch, handler)
    sleeps = _install_fast_sleep(monkeypatch)

    await chat_completion("some/model", [{"role": "user", "content": "hi"}])

    assert sleeps == [7.0]


async def test_chat_completion_raises_after_exhausting_retries_without_leaking_raw_body(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return _rate_limited_response()  # every attempt rate-limited

    _install_mock_transport(monkeypatch, handler)
    _install_fast_sleep(monkeypatch)

    with pytest.raises(OpenRouterError) as exc_info:
        await chat_completion("some/model", [{"role": "user", "content": "hi"}])

    exc = exc_info.value
    assert exc.status_code == 429
    # The raw upstream body IS in str(exc) — server-side logging needs it.
    assert "user_abc123" in str(exc)
    # ...but NEVER in the sanitized message a pastor would actually see.
    assert "user_abc123" not in exc.user_message
    assert "capacity" in exc.user_message.lower()


async def test_chat_completion_non_rate_limit_error_gets_a_generic_user_message(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal server error, something exploded")

    _install_mock_transport(monkeypatch, handler)
    _install_fast_sleep(monkeypatch)

    with pytest.raises(OpenRouterError) as exc_info:
        await chat_completion("some/model", [{"role": "user", "content": "hi"}])

    exc = exc_info.value
    assert "exploded" not in exc.user_message
    assert "try again" in exc.user_message.lower()


async def test_stream_chat_completion_retries_a_429_before_any_delta_and_succeeds(monkeypatch):
    calls: list[httpx.Request] = []
    sse_body = b'data: {"choices":[{"delta":{"content":"Hello "}}]}\n\n' b'data: {"choices":[{"delta":{"content":"world."}}]}\n\n' b"data: [DONE]\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return _rate_limited_response()
        return httpx.Response(200, content=sse_body, headers={"content-type": "text/event-stream"})

    _install_mock_transport(monkeypatch, handler)
    _install_fast_sleep(monkeypatch)

    deltas = [d async for d in stream_chat_completion("some/model", [{"role": "user", "content": "hi"}])]

    assert deltas == ["Hello ", "world."]
    assert len(calls) == 2


async def test_chat_completion_reports_each_retry_on_the_queue(monkeypatch):
    """2026-09-03: retry_queue is what makes a real OpenRouter retry
    sequence visible to the pastor (a real "retrying (2/3)…" status)
    instead of a silent wait indistinguishable from broken — see
    app/services/generation.py's _heartbeat_while_pending, which drains
    this same queue and turns each item into a real SSE `retry` event."""
    import asyncio

    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return _rate_limited_response() if len(calls) < 3 else _ok_json_response("Third attempt worked.")

    _install_mock_transport(monkeypatch, handler)
    _install_fast_sleep(monkeypatch)

    retry_queue: asyncio.Queue = asyncio.Queue()
    text, _raw = await chat_completion("some/model", [{"role": "user", "content": "hi"}], retry_queue=retry_queue)

    assert text == "Third attempt worked."
    events = []
    while not retry_queue.empty():
        events.append(retry_queue.get_nowait())
    assert len(events) == 2  # 2 retries before the 3rd attempt succeeded
    assert events[0] == {"attempt": 1, "max_attempts": 3, "delay_seconds": 1.0, "status_code": 429, "reason": "upstream_error"}
    assert events[1]["attempt"] == 2


async def test_chat_completion_with_no_retry_queue_behaves_exactly_as_before(monkeypatch):
    """retry_queue is purely additive — every existing caller that
    doesn't pass one must be completely unaffected."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return _rate_limited_response() if len(calls) == 1 else _ok_json_response("worked")

    _install_mock_transport(monkeypatch, handler)
    _install_fast_sleep(monkeypatch)

    text, _raw = await chat_completion("some/model", [{"role": "user", "content": "hi"}])
    assert text == "worked"


async def test_stream_chat_completion_reports_retry_on_the_queue(monkeypatch):
    import asyncio

    calls: list[httpx.Request] = []
    sse_body = b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n' b"data: [DONE]\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return _rate_limited_response()
        return httpx.Response(200, content=sse_body, headers={"content-type": "text/event-stream"})

    _install_mock_transport(monkeypatch, handler)
    _install_fast_sleep(monkeypatch)

    retry_queue: asyncio.Queue = asyncio.Queue()
    deltas = [
        d
        async for d in stream_chat_completion(
            "some/model", [{"role": "user", "content": "hi"}], retry_queue=retry_queue
        )
    ]

    assert deltas == ["ok"]
    event = retry_queue.get_nowait()
    assert event["attempt"] == 1
    assert event["status_code"] == 429
