from django.db import migrations


def create_keep_audio_flag(apps, schema_editor):
    FeatureFlag = apps.get_model("core", "FeatureFlag")
    FeatureFlag.objects.get_or_create(
        name="keep_audio",
        defaults={
            "enabled": False,
            "description": (
                "Eval mode: keeps recordings' audio after playback (and past the 10-minute cleanup) so their "
                "Attempts can be promoted to eval clips. Off: audio is deleted as visitors are told."
            ),
        },
    )


def remove_keep_audio_flag(apps, schema_editor):
    apps.get_model("core", "FeatureFlag").objects.filter(name="keep_audio").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0008_golden_set"),
    ]

    operations = [
        migrations.RunPython(create_keep_audio_flag, remove_keep_audio_flag),
    ]
