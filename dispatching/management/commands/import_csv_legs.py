"""
Import legs from a CSV export into the local DB for testing.
Creates customers, reservations, legs, drivers, fleet vehicles, and
vehicle assignments as needed. Does NOT send any emails.

Usage:
    python manage.py import_csv_legs --csv legs_dashboard_2026-03-31.csv
    python manage.py import_csv_legs --csv legs_dashboard_2026-03-31.csv --clear  # wipe first
"""

import csv
import os
import re
from datetime import datetime, date
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.utils import timezone

from drivers.models import Driver, DriverVehicleAssignment, FleetVehicle
from rates.models import Vehicle, Rate
from reservations.models import Leg, Reservation, Customer


# Screenshot data: driver name -> (fleet_vehicle_number, vehicle_type_str)
DRIVER_VEHICLES = {
    'Julio Bonilla':    ('#001', 'suv'),
    'Steven Kleisath':  ('#003', 'suv'),
    'rizwan':           ('#005', 'van'),
    'Seline':           ('#006', 'suv'),
    'Yovanny Suarez':   ('#007', 'suv'),
    'ken':              ('#008', 'Van(14 Pax)'),
    'Junaid Baidr':     ('#11',  'Van(14 Pax)'),
}

VEHICLE_TYPE_MAP = {
    'Towncar': 'towncar',
    'SUV': 'suv',
    'Mini Van': 'mini_van',
    'Van': 'van',
    'Van (14 Pax)': 'Van(14 Pax)',
}

TRIP_TYPE_MAP = {
    'Arrival': 'arrival',
    'Departure': 'return',
    'Other': 'other',
    'Cruise': 'cruise',
}


def parse_car_seats(car_seats_str):
    """Parse '1 Rear-Facing, 2 Booster' into counts."""
    rf = ff = booster = 0
    if not car_seats_str or car_seats_str.strip().lower() in ('no car seats', ''):
        return rf, ff, booster
    for part in car_seats_str.split(','):
        part = part.strip().lower()
        m = re.match(r'(\d+)\s+(.+)', part)
        if m:
            count = int(m.group(1))
            kind = m.group(2)
            if 'rear' in kind:
                rf = count
            elif 'forward' in kind:
                ff = count
            elif 'booster' in kind:
                booster = count
    return rf, ff, booster


class Command(BaseCommand):
    help = "Import CSV legs into local DB for testing (no emails sent)"

    def add_arguments(self, parser):
        parser.add_argument("--csv", type=str, required=True)
        parser.add_argument("--clear", action="store_true",
                            help="Delete existing legs/reservations for this date first")

    def handle(self, *args, **options):
        csv_path = options["csv"]
        if not os.path.isabs(csv_path):
            csv_path = os.path.join(os.getcwd(), csv_path)

        if not os.path.exists(csv_path):
            self.stdout.write(self.style.ERROR(f"File not found: {csv_path}"))
            return

        # Parse CSV
        rows = []
        with open(csv_path, 'r', encoding='utf-8-sig') as f:
            for row in csv.DictReader(f):
                rows.append(row)

        if not rows:
            self.stdout.write(self.style.ERROR("Empty CSV"))
            return

        target_date = datetime.strptime(rows[0]['pickup_date'], '%Y-%m-%d').date()
        self.stdout.write(f"Importing {len(rows)} legs for {target_date}")

        if options["clear"]:
            deleted = Leg.objects.filter(pickup_date=target_date).delete()
            self.stdout.write(f"Cleared: {deleted}")

        # Step 1: Ensure Vehicle types exist
        vehicle_cache = {}
        for v in Vehicle.objects.all():
            vehicle_cache[v.vehicle_type] = v
        self.stdout.write(f"Vehicles in DB: {list(vehicle_cache.keys())}")

        # Step 2: Ensure drivers exist
        driver_cache = {}  # name -> Driver
        for driver_name, (fleet_num, vtype_str) in DRIVER_VEHICLES.items():
            parts = driver_name.split(' ', 1)
            first = parts[0]
            last = parts[1] if len(parts) > 1 else ''

            driver = Driver.objects.filter(
                profile__first_name__iexact=first
            ).select_related('profile').first()

            if not driver:
                # Create user + driver
                username = f"driver_{first.lower()}"
                user, _ = User.objects.get_or_create(
                    username=username,
                    defaults={'first_name': first, 'last_name': last}
                )
                driver, _ = Driver.objects.get_or_create(
                    profile=user,
                    defaults={
                        'driver_type': 'inhouse',
                        'default_start_hour': 4,
                        'default_end_hour': 23,
                    }
                )
                self.stdout.write(f"  Created driver: {first} {last} (id={driver.id})")
            else:
                self.stdout.write(f"  Found driver: {driver.profile.first_name} {driver.profile.last_name} (id={driver.id})")

            driver_cache[driver_name] = driver

            # Ensure fleet vehicle exists
            clean_num = fleet_num.lstrip('#')
            fleet_v = FleetVehicle.objects.filter(vehicle_number=clean_num).first()
            vehicle_obj = vehicle_cache.get(vtype_str)

            if not fleet_v:
                fleet_v = FleetVehicle.objects.create(
                    vehicle_number=clean_num,
                    vehicle_type=vehicle_obj,
                    year=2024,
                    make='Fleet',
                    model=vtype_str,
                )
                self.stdout.write(f"  Created fleet vehicle: #{clean_num} ({vtype_str})")
            else:
                # Update type if needed
                if vehicle_obj and fleet_v.vehicle_type != vehicle_obj:
                    fleet_v.vehicle_type = vehicle_obj
                    fleet_v.save()
                    self.stdout.write(f"  Updated fleet vehicle #{clean_num} type to {vtype_str}")

            # Ensure vehicle assignment for target date
            DriverVehicleAssignment.objects.update_or_create(
                driver=driver,
                date=target_date,
                defaults={'vehicle': fleet_v}
            )

        # Also map 'placeholder' to None
        driver_name_map = {}
        for csv_name in set(row.get('assigned_driver', '').strip() for row in rows):
            if not csv_name or csv_name.lower() == 'placeholder':
                continue
            # Try exact match first
            if csv_name in driver_cache:
                driver_name_map[csv_name] = driver_cache[csv_name]
                continue
            # Try first name match
            first = csv_name.split()[0]
            for dn, d in driver_cache.items():
                if dn.startswith(first):
                    driver_name_map[csv_name] = d
                    break

        # Step 3: Create reservations and legs
        created_legs = 0
        created_res = 0
        res_cache = {}  # reservation_id -> Reservation

        for row in rows:
            leg_id = int(row['leg_id'])
            res_id = int(row['reservation_id'])
            guest_name = row['guest_name'].strip()
            pickup_time = datetime.strptime(row['pickup_time'].strip(), '%I:%M %p').time()
            pickup_loc = row['pickup_location'].strip()
            dropoff_loc = row['dropoff_location'].strip()
            vtype_csv = row.get('vehicle_type', '').strip()
            vtype_str = VEHICLE_TYPE_MAP.get(vtype_csv, vtype_csv.lower())
            pax = int(row.get('passenger_count', 1) or 1)
            car_seats_raw = row.get('car_seats', '').strip()
            status = row.get('status', 'confirmed').strip()
            assigned_driver_name = row.get('assigned_driver', '').strip()

            rf, ff, booster = parse_car_seats(car_seats_raw)
            need_cs = (rf + ff + booster) > 0

            # Skip if leg already exists
            if Leg.objects.filter(id=leg_id).exists():
                self.stdout.write(f"  Leg {leg_id} already exists, skipping")
                continue

            # Get or create customer
            name_parts = guest_name.split(' ', 1)
            c_first = name_parts[0]
            c_last = name_parts[1] if len(name_parts) > 1 else ''
            customer, _ = Customer.objects.get_or_create(
                first_name=c_first,
                last_name=c_last,
                defaults={
                    'email': f"{c_first.lower()}.{c_last.lower()}@test.local",
                    'phone_number': '407-555-0000',
                }
            )

            # Get vehicle object
            vehicle_obj = vehicle_cache.get(vtype_str)

            # Get a rate for this vehicle type
            rate_obj = None
            if vehicle_obj:
                rate_obj = Rate.objects.filter(vehicle=vehicle_obj).first()
            if not rate_obj:
                rate_obj = Rate.objects.first()

            # Get or create reservation
            if res_id not in res_cache:
                res = Reservation.objects.filter(id=res_id).first()
                if not res:
                    res = Reservation(
                        id=res_id,
                        customer=customer,
                        rate=rate_obj,
                        trip_type='one_way',
                        base_price=Decimal('100.00'),
                        total_price=Decimal('100.00'),
                        status='confirmed',
                        passenger_count=pax,
                        vehicle=vehicle_obj,
                        need_carseats=need_cs,
                        rf_carseats=rf,
                        ff_carseats=ff,
                        booster_seats=booster,
                    )
                    res.save()
                    created_res += 1
                res_cache[res_id] = res

            reservation = res_cache[res_id]

            # Find driver
            driver = driver_name_map.get(assigned_driver_name)

            # Create leg (use explicit ID from CSV)
            leg = Leg(
                id=leg_id,
                reservation=reservation,
                pickup_date=target_date,
                pickup_time=pickup_time,
                pickup_location=pickup_loc,
                dropoff_location=dropoff_loc,
                status=status if status != 'placeholder' else 'confirmed',
                driver=driver,
                revenue_share=Decimal('100.00'),
            )
            # Save with update_fields=None but skip signals that send emails
            # by saving directly
            leg.save()
            created_legs += 1

        self.stdout.write(self.style.SUCCESS(
            f"\nDone! Created {created_res} reservations and {created_legs} legs for {target_date}"
        ))
        self.stdout.write(f"Total legs for {target_date}: {Leg.objects.filter(pickup_date=target_date).count()}")
