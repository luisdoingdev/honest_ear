import uuid

from django.db import models


class Recording(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    audio = models.BinaryField()
    content_type = models.CharField(max_length=50)
    size = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.id} ({self.size} bytes)"
