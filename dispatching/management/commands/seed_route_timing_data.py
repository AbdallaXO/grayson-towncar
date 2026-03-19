"""
Seed realistic completed legs with status history so the Route Timing
Reference page has data to display.

Creates ~30 completed arrival legs (MCO → various destinations) across
different time-of-day and day-type buckets, with realistic dwell and drive
times.  A few legs are intentionally seeded with:
  - Missing status history (incomplete data)
  - exclude_from_analytics = True
  - Outlier timing values (very long dwell or drive)

Usage:
    python manage.py seed_route_timing_data
    python manage.py seed_route_timing_data --clear   # remove seeded data first
"""

import random
from datetime import date, time, timedelta, datetime

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.utils import timezone

from drivers.models import Driver
from reservations.models import Leg, LegStatus, Reservation, Flight


# Tag to identify seeded data
SEED_TAG = "[SEED-RT]"

# Realistic routes from MCO
ROUTES = [
    {
        "pickup": "Orlando International Airport (MCO)",
        "dropoff": "Walt Disney World - Grand Floridian Resort & Spa",
        "drive_range": (28, 42),  # minutes
    },
    {
        "pickup": "Orlando International Airport (MCO)",
        "dropoff": "Universal Orlando - Loews Royal Pacific Resort",
        "drive_range": (22, 35),
    },
    {
        "pickup": "Orlando International Airport (MCO)",
        "dropoff": "Hilton Orlando Bonnet Creek",
        "drive_range": (25, 38),
    },
    {
        "pickup": "Orlando International Airport (MCO)",
        "dropoff": "Margaritaville Resort Orlando",
        "drive_range": (30, 45),
    },
    {
        "pickup": "Orlando International Airport (MCO)",
        "dropoff": "Port Canaveral Cruise Terminal",
        "drive_range": (45, 65),
    },
]

# Return routes
RETURN_ROUTES = [
    {
        "pickup": "Walt Disney World - Grand Floridian Resort & Spa",
        "dropoff": "Orlando International Airport (MCO)",
        "drive_range": (28, 42),
    },
    {
        "pickup": "Universal Orlando - Loews Royal Pacific Resort",
        "dropoff": "Orlando International Airport (MCO)",
        "drive_range": (22, 35),
    },
]


def _make_aware(dt):
    """Make a naive datetime timezone-aware (US/Eastern)."""
    import pytz
    eastern = pytz.timezone("US/Eastern")
    return eastern.localize(dt)


class Command(BaseCommand):
    help = "Seed completed legs with status history for Route Timing page testing"

    def add_arguments(self, parser):
        parser.add_argument("--clear", action="store_true", help="Remove previously seeded data first")

    def handle(self, *args, **options):
        if options["clear"]:
            self._clear()

        # Need an inhouse driver
        drivers = list(Driver.objects.filter(driver_type="inhouse", exclude_from_timing=False)[:2])
        if not drivers:
            self.stderr.write("No inhouse drivers found. Run seed_inhouse_test_data first.")
            return

        # Need a customer and rate for reservations
        existing_res = Reservation.objects.first()
        if not existing_res:
            self.stderr.write("No existing reservation found to copy customer/rate from.")
            return

        customer_id = existing_res.customer_id
        rate_id = existing_res.rate_id

        created_legs = 0
        # Spread legs over the last 60 days
        today = date.today()

        # Arrival legs
        for i in range(28):
            route = random.choice(ROUTES)
            days_ago = random.randint(3, 60)
            leg_date = today - timedelta(days=days_ago)

            # Vary time of day
            hour = random.choice([5, 6, 7, 8, 9, 10, 11, 13, 14, 16, 17, 19, 20, 22, 23])
            minute = random.choice([0, 15, 30, 45])
            pickup_time = time(hour, minute)

            # Dwell time (gate arrival to pickup)
            dwell_minutes = random.randint(20, 55)
            drive_minutes = random.randint(*route["drive_range"])

            # Intentional outliers (legs 0, 7, 14)
            is_outlier = i in (0, 7, 14)
            if is_outlier:
                dwell_minutes = random.randint(90, 150)  # abnormally long dwell
                drive_minutes = random.randint(80, 120)  # abnormally long drive

            # Intentional incomplete data (legs 3, 10, 20)
            is_incomplete = i in (3, 10, 20)

            # Intentional excluded (legs 5, 15)
            is_excluded = i in (5, 15)

            # Create reservation
            res = Reservation.objects.create(
                customer_id=customer_id,
                rate_id=rate_id,
                trip_type="arrival",
                passenger_count=random.randint(1, 4),
                luggage_count=random.randint(1, 6),
                store_stop=False,
                special_requests="",
                base_price=75.00,
                additional_charges=0,
                total_price=75.00,
                gratuity_amount=0,
                status="completed",
                booster_seats=0,
                need_carseats=False,
                ff_carseats=0,
                rf_carseats=0,
                private_notes=SEED_TAG,
            )

            # Create flight info for arrivals
            gate_arrival_dt = _make_aware(datetime.combine(leg_date, pickup_time))
            flight = Flight.objects.create(
                flight_type="arrival",
                airline="Delta",
                airline_display_name="Delta Air Lines",
                flight_number=f"DL{random.randint(100, 999)}",
                origin="ATL",
                destination="MCO",
                scheduled_arrival_local=gate_arrival_dt - timedelta(minutes=random.randint(0, 10)),
                actual_gate_arrival_local=gate_arrival_dt,
                status="Landed",
                terminal=random.choice(["A", "B", "C"]),
                gate=str(random.randint(1, 50)),
                baggage_claim="",
                flight_iata="",
            )

            driver = random.choice(drivers)
            leg = Leg.objects.create(
                reservation=res,
                flight_information=flight,
                pickup_date=leg_date,
                pickup_time=pickup_time,
                pickup_location=route["pickup"],
                dropoff_location=route["dropoff"],
                driver=driver,
                status="completed",
                exclude_from_analytics=is_excluded,
                private_notes=SEED_TAG,
            )

            # Create status history
            base_dt = gate_arrival_dt
            if is_incomplete:
                # Only create on-the-way and completed (missing picked-up)
                LegStatus.objects.create(leg=leg, status="on-the-way", timestamp=base_dt + timedelta(minutes=5))
                LegStatus.objects.create(leg=leg, status="completed", timestamp=base_dt + timedelta(minutes=dwell_minutes + drive_minutes))
            else:
                # Full chain: on-the-way → on-location → picked-up → completed
                LegStatus.objects.create(leg=leg, status="on-the-way", timestamp=base_dt - timedelta(minutes=random.randint(10, 30)))
                LegStatus.objects.create(leg=leg, status="on-location", timestamp=base_dt + timedelta(minutes=random.randint(5, 15)))
                LegStatus.objects.create(leg=leg, status="picked-up", timestamp=base_dt + timedelta(minutes=dwell_minutes))
                LegStatus.objects.create(leg=leg, status="completed", timestamp=base_dt + timedelta(minutes=dwell_minutes + drive_minutes))

            label = ""
            if is_outlier:
                label = " [OUTLIER]"
            elif is_incomplete:
                label = " [INCOMPLETE]"
            elif is_excluded:
                label = " [EXCLUDED]"

            self.stdout.write(f"  Leg {leg.id}: {route['pickup'][:20]}→{route['dropoff'][:20]} "
                              f"dwell={dwell_minutes}m drive={drive_minutes}m{label}")
            created_legs += 1

        # Return legs (fewer)
        for i in range(8):
            route = random.choice(RETURN_ROUTES)
            days_ago = random.randint(3, 60)
            leg_date = today - timedelta(days=days_ago)

            hour = random.choice([5, 6, 8, 10, 14, 16, 18])
            minute = random.choice([0, 15, 30, 45])
            pickup_time = time(hour, minute)

            drive_minutes = random.randint(*route["drive_range"])

            res = Reservation.objects.create(
                customer_id=customer_id,
                rate_id=rate_id,
                trip_type="return",
                passenger_count=random.randint(1, 4),
                luggage_count=random.randint(1, 6),
                store_stop=False,
                special_requests="",
                base_price=75.00,
                additional_charges=0,
                total_price=75.00,
                gratuity_amount=0,
                status="completed",
                booster_seats=0,
                need_carseats=False,
                ff_carseats=0,
                rf_carseats=0,
                private_notes=SEED_TAG,
            )

            driver = random.choice(drivers)
            leg = Leg.objects.create(
                reservation=res,
                pickup_date=leg_date,
                pickup_time=pickup_time,
                pickup_location=route["pickup"],
                dropoff_location=route["dropoff"],
                driver=driver,
                status="completed",
                private_notes=SEED_TAG,
            )

            base_dt = _make_aware(datetime.combine(leg_date, pickup_time))
            LegStatus.objects.create(leg=leg, status="on-the-way", timestamp=base_dt - timedelta(minutes=random.randint(10, 30)))
            LegStatus.objects.create(leg=leg, status="on-location", timestamp=base_dt - timedelta(minutes=random.randint(0, 5)))
            LegStatus.objects.create(leg=leg, status="picked-up", timestamp=base_dt)
            LegStatus.objects.create(leg=leg, status="completed", timestamp=base_dt + timedelta(minutes=drive_minutes))

            self.stdout.write(f"  Leg {leg.id}: {route['pickup'][:20]}→{route['dropoff'][:20]} drive={drive_minutes}m")
            created_legs += 1

        self.stdout.write(self.style.SUCCESS(f"\nCreated {created_legs} seed legs for route timing testing."))
        self.stdout.write("Includes: 3 outliers, 3 incomplete, 2 excluded")
        self.stdout.write("Visit /route-timing/ to test.")

    def _clear(self):
        """Remove all seeded data."""
        # Find reservations tagged with SEED_TAG
        seeded_reservations = Reservation.objects.filter(private_notes=SEED_TAG)
        count = seeded_reservations.count()
        if count:
            # Legs, LegStatus, and Flights cascade from Reservation
            # But Flight is OneToOne on Leg, so get flight IDs first
            leg_ids = list(Leg.objects.filter(reservation__in=seeded_reservations).values_list("id", flat=True))
            flight_ids = list(Leg.objects.filter(id__in=leg_ids, flight_information__isnull=False).values_list("flight_information_id", flat=True))

            seeded_reservations.delete()
            if flight_ids:
                Flight.objects.filter(id__in=flight_ids).delete()
            self.stdout.write(self.style.WARNING(f"Cleared {count} seeded reservations and their legs/flights."))
        else:
            self.stdout.write("No seeded data found to clear.")
