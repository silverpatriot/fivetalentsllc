"""Audio-to-text transcription: Groq's hosted Whisper endpoint
(whisper-large-v3-turbo) as primary, OpenAI's Whisper API (whisper-1) as
fallback — same layered-provider shape as app/services/bible.py (api.bible
+ bible-api.com), for the same reason: a genuine service failure on one
side (network error, non-2xx, an unparseable response) shouldn't surface
as a customer-visible failure when the other side can serve the request
instead.

Unlike bible.py, there's no equivalent "clean negative" here — an
empty/near-silent audio file transcribing to an empty string is a real,
successful answer, not a failure to fall back from. So the only fallback
trigger is TranscriptionError, not a sentinel return value.

Both providers expose an OpenAI-compatible POST /audio/transcriptions
(multipart: file, model, response_format) endpoint, requested here with
response_format="verbose_json" specifically to get `duration` (seconds)
back in the same response — needed for TRANSCRIPTION_MINUTE usage billing
(app/tasks/usage_reporting.py) without a second library/ffmpeg dependency
just to probe an audio file's length ourselves.

NEITHER provider has been confirmed live against a real account in this
codebase yet — GROQ_API_KEY/OPENAI_API_KEY are both blank until someone
puts real keys in .env. Do that (and re-verify the response shape
_call_provider assumes, and the file-size caps noted in
app/core/config.py) before trusting this beyond local dev — the same
"confirmed live before use" bar every other external integration here
was held to (see app/services/bible.py, openrouter.py, web_search.py).

Deliberately NOT wired into a Celery task: there is no shared storage
between the backend and celery-worker containers (see
app/services/ingestion.py's docstring for the same constraint on document
uploads), so a raw audio file's bytes can only ever be handled in the
process that received the upload. Groq's turbo model transcribes well
faster than realtime, so — like app/api/documents.py's synchronous
extract+chunk — calling this inline from the upload request is the
actual right shape here, not a limitation to work around later.
"""
import logging

import httpx

from app.core.config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)


class TranscriptionError(RuntimeError):
    """A genuine service failure on one provider — network error, non-2xx,
    or a response that doesn't parse the way this module expects.
    transcribe_audio catches this from Groq (the primary) and falls back
    to OpenAI; it's only raised to the caller once both have failed, or
    when neither is configured at all.

    `str(exc)` carries the full detail, including the raw upstream
    response body — useful server-side, not necessarily safe to show a
    pastor verbatim. `user_message` is what a caller should actually put
    in an API response, same split as OpenRouterError
    (app/services/openrouter.py).
    """

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code

    @property
    def user_message(self) -> str:
        if self.status_code == 413:
            return "That audio file is too large to transcribe."
        return "Transcription failed unexpectedly. Please try again — if it keeps happening, let us know."


class TranscriptionResult:
    def __init__(self, text: str, duration_seconds: float, source: str) -> None:
        self.text = text.strip()
        self.duration_seconds = duration_seconds
        self.source = source  # "groq" | "openai" — which provider actually answered


async def _call_provider(
    *, base_url: str, api_key: str, model: str, filename: str, data: bytes, source: str
) -> TranscriptionResult:
    try:
        async with httpx.AsyncClient(timeout=180.0) as client:
            resp = await client.post(
                f"{base_url}/audio/transcriptions",
                headers={"Authorization": f"Bearer {api_key}"},
                data={"model": model, "response_format": "verbose_json"},
                files={"file": (filename, data)},
            )
    except httpx.HTTPError as exc:
        raise TranscriptionError(f"{source} request failed: {exc}") from exc

    if resp.status_code >= 400:
        raise TranscriptionError(f"{source} {resp.status_code}: {resp.text[:2000]}", status_code=resp.status_code)

    try:
        payload = resp.json()
        text = payload["text"]
        duration = float(payload["duration"])
    except (KeyError, ValueError, TypeError) as exc:
        raise TranscriptionError(f"{source} returned an unexpected response shape: {resp.text[:2000]}") from exc

    return TranscriptionResult(text=text, duration_seconds=duration, source=source)


async def transcribe_audio(data: bytes, filename: str) -> TranscriptionResult:
    """Transcribe one audio file's raw bytes. Tries Groq first (when
    GROQ_API_KEY is set), falls back to OpenAI (when OPENAI_API_KEY is
    set) on any TranscriptionError from Groq. Raises TranscriptionError
    only if the configured provider(s) all fail, or neither is
    configured — a caller (a future app/api/media.py) should treat that
    as a hard failure: mark the media_file 'failed', surface
    exc.user_message, and record no TRANSCRIPTION_MINUTE usage event,
    since nothing billable actually happened.
    """
    if settings.groq_api_key:
        try:
            return await _call_provider(
                base_url=settings.groq_base_url,
                api_key=settings.groq_api_key,
                model=settings.whisper_model_groq,
                filename=filename,
                data=data,
                source="groq",
            )
        except TranscriptionError:
            logger.warning("Groq transcription failed for %r — falling back to OpenAI", filename, exc_info=True)
    else:
        logger.info("GROQ_API_KEY not configured — going straight to OpenAI for %r", filename)

    if not settings.openai_api_key:
        raise TranscriptionError(
            "Transcription is not configured — set GROQ_API_KEY and/or OPENAI_API_KEY in .env"
        )

    return await _call_provider(
        base_url=settings.openai_base_url,
        api_key=settings.openai_api_key,
        model=settings.whisper_model_openai,
        filename=filename,
        data=data,
        source="openai",
    )
