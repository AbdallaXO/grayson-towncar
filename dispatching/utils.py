from django.db.models import OuterRef, Subquery, Count, Sum, Q
from django.utils import timezone
from datetime import datetime, timedelta
from decimal import Decimal
from payment.models import Payment
from drivers.models import Driver
from reservations.models import Leg


def get_filtered_legs_queryset(date_filter=None, date_from=None, date_to=None, 
                              status_filter=None, time_filter="all"):
    """
    Get a filtered queryset of legs based on various filter parameters.
    
    Args:
        date_filter: Single date filter
        date_from: Start date for range filter
        date_to: End date for range filter
        status_filter: Status filter
        time_filter: Time range filter (all, week, next_week)
    
    Returns:
        Filtered Leg queryset
    """
    today = timezone.localdate()
    
    # Base queryset with all necessary related fields
    legs_query = Leg.objects.select_related(
        "reservation",
        "reservation__customer",
        "reservation__vehicle",
        "driver",
        "flight_information",
    ).prefetch_related(
        "reservation__legs",
        "reservation__payments",
    )
    
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
        if leg.reservation.vehicle:
            vehicle_type = leg.reservation.vehicle.vehicle_type
            vehicle_name = leg.reservation.vehicle.get_vehicle_type_display()
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
    trip_type_stats = {"arrival": 0, "return": 0, "other": 0}
    
    for leg in legs:
        trip_type = leg.get_trip_type()
        trip_type_stats[trip_type] += 1
    
    return trip_type_stats


def calculate_status_statistics(legs):
    """
    Calculate status statistics from a list of legs.
    
    Args:
        legs: List of Leg objects
    
    Returns:
        Dictionary with status counts
    """
    status_stats = {"completed": 0, "in-progress": 0, "picked-up": 0, "on-location": 0}
    
    for leg in legs:
        if leg.status:
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


def get_comprehensive_statistics(date_filter=None, date_from=None, date_to=None, 
                                status_filter=None, time_filter="all"):
    """
    Get comprehensive statistics for the statistics page.
    
    Args:
        date_filter: Single date filter
        date_from: Start date for range filter
        date_to: End date for range filter
        status_filter: Status filter
        time_filter: Time range filter
    
    Returns:
        Dictionary with all statistics
    """
    # Get filtered legs with optimized queries
    legs_query = get_filtered_legs_queryset(
        date_filter, date_from, date_to, status_filter, time_filter
    )
    all_legs = list(legs_query)
    
    # Calculate all statistics using optimized methods
    vehicle_stats = calculate_vehicle_statistics(all_legs)
    trip_type_stats = calculate_trip_type_statistics(all_legs)
    status_stats = calculate_status_statistics(all_legs)
    driver_stats, active_drivers_count = calculate_driver_statistics(all_legs)
    
    # Calculate total revenue using cached data
    total_revenue = sum(
        leg.reservation.total_price for leg in all_legs if leg.reservation
    )
    
    return {
        'vehicle_stats': vehicle_stats,
        'trip_type_stats': trip_type_stats,
        'status_stats': status_stats,
        'driver_stats': driver_stats,
        'active_drivers_count': active_drivers_count,
        'total_legs': len(all_legs),
        'total_revenue': total_revenue,
    }


def get_optimized_legs_for_calendar(date_from=None, date_to=None, status_filter=None):
    """
    Get optimized legs queryset specifically for calendar views.
    Includes all necessary related data to avoid N+1 queries.
    
    Args:
        date_from: Start date
        date_to: End date
        status_filter: Status filter
    
    Returns:
        Optimized Leg queryset
    """
    legs_query = Leg.objects.select_related(
        "reservation",
        "reservation__customer",
        "reservation__vehicle",
        "driver",
        "flight_information",
    ).prefetch_related(
        "reservation__legs",
        "reservation__payments",
    )
    
    # Apply date filters
    if date_from and date_to:
        legs_query = legs_query.filter(pickup_date__range=[date_from, date_to])
    
    # Apply status filter
    if status_filter:
        legs_query = legs_query.filter(status=status_filter)
    
    return legs_query.order_by("pickup_date", "pickup_time")
