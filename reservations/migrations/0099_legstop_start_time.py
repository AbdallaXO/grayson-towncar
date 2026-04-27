from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("reservations", "0098_simplify_legstop_choices"),
    ]

    operations = [
        migrations.AddField(
            model_name="legstop",
            name="start_time",
            field=models.TimeField(
                blank=True,
                null=True,
                help_text="When this stop begins. Required for charter (hourly) stops, optional otherwise.",
            ),
        ),
    ]
