from django.db import migrations


def create_live_captions_flag(apps, schema_editor):
    FeatureFlag = apps.get_model("core", "FeatureFlag")
    FeatureFlag.objects.get_or_create(
        name="live_captions",
        defaults={
            "enabled": True,
            "description": (
                "Streams the microphone to Deepgram and shows captions while speaking. "
                "Needs the transcription flag and a Deepgram key with Member permissions. "
                "When off, recordings are transcribed after Stop."
            ),
        },
    )


def remove_live_captions_flag(apps, schema_editor):
    apps.get_model("core", "FeatureFlag").objects.filter(name="live_captions").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0004_seed_transcription_flag"),
    ]

    operations = [
        migrations.RunPython(create_live_captions_flag, remove_live_captions_flag),
    ]
