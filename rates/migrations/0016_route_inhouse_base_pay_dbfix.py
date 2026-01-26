from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("rates", "0015_rate_inhouse_base_pay"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.AddField(
                    model_name="route",
                    name="inhouse_base_pay",
                    field=models.DecimalField(
                        blank=True,
                        decimal_places=2,
                        help_text="Default base pay for inhouse drivers on this route",
                        max_digits=10,
                        null=True,
                    ),
                ),
            ],
            state_operations=[],
        ),
    ]
