"""Run one wake-up sweep by hand — for local dry-runs and prod debugging.

    manage.py wakeup_cycle                      # now
    manage.py wakeup_cycle --at 2026-06-13T04:00

Honors WAKEUP_CHECKS_ENABLED unless --force is given (so a local dry-run on
the scrubbed DB doesn't need the env flag — but remember the scrubbed DB has
REAL driver phone numbers; without Twilio creds in the env nothing sends).
"""
from datetime import datetime

from django.core.management.base import BaseCommand, CommandError
from django.test.utils import override_settings
from django.utils import timezone

from drivers.wakeup import run_wakeup_cycle


class Command(BaseCommand):
    help = "Run one early-morning wake-up check sweep."

    def add_arguments(self, parser):
        parser.add_argument(
            "--at", help="Pretend it's this local time (ISO, e.g. 2026-06-13T04:00)."
        )
        parser.add_argument(
            "--force", action="store_true",
            help="Run even when WAKEUP_CHECKS_ENABLED is off.",
        )

    def handle(self, *args, **options):
        now = None
        if options["at"]:
            try:
                now = timezone.make_aware(datetime.fromisoformat(options["at"]))
            except ValueError:
                raise CommandError(f"Bad --at value: {options['at']!r}")
        if options["force"]:
            with override_settings(WAKEUP_CHECKS_ENABLED=True):
                summary = run_wakeup_cycle(now=now)
        else:
            summary = run_wakeup_cycle(now=now)
        self.stdout.write(str(summary))
