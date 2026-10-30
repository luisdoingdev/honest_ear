from django.db import migrations


def create_transcription_flag(apps, schema_editor):
    FeatureFlag = apps.get_model("core", "FeatureFlag")
    FeatureFlag.objects.get_or_create(
        name="transcription",
        defaults={
            "enabled": True,
            "description": "Sends each recording to Deepgram and shows the transcript. Needs DEEPGRAM_API_KEY.",
        },
    )


def remove_transcription_flag(apps, schema_editor):
    apps.get_model("core", "FeatureFlag").objects.filter(name="transcription").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0003_seed_recording_flag"),
    ]

    operations = [
        migrations.RunPython(create_transcription_flag, remove_transcription_flag),
    ]
