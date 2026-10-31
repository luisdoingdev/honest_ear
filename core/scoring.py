import httpx
from django.conf import settings

SECRET_HEADER = "X-Scorer-Secret"


class ScorerError(Exception):
    pass


def is_configured():
    return bool(settings.SCORER_URL and settings.SCORER_SECRET)


def phonemes_for(audio, content_type):
    """Send a recording to the Modal phoneme service.

    Returns {"phonemes", "inference_ms", "duration_s"}. Raises ScorerError, never with the secret in it.
    """
    timeout = settings.SCORER_TIMEOUT_SECONDS
    try:
        response = httpx.post(
            settings.SCORER_URL,
            content=audio,
            headers={"Content-Type": content_type, SECRET_HEADER: settings.SCORER_SECRET},
            timeout=timeout,
            # Modal answers a cold or busy endpoint with a redirect to keep waiting on.
            follow_redirects=True,
        )
    except httpx.TimeoutException as exc:
        raise ScorerError(f"Scorer timed out after {timeout:g}s (cold start?)") from exc
    except httpx.HTTPError as exc:
        raise ScorerError(f"Scorer request failed: {type(exc).__name__}") from exc

    if response.status_code == 401:
        raise ScorerError("Scorer rejected the secret (HTTP 401): SCORER_SECRET doesn't match the Modal secret")
    if response.is_error:
        raise ScorerError(f"Scorer returned HTTP {response.status_code}")
    try:
        body = response.json()
        return {
            "phonemes": str(body["phonemes"]),
            "inference_ms": float(body["inference_ms"]),
            "duration_s": float(body["duration_s"]),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise ScorerError("Scorer response had no phonemes") from exc
