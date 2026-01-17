from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("drivers", "0010_merge_20260117_1730"),
    ]

    operations = [
        migrations.CreateModel(
            name="FleetVehicle",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("vehicle_number", models.CharField(max_length=50, unique=True)),
                ("year", models.PositiveIntegerField()),
                ("make", models.CharField(max_length=50)),
                ("model", models.CharField(max_length=50)),
                ("notes", models.TextField(blank=True)),
            ],
            options={
                "ordering": ["vehicle_number"],
            },
        ),
        migrations.RemoveField(
            model_name="drivervehicleassignment",
            name="vehicle_number",
        ),
        migrations.AddField(
            model_name="drivervehicleassignment",
            name="vehicle",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, to="drivers.fleetvehicle"),
        ),
    ]
