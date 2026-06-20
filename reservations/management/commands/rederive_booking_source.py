"""
Re-derive Reservation.booking_source for EVERY existing row from the current
attribution logic, so older bookings pick up channels that were added after
they were created (e.g. Bing, ChatGPT, Gemini, Perplexity — which previously
fell through to "direct").

WHY THIS EXISTS (vs recompute_booking_source): recompute only repairs the
travel-agent direction. When the channel TAXONOMY changes (new AI/search
channels), every old row needs re-classifying from its stored
gclid/fbclid/utm_source/referrer_host. This command does exactly that.

SAFE BY DESIGN:
  - Rows tagged 'phone' are LEFT ALONE — a dispatcher/phone booking carries no
    UTM, so re-deriving it (request=None) would wrongly collapse it to 'direct'.
    Phone is only ever set with staff request context, which we don't have here.
  - travel-agent rows re-derive to 'travel_agent' (the FK wins in derive), so
    agent attribution is preserved/​repaired in the same pass.
  - Pure read of stored fields; idempotent; dry-run unless --apply.

    python manage.py rederive_booking_source            # dry-run (preview)
    python manage.py rederive_booking_source --apply    # write changes
"""
from django.core.management.base import BaseCommand
from django.db.models import Count

from reservations.models import Reservation
from reservations.attribution import rederive_all_booking_sources


class Command(BaseCommand):
    help = "Re-derive booking_source for all non-phone reservations (dry-run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Write the changes. Without this flag the command only previews.",
        )
        parser.add_argument(
            "--batch",
            type=int,
            default=1000,
            help="Bulk-update batch size (default 1000).",
        )

    def _distribution(self, label):
        self.stdout.write(f"\n  booking_source distribution ({label}):")
        for r in (
            Reservation.objects.values("booking_source")
            .annotate(n=Count("id"))
            .order_by("-n")
        ):
            self.stdout.write(f"    {r['booking_source'] or '(blank)':18} {r['n']:>6}")

    def handle(self, *args, **opts):
        apply = opts["apply"]

        self.stdout.write(self.style.MIGRATE_HEADING("Re-derive Reservation.booking_source"))
        self._distribution("before")

        # Same engine the in-app "Reclassify sources" button uses.
        result = rederive_all_booking_sources(apply=apply, batch_size=opts["batch"])

        self.stdout.write(
            f"\n  Rows needing reclassification: "
            f"{self.style.WARNING(str(result['changed']))}"
        )
        for old, new, n in result["transitions"]:
            self.stdout.write(f"       {old:18} -> {new:18} ({n})")

        if not apply:
            self.stdout.write(
                self.style.NOTICE(
                    f"\nDRY RUN — would update {result['changed']} reservation(s). "
                    "Re-run with --apply to write."
                )
            )
            return

        self.stdout.write(
            self.style.SUCCESS(f"\nApplied: {result['changed']} reservation(s) reclassified.")
        )
        self._distribution("after")
