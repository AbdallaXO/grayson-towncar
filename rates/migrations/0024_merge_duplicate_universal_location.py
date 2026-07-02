"""Merge the duplicate 'Universal Studios Area' Location into 'Universal Studios
Area Hotels'.

The two rows split Universal's routes: 'Universal Studios Area' carried ONLY the
Universal → Port Canaveral rate, while 'Universal Studios Area Hotels' carried the
airport / Disney / Sanford / Kissimmee rates. Because both names appeared, adjacent
and indistinguishable, in the public quote dropdown, a tourist picking the wrong one
dead-ended the flagship MCO → Universal route (or the Universal → cruise route) into
"Route Not Set Up Online". Collapsing to one row makes a single Universal entry carry
every rate, so neither direction dead-ends.
"""
from django.db import migrations

DUP_NAME = "Universal Studios Area"
KEEP_NAME = "Universal Studios Area Hotels"


def merge_universal(apps, schema_editor):
    Location = apps.get_model("rates", "Location")
    Route = apps.get_model("rates", "Route")
    Rate = apps.get_model("rates", "Rate")
    LegStop = apps.get_model("reservations", "LegStop")

    dup = Location.objects.filter(name=DUP_NAME).first()
    keep = Location.objects.filter(name=KEEP_NAME).first()
    if not dup or not keep or dup.id == keep.id:
        return

    def repoint(route, *, origin_id=None, destination_id=None):
        """Move a route onto the kept Location. If that (origin, destination) route
        already exists, fold this route's rates into it and drop the duplicate route;
        otherwise just repoint the endpoint."""
        target_o = origin_id if origin_id is not None else route.origin_id
        target_d = destination_id if destination_id is not None else route.destination_id
        existing = (
            Route.objects.filter(origin_id=target_o, destination_id=target_d)
            .exclude(id=route.id)
            .first()
        )
        if existing:
            Rate.objects.filter(route=route).update(route=existing)
            route.delete()
        else:
            if origin_id is not None:
                route.origin_id = origin_id
            if destination_id is not None:
                route.destination_id = destination_id
            route.save()

    for route in list(Route.objects.filter(origin=dup)):
        repoint(route, origin_id=keep.id)
    for route in list(Route.objects.filter(destination=dup)):
        repoint(route, destination_id=keep.id)

    # Preserve any structured LegStop matches before removing the duplicate row.
    LegStop.objects.filter(location=dup).update(location=keep)

    dup.delete()


def noop_reverse(apps, schema_editor):
    # Irreversible: the two rows can't be reliably un-merged. No-op so the migration
    # can still be unapplied without error.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("rates", "0023_vehicle_requires_certification"),
        ("reservations", "0118_lead_referrer_host"),
    ]

    operations = [
        migrations.RunPython(merge_universal, noop_reverse),
    ]
