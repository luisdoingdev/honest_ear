from django.core.cache import cache
from django.db import connection
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .models import Recording

MAX_RECORDING_BYTES = 5 * 1024 * 1024
ALLOWED_AUDIO_TYPES = {"audio/webm", "audio/mp4", "audio/ogg"}


def landing(request):
    return render(request, "core/landing.html")


def record(request):
    return render(request, "core/record.html")


@require_POST
def create_recording(request):
    upload = request.FILES.get("audio")
    if upload is None or upload.size == 0:
        return JsonResponse({"error": "No audio was uploaded."}, status=400)

    content_type = upload.content_type.split(";")[0].strip().lower()
    if content_type not in ALLOWED_AUDIO_TYPES:
        return JsonResponse({"error": f"Unsupported audio type: {content_type}"}, status=415)
    if upload.size > MAX_RECORDING_BYTES:
        return JsonResponse({"error": "Recording is too large (5 MB max)."}, status=413)

    recording = Recording.objects.create(
        audio=upload.read(), content_type=content_type, size=upload.size
    )
    return JsonResponse(
        {"id": str(recording.id), "audio_url": reverse("recording-audio", args=[recording.id])},
        status=201,
    )


@require_GET
def recording_audio(request, recording_id):
    recording = get_object_or_404(Recording, pk=recording_id)
    response = HttpResponse(bytes(recording.audio), content_type=recording.content_type)
    response["Cache-Control"] = "no-store"
    return response


@require_http_methods(["DELETE"])
def delete_recording(request, recording_id):
    Recording.objects.filter(pk=recording_id).delete()
    return HttpResponse(status=204)


def health(request):
    status = {}
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        status["database"] = "ok"
    except Exception as exc:
        status["database"] = f"error: {exc}"
    try:
        cache.set("health", "ok", 5)
        status["redis"] = cache.get("health")
    except Exception as exc:
        status["redis"] = f"error: {exc}"
    healthy = all(v == "ok" for v in status.values())
    return JsonResponse(status, status=200 if healthy else 503)
