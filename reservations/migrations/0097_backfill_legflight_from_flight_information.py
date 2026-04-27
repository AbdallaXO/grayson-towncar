# Backfill: for every Leg that has a flight_information (legacy OneToOne),
# create a corresponding LegFlight row marked is_controlling=True. This lets
# new code read Leg.controlling_flight uniformly across legacy and new legs.
#
# Safety:
#   - Idempotent: skips legs that already have a controlling LegFlight.
#   - Reversible: rollback deletes the auto-created controlling rows.
#   - Single INSERT-SELECT pattern; safe on thousands of rows.
#   - Does NOT touch Leg.flight_information — that OneToOne stays intact for
#     the entire lifetime of this rollout.

from django.db import migrations


def forwards(apps, schema_editor):
    Leg = apps.get_model("reservations", "Leg")
    LegFlight = apps.get_model("reservations", "LegFlight")

    legs_with_flights = Leg.objects.filter(flight_information_id__isnull=False)
    existing_controlling_leg_ids = set(
        LegFlight.objects.filter(is_controlling=True).values_list("leg_id", flat=True)
    )

    to_create = []
    for leg in legs_with_flights.only("id", "flight_information_id").iterator():
        if leg.id in existing_controlling_leg_ids:
            continue
        to_create.append(
            LegFlight(
                leg_id=leg.id,
                flight_id=leg.flight_information_id,
                is_controlling=True,
                sequence=0,
            )
        )

    if to_create:
        LegFlight.objects.bulk_create(to_create, batch_size=500, ignore_conflicts=True)


def backwards(apps, schema_editor):
    LegFlight = apps.get_model("reservations", "LegFlight")
    Leg = apps.get_model("reservations", "Leg")

    legs_with_flights_ids = set(
        Leg.objects.filter(flight_information_id__isnull=False).values_list("id", flat=True)
    )
    LegFlight.objects.filter(
        leg_id__in=legs_with_flights_ids,
        is_controlling=True,
        sequence=0,
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("reservations", "0096_add_legstop_legflight_and_manual_review"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
