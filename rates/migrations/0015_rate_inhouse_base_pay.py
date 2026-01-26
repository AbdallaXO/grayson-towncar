from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("rates", "0014_alter_rate_options"),
    ]

    operations = [
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
    ]
