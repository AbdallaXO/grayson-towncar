"""Clermont is Local, except the run out to the cruise port.

Clermont was the address that started all of this: leg 26972, a 60-mile run to
Port Canaveral, priced at $25 because the matcher knew nothing about Clermont
and the old code borrowed the customer's MCO → Disney rate.

What the paid history actually says (completed in-house legs in the 2026-08-27
snapshot):

  Clermont <-> MCO      $25 x 7, $30 x 1, $40 x 1   -> Local
  Clermont -> the port  $55 (leg 21992, corrected by hand by Rayyan)

So Clermont belongs in Local, and the port run is a genuine exception in the
same way Championsgate is: right zone, wrong price for one pairing. Without the
exception row below, the zone rate would make that trip $40 — closer than $25
and still not what we pay.

The Route carries no Rate, so nothing about what a customer is quoted changes;
Route.inhouse_base_pay is read only by driver pay, and the reverse direction
matches the same row.

Idempotent, and reversible.
"""
from decimal import Decimal

from django.db import migrations

CLERMONT = "Clermont"
PORT = "Port Canaveral"
LOCAL = "Local"
PORT_PAY = Decimal("55.00")


def add_clermont(apps, schema_editor):
    Location = apps.get_model("rates", "Location")
    Route = apps.get_model("rates", "Route")
    Zone = apps.get_model("rates", "Zone")

    local = Zone.objects.filter(name=LOCAL).first()
    if local is None:
        return

    clermont, _ = Location.objects.get_or_create(
        name=CLERMONT, defaults={"pay_zone": local, "aliases": ""}
    )
    if clermont.pay_zone_id is None:
        clermont.pay_zone = local
        clermont.save(update_fields=["pay_zone"])

    port = Location.objects.filter(name=PORT).first()
    if port is None:
        return

    route = (
        Route.objects.filter(origin=clermont, destination=port).first()
        or Route.objects.filter(origin=port, destination=clermont).first()
    )
    if route is None:
        Route.objects.create(
            origin=clermont,
            destination=port,
            inhouse_base_pay=PORT_PAY,
            description="Clermont is a Local place, but the cruise-port run is a "
                        "long one. Paid $55 either direction.",
        )
    elif route.inhouse_base_pay is None:
        route.inhouse_base_pay = PORT_PAY
        route.save(update_fields=["inhouse_base_pay"])


def remove_clermont(apps, schema_editor):
    Location = apps.get_model("rates", "Location")
    Route = apps.get_model("rates", "Route")

    clermont = Location.objects.filter(name=CLERMONT).first()
    if clermont is None:
        return
    Route.objects.filter(origin=clermont).delete()
    Route.objects.filter(destination=clermont).delete()
    clermont.delete()


class Migration(migrations.Migration):

    dependencies = [
        ("rates", "0026_seed_pay_zones_and_local_aliases"),
    ]

    operations = [
        migrations.RunPython(add_clermont, remove_clermont),
    ]
