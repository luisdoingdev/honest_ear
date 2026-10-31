import logging

from django.contrib import admin, messages
from django.utils.html import format_html

from . import evaluation
from .models import Attempt, EvalClip, FeatureFlag, Recording

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


@admin.register(Attempt)
class AttemptAdmin(admin.ModelAdmin):
    list_display = ("created_at", "transcript_source", "transcript", "phonemes", "audio_duration_s")
    list_filter = ("transcript_source",)
    readonly_fields = [field.name for field in Attempt._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(EvalClip)
class EvalClipAdmin(admin.ModelAdmin):
    """The labeling bench: upload a clip, type what was meant, label it by ear, then "Run eval" on a selection."""

    list_display = ("short_text", "language", "label", "phoneme_score", "word_score", "evaluated_at", "has_error")
    list_display_links = ("short_text",)
    list_editable = ("label",)
    list_filter = ("label", "language", ("evaluated_at", admin.EmptyFieldListFilter))
    search_fields = ("intended_text", "heard_transcript")
    actions = ["run_eval"]
    readonly_fields = (
        "player", "phoneme_score", "word_score", "expected_phonemes", "heard_phonemes",
        "heard_transcript", "eval_error", "evaluated_at", "created_at",
    )
    fieldsets = (
        (None, {"fields": ("audio", "player", "intended_text", "language", "label")}),
        ("Scores (filled in by Run eval)", {"fields": ("phoneme_score", "word_score", "evaluated_at", "eval_error")}),
        ("What the pipeline heard", {"fields": ("expected_phonemes", "heard_phonemes", "heard_transcript")}),
    )

    @admin.display(description="Intended text", ordering="intended_text")
    def short_text(self, obj):
        return obj.intended_text if len(obj.intended_text) <= 60 else obj.intended_text[:57] + "…"

    @admin.display(description="Error", boolean=True)
    def has_error(self, obj):
        return bool(obj.eval_error) if obj.evaluated_at else None

    @admin.display(description="Listen")
    def player(self, obj):
        if not obj.audio:
            return "—"
        return format_html('<audio controls preload="none" src="{}"></audio>', obj.audio.url)

    @admin.action(description="Run eval on the selected clips")
    def run_eval(self, request, queryset):
        failed = 0
        for clip in queryset:
            evaluation.evaluate(clip)
            failed += bool(clip.eval_error)
        done = queryset.count()
        if failed:
            self.message_user(
                request,
                f"Evaluated {done} clip(s); {failed} had errors (see the Error column and each clip's eval error).",
                messages.WARNING,
            )
        else:
            self.message_user(request, f"Evaluated {done} clip(s).", messages.SUCCESS)

