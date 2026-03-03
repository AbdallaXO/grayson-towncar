"""
Seed test in-house drivers, fleet vehicles, weekly schedules, and this-week vehicle
assignments so you can preview the In-House Schedule page with realistic data.

Usage:
    python manage.py seed_inhouse_test_data
    python manage.py seed_inhouse_test_data --clear   # wipe seeded data first
    python manage.py seed_inhouse_test_data --no-assign  # skip vehicle assignments

All created objects are tagged with a username prefix "test_" so they are easy
to identify and the --clear flag can remove them safely without touching real data.
"""

from datetime import date, timedelta

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.utils import timezone

from drivers.models import Driver, DriverWeeklySchedule, DriverVehicleAssignment, FleetVehicle


# ---------------------------------------------------------------------------
# Seed data
# ---------------------------------------------------------------------------

DRIVERS = [
    {
        "username": "test_junaid",
        "first_name": "Junaid",
        "last_name": "Baidr",
        "phone": "407-555-0101",
        "schedule": "Mon-Fri: 4AM-3PM",
        "default_start": 4,
        "default_end": 15,
        # day_of_week: (is_available, start_hour, end_hour, preference)
        "weekly": {
            0: (True,  4, 15, ""),
            1: (True,  4, 15, ""),
            2: (True,  4, 15, ""),
            3: (True,  4, 15, ""),
            4: (True,  4, 15, ""),
            5: (False, 6, 23, ""),
            6: (False, 6, 23, ""),
        },
    },
    {
        "username": "test_david",
        "first_name": "David",
        "last_name": "Encarancion",
        "phone": "848-203-7511",
        "schedule": "Mon-Sat: 5AM-4PM",
        "default_start": 5,
        "default_end": 16,
        "weekly": {
            0: (True,  5, 16, "prefer_arrival"),
            1: (True,  5, 16, "prefer_arrival"),
            2: (True,  5, 16, "prefer_arrival"),
            3: (True,  5, 16, "prefer_arrival"),
            4: (True,  5, 16, "prefer_arrival"),
            5: (True,  5, 14, ""),
            6: (False, 6, 23, ""),
        },
    },
    {
        "username": "test_alex",
        "first_name": "Alex",
        "last_name": "",
        "phone": "",
        "schedule": "Mon-Sun: 4AM-6PM",
        "default_start": 4,
        "default_end": 18,
        "weekly": {
            0: (True, 4, 18, ""),
            1: (True, 4, 18, ""),
            2: (True, 4, 18, ""),
            3: (True, 4, 18, ""),
            4: (True, 4, 18, ""),
            5: (True, 4, 18, ""),
            6: (True, 4, 18, ""),
        },
    },
    {
        "username": "test_michael_olmo",
        "first_name": "Michael",
        "last_name": "Olmo",
        "phone": "",
        "schedule": "Mon-Fri: 5AM-3PM",
        "default_start": 5,
        "default_end": 15,
        "weekly": {
            0: (True,  5, 15, ""),
            1: (True,  5, 15, ""),
            2: (True,  5, 15, ""),
            3: (True,  5, 15, ""),
            4: (True,  5, 15, ""),
            5: (False, 6, 23, ""),
            6: (False, 6, 23, ""),
        },
    },
    {
        "username": "test_yovanny",
        "first_name": "Yovanny",
        "last_name": "Suarez",
        "phone": "407-399-6951",
        "schedule": "Mon-Sun: 4AM-8PM",
        "default_start": 4,
        "default_end": 20,
        "weekly": {
            0: (True, 4, 20, ""),
            1: (True, 4, 20, ""),
            2: (True, 4, 20, ""),
            3: (True, 4, 20, ""),
            4: (True, 4, 20, ""),
            5: (True, 4, 20, ""),
            6: (True, 4, 20, ""),
        },
    },
    {
        "username": "test_angel",
        "first_name": "Angel",
        "last_name": "Almanzar",
        "phone": "407-325-9029",
        "schedule": "Sat: 4AM-2PM, Tue: 4AM-2PM, EOD: Open",
        "default_start": 4,
        "default_end": 23,
        "weekly": {
            0: (True,  4, 23, ""),
            1: (True,  4, 14, ""),   # Tuesday
            2: (True,  4, 23, ""),
            3: (True,  4, 23, ""),
            4: (True,  4, 23, ""),
            5: (True,  4, 14, ""),   # Saturday
            6: (True,  4, 23, ""),
        },
    },
    {
        "username": "test_hasan",
        "first_name": "Hasan",
        "last_name": "",
        "phone": "407-242-3391",
        "schedule": "Mon-Fri: 5AM-5PM",
        "default_start": 5,
        "default_end": 17,
        "weekly": {
            0: (True,  5, 17, ""),
            1: (True,  5, 17, ""),
            2: (True,  5, 17, ""),
            3: (True,  5, 17, ""),
            4: (True,  5, 17, ""),
            5: (False, 6, 23, ""),
            6: (False, 6, 23, ""),
        },
    },
    {
        "username": "test_shipo",
        "first_name": "Shipo",
        "last_name": "",
        "phone": "321-202-9865",
        "schedule": "Mon-Sun: 4AM-EOD",
        "default_start": 4,
        "default_end": 23,
        "weekly": {
            0: (True, 4, 23, ""),
            1: (True, 4, 23, ""),
            2: (True, 4, 23, ""),
            3: (True, 4, 23, ""),
            4: (True, 4, 23, ""),
            5: (True, 4, 23, ""),
            6: (True, 4, 23, ""),
        },
    },
    {
        "username": "test_runer",
        "first_name": "Runer",
        "last_name": "",
        "phone": "321-806-7052",
        "schedule": "Mon-Sun: 4AM-EOD",
        "default_start": 4,
        "default_end": 23,
        "weekly": {
            0: (True, 4, 23, ""),
            1: (True, 4, 23, ""),
            2: (True, 4, 23, ""),
            3: (True, 4, 23, ""),
            4: (True, 4, 23, ""),
            5: (True, 4, 23, ""),
            6: (True, 4, 23, ""),
        },
    },
    {
        "username": "test_roberto",
        "first_name": "Roberto",
        "last_name": "",
        "phone": "321-305-1414",
        "schedule": "Mon-Sun: 4AM-EOD",
        "default_start": 4,
        "default_end": 23,
        "weekly": {
            0: (True, 4, 23, ""),
            1: (True, 4, 23, ""),
            2: (True, 4, 23, ""),
            3: (True, 4, 23, ""),
            4: (True, 4, 23, ""),
            5: (True, 4, 23, ""),
            6: (True, 4, 23, ""),
        },
    },
    {
        "username": "test_julio",
        "first_name": "Julio",
        "last_name": "Bonilla",
        "phone": "407-731-7250",
        "schedule": "Mon-Fri: 5AM-4PM, Sat: 6AM-2PM",
        "default_start": 5,
        "default_end": 16,
        "weekly": {
            0: (True,  5, 16, ""),
            1: (True,  5, 16, ""),
            2: (True,  5, 16, ""),
            3: (True,  5, 16, ""),
            4: (True,  5, 16, ""),
            5: (True,  6, 14, ""),
            6: (False, 6, 23, ""),
        },
    },
    {
        "username": "test_neuma",
        "first_name": "Neuma",
        "last_name": "",
        "phone": "407-624-7385",
        "schedule": "Mon-Sun: 4AM-EOD",
        "default_start": 4,
        "default_end": 23,
        "weekly": {
            0: (True, 4, 23, ""),
            1: (True, 4, 23, ""),
            2: (True, 4, 23, ""),
            3: (True, 4, 23, ""),
            4: (True, 4, 23, ""),
            5: (True, 4, 23, ""),
            6: (True, 4, 23, ""),
        },
    },
    {
        "username": "test_michelle",
        "first_name": "Michelle",
        "last_name": "Francis",
        "phone": "407-879-6860",
        "schedule": "Mon-Fri: 6AM-4PM",
        "default_start": 6,
        "default_end": 16,
        "weekly": {
            0: (True,  6, 16, ""),
            1: (True,  6, 16, ""),
            2: (True,  6, 16, ""),
            3: (True,  6, 16, ""),
            4: (True,  6, 16, ""),
            5: (False, 6, 23, ""),
            6: (False, 6, 23, ""),
        },
    },
    {
        "username": "test_carlos",
        "first_name": "Carlos",
        "last_name": "Medina",
        "phone": "787-505-2264",
        "schedule": "Mon-Sun: 4AM-EOD",
        "default_start": 4,
        "default_end": 23,
        "weekly": {
            0: (True, 4, 23, ""),
            1: (True, 4, 23, ""),
            2: (True, 4, 23, ""),
            3: (True, 4, 23, ""),
            4: (True, 4, 23, ""),
            5: (True, 4, 23, ""),
            6: (True, 4, 23, ""),
        },
    },
]

VEHICLES = [
    {"number": "1",  "year": 2021, "make": "Cadillac",   "model": "XT6",                  "notes": ""},
    {"number": "2",  "year": 2020, "make": "Mercedes",   "model": "Metris Mini-Van",       "notes": ""},
    {"number": "3",  "year": 2026, "make": "Chevrolet",  "model": "Suburban",              "notes": ""},
    {"number": "4",  "year": 2025, "make": "Mercedes",   "model": "Sprinter",              "notes": "14 Pax"},
    {"number": "5",  "year": 2022, "make": "Ford",       "model": "Transit Low Roof",      "notes": ""},
    {"number": "6",  "year": 2023, "make": "Chevrolet",  "model": "Suburban",              "notes": ""},
    {"number": "7",  "year": 2019, "make": "Chevrolet",  "model": "Suburban",              "notes": ""},
    {"number": "8",  "year": 2017, "make": "Ford",       "model": "Transit High Roof",     "notes": "14 Pax"},
    {"number": "9",  "year": 2021, "make": "Lincoln",    "model": "Navigator L",           "notes": ""},
    {"number": "10", "year": 2016, "make": "Mercedes",   "model": "Metris Mini-Van",       "notes": ""},
    {"number": "11", "year": 2015, "make": "Ford",       "model": "Transit Low Roof",      "notes": ""},
]

# Which vehicle number to assign each driver index (0-based) for the current week.
# None means leave unassigned (to show the yellow warning highlight).
WEEKLY_ASSIGNMENTS = [
    "3",   # Junaid      -> #3
    "6",   # David       -> #6
    "7",   # Alex        -> #7
    "1",   # Michael     -> #1
    "5",   # Yovanny     -> #5
    "9",   # Angel       -> #9
    "2",   # Hasan       -> #2
    "10",  # Shipo       -> #10
    "4",   # Runer       -> #4
    "8",   # Roberto     -> #8
    "11",  # Julio       -> #11
    None,  # Neuma       -> unassigned (demo yellow row)
    None,  # Michelle    -> unassigned
    "0",   # Carlos      — will be skipped since "0" not in vehicles; treated as None
]


class Command(BaseCommand):
    help = "Seed test in-house drivers, fleet vehicles and schedules for UI preview."

    def add_arguments(self, parser):
        parser.add_argument(
            "--clear",
            action="store_true",
            help="Remove previously seeded test objects before re-seeding.",
        )
        parser.add_argument(
            "--no-assign",
            action="store_true",
            dest="no_assign",
            help="Skip creating vehicle assignments for this week.",
        )

    def handle(self, *args, **options):
        if options["clear"]:
            self._clear()

        vehicles = self._seed_vehicles()
        drivers = self._seed_drivers()
        self._seed_weekly_schedules(drivers)

        if not options["no_assign"]:
            self._seed_assignments(drivers, vehicles)

        self.stdout.write(self.style.SUCCESS(
            f"\nDone.  Created/updated {len(drivers)} drivers, {len(vehicles)} vehicles."
        ))
        self.stdout.write(
            "Visit /dispatching/inhouse-schedule/ to preview the week view."
        )

    # ------------------------------------------------------------------
    def _clear(self):
        usernames = [d["username"] for d in DRIVERS]
        users = User.objects.filter(username__in=usernames)
        driver_ids = Driver.objects.filter(profile__in=users).values_list("id", flat=True)
        DriverVehicleAssignment.objects.filter(driver_id__in=driver_ids).delete()
        DriverWeeklySchedule.objects.filter(driver_id__in=driver_ids).delete()
        Driver.objects.filter(id__in=driver_ids).delete()
        users.delete()
        vehicle_numbers = [v["number"] for v in VEHICLES]
        FleetVehicle.objects.filter(vehicle_number__in=vehicle_numbers).delete()
        self.stdout.write(self.style.WARNING("Cleared existing seed data."))

    def _seed_vehicles(self):
        created_map = {}
        for v in VEHICLES:
            obj, created = FleetVehicle.objects.get_or_create(
                vehicle_number=v["number"],
                defaults={
                    "year": v["year"],
                    "make": v["make"],
                    "model": v["model"],
                    "notes": v["notes"],
                },
            )
            if not created:
                # Update in case fields changed
                obj.year = v["year"]
                obj.make = v["make"]
                obj.model = v["model"]
                obj.notes = v["notes"]
                obj.save()
            created_map[v["number"]] = obj
            verb = "Created" if created else "Updated"
            self.stdout.write(f"  {verb} vehicle #{v['number']} {v['year']} {v['make']} {v['model']}")
        return created_map

    def _seed_drivers(self):
        driver_objs = []
        for d in DRIVERS:
            user, _ = User.objects.get_or_create(
                username=d["username"],
                defaults={
                    "first_name": d["first_name"],
                    "last_name": d["last_name"],
                    "is_active": True,
                },
            )
            user.first_name = d["first_name"]
            user.last_name = d["last_name"]
            user.save()

            driver, created = Driver.objects.get_or_create(
                profile=user,
                defaults={
                    "driver_type": "inhouse",
                    "phone_number": d["phone"],
                    "schedule": d["schedule"],
                    "default_start_hour": d["default_start"],
                    "default_end_hour": d["default_end"],
                },
            )
            if not created:
                driver.driver_type = "inhouse"
                driver.phone_number = d["phone"]
                driver.schedule = d["schedule"]
                driver.default_start_hour = d["default_start"]
                driver.default_end_hour = d["default_end"]
                driver.save()

            verb = "Created" if created else "Updated"
            self.stdout.write(f"  {verb} driver {driver}")
            driver_objs.append((d, driver))
        return driver_objs

    def _seed_weekly_schedules(self, driver_pairs):
        for d_data, driver in driver_pairs:
            for day, (avail, start, end, pref) in d_data["weekly"].items():
                DriverWeeklySchedule.objects.update_or_create(
                    driver=driver,
                    day_of_week=day,
                    defaults={
                        "is_available": avail,
                        "start_hour": start,
                        "end_hour": end,
                        "preference": pref,
                    },
                )

    def _seed_assignments(self, driver_pairs, vehicles):
        today = timezone.localdate()
        week_start = today - timedelta(days=today.weekday())  # Monday
        week_dates = [week_start + timedelta(days=i) for i in range(7)]

        for idx, (_, driver) in enumerate(driver_pairs):
            vehicle_number = WEEKLY_ASSIGNMENTS[idx] if idx < len(WEEKLY_ASSIGNMENTS) else None
            vehicle = vehicles.get(vehicle_number) if vehicle_number else None
            if vehicle is None:
                continue
            for day in week_dates:
                DriverVehicleAssignment.objects.update_or_create(
                    driver=driver,
                    date=day,
                    defaults={"vehicle": vehicle},
                )
        self.stdout.write(f"  Assigned vehicles for {len(week_dates)}-day week ({week_start} – {week_dates[-1]})")
