from urllib.parse import urlencode

import httpx
from django.conf import settings

DEEPGRAM_URL = "https://api.deepgram.com/v1/listen"
DEEPGRAM_GRANT_URL = "https://api.deepgram.com/v1/auth/grant"
# Flux, Deepgram's turn-based model, only answers on /v2/listen.
DEEPGRAM_STREAMING_URL = "wss://api.deepgram.com/v2/listen"
# nova-3's multilingual mode: Spanish, English, or both mixed in one sentence.
LANGUAGE = "multi"
# Flux's multilingual model; the hints bias it towards the languages Colombian visitors mix.
STREAMING_LANGUAGE_HINTS = ["es", "en"]
# Stays under gunicorn's 30-second worker timeout, since this runs inside the upload request.
TIMEOUT_SECONDS = 20
# A streaming token only has to be valid while the browser opens its websocket.
STREAMING_TOKEN_TTL_SECONDS = 30
# The browser sends raw 16 kHz mono PCM (pcm-worklet.js): MediaRecorder's MP4 on iPhones can't be streamed.
STREAMING_SAMPLE_RATE = 16000


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


def streaming_connection():
    """Where and how the browser opens its live-caption websocket, with a short-lived token.

    The server picks every option, so the page can't change the model, language or privacy setting.
    """
    try:
        response = httpx.post(
            DEEPGRAM_GRANT_URL,
            headers={"Authorization": f"Token {settings.DEEPGRAM_API_KEY}"},
            json={"ttl_seconds": STREAMING_TOKEN_TTL_SECONDS},
            timeout=TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        token = response.json()["access_token"]
    except httpx.HTTPStatusError as exc:
        raise TranscriptionError(_status_error(exc.response)) from exc
    except httpx.HTTPError as exc:
        raise TranscriptionError(f"Deepgram request failed: {type(exc).__name__}") from exc
    except (KeyError, ValueError) as exc:
        raise TranscriptionError("Deepgram response had no token") from exc

    # Flux reports turns itself, so v1 options like interim_results, endpointing or smart_format don't apply.
    params = {
        "model": settings.DEEPGRAM_STREAMING_MODEL,
        "language_hint": STREAMING_LANGUAGE_HINTS,
        "encoding": "linear16",
        "sample_rate": STREAMING_SAMPLE_RATE,
        "mip_opt_out": "true",
    }
    # Browsers can't set an Authorization header on a websocket, so the token travels as a subprotocol.
    url = f"{DEEPGRAM_STREAMING_URL}?{urlencode(params, doseq=True)}"
    return {"url": url, "protocols": ["bearer", token]}


def _status_error(response):
    """Deepgram's err_msg says what was wrong with the request; it never contains the audio or words."""
    message = f"Deepgram returned HTTP {response.status_code}"
    try:
        detail = response.json().get("err_msg")
    except ValueError:
        detail = None
    return f"{message}: {detail}" if detail else message
