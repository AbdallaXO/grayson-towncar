"""
Seed a demo day for testing the sandbox scheduling workflow (hold / review / publish).

Creates a handful of legs on a FUTURE date — a couple already assigned (so you can
test reassigning them in a draft and confirm drivers don't see it until publish) and
several unassigned (so there's work to build in the draft). Everything is tagged
'[SANDBOX DEMO]' so teardown is a single flag.

Usage:
    python manage.py seed_sandbox_demo                 # demo day = today + 3
    python manage.py seed_sandbox_demo --date 2026-06-10
    python manage.py seed_sandbox_demo --teardown      # remove demo day(s) + their drafts
"""

from datetime import datetime, time, timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from reservations.models import Customer, Reservation, Leg, ScheduleDraft
from rates.models import Vehicle, Rate
from drivers.models import Driver

TAG = "[SANDBOX DEMO]"

# (pickup_time, pickup, dropoff, assign_to_index_or_None)
DEMO_LEGS = [
    (time(6, 15),  "MCO — Orlando Airport",        "Disney's Grand Floridian", 0),
    (time(7, 30),  "Four Seasons Orlando",          "MCO — Orlando Airport",    1),
    (time(9, 0),   "MCO — Orlando Airport",        "Universal Hard Rock Hotel", None),
    (time(10, 30), "Ritz-Carlton Orlando",          "Port Canaveral",           None),
    (time(13, 0),  "Port Canaveral",                "MCO — Orlando Airport",    None),
]


class Command(BaseCommand):
    help = "Seed (or tear down) a demo day for testing the sandbox scheduling workflow."

    def add_arguments(self, parser):
        parser.add_argument("--date", type=str, default=None,
                            help="Demo date YYYY-MM-DD (default: today + 3 days)")
        parser.add_argument("--teardown", action="store_true",
                            help="Delete demo reservations/legs and drafts instead of creating them")

    def handle(self, *args, **options):
        if options["date"]:
            demo_date = datetime.strptime(options["date"], "%Y-%m-%d").date()
        else:
            demo_date = timezone.localdate() + timedelta(days=3)

        if options["teardown"]:
            return self._teardown(demo_date)

        self._seed(demo_date)

    # ── seed ──
    def _seed(self, demo_date):
        # Reuse an existing rate + vehicle (don't construct the rate graph).
        rate = Rate.objects.first()
        if not rate:
            self.stderr.write(self.style.ERROR("No Rate rows found. Run your rate loader first."))
            return
        vehicle = getattr(rate, "vehicle", None) or Vehicle.objects.first()

        # Two inhouse drivers for the pre-assigned legs.
        drivers = list(
            Driver.objects.filter(driver_type="inhouse", is_active=True, profile__isnull=False)
            .select_related("profile")[:2]
        )
        if len(drivers) < 2:
            self.stderr.write(self.style.ERROR("Need at least 2 active inhouse drivers with logins."))
            return

        # Reuse/create a demo customer.
        customer, _ = Customer.objects.get_or_create(
            email="sandbox.demo@test.local",
            defaults=dict(first_name="Sandbox", last_name="Demo",
                          phone_number="555-0100", zipcode="32830"),
        )

        price = getattr(rate, "oneway_price", None) or 100
        created = 0
        for pickup_time, pickup, dropoff, didx in DEMO_LEGS:
            reservation = Reservation.objects.create(
                customer=customer, rate=rate, vehicle=vehicle,
                trip_type="one_way", passenger_count=2,
                base_price=price, total_price=price, status="confirmed",
                private_notes=f"{TAG} {demo_date.isoformat()}",
            )
            Leg.objects.create(
                reservation=reservation,
                pickup_date=demo_date, pickup_time=pickup_time,
                pickup_location=pickup, dropoff_location=dropoff,
                driver=(drivers[didx] if didx is not None else None),
                status="in-progress",
                private_notes=TAG,
            )
            created += 1

        d0 = self._dlabel(drivers[0])
        d1 = self._dlabel(drivers[1])
        self.stdout.write(self.style.SUCCESS(f"\nSeeded {created} demo legs for {demo_date.isoformat()}."))
        self.stdout.write(f"  Pre-assigned: {d0} (6:15 AM) and {d1} (7:30 AM); 3 legs unassigned.")
        self.stdout.write("\nOpen the board:")
        self.stdout.write(self.style.HTTP_INFO(f"  /dispatching/schedule-board/?date={demo_date.isoformat()}"))
        self.stdout.write("\nLeak test — log in as this driver and confirm they DON'T see a draft reassignment:")
        self.stdout.write(f"  driver login: {drivers[0].profile.username}   (their dashboard: /drivers/?date={demo_date.isoformat()})")
        self.stdout.write(self.style.WARNING("\nTear down when done:  python manage.py seed_sandbox_demo --date "
                                             f"{demo_date.isoformat()} --teardown"))

    # ── teardown ──
    def _teardown(self, demo_date):
        reservations = Reservation.objects.filter(private_notes__contains=TAG)
        if demo_date:
            reservations = reservations.filter(private_notes__contains=demo_date.isoformat())
        n = reservations.count()
        reservations.delete()  # cascades to legs

        drafts = ScheduleDraft.objects.filter(schedule_date=demo_date)
        dn = drafts.count()
        drafts.delete()  # cascades to DraftAssignment + events

        self.stdout.write(self.style.SUCCESS(
            f"Removed {n} demo reservation(s) and {dn} draft(s) for {demo_date.isoformat()}."
        ))

    @staticmethod
    def _dlabel(driver):
        try:
            return driver.profile.get_full_name() or driver.profile.username
        except Exception:
            return f"Driver {driver.id}"
