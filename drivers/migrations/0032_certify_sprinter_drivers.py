from django.db import migrations


SPRINTER_TYPE = "Van(14 Pax)"


def flag_and_backfill(apps, schema_editor):
    """Mark the Sprinter / 14-pax type as requiring certification, then certify the
    drivers who already have (or had) a vehicle assignment to a Sprinter unit — so the
    new hard block doesn't suddenly lock out drivers who currently drive it."""
    Vehicle = apps.get_model("rates", "Vehicle")
    Driver = apps.get_model("drivers", "Driver")
    DriverVehicleAssignment = apps.get_model("drivers", "DriverVehicleAssignment")

    sprinter = Vehicle.objects.filter(vehicle_type=SPRINTER_TYPE).first()
    if sprinter is None:
        return

    if not sprinter.requires_certification:
        sprinter.requires_certification = True
        sprinter.save(update_fields=["requires_certification"])

    driver_ids = (
        DriverVehicleAssignment.objects
        .filter(vehicle__vehicle_type=sprinter)
        .values_list("driver_id", flat=True)
        .distinct()
    )
    for driver in Driver.objects.filter(id__in=list(driver_ids)):
        driver.certified_vehicle_types.add(sprinter)


def unflag(apps, schema_editor):
    Vehicle = apps.get_model("rates", "Vehicle")
    sprinter = Vehicle.objects.filter(vehicle_type=SPRINTER_TYPE).first()
    if sprinter is not None and sprinter.requires_certification:
        sprinter.requires_certification = False
        sprinter.save(update_fields=["requires_certification"])


class Migration(migrations.Migration):

    dependencies = [
        ("drivers", "0031_driver_certified_vehicle_types_and_more"),
        ("rates", "0023_vehicle_requires_certification"),
    ]

    operations = [
        migrations.RunPython(flag_and_backfill, unflag),
    ]
