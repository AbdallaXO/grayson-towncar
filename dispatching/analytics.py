"""
Analytics calculation functions for scheduling optimization.

This module provides functions to:
- Categorize locations and times
- Calculate timing metrics from historical data
- Aggregate driver performance data
- Generate demand patterns

Used to populate RouteTimingMetric, DriverDailyCapacity, and DemandPattern models.
"""

from datetime import date, datetime, timedelta, time
from decimal import Decimal
from typing import Optional, Dict, List, Tuple
from django.db.models import Avg, Count, Q, Sum, F
from django.utils import timezone
import statistics
import holidays


# ============================================================================
# OUTLIER THRESHOLDS (minutes)
# ============================================================================

MAX_DWELL_MINUTES = 120   # Discard dwell > 2 hours
MAX_DRIVE_MINUTES = 240   # Discard drive > 4 hours
MAX_TOTAL_MINUTES = 360   # Discard total > 6 hours

# IQR multiplier for outlier detection (1.5 = standard, 2.0 = lenient)
IQR_MULTIPLIER = 1.5
# Minimum samples needed before applying IQR filtering
IQR_MIN_SAMPLES = 5


def iqr_filter(values: list, multiplier: float = IQR_MULTIPLIER) -> list:
    """
    Remove outliers using the Interquartile Range (IQR) method.

    Values outside [Q1 - multiplier*IQR, Q3 + multiplier*IQR] are removed.
    With multiplier=1.5 this is the standard Tukey fence.

    Returns filtered list. If fewer than IQR_MIN_SAMPLES, returns original
    list unchanged (not enough data for meaningful IQR).
    """
    if len(values) < IQR_MIN_SAMPLES:
        return values

    sorted_vals = sorted(values)
    q1, _, q3 = statistics.quantiles(sorted_vals, n=4)
    iqr = q3 - q1

    lower = q1 - multiplier * iqr
    upper = q3 + multiplier * iqr

    return [v for v in values if lower <= v <= upper]


def adaptive_max(values: list, fallback_max: int) -> int:
    """
    Compute a tighter per-route max using P90 * 1.5.

    If we have 10+ samples, use P90 * 1.5 as the max instead of
    the blanket global max. This catches realistic-but-wrong values
    (e.g. 45 min drive on a route that's normally 28-32 min).

    Returns the tighter of (P90 * 1.5) or fallback_max.
    """
    if len(values) < 10:
        return fallback_max
    p90 = statistics.quantiles(sorted(values), n=10)[8]
    return min(int(p90 * 1.5), fallback_max)


# ============================================================================
# LOCATION CATEGORIZATION
# ============================================================================

def categorize_location(location_text: str) -> str:
    """
    Categorize free-text location into standard categories for analytics.

    Args:
        location_text: Raw location string from leg pickup/dropoff

    Returns:
        Category string: 'MCO Terminal', 'SFB Terminal', 'Airport Hotel',
        'Disney Resort', 'Universal Resort', 'Port Canaveral Area',
        'Other Hotel', 'Residential', 'Other'
    """
    if not location_text:
        return 'Other'

    location_lower = location_text.lower()

    # Airport HOTELS (not terminals) - check first before terminal keywords
    airport_hotel_keywords = [
        'hyatt regency orlando airport', 'hyatt regency mco', 'hyatt place orlando airport',
        'marriott orlando airport', 'courtyard orlando airport', 'embassy suites orlando airport',
        'hampton inn orlando airport', 'holiday inn orlando airport', 'la quinta orlando airport',
        'springhill suites orlando airport', 'fairfield inn orlando airport',
        'homewood suites orlando airport', 'hilton garden inn orlando airport',
        'airport hotel', 'hotel near mco', 'hotel near airport'
    ]
    if any(keyword in location_lower for keyword in airport_hotel_keywords):
        return 'Airport Hotel'

    # Airport TERMINALS (actual pickup/dropoff at terminals)
    mco_terminal_keywords = ['terminal a', 'terminal b', 'terminal c', 'gate ', 'baggage claim']
    if any(keyword in location_lower for keyword in mco_terminal_keywords):
        return 'MCO Terminal'
    # If "mco" or "orlando international" but NOT an airport hotel
    if any(keyword in location_lower for keyword in ['mco', 'orlando international airport', 'orlando airport']):
        # Double check it's not an airport hotel
        if not any(hotel_kw in location_lower for hotel_kw in ['hotel', 'inn', 'suites', 'resort']):
            return 'MCO Terminal'

    # Airline names (people write "Spirit Airlines" meaning MCO pickup)
    airline_keywords = [
        'spirit airlines', 'jetblue', 'jet blue', 'southwest airlines', 'delta airlines',
        'united airlines', 'american airlines', 'frontier airlines', 'allegiant',
        'sun country', 'breeze airways', 'avelo'
    ]
    if any(keyword in location_lower for keyword in airline_keywords):
        return 'MCO Terminal'

    # Sanford Airport Terminal
    sfb_terminal_keywords = ['sfb', 'sanford international', 'orlando sanford airport', 'sanford airport', 'sanford intl']
    if any(keyword in location_lower for keyword in sfb_terminal_keywords):
        return 'SFB Terminal'
    # Catch "sanford" + airport-related context
    if 'sanford' in location_lower and any(kw in location_lower for kw in ['airport', 'intl', 'airline']):
        return 'SFB Terminal'

    # Port Canaveral & Cocoa Beach Area
    port_keywords = [
        'port canaveral', 'canaveral', 'cruise port', 'cruise terminal',
        'cocoa beach', 'cape canaveral', 'cove terminal', 'cruise ship',
        'jetty park', 'cocoa', 'brevard'
    ]
    if any(keyword in location_lower for keyword in port_keywords):
        return 'Port Canaveral Area'

    # Disney properties
    disney_keywords = [
        'disney', 'epcot', 'magic kingdom', 'hollywood studios', 'animal kingdom',
        'contemporary', 'polynesian', 'grand floridian', 'yacht club', 'beach club',
        'boardwalk', 'swan', 'dolphin', 'coronado', 'caribbean beach', 'pop century',
        'art of animation', 'all-star', 'port orleans', 'saratoga springs', 'old key west',
        'wilderness lodge', 'fort wilderness', 'riviera resort', 'disney springs'
    ]
    if any(keyword in location_lower for keyword in disney_keywords):
        return 'Disney Resort'

    # Universal properties
    universal_keywords = [
        'universal', 'loews', 'portofino', 'hard rock', 'royal pacific', 'cabana bay',
        'sapphire falls', 'aventura', 'endless summer', 'surfside', 'dockside',
        'universal studios', 'islands of adventure', 'volcano bay', 'citywalk'
    ]
    if any(keyword in location_lower for keyword in universal_keywords):
        return 'Universal Resort'

    # Other hotels (not Disney, Universal, or Airport hotels)
    hotel_keywords = [
        'hotel', 'resort', 'inn', 'suites', 'marriott', 'hilton', 'hyatt', 'sheraton',
        'radisson', 'holiday inn', 'best western', 'comfort', 'hampton', 'embassy',
        'rosen', 'wyndham', 'doubletree', 'renaissance', 'courtyard'
    ]
    if any(keyword in location_lower for keyword in hotel_keywords):
        return 'Other Hotel'

    # Residential indicators
    residential_keywords = [
        ' drive', ' street', ' road', ' lane', ' avenue', ' circle', ' court', ' way',
        ' blvd', ' dr', ' st', ' rd', ' ln', ' ave', ' cir', ' ct',
        'private residence', 'home', 'house', 'apartment', 'condo'
    ]
    if any(keyword in location_lower for keyword in residential_keywords):
        return 'Residential'

    return 'Other'


# ============================================================================
# TIME CATEGORIZATION
# ============================================================================

def categorize_time_of_day(time_obj) -> str:
    """
    Categorize time into scheduling-relevant buckets.

    Args:
        time_obj: datetime.time or datetime.datetime object

    Returns:
        Category string: 'early_morning', 'morning_rush', 'midday',
        'afternoon', 'evening', 'night'
    """
    if isinstance(time_obj, datetime):
        time_obj = time_obj.time()

    if not isinstance(time_obj, time):
        return 'other'

    hour = time_obj.hour

    if 4 <= hour < 7:
        return 'early_morning'
    elif 7 <= hour < 10:
        return 'morning_rush'
    elif 10 <= hour < 14:
        return 'midday'
    elif 14 <= hour < 18:
        return 'afternoon'
    elif 18 <= hour < 22:
        return 'evening'
    else:  # 22-4
        return 'night'


def categorize_day_type(date_obj) -> str:
    """
    Categorize day as weekday/weekend/holiday.

    Args:
        date_obj: datetime.date or datetime.datetime object

    Returns:
        Category string: 'weekday', 'weekend', 'holiday'
    """
    if isinstance(date_obj, datetime):
        date_obj = date_obj.date()

    # Check if it's a US holiday
    us_holidays = holidays.US(years=date_obj.year)
    if date_obj in us_holidays:
        return 'holiday'

    # Check if weekend
    if date_obj.weekday() >= 5:  # Saturday=5, Sunday=6
        return 'weekend'

    return 'weekday'


# ============================================================================
# CORE TIMING CALCULATIONS
# ============================================================================

def calculate_airport_dwell_time(leg) -> Optional[int]:
    """
    For arrival legs with flight info: calculate minutes from actual/estimated
    gate arrival to when status changed to 'picked-up'.

    NOTE: Since LegStatus tracking is recent, this may return None for most
    historical legs until more timestamp data accumulates.

    Args:
        leg: Leg instance

    Returns:
        Minutes (int) or None if data incomplete
    """
    from reservations.models import LegStatus

    # Only for arrival legs
    if leg.get_trip_type() != 'arrival':
        return None

    # Need flight information
    if not leg.flight_information:
        return None

    flight = leg.flight_information

    # Get best available arrival time (prioritize actual over estimated over scheduled)
    gate_arrival = (
        flight.actual_gate_arrival_local
        or flight.estimated_gate_arrival_local
        or flight.scheduled_gate_arrival_local
    )

    if not gate_arrival:
        return None

    # Get picked-up timestamp from status history
    # Use Python filtering to avoid N+1 when status_history is prefetched
    picked_up_status = None
    if hasattr(leg, '_prefetched_objects_cache') and 'status_history' in leg._prefetched_objects_cache:
        for s in leg.status_history.all():
            if s.status == 'picked-up':
                picked_up_status = s
                break
    else:
        picked_up_status = leg.status_history.filter(status='picked-up').first()
    if not picked_up_status:
        return None  # No timestamp data available

    # Calculate time difference
    if timezone.is_aware(gate_arrival):
        gate_arrival = timezone.make_naive(gate_arrival, timezone.get_current_timezone())

    picked_up_time = picked_up_status.timestamp
    if timezone.is_aware(picked_up_time):
        picked_up_time = timezone.make_naive(picked_up_time, timezone.get_current_timezone())

    # Calculate minutes
    delta = picked_up_time - gate_arrival
    minutes = int(delta.total_seconds() / 60)

    # Sanity check: dwell time should be positive and reasonable (< 5 hours)
    if minutes < 0 or minutes > 300:
        return None

    return minutes


def calculate_drive_time(leg) -> Optional[int]:
    """
    Calculate minutes from 'picked-up' status to 'completed' status.
    Uses LegStatus history.

    NOTE: Since LegStatus tracking is recent, this may return None for most
    historical legs until more timestamp data accumulates.

    Args:
        leg: Leg instance

    Returns:
        Minutes (int) or None if data incomplete
    """
    from reservations.models import LegStatus

    # Get picked-up and completed timestamps
    # Use Python filtering to avoid N+1 when status_history is prefetched
    picked_up_status = None
    completed_status = None
    if hasattr(leg, '_prefetched_objects_cache') and 'status_history' in leg._prefetched_objects_cache:
        for s in leg.status_history.all():
            if s.status == 'picked-up' and picked_up_status is None:
                picked_up_status = s
            elif s.status == 'completed' and completed_status is None:
                completed_status = s
    else:
        picked_up_status = leg.status_history.filter(status='picked-up').first()
        completed_status = leg.status_history.filter(status='completed').first()

    if not picked_up_status or not completed_status:
        return None  # No timestamp data available

    # Calculate time difference
    picked_up_time = picked_up_status.timestamp
    completed_time = completed_status.timestamp

    if timezone.is_aware(picked_up_time):
        picked_up_time = timezone.make_naive(picked_up_time, timezone.get_current_timezone())
    if timezone.is_aware(completed_time):
        completed_time = timezone.make_naive(completed_time, timezone.get_current_timezone())

    delta = completed_time - picked_up_time
    minutes = int(delta.total_seconds() / 60)

    # Sanity check: drive time should be positive and reasonable (< 4 hours)
    if minutes < 0 or minutes > 240:
        return None

    return minutes


def calculate_turnaround_time(leg1, leg2) -> Optional[int]:
    """
    Calculate minutes from leg1 completion to leg2 pickup.
    Represents driver availability gap between jobs.

    NOTE: Since LegStatus tracking is recent, this may return None for most
    historical legs until more timestamp data accumulates.

    Args:
        leg1: First Leg instance
        leg2: Second Leg instance

    Returns:
        Minutes (int) or None if legs don't connect or data incomplete
    """
    from reservations.models import LegStatus

    # Must be same driver
    if not leg1.driver or not leg2.driver or leg1.driver != leg2.driver:
        return None

    # Get leg1 completed timestamp (may not exist for older legs)
    leg1_completed = leg1.status_history.filter(status='completed').first()
    if not leg1_completed:
        return None  # No timestamp data available

    # Get leg2 picked-up timestamp (or next available status)
    leg2_pickup = leg2.status_history.filter(
        status__in=['picked-up', 'on-location', 'on-the-way']
    ).order_by('timestamp').first()

    if not leg2_pickup:
        return None  # No timestamp data available

    # Calculate time difference
    leg1_time = leg1_completed.timestamp
    leg2_time = leg2_pickup.timestamp

    if timezone.is_aware(leg1_time):
        leg1_time = timezone.make_naive(leg1_time, timezone.get_current_timezone())
    if timezone.is_aware(leg2_time):
        leg2_time = timezone.make_naive(leg2_time, timezone.get_current_timezone())

    delta = leg2_time - leg1_time
    minutes = int(delta.total_seconds() / 60)

    # Sanity check: turnaround should be positive and reasonable (< 12 hours)
    if minutes < 0 or minutes > 720:
        return None

    return minutes


# ============================================================================
# AGGREGATION FUNCTIONS
# ============================================================================

def _safe_percentile(data: list, n: int, index: int, min_samples: int) -> Optional[int]:
    """Calculate a percentile safely, returning None if not enough data."""
    if len(data) < min_samples:
        return None
    return int(statistics.quantiles(data, n=n)[index])


def calculate_route_timing_metrics(
    trip_type: str,
    pickup_cat: str,
    dropoff_cat: str,
    time_of_day: str,
    day_type: str,
    legs_queryset=None,
) -> Dict:
    """
    Aggregate all historical legs matching criteria and calculate timing metrics.

    Args:
        trip_type: 'arrival', 'return', 'cruise', 'other'
        pickup_cat: Pickup location category
        dropoff_cat: Dropoff location category
        time_of_day: Time of day category
        day_type: Day type category
        legs_queryset: Optional pre-filtered queryset (avoids re-fetching all legs)

    Returns:
        dict with calculated metrics including avg/median/p75/p90 for
        dwell, drive, and total times, plus sample_count.
    """
    from reservations.models import Leg

    # Use provided queryset or fetch all completed legs
    if legs_queryset is None:
        legs_queryset = Leg.objects.filter(
            status='completed'
        ).prefetch_related('status_history', 'flight_information')

    # Filter by bucket criteria (in-memory)
    matching_legs = []
    for leg in legs_queryset:
        if leg.get_trip_type() != trip_type:
            continue
        if categorize_location(leg.pickup_location) != pickup_cat:
            continue
        if categorize_location(leg.dropoff_location) != dropoff_cat:
            continue
        if categorize_time_of_day(leg.pickup_time) != time_of_day:
            continue
        if categorize_day_type(leg.pickup_date) != day_type:
            continue
        matching_legs.append(leg)

    empty_result = {
        'avg_airport_dwell_time': None,
        'median_airport_dwell_time': None,
        'p75_airport_dwell_time': None,
        'p90_airport_dwell_time': None,
        'avg_drive_time': None,
        'median_drive_time': None,
        'p75_drive_time': None,
        'p90_drive_time': None,
        'avg_total_time': None,
        'median_total_time': None,
        'p75_total_time': None,
        'p90_total_time': None,
        'sample_count': 0,
    }

    if not matching_legs:
        return empty_result

    # Collect raw durations per leg, with outlier filtering
    dwell_times = []
    drive_times = []
    total_times = []

    for leg in matching_legs:
        dwell = calculate_airport_dwell_time(leg) if trip_type == 'arrival' else None
        drive = calculate_drive_time(leg)

        # Hard threshold filter
        if dwell is not None and (dwell < 0 or dwell > MAX_DWELL_MINUTES):
            dwell = None
        if drive is not None and (drive < 0 or drive > MAX_DRIVE_MINUTES):
            drive = None

        if dwell is not None:
            dwell_times.append(dwell)
        if drive is not None:
            drive_times.append(drive)

        # Total time: dwell + drive for arrivals, drive for others
        if trip_type == 'arrival' and dwell is not None and drive is not None:
            total = dwell + drive
            if total <= MAX_TOTAL_MINUTES:
                total_times.append(total)
        elif drive is not None:
            if drive <= MAX_TOTAL_MINUTES:
                total_times.append(drive)

    # Adaptive per-route max (tighter than global if enough data)
    dwell_max = adaptive_max(dwell_times, MAX_DWELL_MINUTES)
    drive_max = adaptive_max(drive_times, MAX_DRIVE_MINUTES)
    dwell_times = [v for v in dwell_times if v <= dwell_max]
    drive_times = [v for v in drive_times if v <= drive_max]
    total_times = [v for v in total_times if v <= adaptive_max(total_times, MAX_TOTAL_MINUTES)]

    # IQR outlier removal
    dwell_times = iqr_filter(dwell_times)
    drive_times = iqr_filter(drive_times)
    total_times = iqr_filter(total_times)

    # Build result with percentile safety rules:
    #   len < 1 -> avg = None
    #   len < 2 -> median/p75/p90 = None
    #   len < 4 -> p75 = None
    #   len < 10 -> p90 = None
    result = dict(empty_result)
    result['sample_count'] = len(matching_legs)

    # Dwell stats (arrivals only)
    if dwell_times:
        result['avg_airport_dwell_time'] = int(statistics.mean(dwell_times))
        if len(dwell_times) >= 2:
            result['median_airport_dwell_time'] = int(statistics.median(dwell_times))
        result['p75_airport_dwell_time'] = _safe_percentile(dwell_times, 4, 2, 4)
        result['p90_airport_dwell_time'] = _safe_percentile(dwell_times, 10, 8, 10)

    # Drive stats
    if drive_times:
        result['avg_drive_time'] = int(statistics.mean(drive_times))
        if len(drive_times) >= 2:
            result['median_drive_time'] = int(statistics.median(drive_times))
        result['p75_drive_time'] = _safe_percentile(drive_times, 4, 2, 4)
        result['p90_drive_time'] = _safe_percentile(drive_times, 10, 8, 10)

    # Total stats
    if total_times:
        result['avg_total_time'] = int(statistics.mean(total_times))
        if len(total_times) >= 2:
            result['median_total_time'] = int(statistics.median(total_times))
        result['p75_total_time'] = _safe_percentile(total_times, 4, 2, 4)
        result['p90_total_time'] = _safe_percentile(total_times, 10, 8, 10)

    return result


def calculate_driver_daily_capacity_for_date(driver, target_date: date) -> Dict:
    """
    Analyze all legs for a driver on specific date and calculate capacity metrics.

    NOTE: Turnaround time metrics require LegStatus timestamps, which may not be
    available for older legs.

    Args:
        driver: Driver instance
        target_date: Date to analyze

    Returns:
        dict with calculated metrics:
        {
            'total_legs': int,
            'total_revenue': Decimal,
            'total_active_hours': Decimal,
            'avg_turnaround_time': int,
            'longest_gap_minutes': int,
            'arrival_count': int,
            'return_count': int,
            'cruise_count': int,
            'other_count': int,
        }
    """
    from reservations.models import Leg

    # Get all completed legs for this driver on this date
    legs = list(Leg.objects.filter(
        driver=driver,
        pickup_date=target_date,
        status='completed'
    ).prefetch_related('status_history').order_by('pickup_time'))

    if not legs:
        return {
            'total_legs': 0,
            'total_revenue': Decimal('0.00'),
            'total_active_hours': None,
            'avg_turnaround_time': None,
            'longest_gap_minutes': None,
            'arrival_count': 0,
            'return_count': 0,
            'cruise_count': 0,
            'other_count': 0,
        }

    # Count trip types
    trip_type_counts = {
        'arrival': 0,
        'return': 0,
        'cruise': 0,
        'other': 0,
    }
    for leg in legs:
        trip_type = leg.get_trip_type()
        trip_type_counts[trip_type] += 1

    # Calculate total revenue
    total_revenue = sum((leg.revenue_share or Decimal('0.00')) for leg in legs)

    # Calculate active hours (first pickup to last dropoff) - requires timestamps
    first_pickup = legs[0].status_history.filter(status='picked-up').first()
    last_completed = legs[-1].status_history.filter(status='completed').first()

    total_active_hours = None
    if first_pickup and last_completed:
        delta = last_completed.timestamp - first_pickup.timestamp
        total_active_hours = Decimal(str(delta.total_seconds() / 3600)).quantize(Decimal('0.01'))

    # Calculate turnaround times - requires timestamps
    turnaround_times = []
    for i in range(len(legs) - 1):
        turnaround = calculate_turnaround_time(legs[i], legs[i + 1])
        if turnaround is not None:
            turnaround_times.append(turnaround)

    avg_turnaround = int(statistics.mean(turnaround_times)) if turnaround_times else None
    longest_gap = max(turnaround_times) if turnaround_times else None

    return {
        'total_legs': len(legs),
        'total_revenue': total_revenue,
        'total_active_hours': total_active_hours,
        'avg_turnaround_time': avg_turnaround,
        'longest_gap_minutes': longest_gap,
        'arrival_count': trip_type_counts['arrival'],
        'return_count': trip_type_counts['return'],
        'cruise_count': trip_type_counts['cruise'],
        'other_count': trip_type_counts['other'],
    }


def calculate_demand_pattern_for_hour(target_date: date, hour: int) -> Dict:
    """
    Aggregate all legs with pickups in specified hour and calculate demand metrics.

    Args:
        target_date: Date to analyze
        hour: Hour of day (0-23)

    Returns:
        dict with calculated metrics:
        {
            'arrival_legs': int,
            'return_legs': int,
            'cruise_legs': int,
            'other_legs': int,
            'total_legs': int,
            'inhouse_drivers_used': int,
            'affiliate_drivers_used': int,
            'total_revenue': Decimal,
            'day_of_week': int,
        }
    """
    from reservations.models import Leg

    # Get all legs with pickup in this hour
    hour_start = time(hour, 0)
    hour_end = time(hour, 59, 59) if hour < 23 else time(23, 59, 59)

    legs = list(Leg.objects.filter(
        pickup_date=target_date,
        pickup_time__gte=hour_start,
        pickup_time__lt=hour_end
    ).select_related('driver', 'reservation'))

    if not legs:
        return {
            'arrival_legs': 0,
            'return_legs': 0,
            'cruise_legs': 0,
            'other_legs': 0,
            'total_legs': 0,
            'inhouse_drivers_used': 0,
            'affiliate_drivers_used': 0,
            'total_revenue': Decimal('0.00'),
            'day_of_week': target_date.weekday(),
        }

    # Count trip types
    trip_type_counts = {
        'arrival': 0,
        'return': 0,
        'cruise': 0,
        'other': 0,
    }
    for leg in legs:
        trip_type = leg.get_trip_type()
        trip_type_counts[trip_type] += 1

    # Count driver types
    inhouse_count = 0
    affiliate_count = 0
    for leg in legs:
        if leg.driver:
            if leg.driver.driver_type == 'inhouse':
                inhouse_count += 1
            else:
                affiliate_count += 1

    # Calculate total revenue
    total_revenue = sum((leg.revenue_share or Decimal('0.00')) for leg in legs)

    return {
        'arrival_legs': trip_type_counts['arrival'],
        'return_legs': trip_type_counts['return'],
        'cruise_legs': trip_type_counts['cruise'],
        'other_legs': trip_type_counts['other'],
        'total_legs': len(legs),
        'inhouse_drivers_used': inhouse_count,
        'affiliate_drivers_used': affiliate_count,
        'total_revenue': total_revenue,
        'day_of_week': target_date.weekday(),
    }


# ============================================================================
# BATCH UPDATE FUNCTIONS
# ============================================================================

def update_all_route_timing_metrics(recent_days: int = None):
    """
    Recalculate all RouteTimingMetric records from historical data.

    Optimized: loads all legs once, categorizes each leg once, groups by
    combination, then computes metrics per group. One DB round-trip instead
    of one per combination.

    Args:
        recent_days: If set, only include legs with pickup_date within this many days.
                     None = all completed legs.

    Returns:
        Tuple of (created_count, updated_count)
    """
    from collections import defaultdict
    from reservations.models import RouteTimingMetric, Leg

    print("Calculating route timing metrics from historical data...")

    # Get completed legs, optionally filtered by recency — single DB query
    completed_legs = Leg.objects.filter(status='completed').prefetch_related(
        'status_history', 'flight_information'
    )
    if recent_days is not None:
        cutoff = date.today() - timedelta(days=recent_days)
        completed_legs = completed_legs.filter(pickup_date__gte=cutoff)
        print(f"  Filtering to legs from last {recent_days} days (since {cutoff})")

    # Load all legs into memory ONCE, categorize each leg ONCE, group by combination
    grouped_legs = defaultdict(list)
    for leg in completed_legs:
        trip_type = leg.get_trip_type()
        pickup_cat = categorize_location(leg.pickup_location)
        dropoff_cat = categorize_location(leg.dropoff_location)
        time_cat = categorize_time_of_day(leg.pickup_time)
        day_cat = categorize_day_type(leg.pickup_date)

        key = (trip_type, pickup_cat, dropoff_cat, time_cat, day_cat)
        grouped_legs[key].append(leg)

    print(f"Found {len(grouped_legs)} unique route combinations to analyze")

    metrics_created = 0
    metrics_updated = 0
    metrics_skipped = 0

    empty_result = {
        'avg_airport_dwell_time': None,
        'median_airport_dwell_time': None,
        'p75_airport_dwell_time': None,
        'p90_airport_dwell_time': None,
        'avg_drive_time': None,
        'median_drive_time': None,
        'p75_drive_time': None,
        'p90_drive_time': None,
        'avg_total_time': None,
        'median_total_time': None,
        'p75_total_time': None,
        'p90_total_time': None,
        'sample_count': 0,
    }

    for (trip_type, pickup_cat, dropoff_cat, time_cat, day_cat), legs_list in grouped_legs.items():
        # Compute timing stats directly from pre-grouped legs
        dwell_times = []
        drive_times = []
        total_times = []

        for leg in legs_list:
            dwell = calculate_airport_dwell_time(leg) if trip_type == 'arrival' else None
            drive = calculate_drive_time(leg)

            if dwell is not None and (dwell < 0 or dwell > MAX_DWELL_MINUTES):
                dwell = None
            if drive is not None and (drive < 0 or drive > MAX_DRIVE_MINUTES):
                drive = None

            if dwell is not None:
                dwell_times.append(dwell)
            if drive is not None:
                drive_times.append(drive)

            if trip_type == 'arrival' and dwell is not None and drive is not None:
                total = dwell + drive
                if total <= MAX_TOTAL_MINUTES:
                    total_times.append(total)
            elif drive is not None:
                if drive <= MAX_TOTAL_MINUTES:
                    total_times.append(drive)

        # Adaptive per-route max (tighter than global if enough data)
        dwell_max = adaptive_max(dwell_times, MAX_DWELL_MINUTES)
        drive_max = adaptive_max(drive_times, MAX_DRIVE_MINUTES)
        dwell_times = [v for v in dwell_times if v <= dwell_max]
        drive_times = [v for v in drive_times if v <= drive_max]
        total_times = [v for v in total_times if v <= adaptive_max(total_times, MAX_TOTAL_MINUTES)]

        # IQR outlier removal
        dwell_times = iqr_filter(dwell_times)
        drive_times = iqr_filter(drive_times)
        total_times = iqr_filter(total_times)

        metrics_data = dict(empty_result)
        metrics_data['sample_count'] = len(legs_list)

        if dwell_times:
            metrics_data['avg_airport_dwell_time'] = int(statistics.mean(dwell_times))
            if len(dwell_times) >= 2:
                metrics_data['median_airport_dwell_time'] = int(statistics.median(dwell_times))
            metrics_data['p75_airport_dwell_time'] = _safe_percentile(dwell_times, 4, 2, 4)
            metrics_data['p90_airport_dwell_time'] = _safe_percentile(dwell_times, 10, 8, 10)

        if drive_times:
            metrics_data['avg_drive_time'] = int(statistics.mean(drive_times))
            if len(drive_times) >= 2:
                metrics_data['median_drive_time'] = int(statistics.median(drive_times))
            metrics_data['p75_drive_time'] = _safe_percentile(drive_times, 4, 2, 4)
            metrics_data['p90_drive_time'] = _safe_percentile(drive_times, 10, 8, 10)

        if total_times:
            metrics_data['avg_total_time'] = int(statistics.mean(total_times))
            if len(total_times) >= 2:
                metrics_data['median_total_time'] = int(statistics.median(total_times))
            metrics_data['p75_total_time'] = _safe_percentile(total_times, 4, 2, 4)
            metrics_data['p90_total_time'] = _safe_percentile(total_times, 10, 8, 10)

        if metrics_data['sample_count'] == 0:
            metrics_skipped += 1
            continue

        metric, created = RouteTimingMetric.objects.update_or_create(
            trip_type=trip_type,
            pickup_location_category=pickup_cat,
            dropoff_location_category=dropoff_cat,
            time_of_day_category=time_cat,
            day_type=day_cat,
            defaults=metrics_data
        )

        if created:
            metrics_created += 1
        else:
            metrics_updated += 1

    print(f"[OK] Created {metrics_created} new metrics, updated {metrics_updated} existing metrics")
    print(f"  (Skipped {metrics_skipped} combinations with no data)")
    return metrics_created, metrics_updated


def update_daily_capacity_metrics(start_date: date, end_date: date):
    """
    Calculate DriverDailyCapacity for all drivers in date range.

    NOTE: Timing metrics require LegStatus timestamps, which may not be available
    for older legs.

    Args:
        start_date: Start date (inclusive)
        end_date: End date (inclusive)
    """
    from reservations.models import DriverDailyCapacity, Leg
    from drivers.models import Driver

    print(f"Calculating driver capacity metrics from {start_date} to {end_date}...")

    # Get all drivers who worked in this period
    drivers = Driver.objects.filter(
        legs__pickup_date__gte=start_date,
        legs__pickup_date__lte=end_date,
        legs__status='completed'
    ).distinct()

    print(f"Found {drivers.count()} drivers with completed legs in this period")

    records_created = 0
    records_updated = 0

    # For each driver and date
    current_date = start_date
    while current_date <= end_date:
        for driver in drivers:
            # Calculate metrics for this driver on this date
            capacity_data = calculate_driver_daily_capacity_for_date(driver, current_date)

            # Skip if no legs this day
            if capacity_data['total_legs'] == 0:
                continue

            # Create or update record
            record, created = DriverDailyCapacity.objects.update_or_create(
                driver=driver,
                date=current_date,
                defaults=capacity_data
            )

            if created:
                records_created += 1
            else:
                records_updated += 1

        current_date += timedelta(days=1)

    print(f"[OK] Capacity: created {records_created} new, updated {records_updated} existing")
    return records_created, records_updated


def update_demand_patterns(start_date: date, end_date: date):
    """
    Calculate DemandPattern for date range.

    Args:
        start_date: Start date (inclusive)
        end_date: End date (inclusive)
    """
    from reservations.models import DemandPattern

    print(f"Calculating demand patterns from {start_date} to {end_date}...")

    records_created = 0
    records_updated = 0

    current_date = start_date
    while current_date <= end_date:
        for hour in range(24):
            # Calculate demand for this hour
            demand_data = calculate_demand_pattern_for_hour(current_date, hour)

            # Create or update record
            record, created = DemandPattern.objects.update_or_create(
                date=current_date,
                hour=hour,
                defaults=demand_data
            )

            if created:
                records_created += 1
            else:
                records_updated += 1

        current_date += timedelta(days=1)

    print(f"[OK] Demand: created {records_created} new, updated {records_updated} existing")
    return records_created, records_updated


# ============================================================================
# INCREMENTAL & SCHEDULER HELPERS
# ============================================================================

def update_single_route_timing_metric(leg):
    """
    Incrementally update the RouteTimingMetric for a single completed leg's bucket.
    Called when a leg status changes to 'completed'.

    Safe to call on any leg - returns silently if data is incomplete.
    """
    import logging
    from reservations.models import RouteTimingMetric, Leg

    logger = logging.getLogger(__name__)

    trip_type = leg.get_trip_type()
    pickup_cat = categorize_location(leg.pickup_location)
    dropoff_cat = categorize_location(leg.dropoff_location)

    if not leg.pickup_time or not leg.pickup_date:
        return

    time_cat = categorize_time_of_day(leg.pickup_time)
    day_cat = categorize_day_type(leg.pickup_date)

    # Re-fetch all completed legs for this specific bucket
    all_bucket_legs = Leg.objects.filter(
        status='completed'
    ).prefetch_related('status_history', 'flight_information')

    metrics_data = calculate_route_timing_metrics(
        trip_type, pickup_cat, dropoff_cat, time_cat, day_cat,
        legs_queryset=all_bucket_legs,
    )

    if metrics_data['sample_count'] > 0:
        RouteTimingMetric.objects.update_or_create(
            trip_type=trip_type,
            pickup_location_category=pickup_cat,
            dropoff_location_category=dropoff_cat,
            time_of_day_category=time_cat,
            day_type=day_cat,
            defaults=metrics_data,
        )
        logger.info(
            f"Updated RouteTimingMetric for {trip_type} {pickup_cat}→{dropoff_cat} "
            f"({time_cat}/{day_cat}): {metrics_data['sample_count']} samples"
        )


def get_route_timing_for_scheduler(
    pickup_cat: str,
    dropoff_cat: str,
    trip_type: str = None,
) -> Optional[Dict]:
    """
    Lightweight lookup for the scheduler: get best available timing metrics
    for a route, aggregated across all time-of-day/day-type buckets.

    Returns the bucket with the highest sample_count, plus a confidence level.

    Returns:
        Dict with timing data and confidence, or None if no data.
    """
    from reservations.models import RouteTimingMetric

    filters = {
        'pickup_location_category': pickup_cat,
        'dropoff_location_category': dropoff_cat,
        'sample_count__gte': 1,
    }
    if trip_type:
        filters['trip_type'] = trip_type

    metrics = RouteTimingMetric.objects.filter(**filters).order_by('-sample_count')
    if not metrics.exists():
        return None

    best = metrics.first()
    total_samples = sum(m.sample_count for m in metrics)

    if total_samples >= 20:
        confidence = 'high'
    elif total_samples >= 10:
        confidence = 'medium'
    elif total_samples >= 5:
        confidence = 'low'
    else:
        confidence = 'none'

    return {
        'avg_drive_time': best.avg_drive_time,
        'p75_drive_time': best.p75_drive_time,
        'avg_dwell_time': best.avg_airport_dwell_time,
        'p75_dwell_time': best.p75_airport_dwell_time,
        'avg_total_time': best.avg_total_time,
        'sample_count': total_samples,
        'confidence': confidence,
    }
