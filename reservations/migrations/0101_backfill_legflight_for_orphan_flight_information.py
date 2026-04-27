# Backfill: some legs have Leg.flight_information set (legacy OneToOne) but no
# matching LegFlight row — usually because a second flight was added via the
# new UI before the legacy one was wrapped, or because a leg was created/edited
# through a legacy code path after migration 0097 ran. The flights panel only
# falls back to flight_information when zero LegFlight rows exist, so the
# legacy flight ends up hidden.
#
# This migration finds those legs and creates a controlling LegFlight that
# wraps Leg.flight_information. Idempotent — skips legs that already have a
# LegFlight pointing at flight_information.

from django.db import migrations


def forwards(apps, schema_editor):
    Leg = apps.get_model("reservations", "Leg")
    LegFlight = apps.get_model("reservations", "LegFlight")

    legs = Leg.objects.filter(flight_information_id__isnull=False).only(
        "id", "flight_information_id"
    )

    for leg in legs.iterator():
        wraps_legacy = LegFlight.objects.filter(
            leg_id=leg.id, flight_id=leg.flight_information_id
        ).first()
        if wraps_legacy:
            if not wraps_legacy.is_controlling:
                LegFlight.objects.filter(leg_id=leg.id, is_controlling=True).update(
                    is_controlling=False
                )
                wraps_legacy.is_controlling = True
                wraps_legacy.save(update_fields=["is_controlling"])
            continue

        LegFlight.objects.filter(leg_id=leg.id, is_controlling=True).update(
            is_controlling=False
        )
        last = (
            LegFlight.objects.filter(leg_id=leg.id)
            .order_by("-sequence", "-id")
            .first()
        )
        next_sequence = (last.sequence + 1) if last else 0
        LegFlight.objects.create(
            leg_id=leg.id,
            flight_id=leg.flight_information_id,
            is_controlling=True,
            sequence=next_sequence,
        )


def backwards(apps, schema_editor):
    # Reverse is intentionally a no-op: we cannot tell which controlling rows
    # this migration created versus pre-existing ones, and removing them would
    # leave the legacy flight invisible again. 0097's reverse already handles
    # the original backfill window.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("reservations", "0100_legstop_location_optional"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
