import uuid

from django.db import models


class FeatureFlag(models.Model):
    name = models.SlugField(max_length=50, unique=True)
    enabled = models.BooleanField(default=False)
    description = models.CharField(max_length=200, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({'on' if self.enabled else 'off'})"

    @classmethod
    def is_enabled(cls, name):
        """Unknown flags count as off, so a missing flag never turns a feature on."""
        return cls.objects.filter(name=name, enabled=True).exists()


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
