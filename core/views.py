import logging
from concurrent.futures import ThreadPoolExecutor

from django.core.cache import cache
from django.db import connection
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from . import scoring, transcription
from .models import Attempt, FeatureFlag, Recording

logger = logging.getLogger(__name__)

RECORDING_FLAG = "recording"
TRANSCRIPTION_FLAG = "transcription"
LIVE_CAPTIONS_FLAG = "live_captions"
MAX_RECORDING_BYTES = 5 * 1024 * 1024
ALLOWED_AUDIO_TYPES = {"audio/webm", "audio/mp4", "audio/ogg"}
# Live captions' text comes from the page; anything longer than a 60-second clip could hold is cut.
MAX_LIVE_TRANSCRIPT_CHARS = 10_000
# Each token opens a paid Deepgram stream, so anyone hammering the endpoint is cut off per IP.
LIVE_TOKENS_PER_MINUTE = 10


def transcription_enabled():
    return FeatureFlag.is_enabled(TRANSCRIPTION_FLAG) and transcription.is_configured()


def live_captions_enabled():
    return transcription_enabled() and FeatureFlag.is_enabled(LIVE_CAPTIONS_FLAG)


def record(request):
    return render(
        request,
        "core/record.html",
        {
            "recording_enabled": FeatureFlag.is_enabled(RECORDING_FLAG),
            "live_captions_enabled": live_captions_enabled(),
        },
    )


def client_ip(request):
    # Render's proxy appends the real client address last; earlier entries can be forged by the client.
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    return forwarded.rsplit(",", 1)[-1].strip() or request.META.get("REMOTE_ADDR", "")


def within_live_token_limit(ip):
    """Counts this IP's tokens in the current minute."""
    key = f"live-token:{ip}"
    cache.add(key, 0, timeout=60)
    return cache.incr(key) <= LIVE_TOKENS_PER_MINUTE


@require_POST
def live_transcription_token(request):
    unavailable = JsonResponse({"error": _("Live captions are unavailable right now.")}, status=503)
    if not live_captions_enabled():
        logger.info("Live captions token refused: turned off or not configured")
        return unavailable

    ip = client_ip(request)
    try:
        allowed = within_live_token_limit(ip)
    except Exception:
        # Without the counter there's no limit, so refuse rather than hand out unmetered paid streams.
        logger.exception("Live captions token refused: rate-limit cache unavailable")
        return unavailable
    if not allowed:
        logger.warning("Live captions token refused: rate limit for %s", ip)
        return JsonResponse({"error": _("Too many sessions. Try again in a minute.")}, status=429)

    try:
        stream = transcription.streaming_connection()
    except transcription.TranscriptionError as exc:
        logger.warning("Live captions token not issued: %s", exc)
        return unavailable

    # The token itself is never logged.
    logger.info("Live captions token issued for %s", ip)
    return JsonResponse(stream)


@require_POST
def create_recording(request):
    if not FeatureFlag.is_enabled(RECORDING_FLAG):
        logger.warning("Recording rejected: the %s feature flag is off", RECORDING_FLAG)
        return JsonResponse({"error": _("Recording is turned off right now.")}, status=503)

    upload = request.FILES.get("audio")
    if upload is None or upload.size == 0:
        logger.warning("Recording rejected: no audio uploaded")
        return JsonResponse({"error": _("No audio was uploaded.")}, status=400)

    content_type = upload.content_type.split(";")[0].strip().lower()
    if content_type not in ALLOWED_AUDIO_TYPES:
        logger.warning("Recording rejected: unsupported type %s (%d bytes)", content_type, upload.size)
        message = _("Unsupported audio type: %(type)s") % {"type": content_type}
        return JsonResponse({"error": message}, status=415)
    if upload.size > MAX_RECORDING_BYTES:
        logger.warning("Recording rejected: too large (%d bytes, type %s)", upload.size, content_type)
        return JsonResponse({"error": _("Recording is too large (5 MB max).")}, status=413)

    recording = Recording.objects.create(
        audio=upload.read(), content_type=content_type, size=upload.size
    )
    logger.info("Recording %s created (%d bytes, %s)", recording.id, recording.size, content_type)
    # The page sends the words it already got from live captions; then Deepgram isn't paid a second time.
    live_text = request.POST.get("live_transcript")
    if live_text is not None:
        live_text = live_text[:MAX_LIVE_TRANSCRIPT_CHARS]
        logger.info("Recording %s already transcribed live", recording.id)

    attempt, extra = analyze_recording(recording, live_text)
    return JsonResponse(
        {
            "id": str(recording.id),
            "audio_url": reverse("recording-audio", args=[recording.id]),
            "attempt_id": str(attempt.id),
            **extra,
        },
        status=201,
    )


def analyze_recording(recording, live_text):
    """Phonemes from the Modal scorer and the transcript for the same clip, saved together as an Attempt.

    Both services are called at the same time, so the upload waits for the slower one rather than the sum.
    Failures are logged and stored on the attempt; they never fail the upload.
    """
    audio = bytes(recording.audio)
    with ThreadPoolExecutor(max_workers=1) as pool:
        scored = pool.submit(score_recording, recording.id, audio, recording.content_type)
        if live_text is None:
            text, transcript_error = transcribe_recording(recording.id, audio, recording.content_type)
            source = Attempt.TranscriptSource.DEEPGRAM if text is not None else Attempt.TranscriptSource.NONE
        else:
            text, transcript_error, source = live_text, "", Attempt.TranscriptSource.LIVE
        phonemes, scorer_error = scored.result()

    attempt = Attempt.objects.create(
        recording=recording,
        transcript=text,
        transcript_source=source,
        transcript_error=transcript_error[:300],
        phonemes=phonemes["phonemes"] if phonemes else None,
        phoneme_inference_ms=phonemes["inference_ms"] if phonemes else None,
        audio_duration_s=phonemes["duration_s"] if phonemes else None,
        scorer_error=scorer_error[:300],
    )

    extra = {"phonemes": attempt.phonemes}
    if live_text is None:
        extra["transcript"] = text
        if transcript_error:
            extra["transcript_error"] = _("Couldn't transcribe this recording.")
    return attempt, extra


def transcribe_recording(recording_id, audio, content_type):
    """Returns (text, error). text is None when transcription is off or failed."""
    if not transcription_enabled():
        return None, ""
    try:
        text = transcription.transcribe(audio, content_type)
    except transcription.TranscriptionError as exc:
        logger.warning("Recording %s not transcribed: %s", recording_id, exc)
        return None, str(exc)

    # Only the length is logged: the words themselves are the visitor's private speech.
    logger.info("Recording %s transcribed (%s, %d characters)", recording_id, transcription.LANGUAGE, len(text))
    return text, ""


def score_recording(recording_id, audio, content_type):
    """Returns (result, error). result is None when the scorer is off, cold or down."""
    if not scoring.is_configured():
        return None, ""
    try:
        result = scoring.phonemes_for(audio, content_type)
    except scoring.ScorerError as exc:
        logger.warning("Recording %s not scored: %s", recording_id, exc)
        return None, str(exc)

    logger.info(
        "Recording %s phonemes (%d characters, %.0f ms inference, %.2f s audio)",
        recording_id,
        len(result["phonemes"]),
        result["inference_ms"],
        result["duration_s"],
    )
    return result, ""


@require_GET
def recording_audio(request, recording_id):
    recording = Recording.objects.filter(pk=recording_id).first()
    if recording is None:
        logger.info("Recording %s requested but not found", recording_id)
        raise Http404

    logger.info("Recording %s served (%d bytes)", recording.id, recording.size)
    response = HttpResponse(bytes(recording.audio), content_type=recording.content_type)
    response["Cache-Control"] = "no-store"
    return response


@require_http_methods(["DELETE"])
def delete_recording(request, recording_id):
    deleted, _ = Recording.objects.filter(pk=recording_id).delete()
    if deleted:
        logger.info("Recording %s deleted after playback", recording_id)
    else:
        logger.info("Recording %s delete requested but already gone", recording_id)
    return HttpResponse(status=204)


def health(request):
    status = {}
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        status["database"] = "ok"
    except Exception:
        logger.exception("Health check: database unavailable")
        status["database"] = "error"
    try:
        cache.set("health", "ok", 5)
        status["redis"] = cache.get("health")
    except Exception:
        logger.exception("Health check: redis unavailable")
        status["redis"] = "error"
    healthy = all(v == "ok" for v in status.values())
    return JsonResponse(status, status=200 if healthy else 503)
