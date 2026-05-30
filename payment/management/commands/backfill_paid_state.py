"""
Backfill the denormalized paid-state columns on Reservation
(is_paid / paid_amount / gross_paid / total_refunded / first_paid_at) from the
authoritative Payment rows.

These columns are maintained going forward by payment.signals on every Payment
save/delete, but reservations whose payments settled before that signal existed
(or were created via imports / bulk admin edits that bypass post_save) drifted
out of sync. This command recomputes them idempotently.

Usage:
    python manage.py backfill_paid_state --dry-run     # report drift, write nothing
    python manage.py backfill_paid_state               # apply the fix

--dry-run performs only reads, so it is safe to run against a read-only
connection (e.g. USE_PROD_RO=1) to preview the impact before committing.
"""
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from payment.signals import compute_paid_state
from reservations.models import Reservation

ZERO = Decimal("0.00")


class Command(BaseCommand):
    help = "Recompute Reservation paid-state columns from Payment rows."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change without writing.",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=500,
            help="Reservations per progress tick (default 500).",
        )

    def handle(self, *args, **opts):
        dry = opts["dry_run"]
        batch = opts["batch_size"]

        # Only reservations that actually have payments can be out of sync; the
        # rest already have the correct zero/None defaults.
        qs = (
            Reservation.objects.filter(payments__isnull=False)
            .distinct()
            .order_by("pk")
        )
        total = qs.count()
        self.stdout.write(
            f"{'DRY-RUN: ' if dry else ''}Checking {total} reservations with payments…"
        )

        changed = 0
        net_delta = ZERO
        for i, res in enumerate(qs.iterator(chunk_size=batch), start=1):
            target = compute_paid_state(res)
            current = {
                "is_paid": res.is_paid,
                "paid_amount": res.paid_amount or ZERO,
                "gross_paid": res.gross_paid or ZERO,
                "total_refunded": res.total_refunded or ZERO,
                "first_paid_at": res.first_paid_at,
            }
            # Normalize for comparison (Decimal vs None)
            differs = (
                bool(current["is_paid"]) != bool(target["is_paid"])
                or current["paid_amount"] != target["paid_amount"]
                or current["gross_paid"] != target["gross_paid"]
                or current["total_refunded"] != target["total_refunded"]
                or current["first_paid_at"] != target["first_paid_at"]
            )
            if differs:
                changed += 1
                net_delta += target["paid_amount"] - (current["paid_amount"] or ZERO)
                if not dry:
                    Reservation.objects.filter(pk=res.pk).update(**target)
            if i % batch == 0:
                self.stdout.write(f"  …{i}/{total} scanned, {changed} need fixing")

        verb = "would be corrected" if dry else "corrected"
        self.stdout.write(self.style.SUCCESS(
            f"Done. {changed} reservation(s) {verb}. "
            f"Net paid_amount delta: ${net_delta:,.2f}"
        ))
        if dry and changed:
            self.stdout.write(
                "Re-run without --dry-run (in normal write mode) to apply."
            )
