"""
Management command to backfill revenue_share and profit_estimate on all existing legs.

Run once to correct historical data after the Cartesian-product bug fix:

    python manage.py recalculate_leg_revenue_shares

Use --dry-run to preview what would change without touching the database.
Use --reservation <id> to target a single reservation for spot-checking.
"""

from django.core.management.base import BaseCommand
from django.db.models import Count
from decimal import Decimal


class Command(BaseCommand):
    help = "Recalculate revenue_share and profit_estimate for all leg records"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would change without writing to the database",
        )
        parser.add_argument(
            "--reservation",
            type=int,
            metavar="ID",
            help="Only recalculate legs for this reservation ID",
        )

    def handle(self, *args, **options):
        from reservations.models import Reservation, Leg

        dry_run = options["dry_run"]
        reservation_id = options.get("reservation")

        qs = (
            Reservation.objects.annotate(leg_count=Count("legs"))
            .filter(leg_count__gt=0)
            .order_by("id")
        )
        if reservation_id:
            qs = qs.filter(id=reservation_id)

        total = qs.count()
        self.stdout.write(
            f"{'[DRY RUN] ' if dry_run else ''}Processing {total} reservation(s)…"
        )

        updated_legs = 0
        skipped = 0
        errors = 0

        for reservation in qs.iterator(chunk_size=200):
            if not reservation.total_price:
                skipped += 1
                continue

            leg_count = reservation.leg_count
            correct_share = (
                reservation.total_price / Decimal(leg_count)
            ).quantize(Decimal("0.01"))

            legs = list(reservation.legs.all())

            for leg in legs:
                old_share = leg.revenue_share
                new_profit = (correct_share - leg.total_driver_pay).quantize(
                    Decimal("0.01")
                )
                old_profit = leg.profit_estimate

                share_changed = old_share != correct_share
                profit_changed = old_profit != new_profit

                if not share_changed and not profit_changed:
                    continue

                if dry_run:
                    self.stdout.write(
                        f"  Leg {leg.id} (res #{reservation.id}): "
                        f"revenue_share {old_share} → {correct_share}  |  "
                        f"profit {old_profit} → {new_profit}"
                    )
                else:
                    try:
                        Leg.objects.filter(pk=leg.pk).update(
                            revenue_share=correct_share,
                            profit_estimate=new_profit,
                        )
                    except Exception as exc:
                        self.stderr.write(
                            f"  ERROR updating leg {leg.id}: {exc}"
                        )
                        errors += 1
                        continue

                updated_legs += 1

        verb = "Would update" if dry_run else "Updated"
        self.stdout.write(
            self.style.SUCCESS(
                f"\n{verb} {updated_legs} leg(s) across {total - skipped} reservation(s). "
                f"Skipped {skipped} (no total_price). Errors: {errors}."
            )
        )
