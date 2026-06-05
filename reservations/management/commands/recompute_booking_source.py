"""
Repair drift in Reservation.booking_source so every dashboard that GROUPs BY it
(Revenue KPIs, Accrual, Lead Analytics, Reservation Sources) counts travel-agent
bookings accurately — never leaking them into Google/Meta/Direct, never
double-counting.

WHY THIS DRIFTS: booking_source is derived once (at booking creation) by
reservations.attribution.derive_booking_source and is NOT recomputed when a
travel_agent is linked/unlinked later. So a booking whose agent was attached
after the fact can stay tagged 'direct'/'google_ads'/etc.

This command fixes ONLY the travel-agent direction (safe + definitive):
  A) travel_agent linked  but booking_source != 'travel_agent'  -> 'travel_agent'
  B) booking_source == 'travel_agent' but NO agent linked        -> re-derived
     (derive_booking_source with no request -> the real ad/direct source)

It deliberately leaves every other row alone (e.g. 'phone' bookings, which can't
be re-derived without the original request). Dry-run by default; pass --apply to
write. Idempotent — a second run reports 0 changes.

    python manage.py recompute_booking_source            # dry-run (preview)
    python manage.py recompute_booking_source --apply     # write changes
"""
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Count

from reservations.models import Reservation
from reservations.attribution import derive_booking_source


class Command(BaseCommand):
    help = "Repair travel-agent drift in Reservation.booking_source (dry-run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Write the changes. Without this flag the command only previews.",
        )

    def _distribution(self, label):
        self.stdout.write(f"\n  booking_source distribution ({label}):")
        for r in (
            Reservation.objects.values("booking_source")
            .annotate(n=Count("id"))
            .order_by("-n")
        ):
            self.stdout.write(f"    {r['booking_source'] or '(blank)':16} {r['n']:>6}")

    def handle(self, *args, **opts):
        apply = opts["apply"]

        forward = Reservation.objects.filter(travel_agent__isnull=False).exclude(
            booking_source="travel_agent"
        )
        reverse = Reservation.objects.filter(
            booking_source="travel_agent", travel_agent__isnull=True
        )

        forward_count = forward.count()
        reverse_count = reverse.count()

        self.stdout.write(self.style.MIGRATE_HEADING("Reservation.booking_source repair"))
        self._distribution("before")

        # ---- A) agent linked but mislabeled -> travel_agent ----
        self.stdout.write(
            f"\n  A) agent linked but booking_source != 'travel_agent': "
            f"{self.style.WARNING(str(forward_count))}"
        )
        for r in forward.values("booking_source").annotate(n=Count("id")).order_by("-n"):
            self.stdout.write(f"       {r['booking_source']:14} -> travel_agent   ({r['n']})")

        # ---- B) labeled travel_agent but no agent -> re-derive ----
        self.stdout.write(
            f"\n  B) booking_source == 'travel_agent' but no agent FK: "
            f"{self.style.WARNING(str(reverse_count))}"
        )
        reverse_updates = []  # (pk, new_source)
        for r in reverse.only(
            "id", "booking_source", "gclid", "fbclid",
            "utm_source", "utm_medium", "travel_agent",
        ):
            new_source = derive_booking_source(r, request=None)
            reverse_updates.append((r.id, new_source))
            self.stdout.write(f"       #{r.id} travel_agent -> {new_source}")

        if not apply:
            self.stdout.write(
                self.style.NOTICE(
                    f"\nDRY RUN — would update {forward_count + reverse_count} reservation(s). "
                    "Re-run with --apply to write."
                )
            )
            return

        with transaction.atomic():
            updated_forward = forward.update(booking_source="travel_agent")
            updated_reverse = 0
            for pk, new_source in reverse_updates:
                Reservation.objects.filter(pk=pk).update(booking_source=new_source)
                updated_reverse += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"\nApplied: {updated_forward} agent bookings -> travel_agent, "
                f"{updated_reverse} orphans re-derived."
            )
        )
        self._distribution("after")
