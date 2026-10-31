from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    """EvalClip becomes the golden set: label → human_label (kept, not re-created), notes, source_attempt.
    The v1 score columns go; results live on EvalResult from now on."""

    dependencies = [
        ("core", "0007_evalclip"),
    ]

    operations = [
        migrations.RenameField("evalclip", "label", "human_label"),
        migrations.AlterField(
            "evalclip",
            "human_label",
            models.CharField(
                blank=True,
                choices=[("good", "Good"), ("okay", "Okay"), ("off", "Off")],
                help_text="Your judgement, by ear.",
                max_length=4,
            ),
        ),
        migrations.AlterField(
            "evalclip",
            "intended_text",
            models.TextField(
                blank=True,
                help_text="What the speaker meant to say, typed by a human. Never copy it from Deepgram's transcript.",
            ),
        ),
        migrations.AddField("evalclip", "notes", models.TextField(blank=True)),
        migrations.AddField(
            "evalclip",
            "source_attempt",
            models.ForeignKey(
                blank=True,
                help_text="Set when the clip was promoted from a real recording.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="eval_clips",
                to="core.attempt",
            ),
        ),
        *[
            migrations.RemoveField("evalclip", field)
            for field in (
                "phoneme_score", "word_score", "expected_phonemes", "heard_phonemes",
                "heard_transcript", "eval_error", "evaluated_at",
            )
        ],
    ]
