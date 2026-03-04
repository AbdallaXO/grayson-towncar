"""
Management command to load sample leg data from CSV into local database for testing.

Usage:
    python manage.py load_test_legs "path/to/legs.csv"
    python manage.py load_test_legs "path/to/legs.csv" --clear  # Clear existing test data first
"""

import csv
from datetime import datetime
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.contrib.auth.models import User

from reservations.models import Customer, Reservation, Leg
from rates.models import Vehicle, Rate
from drivers.models import Driver


# Drivers that should be marked as inhouse for testing
INHOUSE_DRIVERS = {
    "carlos medina",
    "michael olmo",
    "david encarnacion",
    "david encarancion",
    "yovanny suarez",
    "angel almanzar",
    "julio bonilla",
    "shipo",
    "neuma",
    "roberto",
    "alex",
    "runer",
    "junaid baidr",
    "abdalla",
    "babu",
    "hany",
    "oualid",
    "wael",
}

# Map CSV vehicle types to model vehicle_type choices
VEHICLE_TYPE_MAP = {
    "towncar": "towncar",
    "suv": "suv",
    "mini van": "mini_van",
    "van": "van",
    "van (14 pax)": "Van(14 Pax)",
}

# Map CSV trip types to reservation trip_type
TRIP_TYPE_MAP = {
    "departure": "one_way",
    "arrival": "one_way",
    "cruise transfer": "one_way",
    "other": "one_way",
}

# Map CSV status to leg DRIVER_STATUS
STATUS_MAP = {
    "completed": "completed",
    "confirmed": "confirmed",
    "picked-up": "picked-up",
    "in-progress": "in-progress",
    "on-the-way": "on-the-way",
    "on-location": "on-location",
}


class Command(BaseCommand):
    help = "Load sample leg data from CSV into local database for capacity planner testing"

    def add_arguments(self, parser):
        parser.add_argument("csv_path", type=str, help="Path to CSV file with leg data")
        parser.add_argument(
            "--clear",
            action="store_true",
            help="Clear previously loaded test data before loading",
        )

    def handle(self, *args, **options):
        csv_path = options["csv_path"]
        clear = options["clear"]

        if clear:
            self._clear_test_data()

        self.stdout.write(f"Loading legs from: {csv_path}")

        # Ensure we have all vehicle types
        self._ensure_vehicles()

        # Get a default rate for creating reservations
        default_rate = Rate.objects.first()
        if not default_rate:
            self.stderr.write(self.style.ERROR("No rates found in database. Run: python manage.py loadrates"))
            return

        # Read CSV
        with open(csv_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        self.stdout.write(f"Found {len(rows)} legs in CSV")

        # Group by reservation_id to create reservations
        reservations_map = {}
        for row in rows:
            res_id = row["reservation_id"]
            if res_id not in reservations_map:
                reservations_map[res_id] = []
            reservations_map[res_id].append(row)

        self.stdout.write(f"Found {len(reservations_map)} unique reservations")

        # Track created objects
        drivers_cache = {}
        customers_cache = {}
        created_legs = 0

        for prod_res_id, leg_rows in reservations_map.items():
            first_row = leg_rows[0]
            guest_name = first_row["guest_name"]

            # Create or get customer
            customer = self._get_or_create_customer(guest_name, customers_cache)

            # Determine vehicle from first leg
            vehicle_type_str = first_row.get("vehicle_type", "SUV").lower()
            vehicle_key = VEHICLE_TYPE_MAP.get(vehicle_type_str, "suv")
            vehicle = Vehicle.objects.filter(vehicle_type=vehicle_key).first()
            if not vehicle:
                vehicle = Vehicle.objects.first()

            # Find a rate for this vehicle
            rate = Rate.objects.filter(vehicle=vehicle).first() or default_rate

            # Create reservation
            trip_type = "round_trip" if len(leg_rows) > 1 else "one_way"
            price = rate.round_trip_price if trip_type == "round_trip" else rate.oneway_price

            reservation = Reservation.objects.create(
                customer=customer,
                rate=rate,
                vehicle=vehicle,
                trip_type=trip_type,
                passenger_count=int(first_row.get("passenger_count", 1)),
                base_price=price,
                total_price=price,
                status="confirmed",
                private_notes=f"[TEST DATA] Production res #{prod_res_id}",
            )

            # Create legs for this reservation
            for row in leg_rows:
                driver = self._get_or_create_driver(row.get("assigned_driver", ""), drivers_cache)

                # Parse pickup time
                pickup_time_str = row["pickup_time"].strip()
                pickup_time = datetime.strptime(pickup_time_str, "%I:%M %p").time()

                # Parse pickup date
                pickup_date = datetime.strptime(row["pickup_date"].strip(), "%Y-%m-%d").date()

                # Get status
                status = STATUS_MAP.get(row.get("status", "").lower().strip(), "in-progress")

                # Get vehicle for this specific leg
                leg_vehicle_str = row.get("vehicle_type", "SUV").lower()
                leg_vehicle_key = VEHICLE_TYPE_MAP.get(leg_vehicle_str, "suv")

                Leg.objects.create(
                    reservation=reservation,
                    pickup_date=pickup_date,
                    pickup_time=pickup_time,
                    pickup_location=row["pickup_location"],
                    dropoff_location=row["dropoff_location"],
                    driver=driver,
                    status=status,
                    private_notes=f"[TEST] Prod leg #{row['leg_id']} | {row.get('trip_type', '')} | Vehicle: {row.get('vehicle_type', '')}",
                )
                created_legs += 1

        self.stdout.write(self.style.SUCCESS(
            f"\nDone! Created:"
            f"\n  {len(customers_cache)} customers"
            f"\n  {len(reservations_map)} reservations"
            f"\n  {created_legs} legs"
            f"\n  {len(drivers_cache)} drivers"
        ))

        # Show driver breakdown
        inhouse = [name for name, d in drivers_cache.items() if d.driver_type == "inhouse"]
        affiliate = [name for name, d in drivers_cache.items() if d.driver_type == "affiliate"]
        self.stdout.write(f"\n  In-house drivers ({len(inhouse)}): {', '.join(inhouse)}")
        self.stdout.write(f"  Affiliate drivers ({len(affiliate)}): {', '.join(affiliate)}")
        self.stdout.write(f"\nTest the planner at: /dispatching/capacity-planner/?date={rows[0]['pickup_date'].strip()}")

    def _ensure_vehicles(self):
        """Make sure all vehicle types exist locally."""
        existing = set(Vehicle.objects.values_list("vehicle_type", flat=True))
        if "Van(14 Pax)" not in existing:
            Vehicle.objects.create(
                vehicle_type="Van(14 Pax)",
                capacity=14,
                luggage_capacity=14,
            )
            self.stdout.write("  Created Van (14 Pax) vehicle")

    def _get_or_create_customer(self, guest_name, cache):
        """Get or create a customer from guest name."""
        if guest_name in cache:
            return cache[guest_name]

        parts = guest_name.strip().split(" ", 1)
        first_name = parts[0]
        last_name = parts[1] if len(parts) > 1 else ""

        # Check for existing customer with same name
        customer = Customer.objects.filter(
            first_name=first_name, last_name=last_name
        ).first()

        if not customer:
            email = f"{first_name.lower()}.{last_name.lower().replace(' ', '')}@test.local"
            customer = Customer.objects.create(
                first_name=first_name,
                last_name=last_name,
                email=email,
                phone_number="555-0000",
                zipcode="00000",
            )

        cache[guest_name] = customer
        return customer

    def _get_or_create_driver(self, driver_name, cache):
        """Get or create a driver from driver name."""
        driver_name = driver_name.strip()
        if not driver_name:
            return None

        if driver_name in cache:
            return cache[driver_name]

        parts = driver_name.split(" ", 1)
        first_name = parts[0].strip().title()
        last_name = parts[1].strip().title() if len(parts) > 1 else ""

        # Try to match existing driver by first_name (case-insensitive)
        user_qs = User.objects.filter(first_name__iexact=first_name)
        if last_name:
            user_qs = user_qs.filter(last_name__iexact=last_name)
        for user in user_qs:
            driver = Driver.objects.filter(profile=user).first()
            if driver:
                self.stdout.write(f"  Matched driver '{driver_name}' -> existing #{driver.id} (user={user.username})")
                cache[driver_name] = driver
                return driver

        # Fallback: check by username
        username = driver_name.lower().replace(" ", "_")
        user = User.objects.filter(username=username).first()
        if user:
            driver = Driver.objects.filter(profile=user).first()
            if driver:
                cache[driver_name] = driver
                return driver

        # Create new user + driver
        if not user:
            user = User.objects.create_user(
                username=username,
                first_name=first_name,
                last_name=last_name,
                password="testpass123",
            )

        driver_type = "inhouse" if driver_name.lower() in INHOUSE_DRIVERS else "affiliate"
        driver = Driver.objects.create(
            profile=user,
            driver_type=driver_type,
        )
        self.stdout.write(f"  Created new driver '{driver_name}' #{driver.id} ({driver_type})")

        cache[driver_name] = driver
        return driver

    def _clear_test_data(self):
        """Clear previously loaded test data (identified by [TEST DATA] in notes)."""
        test_reservations = Reservation.objects.filter(private_notes__contains="[TEST DATA]")
        count = test_reservations.count()
        if count:
            test_reservations.delete()  # Cascades to legs
            self.stdout.write(f"Cleared {count} test reservations and their legs")
        else:
            self.stdout.write("No test data to clear")
