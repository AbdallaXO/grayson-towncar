from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("reservations", "0099_legstop_start_time"),
    ]

    operations = [
        migrations.AlterField(
            model_name="legstop",
            name="location_text",
            field=models.CharField(
                blank=True,
                max_length=255,
                help_text="Free-text address or venue name for this stop. Optional for charter stops (driver takes them anywhere).",
            ),
        ),
    ]
