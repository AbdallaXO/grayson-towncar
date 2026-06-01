from datetime import time
from decimal import Decimal

from django.db import migrations
from django.db.models import Q


def backfill_marker(apps, schema_editor):
    """Mark existing legs whose BOOKED pickup is in the 10 PM-6 AM window AND whose
    reservation actually carried the fee (additional_charges >= $20) as already
    charged (afterhours_fee = 20). Online bookings add the $20 at booking via
    extra_charges(), so without this marker they would wrongly show "after-hours
    fee owed" on the dashboard.

    The `additional_charges >= 20` guard is deliberate: dispatcher/phone bookings
    do NOT auto-apply the fee, so legs whose reservation has no such charge are
    left at 0 — a genuinely-owed late-night trip that was never billed will then
    correctly surface on the dashboard to be charged once, rather than being
    silently suppressed. Only legs DELAYED into the window after booking still flag.
    """
    Leg = apps.get_model("reservations", "Leg")
    Leg.objects.filter(
        Q(pickup_time__gte=time(22, 0)) | Q(pickup_time__lt=time(6, 0)),
        afterhours_fee__lt=Decimal("20.00"),
        reservation__additional_charges__gte=Decimal("20.00"),
    ).update(afterhours_fee=Decimal("20.00"))


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("reservations", "0106_historicalleg_afterhours_fee_leg_afterhours_fee"),
    ]

    operations = [
        migrations.RunPython(backfill_marker, noop),
    ]
