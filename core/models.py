import uuid

from django.core.validators import FileExtensionValidator
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


class Attempt(models.Model):
    """What was heard in one recording, kept as text after the audio itself is deleted.

    The Deepgram transcript and the Modal phonemes sit side by side; pronunciation scoring will compare them.
    """

    class TranscriptSource(models.TextChoices):
        LIVE = "live", "Live captions (Deepgram Flux)"
        DEEPGRAM = "deepgram", "After Stop (Deepgram nova-3)"
        NONE = "none", "Not transcribed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # The audio row is deleted after playback; the attempt stays.
    recording = models.ForeignKey(
        Recording, null=True, blank=True, on_delete=models.SET_NULL, related_name="attempts"
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    transcript = models.TextField(null=True, blank=True)
    transcript_source = models.CharField(
        max_length=10, choices=TranscriptSource.choices, default=TranscriptSource.NONE
    )
    transcript_error = models.CharField(max_length=300, blank=True)
    phonemes = models.TextField(null=True, blank=True)
    phoneme_inference_ms = models.FloatField(null=True, blank=True)
    audio_duration_s = models.FloatField(null=True, blank=True)
    scorer_error = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.id} ({self.created_at:%Y-%m-%d %H:%M})"


EVAL_AUDIO_EXTENSIONS = ["webm", "ogg", "mp4", "m4a", "mp3", "wav"]


class EvalClip(models.Model):
    """One clip in the golden set: what was meant, and a human's judgement of how it sounded.

    The bench (EvalRun / EvalResult) checks whether the pronunciation score agrees with that judgement.
    """

    class Label(models.TextChoices):
        GOOD = "good", "Good"
        OKAY = "okay", "Okay"
        OFF = "off", "Off"

    class Language(models.TextChoices):
        SPANISH = "es", "Spanish"
        ENGLISH = "en", "English"

    audio = models.FileField(
        upload_to="eval_clips/", validators=[FileExtensionValidator(EVAL_AUDIO_EXTENSIONS)]
    )
    intended_text = models.TextField(
        blank=True,
        help_text="What the speaker meant to say, typed by a human. Never copy it from Deepgram's transcript.",
    )
    language = models.CharField(
        max_length=2, choices=Language.choices, default=Language.SPANISH,
        help_text="Of the intended text; picks the pronunciation its expected phonemes come from.",
    )
    human_label = models.CharField(
        max_length=4, choices=Label.choices, blank=True, help_text="Your judgement, by ear."
    )
    notes = models.TextField(blank=True)
    source_attempt = models.ForeignKey(
        "Attempt", null=True, blank=True, on_delete=models.SET_NULL, related_name="eval_clips",
        help_text="Set when the clip was promoted from a real recording.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "eval clip"

    def __str__(self):
        label = self.get_human_label_display() or "Unlabeled"
        return f"{label}: {self.intended_text[:40] or '(no intended text yet)'}"


class EvalRun(models.Model):
    """One pass of the bench over some eval clips, with the scoring config it used and its summary."""

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    config_version = models.CharField(max_length=40)
    config = models.JSONField(help_text="Snapshot of core/eval_config/scoring.json at run time.")
    summary = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "eval run"

    def __str__(self):
        return f"Run {self.pk} ({self.created_at:%Y-%m-%d %H:%M}, config v{self.config_version})"


class EvalResult(models.Model):
    class Status(models.TextChoices):
        SCORED = "scored", "Scored"
        TOO_SHORT = "too_short", "Too short"
        WRONG_SENTENCE = "wrong_sentence", "Wrong sentence"
        ERROR = "error", "Error"

    run = models.ForeignKey(EvalRun, on_delete=models.CASCADE, related_name="results")
    clip = models.ForeignKey(EvalClip, on_delete=models.CASCADE, related_name="results")
    # The label when the run happened: relabeling a clip later doesn't rewrite an old run's agreement.
    human_label = models.CharField(max_length=4, choices=EvalClip.Label.choices, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices)
    per = models.FloatField(null=True, blank=True, verbose_name="PER")
    predicted_band = models.CharField(max_length=4, choices=EvalClip.Label.choices, blank=True)
    alignment = models.JSONField(default=list, blank=True, help_text="Per word: ops (match/sub/ins/del).")
    word_overlap = models.FloatField(null=True, blank=True)
    duration_s = models.FloatField(null=True, blank=True)
    transcript = models.TextField(blank=True)
    heard_phonemes = models.TextField(blank=True)
    error = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ["run", "pk"]
        constraints = [models.UniqueConstraint(fields=["run", "clip"], name="one_result_per_clip_per_run")]

    def __str__(self):
        return f"{self.clip} in run {self.run_id}: {self.status}"
