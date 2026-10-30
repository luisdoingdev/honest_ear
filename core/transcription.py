import httpx
from django.conf import settings

DEEPGRAM_URL = "https://api.deepgram.com/v1/listen"
# nova-3's multilingual mode: Spanish, English, or both mixed in one sentence.
LANGUAGE = "multi"
# Stays under gunicorn's 30-second worker timeout, since this runs inside the upload request.
TIMEOUT_SECONDS = 20


class TranscriptionError(Exception):
    pass


def is_configured():
    return bool(settings.DEEPGRAM_API_KEY)


def transcribe(audio, content_type):
    """Send a recording to Deepgram's pre-recorded API and return the transcript ("" when silent)."""
    try:
        response = httpx.post(
            DEEPGRAM_URL,
            # Streaming-only options (interim_results, endpointing) make this endpoint return 400.
            params={
                "model": settings.DEEPGRAM_MODEL,
                "language": LANGUAGE,
                "smart_format": "true",
                # Keep recordings out of Deepgram's Model Improvement Program.
                "mip_opt_out": "true",
            },
            headers={
                "Authorization": f"Token {settings.DEEPGRAM_API_KEY}",
                "Content-Type": content_type,
            },
            content=audio,
            timeout=TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return response.json()["results"]["channels"][0]["alternatives"][0]["transcript"]
    except httpx.HTTPStatusError as exc:
        raise TranscriptionError(_status_error(exc.response)) from exc
    except httpx.HTTPError as exc:
        raise TranscriptionError(f"Deepgram request failed: {type(exc).__name__}") from exc
    except (KeyError, IndexError, ValueError) as exc:
        raise TranscriptionError("Deepgram response had no transcript") from exc


def _status_error(response):
    """Deepgram's err_msg says what was wrong with the request; it never contains the audio or words."""
    message = f"Deepgram returned HTTP {response.status_code}"
    try:
        detail = response.json().get("err_msg")
    except ValueError:
        detail = None
    return f"{message}: {detail}" if detail else message
