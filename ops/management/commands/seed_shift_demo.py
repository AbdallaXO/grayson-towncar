"""
Create a demo dispatcher so the shift checklists can be walked as a dispatcher.

WHY THIS EXISTS. A superuser's navbar points "Clock" at the MANAGE page, not the
personal clock (dispatcher_navbar.html), so a founder account has no Clock In
button and can never see the opener hand-off that follows a punch. This makes a
throwaway non-superuser account that does.

    python manage.py seed_shift_demo                  # create / refresh
    python manage.py seed_shift_demo --opener         # ...and mark them today's opener
    python manage.py seed_shift_demo --remove         # take it away again

LOCAL ONLY. Refuses to run on Railway. The account is a plain staff login with
no powers beyond any other dispatcher — it can see the board and the checklists,
and nothing financial.
"""

import os
from datetime import time

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from ops.models import StaffWeeklySchedule

USERNAME = "demo.dispatcher"
PASSWORD = "demo-shift-2026"
FIRST, LAST = "Demo", "Dispatcher"


class Command(BaseCommand):
    help = "Create a demo dispatcher account for walking through the shift checklists."

    def add_arguments(self, parser):
        parser.add_argument(
            "--opener", action="store_true",
            help="Also give them today's opener mark, so clocking in hands them the list.",
        )
        parser.add_argument(
            "--remove", action="store_true",
            help="Delete the demo account and its schedule rows.",
        )

    def handle(self, *args, **options):
        if os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("PGHOST"):
            raise CommandError(
                "Not on a deployed environment. This makes a shared login with a "
                "known password — local development only."
            )

        today = timezone.localdate()
        opener_before = self._opener_name(today)

        if options["remove"]:
            deleted, _ = User.objects.filter(username=USERNAME).delete()
            if deleted:
                self.stdout.write(self.style.SUCCESS(f"Removed {USERNAME}."))
                after = self._opener_name(today)
                if after:
                    self.stdout.write(f"Today's opener is {after} again.")
            else:
                self.stdout.write(f"No {USERNAME} to remove.")
            return

        user, created = User.objects.get_or_create(
            username=USERNAME,
            defaults={"first_name": FIRST, "last_name": LAST, "is_staff": True},
        )
        user.first_name, user.last_name = FIRST, LAST
        user.is_staff = True          # sees the dispatcher pages
        user.is_superuser = False     # ...and gets the PERSONAL clock page
        user.is_active = True
        user.set_password(PASSWORD)
        user.save()

        # A window that spans the day, so a punch at any hour counts as in-schedule
        # rather than becoming an approval request (ops/services.py::clock_in_or_request).
        row, _ = StaffWeeklySchedule.objects.update_or_create(
            user=user, day_of_week=today.weekday(),
            defaults={
                "is_working": True,
                "start_time": time(0, 30),
                "end_time": time(23, 59),
                "location": "office",
                "role": "opener" if options["opener"] else "",
                "note": "Demo account — safe to delete.",
            },
        )

        verb = "Created" if created else "Refreshed"
        self.stdout.write(self.style.SUCCESS(f"{verb} {USERNAME}."))
        self.stdout.write("")
        self.stdout.write(f"  Username:  {USERNAME}")
        self.stdout.write(f"  Password:  {PASSWORD}")
        self.stdout.write(f"  Scheduled: {today:%A} 12:30 AM – 11:59 PM, in office")
        if options["opener"]:
            self.stdout.write(self.style.SUCCESS(
                "  Marked as today's opener — clocking in will hand them the opening list."
            ))
            # Only one person opens a day. Taking the mark takes it from somebody,
            # and finding that out by surprise on the staffing board would be worse
            # than being told here.
            if opener_before and opener_before != USERNAME:
                self.stdout.write(self.style.WARNING(
                    f"\n  Heads up: {opener_before} was today's opener until now.\n"
                    f"  Only one person opens a day, so the demo has taken it for "
                    f"{today:%A}.\n"
                    f"  Run --remove when you're done and it goes back to them."
                ))
        else:
            self.stdout.write(
                "  Not marked as opener. Re-run with --opener to test the hand-off."
            )
            if opener_before:
                self.stdout.write(f"  Today's opener is still {opener_before}.")
        self.stdout.write("")
        self.stdout.write("Log in from a private window so you stay signed in as yourself.")
        self.stdout.write("Remove it later with:  manage.py seed_shift_demo --remove")

    @staticmethod
    def _opener_name(target_date):
        """Who the staffing board currently calls the opener, or None."""
        from ops import shift_services
        try:
            opener = shift_services.scheduled_opener(target_date)
        except Exception:
            return None
        return opener.username if opener else None
