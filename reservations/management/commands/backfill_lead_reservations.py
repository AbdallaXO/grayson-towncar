"""
Backfill converted_reservation for leads that were marked converted
but never got linked to their matching reservation.
"""
from django.core.management.base import BaseCommand
from reservations.models import Lead, Reservation


class Command(BaseCommand):
    help = "Link converted leads to their matching reservations for revenue attribution"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be linked without making changes",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        unlinked = Lead.objects.filter(
            converted=True,
            converted_reservation__isnull=True,
        )
        total = unlinked.count()
        self.stdout.write(f"Found {total} converted leads without a linked reservation.")

        linked = 0
        skipped = 0

        for lead in unlinked.iterator():
            matching = None

            # 1) Match by email
            if lead.email:
                matching = (
                    Reservation.objects.filter(customer__email__iexact=lead.email)
                    .order_by("-pickup_date")
                    .first()
                )

            # 2) Match by phone — exact
            if not matching and lead.phone:
                matching = (
                    Reservation.objects.filter(customer__phone_number__iexact=lead.phone)
                    .order_by("-pickup_date")
                    .first()
                )

            # 3) Match by phone — digit normalization (last 10 digits)
            if not matching and lead.phone:
                lead_digits = "".join(filter(str.isdigit, lead.phone))
                if len(lead_digits) >= 10:
                    lead_last10 = lead_digits[-10:]
                    last4 = lead_last10[-4:]
                    candidates = (
                        Reservation.objects.filter(customer__phone_number__contains=last4)
                        .select_related("customer")
                        .order_by("-pickup_date")
                    )
                    for res in candidates:
                        cand_digits = "".join(filter(str.isdigit, res.customer.phone_number))
                        if len(cand_digits) >= 10 and cand_digits[-10:] == lead_last10:
                            matching = res
                            break

            if matching:
                if dry_run:
                    self.stdout.write(
                        f"  [DRY RUN] Lead #{lead.id} ({lead}) -> Reservation #{matching.id} (${matching.total_price})"
                    )
                else:
                    lead.converted_reservation = matching
                    lead.save(update_fields=["converted_reservation"])
                linked += 1
            else:
                skipped += 1

        prefix = "[DRY RUN] " if dry_run else ""
        self.stdout.write(
            self.style.SUCCESS(
                f"{prefix}Done. Linked: {linked}, No match found: {skipped}, Total: {total}"
            )
        )
