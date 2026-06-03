"""
One-off cleanup for EXISTING stale duplicate leads.

When a customer submitted both a round-trip and a one-way quote, two Lead rows
were created (different trip_type, so the create-time dedup didn't merge them).
Booking then converted only ONE of them (``auto_convert_lead_on_reservation``
uses ``.first()``), leaving the twin stuck at "interested" — still shown on the
leads board and (until the safety net shipped) still eligible for the 3-day
pre-pickup nudge.

This is NOT what the "Check for Auto-Conversion" admin action does: that matches
each lead to its OWN reservation, so it can never convert the stale twin (the
twin has no reservation — the customer booked under the sibling). This command
converges a twin because its SIBLING booked.

Scale-safe by design (the admin action times out the web worker at ~3k leads):
runs offline, preloads booked (phone/email, date) keys into a set, so the whole
sweep is 2 bulk reads + a write per converged twin — no per-lead query, no
request timeout. Idempotent: a converged twin is no longer "active", so
re-running is a no-op. Defaults to upcoming trips (today+); pass
``--include-past`` for all. Run ``--dry-run`` first.
"""
from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from reservations.models import Lead

ACTIVE_STATUSES = ["new", "contacted", "interested", "future_contact"]


class Command(BaseCommand):
    help = (
        "Converge stale duplicate leads: mark an active lead 'converted' when a "
        "same-person (phone/email), same-pickup_date sibling already booked."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Show what would change without writing.",
        )
        parser.add_argument(
            "--include-past", action="store_true",
            help="Also converge twins whose pickup_date is in the past "
                 "(default: only today and future).",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        today = timezone.localdate()

        # Preload every booked lead's (phone|email, pickup_date) → (sibling id,
        # reservation id). One pass, in memory: keeps the sweep O(2 queries).
        booked = {}
        booked_rows = (
            Lead.objects.filter(Q(converted=True) | Q(status=Lead.StatusChoices.CONVERTED))
            .exclude(pickup_date__isnull=True)
            .values("id", "normalized_phone", "email", "pickup_date", "converted_reservation_id")
        )
        for row in booked_rows.iterator():
            ref = (row["id"], row["converted_reservation_id"])
            if row["normalized_phone"]:
                booked.setdefault(("p", row["normalized_phone"], row["pickup_date"]), ref)
            if row["email"]:
                booked.setdefault(("e", row["email"].lower(), row["pickup_date"]), ref)

        active = (
            Lead.objects.filter(status__in=ACTIVE_STATUSES)
            .exclude(converted=True)
            .exclude(pickup_date__isnull=True)
        )
        if not options["include_past"]:
            active = active.filter(pickup_date__gte=today)

        checked = 0
        converged = 0

        for lead in active.iterator():
            checked += 1
            match = None
            if lead.normalized_phone:
                match = booked.get(("p", lead.normalized_phone, lead.pickup_date))
            if not match and lead.email:
                match = booked.get(("e", lead.email.lower(), lead.pickup_date))
            if not match:
                continue

            sibling_id, reservation_id = match
            if dry_run:
                self.stdout.write(
                    f"  [DRY RUN] Lead #{lead.id} ({lead}) {lead.pickup_date} "
                    f"-> converge (booked twin #{sibling_id})"
                )
            else:
                lead.status = Lead.StatusChoices.CONVERTED
                lead.converted = True
                lead.converted_at = timezone.now()
                if reservation_id and not lead.converted_reservation_id:
                    lead.converted_reservation_id = reservation_id
                note = (
                    f"Backfill-converged on {timezone.now():%Y-%m-%d %H:%M} "
                    f"- same-trip twin of booked lead #{sibling_id}"
                )
                lead.notes = f"{lead.notes}\n\n{note}" if lead.notes else note
                lead.save(update_fields=[
                    "status", "converted", "converted_at",
                    "converted_reservation", "notes",
                ])
            converged += 1

        prefix = "[DRY RUN] " if dry_run else ""
        self.stdout.write(
            self.style.SUCCESS(
                f"{prefix}Done. Checked {checked} active lead(s); "
                f"converged {converged} duplicate twin(s)."
            )
        )
