"""Seed the disposable SOP capture database with a fictional FLEET.

Run against business.settings_sop ONLY, and after ``seed_sop_fixtures`` — this
builds on the dispatcher, drivers and guests that one creates.

    python manage.py migrate --settings=business.settings_sop
    python manage.py seed_sop_fixtures --settings=business.settings_sop
    python manage.py seed_fleet_sop_fixtures --settings=business.settings_sop

WHY THIS EXISTS SEPARATELY FROM THE REAL DATABASE
The fleet screens are the ones most soaked in personal data: The day prints a
guest's name, their pickup address and their flight number on every block, and
the Desk prints a chauffeur's name against every fault. Capturing those from
content/db.sqlite3 would put live PII into a training document. So the fleet
gets fictional people here, the same way reservations already do.

WHAT EXISTS AFTERWARDS
  * Marcus Hale — the fleet manager the screenshots are taken as. is_staff,
    is_fleet_manager, NOT a superuser, working 7:30-4 like the real one, so the
    capture documents the bar and the suggestions a fleet manager actually gets.
  * Ten units, #001-#010, a believable mix of SUVs, Sprinters, a Metris and a
    towncar, with plates, VINs, permits and odometers.
  * A day's work on most of them, built so the strip has something to show:
    morning runs, midday holes big enough to walk a car in, and a couple of
    units left standing still.
  * #004 throwing a DEF fault cluster, #008 a misfire, #005 in the shop, #002
    with an expiring MCO permit, so the Desk has a queue worth photographing.
  * Oil and tire intervals on every unit, one of them overdue, so the
    maintenance table on a car's page is not empty.

Every fictional person uses an @example.com address and a 555-01xx phone.
Plates and VINs are invented and deliberately not valid check-digit VINs.
"""

from datetime import date, time, timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from drivers.models import (
    Driver, DriverVehicleAssignment, FleetVehicle, VehicleDowntime, VehicleFault,
    VehicleIssue, VehicleServiceSchedule,
)
from rates.models import Rate, Vehicle
from reservations.models import Customer, Leg, Reservation
from users.models import UserProfile

FLEET_MANAGER = {
    "username": "mhale",
    "first_name": "Marcus",
    "last_name": "Hale",
    "email": "marcus.hale@example.com",
    "phone": "555-0119",
}

# Chauffeurs beyond the two the reservation seeder makes, so a ten-car board
# does not show the same two names down every row.
CHAUFFEURS = [
    ("rmendez", "Rafael", "Mendez", "555-0121"),
    ("dokafor", "Daniel", "Okafor", "555-0134"),
    ("jporter", "Joel", "Porter", "555-0147"),
    ("lhaddad", "Leila", "Haddad", "555-0152"),
    ("sbranch", "Simone", "Branch", "555-0163"),
    ("tvargas", "Tomas", "Vargas", "555-0177"),
]

# Guests for the day's trips. Invented, @example.com, 555-01xx.
DAY_GUESTS = [
    ("Eleanor", "Whitfield", "555-0191"),
    ("Priya", "Raghunathan", "555-0192"),
    ("Daniel", "Okonkwo", "555-0193"),
    ("Hannah", "Lindqvist", "555-0194"),
    ("Marco", "Bellini", "555-0195"),
    ("Grace", "Adeyemi", "555-0196"),
    ("Tobias", "Köhler", "555-0197"),
    ("Rosa", "Delgado", "555-0198"),
]

# (number, type key, year, make, model, plate, odometer)
UNITS = [
    ("001", "suv",      2023, "Chevrolet", "Suburban",   "GTC 1001", 84_200),
    ("002", "mini_van", 2022, "Mercedes",  "Metris",     "GTC 1002", 112_640),
    ("003", "suv",      2023, "Chevrolet", "Suburban",   "GTC 1003", 76_980),
    ("004", "van",      2021, "Mercedes",  "Sprinter",   "GTC 1004", 148_310),
    ("005", "van",      2020, "Mercedes",  "Sprinter",   "GTC 1005", 171_455),
    ("006", "suv",      2024, "Cadillac",  "Escalade",   "GTC 1006", 41_120),
    ("007", "suv",      2023, "Chevrolet", "Suburban",   "GTC 1007", 69_540),
    ("008", "van",      2022, "Mercedes",  "Sprinter",   "GTC 1008", 131_275),
    ("009", "suv",      2022, "Chevrolet", "Suburban",   "GTC 1009", 98_310),
    ("010", "towncar",  2021, "Lincoln",   "Continental", "GTC 1010", 122_060),
]

# Each unit's day: (chauffeur index, [(hour, minute, trip_type), ...]).
# Shaped on purpose — see the module docstring. Units left out stand still.
DAY_PLAN = {
    "001": (0, [(6, 30, "arrival"), (8, 45, "return"), (13, 30, "arrival"), (16, 10, "return")]),
    "002": (1, [(7, 0, "return"), (9, 30, "arrival"), (14, 15, "return")]),
    "003": (2, [(6, 45, "arrival"), (12, 40, "return"), (15, 20, "arrival")]),
    "004": (3, [(7, 15, "return"), (10, 50, "arrival"), (14, 40, "return")]),
    "006": (4, [(8, 10, "arrival"), (11, 20, "return"), (15, 0, "arrival")]),
    "007": (5, [(6, 20, "return"), (9, 15, "arrival"), (13, 5, "return"), (17, 30, "arrival")]),
    "008": (6, [(7, 40, "arrival"), (12, 10, "return")]),
    "010": (7, [(8, 30, "return"), (11, 45, "arrival"), (15, 40, "return")]),
}

# A leg has no trip_type column — Leg.get_trip_type() reads it off the pickup
# and dropoff, so the route is what makes a block an arrival on the strip.
ARRIVALS = [
    ("Orlando International Airport (MCO), Terminal B", "Disney's Grand Floridian Resort"),
    ("Orlando International Airport (MCO), Terminal A", "Disney's Yacht Club Resort"),
    ("Orlando International Airport (MCO), Terminal B", "Disney's Riviera Resort"),
    ("Orlando International Airport (MCO), Terminal C", "Universal's Portofino Bay Hotel"),
]
RETURNS = [
    ("Disney's Polynesian Village Resort", "Orlando International Airport (MCO)"),
    ("Universal's Portofino Bay Hotel", "Orlando International Airport (MCO)"),
    ("Disney's Coronado Springs Resort", "Orlando International Airport (MCO)"),
    ("Disney's Contemporary Resort", "Orlando International Airport (MCO)"),
]


class Command(BaseCommand):
    help = "Seed a fictional fleet into the SOP capture database."

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true",
                            help="Delete the seeded fleet first and rebuild it.")

    def handle(self, *args, **options):
        name = str(settings.DATABASES["default"]["NAME"])
        if "db_sop_capture" not in name:
            raise CommandError(
                "Refusing to run: this writes fixtures, and the database is not the "
                f"disposable capture one (got {name}). Add --settings=business.settings_sop."
            )
        if not User.objects.filter(username="jdoe").exists():
            raise CommandError(
                "Run seed_sop_fixtures first — this builds on its drivers and rates."
            )

        with transaction.atomic():
            if options["force"]:
                self._clear()
            self._seed()
        self.stdout.write(self.style.SUCCESS(
            f"Seeded {FleetVehicle.objects.count()} units and a day's work. "
            f"Capture as {FLEET_MANAGER['username']}."
        ))

    # ── teardown ────────────────────────────────────────────────────────────
    def _clear(self):
        Leg.objects.filter(reservation__customer__phone_number__startswith="555-019").delete()
        Reservation.objects.filter(customer__phone_number__startswith="555-019").delete()
        Customer.objects.filter(phone_number__startswith="555-019").delete()
        DriverVehicleAssignment.objects.all().delete()
        VehicleFault.objects.all().delete()
        VehicleIssue.objects.all().delete()
        VehicleDowntime.objects.all().delete()
        VehicleServiceSchedule.objects.all().delete()
        FleetVehicle.objects.all().delete()

    # ── the fleet ───────────────────────────────────────────────────────────
    def _seed(self):
        today = timezone.localdate()
        types = {v.vehicle_type: v for v in Vehicle.objects.all()}

        def vtype(key):
            # rates_data.json spells the big van "Van(14 Pax)"; fall back to any
            # vehicle rather than failing the whole seed on a naming difference.
            for candidate in (key, "Van(14 Pax)", "suv"):
                if candidate in types:
                    return types[candidate]
            return next(iter(types.values()))

        manager = self._fleet_manager()
        chauffeurs = self._chauffeurs()

        units = {}
        for number, key, year, make, model, plate, odo in UNITS:
            unit, _ = FleetVehicle.objects.update_or_create(
                vehicle_number=number,
                defaults={
                    "vehicle_type": vtype(key),
                    "year": year, "make": make, "model": model,
                    "license_plate": plate,
                    "vin": f"1GT{number}SOPFIXTURE{number}",
                    "is_active": True,
                    "in_service_since": today - timedelta(days=700),
                    "samsara_odometer_meters": int(odo * 1609.34),
                    "samsara_odometer_at": timezone.now() - timedelta(hours=3),
                    "samsara_fuel_percent": 40 + (int(number) * 5) % 55,
                    "samsara_last_seen_at": timezone.now() - timedelta(minutes=20),
                    "samsara_last_synced_at": timezone.now() - timedelta(minutes=20),
                    "permit_mco": True,
                    "permit_mco_expires_on": today + timedelta(days=13 if number == "002" else 240),
                    "permit_sanford": number in {"001", "003", "006", "007"},
                    "permit_port_canaveral": number in {"004", "005", "008"},
                    "registration_expires_on": today + timedelta(days=300),
                    "insurance_expires_on": today + timedelta(days=180),
                    "transponder_number": f"SP-{4400 + int(number)}",
                    "max_passenger_capacity": 14 if key == "van" else 6,
                },
            )
            units[number] = unit

        self._schedules(units, today)
        self._problems(units, today)
        self._the_day(units, chauffeurs, today)
        return manager

    def _fleet_manager(self):
        user, _ = User.objects.update_or_create(
            username=FLEET_MANAGER["username"],
            defaults={
                "first_name": FLEET_MANAGER["first_name"],
                "last_name": FLEET_MANAGER["last_name"],
                "email": FLEET_MANAGER["email"],
                "is_staff": True, "is_superuser": False, "is_active": True,
            },
        )
        user.set_unusable_password()
        user.save(update_fields=["password"])
        UserProfile.objects.update_or_create(
            user=user,
            defaults={
                "phone_number": FLEET_MANAGER["phone"],
                "is_fleet_manager": True,
                # The hours the real fleet manager works. The inspection round
                # and the strip's gold windows are both clipped to these, so a
                # capture taken with anything else documents the wrong screen.
                "shift_start": time(7, 30),
                "shift_end": time(16, 0),
            },
        )
        return user

    def _chauffeurs(self):
        out = list(Driver.objects.filter(driver_type="inhouse", is_active=True))
        for username, first, last, phone in CHAUFFEURS:
            user, _ = User.objects.update_or_create(
                username=username,
                defaults={"first_name": first, "last_name": last,
                          "email": f"{first.lower()}.{last.lower()}@example.com"},
            )
            UserProfile.objects.update_or_create(
                user=user, defaults={"phone_number": phone})
            driver, _ = Driver.objects.update_or_create(
                profile=user, defaults={"driver_type": "inhouse", "is_active": True})
            if driver not in out:
                out.append(driver)
        return out

    def _schedules(self, units, today):
        for number, unit in units.items():
            n = int(number)
            VehicleServiceSchedule.objects.update_or_create(
                vehicle=unit, service_type="oil",
                defaults={
                    "interval_miles": 7_000, "interval_days": 180,
                    # #009's baseline is old enough that it reads overdue, so the
                    # status column has something other than "fine" to show.
                    "last_done_on": today - timedelta(days=200 if number == "009" else 40 + n * 3),
                    "last_done_odometer_miles": Decimal(
                        str(unit.odometer_miles - (6_800 if number == "009" else 1_500 + n * 120))
                    ) if unit.odometer_miles else None,
                    "is_active": True,
                },
            )
            VehicleServiceSchedule.objects.update_or_create(
                vehicle=unit, service_type="tires",
                defaults={"interval_miles": 40_000, "is_active": True,
                          "last_done_on": today - timedelta(days=150 + n * 5)},
            )

    def _problems(self, units, today):
        now = timezone.now()
        # A DEF cluster on #004 — four codes, one problem. The Desk groups them.
        for code in ("P202E", "P208E", "P20EA", "P20F4"):
            VehicleFault.objects.update_or_create(
                vehicle=units["004"], external_id=f"sop-{code}",
                defaults={
                    "code": code, "severity": "Check",
                    "description": "Reductant Injection Valve Circuit Range/Performance Bank 1 Unit 1",
                    "first_seen_at": now - timedelta(days=3),
                    "last_seen_at": now - timedelta(hours=2),
                    "occurrence_count": 14,
                },
            )
        for code in ("P0301", "P0420"):
            VehicleFault.objects.update_or_create(
                vehicle=units["008"], external_id=f"sop-{code}",
                defaults={
                    "code": code, "severity": "Check",
                    "description": "Cylinder 1 Misfire Detected",
                    "first_seen_at": now - timedelta(days=2),
                    "last_seen_at": now - timedelta(hours=5),
                    "occurrence_count": 6,
                },
            )
        VehicleIssue.objects.update_or_create(
            vehicle=units["007"], title="Driver side front TPMS sensor needs replacing",
            defaults={"severity": "soon",
                      "details": "Warning light on since Monday. Tyre itself holds pressure."},
        )
        VehicleDowntime.objects.update_or_create(
            vehicle=units["005"], starts_on=today - timedelta(days=2),
            defaults={"category": "repair", "reason": "Putting in new engine",
                      "expected_back_on": today + timedelta(days=1),
                      "vendor": "Expert Auto Care"},
        )

    def _the_day(self, units, chauffeurs, today):
        guests = []
        for first, last, phone in DAY_GUESTS:
            guest, _ = Customer.objects.update_or_create(
                phone_number=phone,
                defaults={"first_name": first, "last_name": last,
                          "email": f"{first.lower()}.{last.lower()}@example.com"},
            )
            guests.append(guest)

        rate = Rate.objects.first()
        if rate is None:
            raise CommandError("No rates in the capture database — run seed_sop_fixtures.")

        for number, (chauffeur_index, trips) in DAY_PLAN.items():
            unit = units[number]
            driver = chauffeurs[chauffeur_index % len(chauffeurs)]
            DriverVehicleAssignment.objects.update_or_create(
                driver=driver, date=today, defaults={"vehicle": unit})

            for i, (hour, minute, trip_type) in enumerate(trips):
                guest = guests[(int(number) + i) % len(guests)]
                routes = ARRIVALS if trip_type == "arrival" else RETURNS
                pickup, dropoff = routes[(int(number) + i) % len(routes)]
                reservation = Reservation.objects.create(
                    trip_type="one-way", customer=guest, rate=rate, status="confirmed")
                Leg.objects.create(
                    reservation=reservation, driver=driver,
                    pickup_date=today, pickup_time=time(hour, minute),
                    pickup_location=pickup, dropoff_location=dropoff,
                    status="confirmed",
                )
