"""
Test the bulk auto-assign planner against a real day's data from CSV.

Loads legs from a CSV export, builds mock objects, runs
suggest_assignments_clustered(), and prints proposed schedules.
Does NOT save anything to the DB or send any emails.

Usage:
    python manage.py test_planner --csv legs_dashboard_2026-03-31.csv
"""

import csv
import os
from datetime import date, datetime, timedelta, time
from decimal import Decimal
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from django.core.management.base import BaseCommand

from dispatching.scheduler import (
    build_driver_schedules,
    suggest_assignments,
    suggest_assignments_clustered,
    preload_timing_cache,
    estimate_job_end_time,
    cluster_legs_by_time,
    load_all_driver_vtypes,
    get_vehicle_tier,
    get_compatible_vehicle_types,
    DriverDaySchedule,
    ScheduleSlot,
    check_feasibility,
    DRIVE_TIME_ESTIMATES,
    DEFAULT_DRIVE_TIME,
)
from dispatching.models import SchedulerSettings


# ── Mock objects that mimic Django model interfaces ──────────────────


class MockVehicle:
    """Mimics rates.Vehicle with vehicle_type attribute."""
    def __init__(self, vehicle_type_str):
        self.vehicle_type = vehicle_type_str

    def __str__(self):
        return self.vehicle_type or ""

    def __eq__(self, other):
        if isinstance(other, str):
            return self.vehicle_type == other
        if isinstance(other, MockVehicle):
            return self.vehicle_type == other.vehicle_type
        return NotImplemented

    def __hash__(self):
        return hash(self.vehicle_type)


class MockReservation:
    """Mimics Reservation with vehicle and customer."""
    def __init__(self, res_id, vehicle_type_str, customer_name, store_stop=False):
        self.id = res_id
        self.vehicle = MockVehicle(vehicle_type_str) if vehicle_type_str else None
        self.customer = type('obj', (object,), {'get_full_name': lambda self=None, n=customer_name: n})()
        self.store_stop = store_stop


class MockFlight:
    """Mimics Flight for arrival legs."""
    def __init__(self):
        self.estimated_arrival_local = None
        self.actual_arrival_local = None
        self.actual_gate_arrival_local = None
        self.estimated_gate_arrival_local = None
        self.scheduled_arrival_local = None
        self.scheduled_gate_arrival_local = None
        self.terminal = None

    def __str__(self):
        return "Flight"

    def __bool__(self):
        return True


class MockLeg:
    """Mimics Leg model with just enough for the scheduler."""
    def __init__(self, leg_id, reservation, pickup_date, pickup_time,
                 pickup_location, dropoff_location, trip_type,
                 status='confirmed', driver=None, flight_information=None):
        self.id = leg_id
        self.reservation = reservation
        self.reservation_id = reservation.id if reservation else None
        self.pickup_date = pickup_date
        self.pickup_time = pickup_time
        self.pickup_location = pickup_location
        self.dropoff_location = dropoff_location
        self._trip_type = trip_type
        self.status = status
        self.driver = driver
        self.driver_id = None
        self.flight_information = flight_information
        self.flight_information_id = id(flight_information) if flight_information else None
        self.revenue_share = Decimal('100.00')

    def get_trip_type(self):
        return self._trip_type

    def get_cruise_direction(self):
        return None

    def is_airport_pickup(self):
        pickup_lower = self.pickup_location.lower()
        return 'mco' in pickup_lower or 'sfb' in pickup_lower

    def __str__(self):
        return f"Leg {self.id}"


def _normalize_vehicle_type(vtype_str):
    """Convert CSV vehicle type to scheduler-compatible string."""
    if not vtype_str:
        return None
    vtype_str = vtype_str.strip()
    mapping = {
        'Towncar': 'towncar',
        'towncar': 'towncar',
        'SUV': 'suv',
        'suv': 'suv',
        'Mini Van': 'mini_van',
        'mini_van': 'mini_van',
        'Van': 'van',
        'van': 'van',
        'Van (14 Pax)': 'Van(14 Pax)',
        'Van(14 Pax)': 'Van(14 Pax)',
        'van(14 pax)': 'Van(14 Pax)',
        'VAN(14 PAX)': 'Van(14 Pax)',
    }
    return mapping.get(vtype_str, vtype_str.lower())


def _parse_trip_type(trip_type_str):
    """Convert CSV trip type to scheduler-compatible string."""
    mapping = {
        'Arrival': 'arrival',
        'Departure': 'return',
        'Other': 'other',
        'Cruise': 'cruise',
    }
    return mapping.get(trip_type_str, trip_type_str.lower())


def _parse_time(time_str):
    """Parse '6:00 AM' to time object."""
    return datetime.strptime(time_str.strip(), '%I:%M %p').time()


class Command(BaseCommand):
    help = "Test planner on CSV leg data (read-only, no DB writes, no emails)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--csv",
            type=str,
            required=True,
            help="Path to CSV file with legs data",
        )

    def handle(self, *args, **options):
        csv_path = options["csv"]
        if not os.path.isabs(csv_path):
            csv_path = os.path.join(os.getcwd(), csv_path)

        if not os.path.exists(csv_path):
            self.stdout.write(self.style.ERROR(f"File not found: {csv_path}"))
            return

        # Load settings
        cfg = SchedulerSettings.get_settings()

        # Preload timing cache
        preload_timing_cache()

        # Parse CSV
        legs = []
        target_date = None
        with open(csv_path, 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            for row in reader:
                leg_id = int(row['leg_id'])
                res_id = int(row['reservation_id'])
                pickup_date = datetime.strptime(row['pickup_date'], '%Y-%m-%d').date()
                if target_date is None:
                    target_date = pickup_date

                pickup_time = _parse_time(row['pickup_time'])
                vehicle_type = _normalize_vehicle_type(row.get('vehicle_type', ''))
                trip_type = _parse_trip_type(row.get('trip_type', 'Other'))
                status = row.get('status', 'confirmed').strip()
                customer_name = row.get('guest_name', '').strip()

                reservation = MockReservation(res_id, vehicle_type, customer_name)

                # Add flight info for arrivals at airports
                flight_info = None
                if trip_type == 'arrival':
                    flight_info = MockFlight()

                leg = MockLeg(
                    leg_id=leg_id,
                    reservation=reservation,
                    pickup_date=pickup_date,
                    pickup_time=pickup_time,
                    pickup_location=row['pickup_location'].strip(),
                    dropoff_location=row['dropoff_location'].strip(),
                    trip_type=trip_type,
                    status=status,
                    flight_information=flight_info,
                )
                legs.append(leg)

        self.stdout.write(f"\n{'='*70}")
        self.stdout.write(f"  PLANNER TEST - {target_date.strftime('%A, %B %d, %Y')}")
        self.stdout.write(f"  Loaded {len(legs)} legs from CSV")
        self.stdout.write(f"{'='*70}")
        self.stdout.write(f"\nSettings: load_balance_multiplier={cfg.load_balance_multiplier}, "
                          f"exponent={getattr(cfg, 'load_balance_exponent', 'N/A')}, "
                          f"idle_gap_threshold={getattr(cfg, 'idle_gap_threshold', 'N/A')}min, "
                          f"span_threshold={getattr(cfg, 'span_threshold_hours', 'N/A')}hr, "
                          f"backward_chain={getattr(cfg, 'backward_chain_bonus', 'N/A')}, "
                          f"shift_coherence={getattr(cfg, 'shift_coherence_bonus', 'N/A')}")

        # Show current assignments from CSV
        self.stdout.write(f"\n--- CURRENT ASSIGNMENTS (from CSV) ---\n")
        current_by_driver = defaultdict(list)
        for leg in sorted(legs, key=lambda l: l.pickup_time):
            current_by_driver["ALL"].append(leg)

        for leg in sorted(legs, key=lambda l: l.pickup_time):
            vt = leg.reservation.vehicle.vehicle_type if leg.reservation and leg.reservation.vehicle else '???'
            self.stdout.write(
                f"  Leg {leg.id:5d} | {leg.pickup_time.strftime('%I:%M %p').lstrip('0'):>8s} | "
                f"{leg.get_trip_type():>8s} | {vt:>12s} | "
                f"{leg.pickup_location[:35]:35s} -> {leg.dropoff_location[:35]}"
            )

        # Build driver-vehicle mapping from the screenshot data
        # These match the user's screenshot
        DRIVER_VEHICLES = {
            'Julio Bonilla':    ('suv', 4, 23),
            'Steven Kleisath':  ('suv', 4, 23),
            'rizwan':           ('van', 4, 23),
            'Seline':           ('suv', 4, 23),
            'Yovanny Suarez':   ('suv', 4, 23),
            'ken':              ('Van(14 Pax)', 4, 23),
            'Junaid Baidr':     ('Van(14 Pax)', 4, 23),
        }

        # Build synthetic driver schedules
        driver_vtypes = {}
        driver_hours = {}
        empty_schedules = {}
        driver_id_map = {}  # name -> fake_id

        for i, (name, (vtype, start_h, end_h)) in enumerate(DRIVER_VEHICLES.items(), start=1):
            did = 1000 + i
            driver_id_map[name] = did
            driver_vtypes[did] = vtype
            driver_hours[did] = (start_h, end_h)
            empty_schedules[did] = DriverDaySchedule(
                driver_id=did,
                driver_name=name,
                driver_type='inhouse',
                slots=[],
            )

        self.stdout.write(f"\n--- DRIVERS ({len(DRIVER_VEHICLES)}) ---\n")
        for name, (vtype, start_h, end_h) in DRIVER_VEHICLES.items():
            did = driver_id_map[name]
            compatible = get_compatible_vehicle_types(vtype)
            self.stdout.write(
                f"  {name:25s} id={did} vehicle={vtype:15s} "
                f"avail={start_h:02d}:00-{end_h:02d}:00 "
                f"can_do={compatible}"
            )

        # Show clusters
        gap_min = getattr(cfg, 'cluster_gap_minutes', 120)
        clusters = cluster_legs_by_time(legs, target_date, gap_minutes=gap_min)
        self.stdout.write(f"\n--- TIME CLUSTERS ({len(clusters)} clusters, gap={gap_min}min) ---\n")
        for ci, cluster in enumerate(clusters):
            trip_types = [l.get_trip_type() for l in cluster]
            type_counts = defaultdict(int)
            for tt in trip_types:
                type_counts[tt] += 1
            type_str = ", ".join(f"{v} {k}" for k, v in sorted(type_counts.items(), key=lambda x: -x[1]))
            self.stdout.write(
                f"  Cluster {ci}: {len(cluster):2d} legs | "
                f"{cluster[0].pickup_time.strftime('%I:%M %p').lstrip('0'):>8s} - "
                f"{cluster[-1].pickup_time.strftime('%I:%M %p').lstrip('0'):>8s} | "
                f"{type_str}"
            )

        # ── Run NEW planner (clustered) ──
        self.stdout.write(f"\n{'='*70}")
        self.stdout.write(f"  RUNNING NEW PLANNER (clustered + enhanced scoring)")
        self.stdout.write(f"{'='*70}\n")

        suggestions = suggest_assignments_clustered(
            legs,
            empty_schedules,
            target_date,
            driver_hours=driver_hours,
            driver_preferences=None,
            driver_vtypes=driver_vtypes,
        )

        # Build proposed schedule
        proposed = defaultdict(list)
        unassigned = []
        leg_by_id = {l.id: l for l in legs}
        for s in suggestions:
            leg = leg_by_id[s.leg_id]
            if s.suggested_driver_id:
                proposed[s.suggested_driver_name].append(leg)
            else:
                unassigned.append(leg)

        self.stdout.write(f"--- NEW PLANNER RESULTS ---\n")
        total_assigned = 0
        for driver_name in sorted(proposed.keys()):
            driver_legs = sorted(proposed[driver_name], key=lambda l: l.pickup_time)
            total_assigned += len(driver_legs)
            first_pickup = datetime.combine(target_date, driver_legs[0].pickup_time)
            last_end = max(estimate_job_end_time(l, target_date) for l in driver_legs)
            span_h = (last_end - first_pickup).total_seconds() / 3600

            # Calculate gaps
            gaps = []
            for i in range(1, len(driver_legs)):
                prev_end = estimate_job_end_time(driver_legs[i-1], target_date)
                next_start = datetime.combine(target_date, driver_legs[i].pickup_time)
                gap_m = (next_start - prev_end).total_seconds() / 60
                gaps.append(gap_m)

            max_gap = max(gaps) if gaps else 0
            gap_str = f"max_gap={max_gap:.0f}min" if gaps else "1 job"

            self.stdout.write(f"  {driver_name:25s} {len(driver_legs):2d} legs | span {span_h:.1f}hr | {gap_str}")
            for leg in driver_legs:
                vt = leg.reservation.vehicle.vehicle_type if leg.reservation and leg.reservation.vehicle else '???'
                self.stdout.write(
                    f"    {leg.pickup_time.strftime('%I:%M %p').lstrip('0'):>8s} "
                    f"{leg.get_trip_type():>8s} {vt:>12s}  "
                    f"{leg.pickup_location[:30]:30s} -> {leg.dropoff_location[:30]}"
                )
            if gaps:
                gap_strs = [f"{g:.0f}min" for g in gaps]
                self.stdout.write(f"    gaps: {', '.join(gap_strs)}")
            self.stdout.write("")

        if unassigned:
            self.stdout.write(f"  UNASSIGNED: {len(unassigned)} legs")
            for leg in sorted(unassigned, key=lambda l: l.pickup_time):
                vt = leg.reservation.vehicle.vehicle_type if leg.reservation and leg.reservation.vehicle else '???'
                self.stdout.write(
                    f"    Leg {leg.id}: {leg.pickup_time.strftime('%I:%M %p').lstrip('0')} "
                    f"{leg.get_trip_type()} {vt} "
                    f"{leg.pickup_location[:30]} -> {leg.dropoff_location[:30]}"
                )

        self.stdout.write(f"\n  SUMMARY: {total_assigned} assigned, {len(unassigned)} unassigned "
                          f"(of {len(legs)} total)")

        # Schedule quality metrics
        self.stdout.write(f"\n  SCHEDULE QUALITY:")
        spans = []
        max_gaps = []
        job_counts = []
        for driver_name, driver_legs in proposed.items():
            driver_legs_sorted = sorted(driver_legs, key=lambda l: l.pickup_time)
            first = datetime.combine(target_date, driver_legs_sorted[0].pickup_time)
            last_end = max(estimate_job_end_time(l, target_date) for l in driver_legs_sorted)
            spans.append((last_end - first).total_seconds() / 3600)
            job_counts.append(len(driver_legs))
            gaps = []
            for i in range(1, len(driver_legs_sorted)):
                prev_end = estimate_job_end_time(driver_legs_sorted[i-1], target_date)
                next_start = datetime.combine(target_date, driver_legs_sorted[i].pickup_time)
                gaps.append((next_start - prev_end).total_seconds() / 60)
            max_gaps.append(max(gaps) if gaps else 0)

        if spans:
            avg_span = sum(spans) / len(spans)
            avg_gap = sum(max_gaps) / len(max_gaps)
            min_jobs = min(job_counts)
            max_jobs = max(job_counts)
            spread = max_jobs - min_jobs
            self.stdout.write(
                f"    avg_span={avg_span:.1f}hr, avg_max_gap={avg_gap:.0f}min, "
                f"jobs={min_jobs}-{max_jobs} (spread={spread})"
            )

        self.stdout.write(f"\n{'='*70}\n")
