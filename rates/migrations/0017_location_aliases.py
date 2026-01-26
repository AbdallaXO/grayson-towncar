from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("rates", "0016_route_inhouse_base_pay_dbfix"),
    ]

    operations = [
        migrations.AddField(
            model_name="location",
            name="aliases",
            field=models.TextField(
                blank=True,
                help_text="Comma-separated alternate names (e.g., MCO, Orlando Airport, Disney)",
            ),
        ),
    ]
