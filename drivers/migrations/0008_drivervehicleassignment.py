from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("drivers", "0007_driver_driver_type"),
    ]

    operations = [
        migrations.CreateModel(
            name="DriverVehicleAssignment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("date", models.DateField()),
                ("vehicle_number", models.CharField(max_length=50)),
                ("driver", models.ForeignKey(on_delete=models.deletion.CASCADE, related_name="vehicle_assignments", to="drivers.driver")),
            ],
            options={
                "ordering": ["date", "driver"],
                "unique_together": {("driver", "date")},
            },
        ),
    ]
