import logging

from django.contrib import admin

from .models import FeatureFlag, Recording

logger = logging.getLogger(__name__)


@admin.register(FeatureFlag)
class FeatureFlagAdmin(admin.ModelAdmin):
    list_display = ("name", "enabled", "description", "updated_at")
    list_editable = ("enabled",)
    readonly_fields = ("updated_at",)

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if "enabled" in form.changed_data or not change:
            logger.warning(
                "Feature flag %s turned %s by %s", obj.name, "on" if obj.enabled else "off", request.user
            )


@admin.register(Recording)
class RecordingAdmin(admin.ModelAdmin):
    list_display = ("id", "content_type", "size", "created_at")
    exclude = ("audio",)
    readonly_fields = ("id", "content_type", "size", "created_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
