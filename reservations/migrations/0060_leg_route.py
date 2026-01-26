from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("reservations", "0059_add_fbclid_tracking"),
        ("rates", "0016_route_inhouse_base_pay_dbfix"),
    ]

    operations = [
        migrations.AddField(
            model_name="leg",
            name="route",
            field=models.ForeignKey(
                blank=True,
                help_text="Matched route for this leg (auto-filled when possible)",
                null=True,
                on_delete=models.SET_NULL,
                related_name="legs",
                to="rates.route",
            ),
        ),
    ]
