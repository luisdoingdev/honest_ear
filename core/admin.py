import logging

from django.contrib import admin, messages
from django.core.files.base import ContentFile
from django.urls import reverse
from django.utils.html import format_html, format_html_join

from . import bench
from .models import Attempt, EvalClip, EvalResult, EvalRun, FeatureFlag, Recording

logger = logging.getLogger(__name__)

# mimetypes would give .weba for audio/webm, which the eval clip's audio validator rejects.
RECORDING_EXTENSIONS = {"audio/webm": ".webm", "audio/mp4": ".mp4", "audio/ogg": ".ogg"}

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
    list_display = ("created_at", "transcript_source", "transcript", "phonemes", "audio_duration_s", "has_audio")
    list_filter = ("transcript_source",)
    actions = ["promote_to_eval_clip"]

    @admin.display(description="Audio kept", boolean=True)
    def has_audio(self, obj):
        return obj.recording_id is not None

    @admin.action(description="Promote to EvalClip")
    def promote_to_eval_clip(self, request, queryset):
        """Copies the audio and links the attempt; intended text and label are left for a human to fill in."""
        promoted, no_audio, already = 0, 0, 0
        for attempt in queryset.select_related("recording"):
            if attempt.recording is None:
                no_audio += 1
            elif attempt.eval_clips.exists():
                already += 1
            else:
                recording = attempt.recording
                extension = RECORDING_EXTENSIONS.get(recording.content_type, ".webm")
                clip = EvalClip(source_attempt=attempt)
                clip.audio.save(f"attempt-{attempt.pk}{extension}", ContentFile(bytes(recording.audio)), save=True)
                promoted += 1
        self.message_user(request, f"Promoted {promoted} attempt(s) to eval clips. Type their intended text and label them.")
        if no_audio:
            self.message_user(
                request,
                f"{no_audio} attempt(s) skipped: their audio was already deleted. "
                "Turn on the keep_audio feature flag to keep audio for new recordings.",
                messages.WARNING,
            )
        if already:
            self.message_user(request, f"{already} attempt(s) skipped: already promoted.", messages.INFO)
    readonly_fields = [field.name for field in Attempt._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(EvalClip)
class EvalClipAdmin(admin.ModelAdmin):
    """The golden set: upload or promote a clip, type what was meant, label it by ear."""

    list_display = ("short_text", "language", "human_label", "source", "created_at")
    list_display_links = ("short_text",)
    list_editable = ("human_label",)
    list_filter = ("human_label", "language", ("source_attempt", admin.EmptyFieldListFilter))
    search_fields = ("intended_text", "notes")
    readonly_fields = ("player", "source_attempt", "created_at", "latest_alignment")
    fields = (
        "audio", "player", "intended_text", "language", "human_label", "notes", "source_attempt", "created_at",
        "latest_alignment",
    )
    actions = ["run_bench"]

    class Media:
        css = {"all": ("core/admin/eval.css",)}

    @admin.display(description="Latest run")
    def latest_alignment(self, obj):
        result = obj.results.select_related("run").order_by("-run__created_at", "-pk").first() if obj.pk else None
        if result is None:
            return "Not run yet: select this clip in the list and choose Run bench."
        run_link = format_html(
            '<a href="{}">run {}</a>', reverse("admin:core_evalrun_change", args=[result.run_id]), result.run_id
        )
        if result.status != EvalResult.Status.SCORED:
            return format_html(
                '<p class="ev-summary">{}: <strong>{}</strong>{}</p>',
                run_link, result.get_status_display(), f" ({result.error})" if result.error else "",
            )
        summary = format_html(
            '<p class="ev-summary">{}: PER <strong>{}</strong> → predicted <strong>{}</strong>, human label <strong>{}</strong></p>',
            run_link, f"{result.per:.3f}", result.get_predicted_band_display(),
            result.get_human_label_display() or "none",
        )
        words = format_html_join(
            "",
            '<div class="ev-word"><div class="ev-word-label">{}</div><div class="ev-ops">{}</div></div>',
            ((word["word"], self._ops(word["ops"])) for word in result.alignment),
        )
        legend = format_html('<p class="ev-legend">Top row: expected. Bottom row: heard. Red: substituted, dropped (–) or extra.</p>')
        return format_html('{}<div class="ev-words">{}</div>{}', summary, words, legend)

    @staticmethod
    def _ops(ops):
        def cell(phoneme):
            return format_html('<span class="ev-gap">–</span>') if phoneme is None else phoneme

        return format_html_join(
            "",
            '<span class="ev-op ev-{}"><span class="ev-exp">{}</span><span class="ev-heard">{}</span></span>',
            ((op["op"], cell(op["expected"]), cell(op["heard"])) for op in ops),
        )

    @admin.action(description="Run bench on the selected clips")
    def run_bench(self, request, queryset):
        run = bench.run_bench(list(queryset.order_by("created_at")))
        summary = run.summary
        url = reverse("admin:core_evalrun_change", args=[run.pk])
        self.message_user(
            request,
            format_html(
                'Run {} done: {} clip(s), {} scored and labeled, agreement {}. <a href="{}">Open the run</a>.',
                run.pk, summary["clips"], summary["judged"],
                "—" if summary["agreement"] is None else f"{summary['agreement']}%", url,
            ),
            messages.SUCCESS if not summary["statuses"]["error"] else messages.WARNING,
        )

    @admin.display(description="Intended text", ordering="intended_text")
    def short_text(self, obj):
        text = obj.intended_text or "(to type)"
        return text if len(text) <= 60 else text[:57] + "…"

    @admin.display(description="Source", ordering="source_attempt")
    def source(self, obj):
        return "promoted" if obj.source_attempt_id else "uploaded"

    @admin.display(description="Listen")
    def player(self, obj):
        if not obj.audio:
            return "—"
        return format_html('<audio controls preload="none" src="{}"></audio>', obj.audio.url)


class EvalResultInline(admin.TabularInline):
    model = EvalResult
    extra = 0
    can_delete = False
    fields = ("clip", "human_label", "status", "per", "predicted_band", "word_overlap", "error")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(EvalRun)
class EvalRunAdmin(admin.ModelAdmin):
    list_display = ("__str__", "created_at", "config_version", "clip_count", "agreement", "spearman")
    readonly_fields = ("created_at", "config_version", "config", "summary")
    inlines = [EvalResultInline]

    def has_add_permission(self, request):
        return False

    @admin.display(description="Clips")
    def clip_count(self, obj):
        return obj.summary.get("clips")

    @admin.display(description="Agreement %")
    def agreement(self, obj):
        return obj.summary.get("agreement")

    @admin.display(description="Spearman")
    def spearman(self, obj):
        return obj.summary.get("spearman")

