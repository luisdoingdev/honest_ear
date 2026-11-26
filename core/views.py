# Template views are sync on purpose: Django runs them in a thread under ASGI, so
# templates can touch the ORM (e.g. request.user) safely. Use async views for I/O-heavy work.
from django.shortcuts import render
from django.views.decorators.http import require_POST

from .tasks import ping


def index(request):
    return render(request, "core/index.html")


@require_POST
def ping_task(request):
    """HTMX endpoint: queue a Celery task and return an HTML fragment."""
    result = ping.delay()
    return render(request, "core/_task_queued.html", {"task_id": result.id})
