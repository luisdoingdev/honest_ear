from django.core.cache import cache
from django.db import connection
from django.http import JsonResponse
from django.shortcuts import render


def landing(request):
    return render(request, "core/landing.html")


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
