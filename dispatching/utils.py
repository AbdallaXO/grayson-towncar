from django.db.models import OuterRef, Subquery, Count, Sum, Q, Prefetch
from django.utils import timezone
from datetime import datetime, timedelta
from decimal import Decimal
from payment.models import Payment
from drivers.models import Driver
from reservations.models import Leg, LegStatus


def get_filtered_legs_queryset(date_filter=None, date_from=None, date_to=None, 
                              status_filter=None, time_filter="all", driver_filter=None, 
                              optimize_for_stats=False):
    """
    Get a filtered queryset of legs based on various filter parameters.
    
    Args:
        date_filter: Single date filter
        date_from: Start date for range filter
        date_to: End date for range filter
        status_filter: Status filter
        time_filter: Time range filter (all, week, next_week)
        driver_filter: Driver ID filter
        optimize_for_stats: If True, use minimal select_related for better performance
    
    Returns:
        Filtered Leg queryset
    """
    today = timezone.localdate()
    
    # Base queryset with optimized related fields for statistics
    if optimize_for_stats:
        # Minimal select_related for better performance with large datasets
        legs_query = Leg.objects.select_related(
            "reservation",
            "reservation__vehicle", "vehicle",
            "driver",
        )
    else:
        # Full select_related for detailed views
        legs_query = Leg.objects.select_related(
            "reservation",
            "reservation__customer",
            "reservation__vehicle", "vehicle",
            "reservation__travel_agent",
            "reservation__travel_agent__user",
            "driver",
            "driver__profile",
            "driver_assigned_by",
            "flight_information",
            "cruise_information",
        ).prefetch_related(
            "reservation__legs",
            "legstop_set",  # leg.additional_dropoffs / has_intermediate_stops in list templates
            Prefetch("reservation__payments", queryset=Payment.objects.order_by('-created_at')),
            Prefetch("status_history", queryset=LegStatus.objects.order_by('-timestamp').select_related('updated_by')),
        )

    # Exclude cancelled reservations and cancelled legs - they should be hidden from normal operations
    legs_query = legs_query.exclude(reservation__status='cancelled').exclude(status='cancelled')
    
    # Apply date filters
    if date_from and date_to:
        try:
            from_date = datetime.strptime(date_from, "%Y-%m-%d").date()
            to_date = datetime.strptime(date_to, "%Y-%m-%d").date()
            legs_query = legs_query.filter(pickup_date__range=[from_date, to_date])
        except ValueError:
            pass
    elif time_filter == "week":
        # This week
        start_date = today - timedelta(days=today.weekday())
        end_date = start_date + timedelta(days=6)
        legs_query = legs_query.filter(pickup_date__range=[start_date, end_date])
    elif time_filter == "next_week":
        # Next week
        start_date = today + timedelta(days=(7 - today.weekday()))
        end_date = start_date + timedelta(days=6)
        legs_query = legs_query.filter(pickup_date__range=[start_date, end_date])
    elif date_filter:
        try:
            filter_date = datetime.strptime(date_filter, "%Y-%m-%d").date()
            legs_query = legs_query.filter(pickup_date=filter_date)
        except ValueError:
            pass
    else:
        # Default: show all future legs
        legs_query = legs_query.filter(pickup_date__gte=today)
    
    # Apply status filter
    if status_filter:
        legs_query = legs_query.filter(status=status_filter)
    
    # Apply driver filter
    if driver_filter:
        if driver_filter == "unassigned":
            legs_query = legs_query.filter(driver__isnull=True)
        else:
            legs_query = legs_query.filter(driver_id=driver_filter)
    
    return legs_query.order_by("pickup_date", "pickup_time")


def calculate_vehicle_statistics(legs):
    """
    Calculate comprehensive vehicle statistics from a list of legs.
    
    Args:
        legs: List of Leg objects
    
    Returns:
        Dictionary with vehicle statistics
    """
    vehicle_stats = {}
    
    for leg in legs:
        _eff_v = leg.effective_vehicle
        if _eff_v:
            vehicle_type = _eff_v.vehicle_type
            vehicle_name = _eff_v.get_vehicle_type_display()
            trip_type = leg.get_trip_type()
            
            if vehicle_type not in vehicle_stats:
                vehicle_stats[vehicle_type] = {
                    'name': vehicle_name,
                    'total': 0,
                    'arrivals': 0,
                    'returns': 0,
                    'other': 0,
                    'completed': 0,
                    'in_progress': 0,
                    'picked_up': 0,
                    'on_location': 0,
                    'revenue': Decimal("0.00"),
                    'arrival_revenue': Decimal("0.00"),
                    'return_revenue': Decimal("0.00"),
                    'other_revenue': Decimal("0.00")
                }
            
            vehicle_stats[vehicle_type]['total'] += 1
            
            # Count by trip type and calculate revenue by trip type
            if trip_type == 'arrival':
                vehicle_stats[vehicle_type]['arrivals'] += 1
                if leg.reservation:
                    vehicle_stats[vehicle_type]['arrival_revenue'] += leg.reservation.total_price
            elif trip_type == 'return':
                vehicle_stats[vehicle_type]['returns'] += 1
                if leg.reservation:
                    vehicle_stats[vehicle_type]['return_revenue'] += leg.reservation.total_price
            else:
                vehicle_stats[vehicle_type]['other'] += 1
                if leg.reservation:
                    vehicle_stats[vehicle_type]['other_revenue'] += leg.reservation.total_price
            
            # Count by status
            if leg.status:
                if leg.status == 'completed':
                    vehicle_stats[vehicle_type]['completed'] += 1
                elif leg.status == 'in-progress':
                    vehicle_stats[vehicle_type]['in_progress'] += 1
                elif leg.status == 'picked-up':
                    vehicle_stats[vehicle_type]['picked_up'] += 1
                elif leg.status == 'on-location':
                    vehicle_stats[vehicle_type]['on_location'] += 1
            
            # Calculate total revenue
            if leg.reservation:
                vehicle_stats[vehicle_type]['revenue'] += leg.reservation.total_price
    
    # Convert to list and sort by total rides
    vehicle_stats_list = []
    for vehicle_type, stats in vehicle_stats.items():
        stats['vehicle_type'] = vehicle_type
        vehicle_stats_list.append(stats)
    
    vehicle_stats_list.sort(key=lambda x: x['total'], reverse=True)
    
    return vehicle_stats_list


def calculate_trip_type_statistics(legs):
    """
    Calculate trip type statistics from a list of legs.
    
    Args:
        legs: List of Leg objects
    
    Returns:
        Dictionary with trip type counts
    """
    trip_type_stats = {"arrival": 0, "return": 0, "cruise": 0, "other": 0}
    
    for leg in legs:
        trip_type = leg.get_trip_type()
        trip_type_stats[trip_type] = trip_type_stats.get(trip_type, 0) + 1
    
    return trip_type_stats


def calculate_status_statistics(legs):
    """
    Calculate status statistics from a list of legs.
    
    Args:
        legs: List of Leg objects
    
    Returns:
        Dictionary with status counts
    """
    status_stats = {
        "completed": 0,
        "in-progress": 0,
        "confirmed": 0,
        "on-the-way": 0,
        "picked-up": 0,
        "on-location": 0,
        "cancelled": 0,
    }

    for leg in legs:
        if leg.status and leg.status in status_stats:
            status_stats[leg.status] += 1
    
    return status_stats


def calculate_driver_statistics(legs):
    """
    Calculate driver statistics from a list of legs.
    
    Args:
        legs: List of Leg objects
    
    Returns:
        Dictionary with driver statistics
    """
    driver_stats = {}
    active_drivers = set()
    
    for leg in legs:
        if leg.driver:
            driver_id = leg.driver.id
            driver_name = str(leg.driver)
            
            if driver_id not in driver_stats:
                driver_stats[driver_id] = {
                    'name': driver_name,
                    'total_legs': 0,
                    'completed_legs': 0,
                    'revenue': Decimal("0.00"),
                    'is_active': False
                }
            
            driver_stats[driver_id]['total_legs'] += 1
            
            if leg.status == 'completed':
                driver_stats[driver_id]['completed_legs'] += 1
            
            if leg.reservation:
                driver_stats[driver_id]['revenue'] += leg.reservation.total_price
            
            # Consider driver active if they have legs in the last 30 days
            if leg.pickup_date >= timezone.localdate() - timedelta(days=30):
                driver_stats[driver_id]['is_active'] = True
                active_drivers.add(driver_id)
    
    # Convert to list and sort by total legs
    driver_stats_list = []
    for driver_id, stats in driver_stats.items():
        stats['driver_id'] = driver_id
        driver_stats_list.append(stats)
    
    driver_stats_list.sort(key=lambda x: x['total_legs'], reverse=True)
    
    return driver_stats_list, len(active_drivers)


def _compute_all_statistics_single_pass(all_legs):
    """Compute vehicle, trip type, status, driver, and revenue stats in ONE pass."""
    thirty_days_ago = timezone.localdate() - timedelta(days=30)
    total_revenue = Decimal("0.00")

    # accumulators
    vehicle_stats = {}
    trip_type_stats = {"arrival": 0, "return": 0, "cruise": 0, "other": 0}
    status_stats = {
        "completed": 0, "in-progress": 0, "confirmed": 0,
        "on-the-way": 0, "picked-up": 0, "on-location": 0, "cancelled": 0,
    }
    driver_stats = {}
    active_drivers = set()

    for leg in all_legs:
        trip_type = leg.get_trip_type()
        reservation = leg.reservation
        res_price = reservation.total_price if reservation else Decimal("0.00")

        # -- total revenue --
        if reservation:
            total_revenue += res_price

        # -- trip type --
        trip_type_stats[trip_type] = trip_type_stats.get(trip_type, 0) + 1

        # -- status --
        if leg.status and leg.status in status_stats:
            status_stats[leg.status] += 1

        # -- vehicle --
        _eff_v = leg.effective_vehicle
        if _eff_v:
            vt = _eff_v.vehicle_type
            if vt not in vehicle_stats:
                vehicle_stats[vt] = {
                    'name': _eff_v.get_vehicle_type_display(),
                    'vehicle_type': vt,
                    'total': 0, 'arrivals': 0, 'returns': 0, 'other': 0,
                    'completed': 0, 'in_progress': 0, 'picked_up': 0, 'on_location': 0,
                    'revenue': Decimal("0.00"),
                    'arrival_revenue': Decimal("0.00"),
                    'return_revenue': Decimal("0.00"),
                    'other_revenue': Decimal("0.00"),
                }
            vs = vehicle_stats[vt]
            vs['total'] += 1
            if trip_type == 'arrival':
                vs['arrivals'] += 1
                vs['arrival_revenue'] += res_price
            elif trip_type == 'return':
                vs['returns'] += 1
                vs['return_revenue'] += res_price
            else:
                vs['other'] += 1
                vs['other_revenue'] += res_price
            if leg.status == 'completed':
                vs['completed'] += 1
            elif leg.status == 'in-progress':
                vs['in_progress'] += 1
            elif leg.status == 'picked-up':
                vs['picked_up'] += 1
            elif leg.status == 'on-location':
                vs['on_location'] += 1
            vs['revenue'] += res_price

        # -- driver --
        if leg.driver:
            did = leg.driver.id
            if did not in driver_stats:
                driver_stats[did] = {
                    'driver_id': did,
                    'name': str(leg.driver),
                    'total_legs': 0, 'completed_legs': 0,
                    'revenue': Decimal("0.00"), 'is_active': False,
                }
            ds = driver_stats[did]
            ds['total_legs'] += 1
            if leg.status == 'completed':
                ds['completed_legs'] += 1
            ds['revenue'] += res_price
            if leg.pickup_date >= thirty_days_ago:
                ds['is_active'] = True
                active_drivers.add(did)

    # sort results
    vehicle_stats_list = sorted(vehicle_stats.values(), key=lambda x: -x['total'])
    driver_stats_list = sorted(driver_stats.values(), key=lambda x: -x['total_legs'])

    return {
        'vehicle_stats': vehicle_stats_list,
        'trip_type_stats': trip_type_stats,
        'status_stats': status_stats,
        'driver_stats': driver_stats_list,
        'active_drivers_count': len(active_drivers),
        'total_revenue': total_revenue,
    }


def get_comprehensive_statistics(date_filter=None, date_from=None, date_to=None,
                                status_filter=None, time_filter="all", driver_filter=None,
                                group_by='day', page=1, per_page=50):
    """
    Get comprehensive statistics for the statistics page.

    Args:
        date_filter: Single date filter
        date_from: Start date for range filter
        date_to: End date for range filter
        status_filter: Status filter
        time_filter: Time range filter
        driver_filter: Driver ID filter
        group_by: Group daily stats by 'day', 'week', or 'month'
        page: Page number for pagination
        per_page: Items per page

    Returns:
        Dictionary with all statistics
    """
    # Get filtered legs with optimized queries for statistics
    legs_query = get_filtered_legs_queryset(
        date_filter, date_from, date_to, status_filter, time_filter, driver_filter,
        optimize_for_stats=True
    )
    all_legs = list(legs_query)

    # Single-pass computation of vehicle/trip/status/driver/revenue stats
    stats = _compute_all_statistics_single_pass(all_legs)

    # Daily stats still needs its own grouping pass
    daily_stats = calculate_daily_leg_statistics(all_legs, group_by, page, per_page)
    stats['daily_stats'] = daily_stats
    stats['total_legs'] = len(all_legs)
    return stats


def calculate_daily_leg_statistics(legs, group_by='day', page=1, per_page=50):
    """
    Calculate daily leg statistics showing legs per day with pagination and grouping.
    
    Args:
        legs: List of Leg objects
        group_by: 'day', 'week', or 'month'
        page: Page number for pagination
        per_page: Items per page
    
    Returns:
        Dictionary with daily statistics including pagination and grouping
    """
    from collections import defaultdict
    from datetime import datetime, timedelta
    from django.core.paginator import Paginator
    
    daily_counts = defaultdict(int)
    daily_revenue = defaultdict(lambda: Decimal("0.00"))
    daily_vehicle_breakdown = defaultdict(lambda: defaultdict(int))
    
    for leg in legs:
        date = leg.pickup_date
        daily_counts[date] += 1
        
        if leg.reservation:
            daily_revenue[date] += leg.reservation.total_price
            
        _eff_v = leg.effective_vehicle
        if _eff_v:
            vehicle_type = _eff_v.get_vehicle_type_display()
            daily_vehicle_breakdown[date][vehicle_type] += 1
    
    # Convert to sorted list of daily stats
    daily_stats = []
    for date in sorted(daily_counts.keys()):
        daily_stats.append({
            'date': date,
            'leg_count': daily_counts[date],
            'revenue': daily_revenue[date],
            'vehicle_breakdown': dict(daily_vehicle_breakdown[date]),
            'day_of_week': date.strftime('%A'),
            'formatted_date': date.strftime('%Y-%m-%d')
        })
    
    # Group by week or month if requested
    if group_by == 'week':
        grouped_stats = group_daily_stats_by_week(daily_stats)
    elif group_by == 'month':
        grouped_stats = group_daily_stats_by_month(daily_stats)
    else:
        grouped_stats = daily_stats
    
    # Get top 10 days with most legs
    top_days = sorted(daily_stats, key=lambda x: x['leg_count'], reverse=True)[:10]
    
    # Calculate weekly averages
    weekly_averages = defaultdict(list)
    for stat in daily_stats:
        # Get the Monday of the week for this date
        monday = stat['date'] - timedelta(days=stat['date'].weekday())
        week_key = monday.strftime('%Y-%m-%d')
        weekly_averages[week_key].append(stat['leg_count'])
    
    # Calculate averages for each week
    weekly_stats = []
    for week_start, leg_counts in weekly_averages.items():
        weekly_stats.append({
            'week_start': week_start,
            'week_end': (datetime.strptime(week_start, '%Y-%m-%d').date() + timedelta(days=6)).strftime('%Y-%m-%d'),
            'total_legs': sum(leg_counts),
            'avg_legs_per_day': round(sum(leg_counts) / len(leg_counts), 2),
            'days_in_week': len(leg_counts)
        })
    
    weekly_stats.sort(key=lambda x: x['week_start'], reverse=True)
    
    # Paginate the grouped stats
    paginator = Paginator(grouped_stats, per_page)
    try:
        paginated_stats = paginator.page(page)
    except:
        paginated_stats = paginator.page(1)
    
    # Create page range for pagination (show max 10 pages)
    start_page = max(1, page - 5)
    end_page = min(paginator.num_pages, page + 5)
    page_range = range(start_page, end_page + 1)
    
    return {
        'daily_stats': paginated_stats,
        'top_days': top_days,
        'weekly_stats': weekly_stats,
        'total_days': len(daily_stats),
        'avg_legs_per_day': round(sum(daily_counts.values()) / len(daily_counts), 2) if daily_counts else 0,
        'max_legs_in_day': max(daily_counts.values()) if daily_counts else 0,
        'min_legs_in_day': min(daily_counts.values()) if daily_counts else 0,
        'group_by': group_by,
        'has_pagination': paginator.num_pages > 1,
        'pagination_info': {
            'current_page': page,
            'total_pages': paginator.num_pages,
            'per_page': per_page,
            'total_items': paginator.count,
            'has_previous': paginated_stats.has_previous(),
            'has_next': paginated_stats.has_next(),
            'previous_page': paginated_stats.previous_page_number() if paginated_stats.has_previous() else None,
            'next_page': paginated_stats.next_page_number() if paginated_stats.has_next() else None,
            'page_range': page_range,
        }
    }


def group_daily_stats_by_week(daily_stats):
    """Group daily stats by week (Monday to Sunday)"""
    from collections import defaultdict
    from datetime import timedelta
    
    weekly_groups = defaultdict(lambda: {
        'week_start': None,
        'week_end': None,
        'total_legs': 0,
        'total_revenue': Decimal("0.00"),
        'days_count': 0,
        'vehicle_breakdown': defaultdict(int),
        'daily_details': []
    })
    
    for stat in daily_stats:
        # Get the Monday of the week for this date
        monday = stat['date'] - timedelta(days=stat['date'].weekday())
        sunday = monday + timedelta(days=6)
        
        week_key = monday.strftime('%Y-%m-%d')
        
        weekly_groups[week_key]['week_start'] = monday.strftime('%Y-%m-%d')
        weekly_groups[week_key]['week_end'] = sunday.strftime('%Y-%m-%d')
        weekly_groups[week_key]['total_legs'] += stat['leg_count']
        weekly_groups[week_key]['total_revenue'] += stat['revenue']
        weekly_groups[week_key]['days_count'] += 1
        weekly_groups[week_key]['daily_details'].append(stat)
        
        # Aggregate vehicle breakdown
        for vehicle, count in stat['vehicle_breakdown'].items():
            weekly_groups[week_key]['vehicle_breakdown'][vehicle] += count
    
    # Convert to list format
    weekly_stats = []
    for week_key in sorted(weekly_groups.keys(), reverse=True):
        group = weekly_groups[week_key]
        weekly_stats.append({
            'period_label': f"{group['week_start']} to {group['week_end']}",
            'leg_count': group['total_legs'],
            'revenue': group['total_revenue'],
            'vehicle_breakdown': dict(group['vehicle_breakdown']),
            'days_count': group['days_count'],
            'avg_legs_per_day': round(group['total_legs'] / group['days_count'], 2) if group['days_count'] > 0 else 0,
            'daily_details': group['daily_details']
        })
    
    return weekly_stats


def group_daily_stats_by_month(daily_stats):
    """Group daily stats by month"""
    from collections import defaultdict
    
    monthly_groups = defaultdict(lambda: {
        'month_label': None,
        'total_legs': 0,
        'total_revenue': Decimal("0.00"),
        'days_count': 0,
        'vehicle_breakdown': defaultdict(int),
        'daily_details': []
    })
    
    for stat in daily_stats:
        month_key = stat['date'].strftime('%Y-%m')
        
        monthly_groups[month_key]['month_label'] = stat['date'].strftime('%B %Y')
        monthly_groups[month_key]['total_legs'] += stat['leg_count']
        monthly_groups[month_key]['total_revenue'] += stat['revenue']
        monthly_groups[month_key]['days_count'] += 1
        monthly_groups[month_key]['daily_details'].append(stat)
        
        # Aggregate vehicle breakdown
        for vehicle, count in stat['vehicle_breakdown'].items():
            monthly_groups[month_key]['vehicle_breakdown'][vehicle] += count
    
    # Convert to list format
    monthly_stats = []
    for month_key in sorted(monthly_groups.keys(), reverse=True):
        group = monthly_groups[month_key]
        monthly_stats.append({
            'period_label': group['month_label'],
            'leg_count': group['total_legs'],
            'revenue': group['total_revenue'],
            'vehicle_breakdown': dict(group['vehicle_breakdown']),
            'days_count': group['days_count'],
            'avg_legs_per_day': round(group['total_legs'] / group['days_count'], 2) if group['days_count'] > 0 else 0,
            'daily_details': group['daily_details']
        })
    
    return monthly_stats


def get_optimized_legs_for_calendar(date_from=None, date_to=None, status_filter=None, driver_filter=None):
    """
    Get optimized legs queryset specifically for calendar views.
    Includes all necessary related data to avoid N+1 queries.
    
    Args:
        date_from: Start date
        date_to: End date
        status_filter: Status filter
        driver_filter: Driver ID filter
    
    Returns:
        Optimized Leg queryset
    """
    legs_query = Leg.objects.select_related(
        "reservation",
        "reservation__customer",
        "reservation__vehicle", "vehicle",
        "reservation__travel_agent",
        "reservation__travel_agent__user",
        "driver",
        "driver__profile",
        "flight_information",
        "cruise_information",
    ).prefetch_related(
        "reservation__legs",
        Prefetch("reservation__payments", queryset=Payment.objects.order_by('-created_at')),
    )
    
    # Exclude cancelled reservations and cancelled legs
    legs_query = legs_query.exclude(reservation__status='cancelled').exclude(status='cancelled')

    # Apply date filters
    if date_from and date_to:
        legs_query = legs_query.filter(pickup_date__range=[date_from, date_to])

    # Apply status filter
    if status_filter:
        legs_query = legs_query.filter(status=status_filter)

    # Apply driver filter
    if driver_filter:
        legs_query = legs_query.filter(driver_id=driver_filter)

    return legs_query.order_by("pickup_date", "pickup_time")


# ============================================================================
# REAL-TIME DISPATCH FLAGS
# ============================================================================

# Minutes before pickup to expect "on-the-way" status
OTW_LEAD_MINUTES = 20
# Cruise pickups need more lead time (Port Canaveral is far from Orlando)
OTW_LEAD_MINUTES_CRUISE = 50

# Status progression sets
STATUSES_OTW_OR_LATER = {'on-the-way', 'on-location', 'picked-up', 'completed'}
STATUSES_PICKED_OR_LATER = {'picked-up', 'completed'}


def detect_leg_flags(leg, now: datetime) -> list:
    """
    Check a leg against the current time and return real-time dispatch flags.

    Meant for today's legs only. Each flag is a dict with:
        level  – 'warning' | 'danger'
        icon   – Bootstrap Icons class
        text   – Short human-readable description

    Flag rules:
        1. Not Confirmed – leg is still 'in-progress' on the day of pickup
        2. Should Be On The Way – within lead time of pickup, not on-the-way+
           (20 min default, 50 min for cruises — Port Canaveral is far)
        3. Not Picked Up – for returns/cruise/other, pickup time passed, not picked-up+
           (arrivals excluded — drivers can wait 1+ hours at the airport)
    """
    flags = []

    # Only flag assigned, non-completed, non-cancelled legs
    if not leg.driver or leg.status in ('completed', 'cancelled'):
        return flags

    if not leg.pickup_time:
        return flags

    pickup_dt = datetime.combine(leg.pickup_date, leg.pickup_time)
    minutes_until_pickup = (pickup_dt - now).total_seconds() / 60
    trip_type = leg.get_trip_type()

    # ── Flag 1: Not Confirmed ──
    if leg.status == 'in-progress':
        flags.append({
            'level': 'warning' if minutes_until_pickup > 60 else 'danger',
            'icon': 'bi-question-circle',
            'text': 'Not confirmed yet',
        })

    # ── Flag 2: Should Be On The Way ──
    lead_minutes = OTW_LEAD_MINUTES_CRUISE if trip_type == 'cruise' else OTW_LEAD_MINUTES
    if minutes_until_pickup <= lead_minutes and leg.status not in STATUSES_OTW_OR_LATER:
        if minutes_until_pickup < 0:
            mins_late = int(abs(minutes_until_pickup))
            flags.append({
                'level': 'danger',
                'icon': 'bi-exclamation-triangle-fill',
                'text': f'Not on the way — {mins_late} min past pickup',
            })
        else:
            mins_left = int(minutes_until_pickup)
            flags.append({
                'level': 'warning',
                'icon': 'bi-clock',
                'text': f'Not on the way yet — pickup in {mins_left} min',
            })

    # ── Flag 3: Not Picked Up (returns / cruise / other) ──
    # Arrivals excluded — gate-to-pickup can take 1+ hours legitimately.
    if trip_type != 'arrival' and minutes_until_pickup < 0:
        if leg.status not in STATUSES_PICKED_OR_LATER:
            mins_late = int(abs(minutes_until_pickup))
            flags.append({
                'level': 'danger' if mins_late > 10 else 'warning',
                'icon': 'bi-person-x',
                'text': f'Not picked up — {mins_late} min past pickup time',
            })

    return flags
