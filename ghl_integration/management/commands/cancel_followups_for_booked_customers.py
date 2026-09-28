"""
Cancel pending follow-up messages aimed at people who have already booked.

The sequence stops when a lead is marked converted, and conversion marks exactly
one lead per reservation — so round-trip twins, leads created after the booking,
and bookings made under a spouse's or agent's email leave an armed sequence
pointing at a paying customer. 248 such messages reached 145 people before
process_follow_up_batch learned to check (see reservations.lead_matching.
already_booked_reservation); this clears the ones still armed.

The send loop now makes the same check at the moment of sending, so these rows are
no longer dangerous — this command exists so the queue reflects reality rather than
relying on every row being caught on its way out, and so the cancellations carry a
reason someone can read later.

Usage:
    python manage.py cancel_followups_for_booked_customers --dry-run   # report only
    python manage.py cancel_followups_for_booked_customers             # cancel them

--dry-run performs only reads.
"""
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from ghl_integration.models import FollowUpTask
from reservations.lead_matching import already_booked_reservation

CANCEL_REASON = "already_booked"


class Command(BaseCommand):
    help = "Cancel pending follow-up tasks for leads whose person has already booked."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be cancelled without writing.",
        )

    def handle(self, *args, **opts):
        dry_run = opts["dry_run"]
        now = timezone.now()

        pending = (
            FollowUpTask.objects.filter(status=FollowUpTask.StatusChoices.PENDING)
            .select_related("lead")
            .order_by("scheduled_at")
        )

        doomed = []
        for task in pending:
            reservation = already_booked_reservation(task.lead)
            if reservation is not None:
                doomed.append((task, reservation))

        if not doomed:
            self.stdout.write(
                self.style.SUCCESS("No pending follow-ups are aimed at booked customers.")
            )
            return

        self.stdout.write(
            f"{len(doomed)} of {pending.count()} pending follow-ups are aimed at "
            f"someone who has already booked:\n"
        )
        for task, reservation in doomed:
            lead = task.lead
            name = f"{lead.first_name or ''} {lead.last_name or ''}".strip() or "(no name)"
            self.stdout.write(
                f"  task #{task.id:<7} step {task.step_number}  fires {task.scheduled_at:%Y-%m-%d %H:%M}  "
                f"lead #{lead.id} {name[:26]:<26} -> reservation #{reservation.id} "
                f"({reservation.status}, {lead.pickup_date})"
            )

        if dry_run:
            self.stdout.write(self.style.WARNING("\n--dry-run: nothing written."))
            return

        with transaction.atomic():
            for task, _reservation in doomed:
                task.status = FollowUpTask.StatusChoices.CANCELLED
                task.cancelled_at = now
                task.cancel_reason = CANCEL_REASON
                task.save(update_fields=["status", "cancelled_at", "cancel_reason"])

        self.stdout.write(self.style.SUCCESS(f"\nCancelled {len(doomed)} follow-up tasks."))
