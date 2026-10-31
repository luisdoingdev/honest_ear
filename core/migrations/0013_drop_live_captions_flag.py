from django.db import migrations

TRANSCRIPTION_BEFORE = "Sends each recording to Deepgram and shows the transcript. Needs DEEPGRAM_API_KEY."
TRANSCRIPTION_AFTER = "Sends each recording to Whisper (the transcriber container) and shows the transcript. Needs TRANSCRIBER_URL."


def forwards(apps, schema_editor):
    FeatureFlag = apps.get_model("core", "FeatureFlag")
    # Live captions were a Deepgram stream; the websocket pipeline has none.
    FeatureFlag.objects.filter(name="live_captions").delete()
    FeatureFlag.objects.filter(name="transcription").update(description=TRANSCRIPTION_AFTER)


def backwards(apps, schema_editor):
    FeatureFlag = apps.get_model("core", "FeatureFlag")
    FeatureFlag.objects.get_or_create(
        name="live_captions",
        defaults={"enabled": True, "description": "Streams the microphone to Deepgram and shows captions while speaking."},
    )
    FeatureFlag.objects.filter(name="transcription").update(description=TRANSCRIPTION_BEFORE)


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0012_attempt_alignment"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
