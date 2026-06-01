"""
Manual / cron entry point for the pre-pickup nudge engine.

Examples:

    # See what would happen without sending anything or writing rows
    python manage.py send_pre_pickup_nudges --dry-run

    # Process a single lead by id (useful for QA on staging)
    python manage.py send_pre_pickup_nudges --lead 1234

    # Default — same code path the scheduler runs hourly
    python manage.py send_pre_pickup_nudges
"""

from django.core.management.base import BaseCommand, CommandError

from ghl_integration.pre_pickup import PrePickupNudgeEngine
from reservations.models import Lead


class Command(BaseCommand):
    help = "Run the date-anchored pre-pickup nudge pipeline (one SMS ~3 days before pickup)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Classify candidates and print the action table without sending or writing anything.",
        )
        parser.add_argument(
            "--lead",
            type=int,
            default=None,
            help="Process only the lead with this id.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        lead_id = options["lead"]

        engine = PrePickupNudgeEngine(dry_run=dry_run)

        if lead_id is not None:
            try:
                lead = Lead.objects.select_related("vehicle").get(id=lead_id)
            except Lead.DoesNotExist:
                raise CommandError(f"No lead found with id={lead_id}")
            engine.process_one(lead)
        else:
            engine.process()

        self._print_report(engine, dry_run=dry_run)

    def _print_report(self, engine, dry_run: bool):
        result = engine.result
        prefix = "DRY-RUN " if dry_run else ""

        if result.actions:
            self.stdout.write(
                self.style.NOTICE(
                    f"\n{prefix}Per-lead actions ({len(result.actions)} candidates):"
                )
            )
            self.stdout.write(f"  {'lead_id':>7}  {'pickup':10}  {'name':28}  action")
            self.stdout.write(f"  {'-' * 7}  {'-' * 10}  {'-' * 28}  {'-' * 28}")
            for entry in result.actions:
                self.stdout.write(
                    f"  {entry['lead_id']:>7}  "
                    f"{entry['pickup_date']:10}  "
                    f"{entry['name'][:28]:28}  "
                    f"{entry['action']}"
                )

        self.stdout.write(self.style.NOTICE(f"\n{prefix}Summary:"))
        self.stdout.write(f"  Sent:              {result.sent}")
        self.stdout.write(f"  Routed to human:   {result.routed_to_human}")
        self.stdout.write(f"  Email fallback:    {result.email_fallback}")
        self.stdout.write(f"  Failed:            {result.failed}")
        if result.skipped:
            self.stdout.write(f"  Skipped:")
            for reason, count in result.skipped.items():
                self.stdout.write(f"    {reason}: {count}")
        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    "\nDry-run mode: no SMS sent, no rows written, no tasks created."
                )
            )
