"""Speech-to-text: Whisper in the transcriber container (scorer/transcriber.py).

POST the raw clip with the shared X-Scorer-Secret header; ?language=es|en, or none and Whisper detects it.
"""

import httpx
from django.conf import settings

from .scoring import SECRET_HEADER


class TranscriptionError(Exception):
    """A short, loggable reason; never contains the audio, the words or the secret."""


def is_configured():
    return bool(settings.TRANSCRIBER_URL and settings.SCORER_SECRET)


def model_name():
    """The model the container runs (e.g. faster-whisper-medium-int8), for the eval run's config snapshot."""
    try:
        response = httpx.get(settings.TRANSCRIBER_URL.rstrip("/") + "/health", timeout=5)
        response.raise_for_status()
        return response.json()["model"]
    except (httpx.HTTPError, KeyError, ValueError):
        return "unknown (transcriber unreachable)"


def transcribe(audio, content_type, language=None):
    """(text, language): the language sent, or the one Whisper detected (any code, e.g. "pt")."""
    timeout = settings.TRANSCRIBER_TIMEOUT_SECONDS
    try:
        response = httpx.post(
            settings.TRANSCRIBER_URL,
            params={"language": language} if language else {},
            content=audio,
            headers={"Content-Type": content_type, SECRET_HEADER: settings.SCORER_SECRET},
            timeout=timeout,
        )
    except httpx.TimeoutException as exc:
        raise TranscriptionError(f"Transcriber timed out after {timeout:g}s") from exc
    except httpx.HTTPError as exc:
        raise TranscriptionError(f"Transcriber request failed: {type(exc).__name__}") from exc
    if response.status_code == 401:
        raise TranscriptionError("Transcriber rejected the secret (HTTP 401)")
    if response.is_error:
        raise TranscriptionError(f"Transcriber returned HTTP {response.status_code}")
    try:
        body = response.json()
        return str(body["transcript"]), body.get("language") or language
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise TranscriptionError("Transcriber response had no transcript") from exc
