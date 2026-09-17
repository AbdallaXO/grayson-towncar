"""
Mark after-hours legs whose fee is provably already in the booking total.

Dry run first — it prints every leg it would touch and every late leg it is
deliberately leaving alone, so the ones that were never billed are visible
rather than silently absorbed:

    python manage.py backfill_afterhours_markers
    python manage.py backfill_afterhours_markers --apply
"""

from decimal import Decimal

from django.core.management.base import BaseCommand

from reservations.afterhours_backfill import _standard_price, apply_backfill, provable_legs
from reservations.models import Reservation
from reservations.utils import AFTERHOURS_FEE_AMOUNT, is_afterhours_time


class Command(BaseCommand):
    help = "Mark after-hours legs whose fee is provably in the booking total."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Write the markers. Without it, nothing is changed.",
        )
        parser.add_argument(
            "--show-unbilled",
            action="store_true",
            help="Also list late legs that look genuinely unbilled (still worth collecting).",
        )

    def handle(self, *args, **options):
        legs = provable_legs()

        self.stdout.write(
            self.style.MIGRATE_HEADING(
                f"\nProvably collected — {len(legs)} leg(s) would be marked\n"
            )
        )
        for leg in legs:
            res = leg.reservation
            self.stdout.write(
                f"  res #{res.id:<7} leg {leg.id:<7} {leg.pickup_date} "
                f"{leg.pickup_time}  total ${res.total_price}"
            )

        if options["show_unbilled"]:
            self._report_unbilled()

        if not options["apply"]:
            self.stdout.write(
                self.style.WARNING(
                    "\nDry run — nothing changed. Re-run with --apply to write them.\n"
                )
            )
            return

        count = apply_backfill()
        self.stdout.write(self.style.SUCCESS(f"\nMarked {count} leg(s).\n"))

    def _report_unbilled(self):
        """Late legs whose total matches the standard rate with no fee on top."""
        rows = []
        qs = (
            Reservation.objects.exclude(status="cancelled")
            .select_related("rate")
            .prefetch_related("legs")
        )
        for res in qs:
            standard = _standard_price(res)
            if standard is None:
                continue
            late = [
                lg for lg in res.legs.all()
                if lg.status != "cancelled" and is_afterhours_time(lg.pickup_time)
                and Decimal(lg.afterhours_fee or 0) < AFTERHOURS_FEE_AMOUNT
            ]
            if not late:
                continue
            if Decimal(res.total_price or 0) == standard:
                for leg in late:
                    rows.append((leg, res, standard))

        rows.sort(key=lambda r: (r[0].pickup_date or ""))
        self.stdout.write(
            self.style.MIGRATE_HEADING(
                f"\nLooks genuinely unbilled — {len(rows)} leg(s), NOT marked\n"
            )
        )
        for leg, res, standard in rows:
            self.stdout.write(
                f"  res #{res.id:<7} leg {leg.id:<7} {leg.pickup_date} "
                f"{leg.pickup_time}  paid ${res.total_price} = standard ${standard}"
            )
