"""
Recompute Agency.total_paid_commission from the AgencyCommissionPayout rows.

The column was maintained by adding each payout's amount as it was created — and
the amount was added twice, once by users.signals.handle_agency_payout_changes and
once again by the caller that had just created the payout. Both call sites now
sync instead of adding (users/models.py), but every agency paid before that fix
still carries the doubled figure: 22 of 50 agencies, $14,170.74 of commission the
books say was paid and wasn't.

The number is display-only — it is read on the Affiliate Explorer and the agency
detail page, and no payout logic depends on it — so this is a safe repair. It
rewrites nothing except that one column, and only where it disagrees with the
payouts.

Usage:
    python manage.py resync_agency_commission_totals --dry-run   # report, write nothing
    python manage.py resync_agency_commission_totals             # apply

--dry-run performs only reads.
"""
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Sum

from users.models import Agency, AgencyCommissionPayout

ZERO = Decimal("0.00")


class Command(BaseCommand):
    help = "Recompute Agency.total_paid_commission from AgencyCommissionPayout rows."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change without writing.",
        )

    def handle(self, *args, **opts):
        dry_run = opts["dry_run"]

        actual = {
            row["agency_id"]: row["total"] or ZERO
            for row in AgencyCommissionPayout.objects.values("agency_id").annotate(
                total=Sum("total_amount")
            )
        }

        drifted = []
        for agency in Agency.objects.all().order_by("id"):
            stored = agency.total_paid_commission or ZERO
            truth = actual.get(agency.id, ZERO)
            if stored != truth:
                drifted.append((agency, stored, truth))

        if not drifted:
            self.stdout.write(self.style.SUCCESS("All agency totals already match their payouts."))
            return

        overstated = sum(s - t for _, s, t in drifted if s > t)
        understated = sum(t - s for _, s, t in drifted if t > s)

        for agency, stored, truth in drifted:
            direction = "over" if stored > truth else "under"
            self.stdout.write(
                f"  #{agency.id:<4} {agency.name[:38]:<38} "
                f"stored ${stored:>10,.2f}  payouts ${truth:>10,.2f}  ({direction})"
            )

        self.stdout.write("")
        self.stdout.write(
            f"{len(drifted)} of {Agency.objects.count()} agencies disagree with their payouts."
        )
        self.stdout.write(f"  overstated by ${overstated:,.2f}")
        self.stdout.write(f"  understated by ${understated:,.2f}")

        if dry_run:
            self.stdout.write(self.style.WARNING("\n--dry-run: nothing written."))
            return

        with transaction.atomic():
            for agency, _stored, truth in drifted:
                agency.total_paid_commission = truth
                agency.save(update_fields=["total_paid_commission"])

        self.stdout.write(self.style.SUCCESS(f"\nCorrected {len(drifted)} agency totals."))
