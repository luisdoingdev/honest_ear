from django.db import migrations


def create_recording_flag(apps, schema_editor):
    FeatureFlag = apps.get_model("core", "FeatureFlag")
    FeatureFlag.objects.get_or_create(
        name="recording",
        defaults={"enabled": True, "description": "Lets visitors record, play back and delete audio."},
    )


def remove_recording_flag(apps, schema_editor):
    apps.get_model("core", "FeatureFlag").objects.filter(name="recording").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0002_featureflag"),
    ]

    operations = [
        migrations.RunPython(create_recording_flag, remove_recording_flag),
    ]
