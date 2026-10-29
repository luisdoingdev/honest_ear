import logging
from datetime import timedelta

from celery import shared_task
from django.utils import timezone

from .models import Recording

logger = logging.getLogger(__name__)


@shared_task
def ping():
    return "pong"


@shared_task
def delete_stale_recordings(max_age_minutes=10):
    cutoff = timezone.now() - timedelta(minutes=max_age_minutes)
    deleted, _ = Recording.objects.filter(created_at__lt=cutoff).delete()
    if deleted:
        logger.warning(
            "Deleted %d stale recording(s) older than %d minutes (never played back)",
            deleted,
            max_age_minutes,
        )
    return deleted
