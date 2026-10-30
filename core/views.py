import logging

from django.core.cache import cache
from django.db import connection
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .models import FeatureFlag, Recording

logger = logging.getLogger(__name__)

RECORDING_FLAG = "recording"
MAX_RECORDING_BYTES = 5 * 1024 * 1024
ALLOWED_AUDIO_TYPES = {"audio/webm", "audio/mp4", "audio/ogg"}


def record(request):
    return render(
        request, "core/record.html", {"recording_enabled": FeatureFlag.is_enabled(RECORDING_FLAG)}
    )


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
    return JsonResponse(
        {"id": str(recording.id), "audio_url": reverse("recording-audio", args=[recording.id])},
        status=201,
    )


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
