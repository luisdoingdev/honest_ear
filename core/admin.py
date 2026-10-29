from django.contrib import admin

from .models import Recording


@admin.register(Recording)
class RecordingAdmin(admin.ModelAdmin):
    list_display = ("id", "content_type", "size", "created_at")
    exclude = ("audio",)
    readonly_fields = ("id", "content_type", "size", "created_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
