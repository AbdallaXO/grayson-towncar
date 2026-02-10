"""
Daily Capacity Planner - Core Scheduling Logic

Provides feasibility checking, assignment suggestions, and batching detection
for optimizing in-house driver coverage.
"""

from datetime import datetime, timedelta, time, date
from decimal import Decimal
from typing import List, Dict, Optional
from dataclasses import dataclass, field


# ============================================================================
# HARDCODED ROUTE TIMING ESTIMATES (minutes)
# Based on Orlando-area geography. These serve as fallbacks when
# RouteTimingMetric has insufficient data.
# ============================================================================

DRIVE_TIME_ESTIMATES = {
    ('MCO Terminal', 'Disney Resort'): 30,
    ('Disney Resort', 'MCO Terminal'): 30,
    ('MCO Terminal', 'Universal Resort'): 25,
    ('Universal Resort', 'MCO Terminal'): 25,
    ('MCO Terminal', 'Port Canaveral Area'): 55,
    ('Port Canaveral Area', 'MCO Terminal'): 55,
    ('MCO Terminal', 'Other Hotel'): 25,
    ('Other Hotel', 'MCO Terminal'): 25,
    ('MCO Terminal', 'Residential'): 30,
    ('Residential', 'MCO Terminal'): 30,
    ('MCO Terminal', 'Airport Hotel'): 12,
    ('Airport Hotel', 'MCO Terminal'): 12,
    ('Disney Resort', 'Port Canaveral Area'): 70,
    ('Port Canaveral Area', 'Disney Resort'): 70,
    ('Disney Resort', 'Universal Resort'): 28,
    ('Universal Resort', 'Disney Resort'): 28,
    ('Disney Resort', 'Other Hotel'): 25,
    ('Other Hotel', 'Disney Resort'): 25,
    ('Disney Resort', 'Disney Resort'): 12,
    ('MCO Terminal', 'MCO Terminal'): 5,
    ('SFB Terminal', 'SFB Terminal'): 5,
    ('Airport Hotel', 'Airport Hotel'): 10,
    ('Other Hotel', 'Other Hotel'): 15,
    ('Residential', 'Residential'): 15,
    ('Port Canaveral Area', 'Port Canaveral Area'): 10,
    ('Other', 'Other'): 20,
    ('Universal Resort', 'Port Canaveral Area'): 60,
    ('Port Canaveral Area', 'Universal Resort'): 60,
    ('Universal Resort', 'Other Hotel'): 15,
    ('Other Hotel', 'Universal Resort'): 15,
    ('Universal Resort', 'Universal Resort'): 10,
    ('SFB Terminal', 'Disney Resort'): 60,
    ('Disney Resort', 'SFB Terminal'): 60,
    ('SFB Terminal', 'Universal Resort'): 49,
    ('SFB Terminal', 'Port Canaveral Area'): 70,
    ('Port Canaveral Area', 'SFB Terminal'): 70,
    ('Airport Hotel', 'Disney Resort'): 25,
    ('Disney Resort', 'Airport Hotel'): 25,
    ('Airport Hotel', 'Universal Resort'): 20,
    ('Universal Resort', 'Airport Hotel'): 20,
    ('SFB Terminal', 'MCO Terminal'): 40,
    ('MCO Terminal', 'SFB Terminal'): 40,
    ('SFB Terminal', 'Other Hotel'): 55,
    ('Other Hotel', 'SFB Terminal'): 55,
    ('SFB Terminal', 'Airport Hotel'): 45,
    ('Airport Hotel', 'SFB Terminal'): 45,
    ('SFB Terminal', 'Residential'): 55,
    ('Residential', 'SFB Terminal'): 55,
}

DEFAULT_DRIVE_TIME = 35  # fallback for unknown routes

# Buffer between jobs (repositioning uncertainty + personal break)
INTER_JOB_BUFFER = 10  # minutes


# ============================================================================
# ROUTE TIMING CACHE — avoids N+1 queries during scheduling
# ============================================================================

_timing_cache = None  # dict keyed by (pickup_cat, dropoff_cat) -> metric dict


def preload_timing_cache():
    """
    Load all RouteTimingMetric records into memory.
    Call this once before running scheduling functions to avoid
    hundreds of individual DB queries.
    """
    global _timing_cache
    from reservations.models import RouteTimingMetric

    _timing_cache = {}
    for m in RouteTimingMetric.objects.filter(sample_count__gte=1):
        key = (m.pickup_location_category, m.dropoff_location_category)
        existing = _timing_cache.get(key)
        # Keep the one with more samples
        if existing is None or m.sample_count > existing['sample_count']:
            _timing_cache[key] = {
                'sample_count': m.sample_count,
                'p75_drive_time': m.p75_drive_time,
                'avg_drive_time': m.avg_drive_time,
                'p75_airport_dwell_time': m.p75_airport_dwell_time,
                'avg_airport_dwell_time': m.avg_airport_dwell_time,
                'trip_type': m.trip_type,
            }


def clear_timing_cache():
    """Clear the cache (e.g., after metrics are updated)."""
    global _timing_cache
    _timing_cache = None


def _get_cached_metric(pickup_cat, dropoff_cat):
    """Look up a metric from cache, or return None if not cached / insufficient samples."""
    if _timing_cache is None:
        return None
    metric = _timing_cache.get((pickup_cat, dropoff_cat))
    if metric and metric['sample_count'] >= 5:
        return metric
    return None


# ============================================================================
# DATA CLASSES
# ============================================================================

@dataclass
class ScheduleSlot:
    """A single leg in a driver's daily schedule."""
    leg_id: int
    pickup_time: time
    pickup_location: str
    pickup_category: str
    dropoff_location: str
    dropoff_category: str
    trip_type: str
    estimated_end_time: datetime
    reservation_id: int
    customer_name: str
    status: str
    has_flight: bool
    flight_info: Optional[str] = None
    revenue: Optional[Decimal] = None
    # Set by view for template positioning
    position_pct: float = 0
    width_pct: float = 0


@dataclass
class DriverDaySchedule:
    """Full day schedule for a single driver."""
    driver_id: int
    driver_name: str
    driver_type: str
    slots: List[ScheduleSlot] = field(default_factory=list)

    @property
    def total_legs(self):
        return len(self.slots)

    @property
    def total_revenue(self):
        return sum((s.revenue or Decimal('0.00')) for s in self.slots)


@dataclass
class FeasibilityResult:
    """Result of checking if a driver can take an additional leg."""
    feasible: bool
    buffer_minutes: int
    warnings: List[str] = field(default_factory=list)
    reason: str = ""


@dataclass
class AssignmentSuggestion:
    """Suggested driver for an unassigned leg."""
    leg_id: int
    suggested_driver_id: Optional[int]
    suggested_driver_name: Optional[str]
    feasibility: Optional[FeasibilityResult]
    reason: str
    priority: int  # 1=best fit, 0=no fit


@dataclass
class BatchingOpportunity:
    """Group of legs that could be done back-to-back by one driver."""
    legs: List[dict]
    location_category: str
    time_window_start: time
    time_window_end: time
    reason: str


# ============================================================================
# CORE FUNCTIONS
# ============================================================================

def get_drive_time(pickup_category: str, dropoff_category: str) -> int:
    """
    Get estimated drive time between two location categories in minutes.
    Prefers P75 from RouteTimingMetric (conservative), falls back to hardcoded.
    Uses in-memory cache when available to avoid repeated DB queries.
    """
    # Try cache first
    cached = _get_cached_metric(pickup_category, dropoff_category)
    if cached:
        if cached['p75_drive_time']:
            return cached['p75_drive_time']
        if cached['avg_drive_time']:
            return cached['avg_drive_time']
    elif _timing_cache is None:
        # Cache not loaded — fall back to DB query (single-use calls)
        from reservations.models import RouteTimingMetric
        metric = RouteTimingMetric.objects.filter(
            pickup_location_category=pickup_category,
            dropoff_location_category=dropoff_category,
            sample_count__gte=5,
        ).order_by('-sample_count').first()
        if metric:
            if metric.p75_drive_time:
                return metric.p75_drive_time
            if metric.avg_drive_time:
                return metric.avg_drive_time

    return DRIVE_TIME_ESTIMATES.get(
        (pickup_category, dropoff_category),
        DEFAULT_DRIVE_TIME,
    )


def get_airport_dwell_time(pickup_category: str, dropoff_category: str) -> int:
    """
    Get estimated airport dwell time (gate arrival → pickup) in minutes.
    Only meaningful for arrival trips. Falls back to 20 min.
    Uses in-memory cache when available.
    """
    # Try cache first
    cached = _get_cached_metric(pickup_category, dropoff_category)
    if cached:
        if cached['p75_airport_dwell_time']:
            return cached['p75_airport_dwell_time']
        if cached['avg_airport_dwell_time']:
            return cached['avg_airport_dwell_time']
    elif _timing_cache is None:
        # Cache not loaded — fall back to DB query
        from reservations.models import RouteTimingMetric
        metric = RouteTimingMetric.objects.filter(
            pickup_location_category=pickup_category,
            dropoff_location_category=dropoff_category,
            trip_type='arrival',
            sample_count__gte=5,
        ).order_by('-sample_count').first()
        if metric:
            if metric.p75_airport_dwell_time:
                return metric.p75_airport_dwell_time
            if metric.avg_airport_dwell_time:
                return metric.avg_airport_dwell_time

    return 45  # Default: 40 min airport dwell (flight land → bags → walk → in car)


def estimate_job_end_time(leg, target_date: date) -> datetime:
    """
    Estimate when a driver finishes this leg.
    For arrivals: pickup_time + dwell + drive.
    For non-arrivals: pickup_time + drive.
    """
    from dispatching.analytics import categorize_location

    pickup_cat = categorize_location(leg.pickup_location)
    dropoff_cat = categorize_location(leg.dropoff_location)
    drive_minutes = get_drive_time(pickup_cat, dropoff_cat)

    pickup_dt = datetime.combine(target_date, leg.pickup_time)

    # For arrivals, add airport dwell time
    trip_type = leg.get_trip_type()
    if trip_type == 'arrival':
        dwell_minutes = get_airport_dwell_time(pickup_cat, dropoff_cat)
        return pickup_dt + timedelta(minutes=dwell_minutes + drive_minutes)

    return pickup_dt + timedelta(minutes=drive_minutes)


def predict_driver_available_time(leg, target_date: date) -> datetime:
    """
    Predict when a driver will be available for the next job after this leg.
    Returns: estimated end time + inter-job buffer.
    """
    return estimate_job_end_time(leg, target_date) + timedelta(minutes=INTER_JOB_BUFFER)


def check_feasibility(
    driver_schedule: DriverDaySchedule,
    new_leg,
    target_date: date,
) -> FeasibilityResult:
    """
    Check whether a driver can fit a new leg into their schedule.

    Checks:
    1. No overlaps with existing jobs
    2. Enough time after preceding job (drive + buffer)
    3. Enough time before following job (drive + buffer)
    """
    from dispatching.analytics import categorize_location

    if not driver_schedule.slots:
        return FeasibilityResult(feasible=True, buffer_minutes=999, reason="Available - no jobs yet")

    new_pickup_dt = datetime.combine(target_date, new_leg.pickup_time)
    new_end_dt = estimate_job_end_time(new_leg, target_date)
    new_pickup_cat = categorize_location(new_leg.pickup_location)
    new_dropoff_cat = categorize_location(new_leg.dropoff_location)

    warnings = []
    sorted_slots = sorted(driver_schedule.slots, key=lambda s: s.pickup_time)

    # Find preceding and following slots
    preceding = None
    following = None
    for slot in sorted_slots:
        slot_pickup_dt = datetime.combine(target_date, slot.pickup_time)
        if slot_pickup_dt <= new_pickup_dt:
            preceding = slot
        elif following is None:
            following = slot

    buffer_minutes = 999

    # Check against preceding slot
    if preceding:
        reposition = get_drive_time(preceding.dropoff_category, new_pickup_cat)
        earliest_available = preceding.estimated_end_time + timedelta(minutes=reposition + INTER_JOB_BUFFER)
        buffer_minutes = int((new_pickup_dt - earliest_available).total_seconds() / 60)

        if buffer_minutes < 0:
            end_str = preceding.estimated_end_time.strftime('%I:%M %p').lstrip('0')
            return FeasibilityResult(
                feasible=False,
                buffer_minutes=buffer_minutes,
                reason=f"Needs {abs(buffer_minutes)} more min. Previous job ends ~{end_str}, "
                       f"+{reposition}min drive +{INTER_JOB_BUFFER}min buffer.",
            )
        if buffer_minutes < 15:
            warnings.append(f"Tight: {buffer_minutes}min after previous job")

    # Check against following slot
    if following:
        following_pickup_dt = datetime.combine(target_date, following.pickup_time)
        reposition = get_drive_time(new_dropoff_cat, following.pickup_category)
        earliest_for_next = new_end_dt + timedelta(minutes=reposition + INTER_JOB_BUFFER)
        following_buffer = int((following_pickup_dt - earliest_for_next).total_seconds() / 60)

        if following_buffer < 0:
            next_str = following.pickup_time.strftime('%I:%M %p').lstrip('0')
            return FeasibilityResult(
                feasible=False,
                buffer_minutes=following_buffer,
                reason=f"Conflicts with next job at {next_str}.",
            )
        if following_buffer < 15:
            warnings.append(f"Tight: {following_buffer}min before next job")
        buffer_minutes = min(buffer_minutes, following_buffer)

    return FeasibilityResult(
        feasible=True,
        buffer_minutes=buffer_minutes,
        warnings=warnings,
        reason=f"{buffer_minutes}min buffer",
    )


def build_driver_schedules(legs, drivers, target_date: date) -> Dict[int, DriverDaySchedule]:
    """
    Build a DriverDaySchedule for each driver from the day's assigned legs.
    """
    from dispatching.analytics import categorize_location

    schedules = {}
    for driver in drivers:
        schedules[driver.id] = DriverDaySchedule(
            driver_id=driver.id,
            driver_name=str(driver),
            driver_type=driver.driver_type,
        )

    for leg in legs:
        if not leg.driver or leg.driver.id not in schedules:
            continue

        pickup_cat = categorize_location(leg.pickup_location)
        dropoff_cat = categorize_location(leg.dropoff_location)
        end_time = estimate_job_end_time(leg, target_date)

        customer_name = ""
        if leg.reservation and leg.reservation.customer:
            customer_name = leg.reservation.customer.get_full_name()

        flight_info = None
        has_flight = bool(leg.flight_information_id if hasattr(leg, 'flight_information_id') else False)
        try:
            if leg.flight_information:
                has_flight = True
                flight_info = str(leg.flight_information)
        except Exception:
            pass

        slot = ScheduleSlot(
            leg_id=leg.id,
            pickup_time=leg.pickup_time,
            pickup_location=leg.pickup_location,
            pickup_category=pickup_cat,
            dropoff_location=leg.dropoff_location,
            dropoff_category=dropoff_cat,
            trip_type=leg.get_trip_type(),
            estimated_end_time=end_time,
            reservation_id=leg.reservation_id,
            customer_name=customer_name,
            status=leg.status or 'in-progress',
            has_flight=has_flight,
            flight_info=flight_info,
            revenue=leg.revenue_share,
        )
        schedules[leg.driver.id].slots.append(slot)

    for schedule in schedules.values():
        schedule.slots.sort(key=lambda s: s.pickup_time)

    return schedules


def suggest_assignments(
    unassigned_legs,
    inhouse_schedules: Dict[int, DriverDaySchedule],
    target_date: date,
) -> List[AssignmentSuggestion]:
    """
    Greedy algorithm: assign unassigned legs to best-fit in-house drivers.
    Legs that don't fit any in-house driver are marked for affiliate.
    """
    from dispatching.analytics import categorize_location

    suggestions = []
    sorted_legs = sorted(unassigned_legs, key=lambda l: l.pickup_time)

    # Work on copies so we can simulate assignments
    working = {}
    for did, sched in inhouse_schedules.items():
        working[did] = DriverDaySchedule(
            driver_id=sched.driver_id,
            driver_name=sched.driver_name,
            driver_type=sched.driver_type,
            slots=list(sched.slots),
        )

    for leg in sorted_legs:
        best_id = None
        best_score = -1
        best_feasibility = None
        pickup_cat = categorize_location(leg.pickup_location)

        for did, sched in working.items():
            feas = check_feasibility(sched, leg, target_date)
            if not feas.feasible:
                continue

            score = 0
            buf = feas.buffer_minutes

            # Score by buffer quality
            if 20 <= buf <= 60:
                score += 100
            elif 10 <= buf < 20:
                score += 70
            elif 60 < buf <= 120:
                score += 80
            elif buf > 120:
                score += 50
            else:
                score += 30

            # Location proximity bonus
            if sched.slots:
                last = sched.slots[-1]
                if last.dropoff_category == pickup_cat:
                    score += 50
                else:
                    repo = DRIVE_TIME_ESTIMATES.get((last.dropoff_category, pickup_cat))
                    if repo and repo <= 15:
                        score += 30
            else:
                score += 40

            # Load balance: fewer legs = higher score
            score -= len(sched.slots) * 5

            if score > best_score:
                best_score = score
                best_id = did
                best_feasibility = feas

        if best_id:
            same_area = ""
            if working[best_id].slots and working[best_id].slots[-1].dropoff_category == pickup_cat:
                same_area = " (same area)"

            suggestion = AssignmentSuggestion(
                leg_id=leg.id,
                suggested_driver_id=best_id,
                suggested_driver_name=working[best_id].driver_name,
                feasibility=best_feasibility,
                reason=best_feasibility.reason + same_area if best_feasibility.buffer_minutes < 999 else f"Available{same_area}",
                priority=1,
            )

            # Simulate the assignment
            dropoff_cat = categorize_location(leg.dropoff_location)
            end_time = estimate_job_end_time(leg, target_date)
            sim_slot = ScheduleSlot(
                leg_id=leg.id,
                pickup_time=leg.pickup_time,
                pickup_location=leg.pickup_location,
                pickup_category=pickup_cat,
                dropoff_location=leg.dropoff_location,
                dropoff_category=dropoff_cat,
                trip_type=leg.get_trip_type(),
                estimated_end_time=end_time,
                reservation_id=leg.reservation_id,
                customer_name="",
                status=leg.status or 'in-progress',
                has_flight=False,
                revenue=leg.revenue_share,
            )
            working[best_id].slots.append(sim_slot)
            working[best_id].slots.sort(key=lambda s: s.pickup_time)
        else:
            suggestion = AssignmentSuggestion(
                leg_id=leg.id,
                suggested_driver_id=None,
                suggested_driver_name=None,
                feasibility=None,
                reason="No in-house driver available",
                priority=0,
            )

        suggestions.append(suggestion)

    return suggestions


def find_batching_opportunities(legs, target_date: date) -> List[BatchingOpportunity]:
    """
    Find legs in the same location category within 30 min that could be batched.
    """
    from dispatching.analytics import categorize_location

    opportunities = []
    seen = set()

    by_category = {}
    for leg in legs:
        cat = categorize_location(leg.pickup_location)
        by_category.setdefault(cat, []).append(leg)

    for category, cat_legs in by_category.items():
        if len(cat_legs) < 2:
            continue

        cat_legs.sort(key=lambda l: l.pickup_time)

        for i, leg_a in enumerate(cat_legs):
            group = [leg_a]
            for j in range(i + 1, len(cat_legs)):
                leg_b = cat_legs[j]
                dt_a = datetime.combine(target_date, leg_a.pickup_time)
                dt_b = datetime.combine(target_date, leg_b.pickup_time)
                if abs((dt_b - dt_a).total_seconds()) / 60 <= 30:
                    if not (leg_a.driver and leg_b.driver and leg_a.driver_id == leg_b.driver_id):
                        group.append(leg_b)
                else:
                    break

            if len(group) >= 2:
                key = frozenset(l.id for l in group)
                if key not in seen:
                    seen.add(key)
                    opportunities.append(BatchingOpportunity(
                        legs=[{
                            'id': l.id,
                            'pickup_time': l.pickup_time,
                            'pickup_location': l.pickup_location,
                            'dropoff_location': l.dropoff_location,
                            'driver_name': str(l.driver) if l.driver else 'Unassigned',
                            'trip_type': l.get_trip_type(),
                        } for l in group],
                        location_category=category,
                        time_window_start=group[0].pickup_time,
                        time_window_end=group[-1].pickup_time,
                        reason=f"{len(group)} jobs at {category} within 30min",
                    ))

    return opportunities


def build_smart_schedule(
    driver_id: int,
    driver_name: str,
    available_legs,
    target_date: date,
    start_hour: int = 0,
    end_hour: int = 23,
    pinned_leg_ids: List[int] = None,
    preferred_trip_type: str = None,
    existing_schedule: DriverDaySchedule = None,
    excluded_leg_ids: List[int] = None,
) -> dict:
    """
    Build an optimal schedule for a single driver within a time window.

    Parameters:
        driver_id: The driver to build the schedule for
        driver_name: Display name
        available_legs: Queryset/list of unassigned legs on this date
        target_date: The date being planned
        start_hour: Earliest hour the driver is available (0-23)
        end_hour: Latest hour the driver should finish by (0-23)
        pinned_leg_ids: Leg IDs that MUST be included (e.g., a cruise the dispatcher wants this driver to have)
        preferred_trip_type: If set, prefer legs of this type ('arrival', 'return', 'cruise', 'other')
        existing_schedule: If the driver already has assigned jobs, pass them here

    Returns:
        {
            'driver_id': int,
            'driver_name': str,
            'schedule': [ScheduleSlot, ...],
            'pinned_included': [int, ...],  # which pinned legs were included
            'pinned_failed': [int, ...],    # which pinned legs couldn't fit
            'total_legs': int,
            'total_revenue': Decimal,
            'utilization_pct': float,
            'warnings': [str, ...],
        }
    """
    from dispatching.analytics import categorize_location

    pinned_leg_ids = pinned_leg_ids or []
    excluded_leg_ids = excluded_leg_ids or []
    warnings = []
    # Track timing details for each newly added slot: leg_id -> {prev_dropoff, drive_time, buffer, ...}
    slot_timing_details = {}

    # Build working schedule (start with existing assignments)
    working = DriverDaySchedule(
        driver_id=driver_id,
        driver_name=driver_name,
        driver_type='inhouse',
        slots=list(existing_schedule.slots) if existing_schedule else [],
    )

    # Time window boundaries
    window_start = time(start_hour, 0)
    window_end = time(end_hour, 59)

    # Filter available legs: within time window AND not excluded
    window_legs = [
        leg for leg in available_legs
        if window_start <= leg.pickup_time <= window_end and leg.id not in excluded_leg_ids
    ]

    # Separate pinned legs (must-do) from optional legs
    pinned_legs = [l for l in window_legs if l.id in pinned_leg_ids]
    optional_legs = [l for l in window_legs if l.id not in pinned_leg_ids]

    # Also check pinned legs outside window (user pinned something at 6 AM but window is 8-16)
    for leg in available_legs:
        if leg.id in pinned_leg_ids and leg.id not in excluded_leg_ids and leg not in pinned_legs:
            pinned_legs.append(leg)
            if not (window_start <= leg.pickup_time <= window_end):
                warnings.append(f"Pinned leg #{leg.id} ({leg.pickup_time.strftime('%I:%M %p').lstrip('0')}) is outside the {start_hour}:00-{end_hour}:00 window")

    pinned_included = []
    pinned_failed = []

    # Step 1: Insert pinned legs first (they're mandatory)
    pinned_sorted = sorted(pinned_legs, key=lambda l: l.pickup_time)
    for leg in pinned_sorted:
        feas = check_feasibility(working, leg, target_date)
        if feas.feasible:
            timing = _capture_timing_details(working, leg, target_date)
            _add_leg_to_schedule(working, leg, target_date)
            slot_timing_details[leg.id] = timing
            pinned_included.append(leg.id)
        else:
            pinned_failed.append(leg.id)
            warnings.append(f"Pinned leg #{leg.id} at {leg.pickup_time.strftime('%I:%M %p').lstrip('0')} doesn't fit: {feas.reason}")

    # Step 2: Fill remaining slots with optional legs, scored by preference
    optional_sorted = sorted(optional_legs, key=lambda l: l.pickup_time)

    for leg in optional_sorted:
        # Check if within time window
        if not (window_start <= leg.pickup_time <= window_end):
            continue

        # Check if driver would finish before window end
        est_end = estimate_job_end_time(leg, target_date)
        if est_end.hour > end_hour + 1:  # allow 1 hour grace for last job
            continue

        feas = check_feasibility(working, leg, target_date)
        if not feas.feasible:
            continue

        # Score this leg
        score = _score_leg_for_smart_schedule(
            leg, working, feas, preferred_trip_type, target_date
        )

        if score > 0:
            timing = _capture_timing_details(working, leg, target_date)
            _add_leg_to_schedule(working, leg, target_date)
            slot_timing_details[leg.id] = timing

    # Calculate utilization
    total_window_minutes = (end_hour - start_hour) * 60
    active_minutes = 0
    for slot in working.slots:
        pickup_dt = datetime.combine(target_date, slot.pickup_time)
        duration = (slot.estimated_end_time - pickup_dt).total_seconds() / 60
        active_minutes += duration

    utilization = round(active_minutes / total_window_minutes * 100, 1) if total_window_minutes > 0 else 0

    return {
        'driver_id': driver_id,
        'driver_name': driver_name,
        'schedule': working.slots,
        'pinned_included': pinned_included,
        'pinned_failed': pinned_failed,
        'total_legs': len(working.slots),
        'total_revenue': working.total_revenue,
        'utilization_pct': utilization,
        'warnings': warnings,
        'existing_count': len(existing_schedule.slots) if existing_schedule else 0,
        'new_count': len(working.slots) - (len(existing_schedule.slots) if existing_schedule else 0),
        'slot_timing_details': slot_timing_details,
    }


def _capture_timing_details(schedule: DriverDaySchedule, new_leg, target_date: date) -> dict:
    """
    Capture the timing reasoning for adding this leg to the schedule.
    Returns dict with drive time, buffer, and route info so the user can see
    why the algorithm chose this job and spot incorrect drive time estimates.
    """
    from dispatching.analytics import categorize_location

    new_pickup_cat = categorize_location(new_leg.pickup_location)
    new_dropoff_cat = categorize_location(new_leg.dropoff_location)
    drive_time = get_drive_time(new_pickup_cat, new_dropoff_cat)
    est_end = estimate_job_end_time(new_leg, target_date)

    details = {
        'pickup_category': new_pickup_cat,
        'dropoff_category': new_dropoff_cat,
        'job_drive_time': drive_time,
        'est_end_time': est_end.strftime('%I:%M %p').lstrip('0'),
    }

    if not schedule.slots:
        details['prev_job'] = None
        details['reposition_from'] = None
        details['reposition_drive_time'] = None
        details['buffer_minutes'] = None
        details['reasoning'] = f"First job. Drive: {new_pickup_cat} → {new_dropoff_cat} ({drive_time} min)"
    else:
        # Find the preceding slot
        new_pickup_dt = datetime.combine(target_date, new_leg.pickup_time)
        sorted_slots = sorted(schedule.slots, key=lambda s: s.pickup_time)
        preceding = None
        for slot in sorted_slots:
            if datetime.combine(target_date, slot.pickup_time) <= new_pickup_dt:
                preceding = slot

        if preceding:
            repo_drive = get_drive_time(preceding.dropoff_category, new_pickup_cat)
            earliest = preceding.estimated_end_time + timedelta(minutes=repo_drive + INTER_JOB_BUFFER)
            buffer = int((new_pickup_dt - earliest).total_seconds() / 60)

            details['prev_job'] = {
                'leg_id': preceding.leg_id,
                'end_time': preceding.estimated_end_time.strftime('%I:%M %p').lstrip('0'),
                'dropoff_category': preceding.dropoff_category,
            }
            details['reposition_from'] = preceding.dropoff_category
            details['reposition_to'] = new_pickup_cat
            details['reposition_drive_time'] = repo_drive
            details['buffer_minutes'] = buffer
            details['reasoning'] = (
                f"Prev job ends ~{preceding.estimated_end_time.strftime('%I:%M %p').lstrip('0')} "
                f"at {preceding.dropoff_category}. "
                f"Reposition: {preceding.dropoff_category} → {new_pickup_cat} ({repo_drive} min) "
                f"+ {INTER_JOB_BUFFER} min buffer = {buffer} min spare. "
                f"Job drive: {new_pickup_cat} → {new_dropoff_cat} ({drive_time} min)"
            )
        else:
            details['prev_job'] = None
            details['reposition_from'] = None
            details['reposition_drive_time'] = None
            details['buffer_minutes'] = None
            details['reasoning'] = f"First job in window. Drive: {new_pickup_cat} → {new_dropoff_cat} ({drive_time} min)"

    return details


def _score_leg_for_smart_schedule(
    leg, schedule: DriverDaySchedule, feasibility: FeasibilityResult,
    preferred_trip_type: str, target_date: date
) -> int:
    """Score a leg for smart schedule insertion. Higher = better fit."""
    from dispatching.analytics import categorize_location

    score = 50  # base score

    # Trip type preference bonus
    trip_type = leg.get_trip_type()
    if preferred_trip_type and trip_type == preferred_trip_type:
        score += 40
    elif preferred_trip_type and trip_type != preferred_trip_type:
        score -= 10  # still include, just lower priority

    # Buffer quality (sweet spot 20-60 min)
    buf = feasibility.buffer_minutes
    if 20 <= buf <= 60:
        score += 30
    elif 10 <= buf < 20:
        score += 15
    elif 60 < buf <= 120:
        score += 20
    elif buf >= 999:
        score += 25  # first job, good

    # Location proximity bonus
    if schedule.slots:
        pickup_cat = categorize_location(leg.pickup_location)
        last = schedule.slots[-1]
        if last.dropoff_category == pickup_cat:
            score += 35  # same area = very efficient

    # Revenue bonus
    if leg.revenue_share and leg.revenue_share > 0:
        score += min(int(leg.revenue_share / 10), 20)  # up to 20 bonus pts

    return score


def _add_leg_to_schedule(schedule: DriverDaySchedule, leg, target_date: date):
    """Helper to add a leg to a working schedule."""
    from dispatching.analytics import categorize_location

    pickup_cat = categorize_location(leg.pickup_location)
    dropoff_cat = categorize_location(leg.dropoff_location)
    end_time = estimate_job_end_time(leg, target_date)

    customer_name = ""
    if leg.reservation and leg.reservation.customer:
        customer_name = leg.reservation.customer.get_full_name()

    slot = ScheduleSlot(
        leg_id=leg.id,
        pickup_time=leg.pickup_time,
        pickup_location=leg.pickup_location,
        pickup_category=pickup_cat,
        dropoff_location=leg.dropoff_location,
        dropoff_category=dropoff_cat,
        trip_type=leg.get_trip_type(),
        estimated_end_time=end_time,
        reservation_id=leg.reservation_id,
        customer_name=customer_name,
        status=leg.status or 'in-progress',
        has_flight=False,
        revenue=leg.revenue_share,
    )
    schedule.slots.append(slot)
    schedule.slots.sort(key=lambda s: s.pickup_time)


def update_drive_time_estimate(from_cat: str, to_cat: str, minutes: int) -> bool:
    """
    Update a drive time estimate in DRIVE_TIME_ESTIMATES.
    Updates both directions.
    Returns True if updated.
    """
    DRIVE_TIME_ESTIMATES[(from_cat, to_cat)] = minutes
    DRIVE_TIME_ESTIMATES[(to_cat, from_cat)] = minutes
    return True


def get_all_drive_time_categories() -> List[str]:
    """Return all unique location categories used in drive time estimates."""
    cats = set()
    for (a, b) in DRIVE_TIME_ESTIMATES.keys():
        cats.add(a)
        cats.add(b)
    return sorted(cats)


def get_coverage_stats(legs) -> dict:
    """Calculate in-house vs affiliate vs unassigned coverage."""
    stats = {
        'total': 0,
        'inhouse': 0,
        'affiliate': 0,
        'unassigned': 0,
        'inhouse_pct': 0,
        'affiliate_pct': 0,
        'unassigned_pct': 0,
        'inhouse_revenue': Decimal('0.00'),
        'affiliate_revenue': Decimal('0.00'),
        'unassigned_revenue': Decimal('0.00'),
        'total_revenue': Decimal('0.00'),
    }

    for leg in legs:
        stats['total'] += 1
        rev = leg.revenue_share or Decimal('0.00')
        stats['total_revenue'] += rev

        if not leg.driver:
            stats['unassigned'] += 1
            stats['unassigned_revenue'] += rev
        elif leg.driver.driver_type == 'inhouse':
            stats['inhouse'] += 1
            stats['inhouse_revenue'] += rev
        else:
            stats['affiliate'] += 1
            stats['affiliate_revenue'] += rev

    total = stats['total']
    if total > 0:
        stats['inhouse_pct'] = round(stats['inhouse'] / total * 100)
        stats['affiliate_pct'] = round(stats['affiliate'] / total * 100)
        stats['unassigned_pct'] = round(stats['unassigned'] / total * 100)

    return stats
