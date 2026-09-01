"""app.services.transcription's provider layering and fallback behavior.
The transport is mocked here (httpx.MockTransport, same technique as
tests/test_openrouter.py) rather than hitting real Groq/OpenAI — no real
key is configured in this environment (see the module docstring), and
deterministically forcing a provider outage isn't possible against a live
API anyway.
"""
import httpx
import pytest

from app.services import transcription as transcription_module
from app.services.transcription import TranscriptionError, transcribe_audio


def _install_mock_transport(monkeypatch, handler):
    """Redirects every httpx.AsyncClient this module constructs through a
    MockTransport instead of a real network call."""
    real_async_client = httpx.AsyncClient

    def _patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(transcription_module.httpx, "AsyncClient", _patched)


def _verbose_json(text: str, duration: float) -> httpx.Response:
    return httpx.Response(200, json={"text": text, "duration": duration})


@pytest.fixture(autouse=True)
def _configure_both_keys(monkeypatch):
    """Every test gets both keys set by default (so fallback is actually
    reachable); individual tests override one or both back to blank to
    exercise "only one configured" / "neither configured"."""
    monkeypatch.setattr(transcription_module.settings, "groq_api_key", "test-groq-key")
    monkeypatch.setattr(transcription_module.settings, "openai_api_key", "test-openai-key")


async def test_transcribe_audio_uses_groq_when_configured(monkeypatch):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return _verbose_json("In the beginning was the Word.", 1523.4)

    _install_mock_transport(monkeypatch, handler)

    result = await transcribe_audio(b"fake audio bytes", "sermon.mp3")

    assert result.text == "In the beginning was the Word."
    assert result.duration_seconds == 1523.4
    assert result.source == "groq"
    assert len(calls) == 1
    assert "api.groq.com" in str(calls[0].url)


async def test_transcribe_audio_falls_back_to_openai_when_groq_fails(monkeypatch):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if "groq" in str(request.url):
            return httpx.Response(500, text="upstream exploded")
        return _verbose_json("Fallback transcript.", 300.0)

    _install_mock_transport(monkeypatch, handler)

    result = await transcribe_audio(b"fake audio bytes", "sermon.mp3")

    assert result.text == "Fallback transcript."
    assert result.source == "openai"
    assert len(calls) == 2
    assert "api.groq.com" in str(calls[0].url)
    assert "api.openai.com" in str(calls[1].url)


async def test_transcribe_audio_raises_when_both_providers_fail(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="both down")

    _install_mock_transport(monkeypatch, handler)

    with pytest.raises(TranscriptionError) as exc_info:
        await transcribe_audio(b"fake audio bytes", "sermon.mp3")

    assert exc_info.value.status_code == 503
    assert "openai" in str(exc_info.value)  # the last (fallback) provider's failure is what's raised


async def test_transcribe_audio_skips_groq_entirely_when_key_not_configured(monkeypatch):
    monkeypatch.setattr(transcription_module.settings, "groq_api_key", "")
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return _verbose_json("Straight to OpenAI.", 60.0)

    _install_mock_transport(monkeypatch, handler)

    result = await transcribe_audio(b"fake audio bytes", "sermon.mp3")

    assert result.source == "openai"
    assert len(calls) == 1  # never even tried Groq


async def test_transcribe_audio_raises_a_clear_error_when_neither_key_configured(monkeypatch):
    monkeypatch.setattr(transcription_module.settings, "groq_api_key", "")
    monkeypatch.setattr(transcription_module.settings, "openai_api_key", "")

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("should never make an HTTP call with no key configured")

    _install_mock_transport(monkeypatch, handler)

    with pytest.raises(TranscriptionError, match="not configured"):
        await transcribe_audio(b"fake audio bytes", "sermon.mp3")


async def test_transcribe_audio_raises_on_unexpected_response_shape(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "shape"})  # no "text"/"duration"

    _install_mock_transport(monkeypatch, handler)

    with pytest.raises(TranscriptionError, match="unexpected response shape"):
        await transcribe_audio(b"fake audio bytes", "sermon.mp3")


def test_transcription_error_user_message_hides_raw_detail_but_413_gets_specific_message():
    generic = TranscriptionError("groq 500: some raw upstream stack trace", status_code=500)
    assert "raw upstream" not in generic.user_message
    assert "try again" in generic.user_message.lower()

    too_large = TranscriptionError("groq 413: payload too large", status_code=413)
    assert "too large" in too_large.user_message.lower()
