"""
Manual / cron entry point for the unpaid-reservation reminder engine.

Examples:

    # See what would happen without sending anything or writing flags
    python manage.py send_unpaid_reminders --dry-run

    # Process a single reservation by uuid (useful for QA on staging)
    python manage.py send_unpaid_reminders --reservation <uuid>

    # Default — same code path the scheduler runs every 30 minutes
    python manage.py send_unpaid_reminders
"""

from django.core.management.base import BaseCommand, CommandError

from ops.unpaid_reminders import UnpaidReminderEngine
from reservations.models import Reservation


class Command(BaseCommand):
    help = "Run the automated unpaid-reservation payment reminder pipeline."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Classify candidates and print the action table without sending emails or writing flags.",
        )
        parser.add_argument(
            "--reservation",
            type=str,
            default=None,
            help="Process only the reservation with this UUID.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        target_uuid = options["reservation"]

        engine = UnpaidReminderEngine(dry_run=dry_run)

        if target_uuid:
            try:
                reservation = Reservation.objects.get(uuid=target_uuid)
            except Reservation.DoesNotExist:
                raise CommandError(f"No reservation found with uuid={target_uuid}")
            engine.process_one(reservation)
        else:
            engine.process()

        self._print_report(engine, dry_run=dry_run)

    def _print_report(self, engine, dry_run: bool):
        result = engine.result
        prefix = "DRY-RUN " if dry_run else ""

        if result.actions:
            self.stdout.write(
                self.style.NOTICE(
                    f"\n{prefix}Per-reservation actions ({len(result.actions)} candidates):"
                )
            )
            self.stdout.write(
                f"  {'res_id':>7}  {'uuid':36}  {'customer':30}  action"
            )
            self.stdout.write(f"  {'-' * 7}  {'-' * 36}  {'-' * 30}  {'-' * 30}")
            for entry in result.actions:
                self.stdout.write(
                    f"  {entry['reservation_id']:>7}  "
                    f"{entry['uuid']:36}  "
                    f"{entry['customer'][:30]:30}  "
                    f"{entry['action']}"
                )

        self.stdout.write(self.style.NOTICE(f"\n{prefix}Summary:"))
        self.stdout.write(f"  Total sent:           {result.total_sent()}")
        for stage, count in result.sent.items():
            if count:
                self.stdout.write(f"    {stage}: {count}")
        self.stdout.write(f"  Flagged for cancel:   {result.flagged_for_cancel}")
        self.stdout.write(f"  Duplicate-blocked:    {result.dup_blocked}")
        if result.skipped:
            self.stdout.write(f"  Skipped:")
            for reason, count in result.skipped.items():
                self.stdout.write(f"    {reason}: {count}")
        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    "\nDry-run mode: no emails sent, no flags written, no tasks created."
                )
            )
