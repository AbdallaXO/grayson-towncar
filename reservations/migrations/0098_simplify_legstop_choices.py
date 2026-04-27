from django.db import migrations, models


def map_legacy_stop_types(apps, schema_editor):
    """Collapse old categories onto the simplified set:
       wait → stop, luggage → stop, other → stop.
    """
    LegStop = apps.get_model("reservations", "LegStop")
    LegStop.objects.filter(stop_type__in=["wait", "luggage", "other"]).update(stop_type="stop")


def reverse_map_stop_types(apps, schema_editor):
    LegStop = apps.get_model("reservations", "LegStop")
    LegStop.objects.filter(stop_type="stop").update(stop_type="wait")


class Migration(migrations.Migration):

    dependencies = [
        ("reservations", "0097_backfill_legflight_from_flight_information"),
    ]

    operations = [
        migrations.RunPython(map_legacy_stop_types, reverse_map_stop_types),
        migrations.AlterField(
            model_name="legstop",
            name="stop_type",
            field=models.CharField(
                choices=[
                    ("dropoff", "Additional drop-off"),
                    ("stop", "Additional stop"),
                    ("pickup", "Additional pickup"),
                    ("charter", "Charter (hourly)"),
                ],
                default="dropoff",
                max_length=10,
            ),
        ),
    ]
