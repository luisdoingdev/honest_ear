import logging

from django.core.cache import cache
from django.db import connection
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_http_methods

from .models import FeatureFlag, Recording

logger = logging.getLogger(__name__)

RECORDING_FLAG = "recording"
KEEP_AUDIO_FLAG = "keep_audio"


def record(request):
    # Audio arrives over the /ws/record/ websocket (core.consumers), not an upload.
    return render(request, "core/record.html", {"recording_enabled": FeatureFlag.is_enabled(RECORDING_FLAG)})


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
    if FeatureFlag.is_enabled(KEEP_AUDIO_FLAG):
        # Eval mode: the audio stays so its attempt can be promoted to an eval clip; the page says so.
        logger.info("Recording %s kept after playback (keep_audio is on)", recording_id)
        return JsonResponse({"kept": True})
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
