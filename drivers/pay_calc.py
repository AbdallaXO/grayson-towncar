"""Driver pay auto-calculation helpers.

Used by Leg.save() to auto-fill driver_base_pay based on:
  - DriverPayRate entries (per-driver overrides + affiliate rates)
  - Route.inhouse_base_pay (default for inhouse drivers)
  - Driver.night_bonus (per-driver night pickup bonus)
"""
from datetime import time
from decimal import Decimal


def _determine_direction(leg):
    """Check if the leg goes in the route's forward or reverse direction.

    Returns 'reverse' if leg pickup matches route.destination,
    'forward' otherwise.
    """
    route = leg.route
    if not route:
        return "forward"

    pickup = (leg.pickup_location or "").lower()
    if not pickup:
        return "forward"

    # Check if pickup matches destination (reverse) or origin (forward)
    origin_name = (route.origin.name or "").lower()
    dest_name = (route.destination.name or "").lower()

    # Check aliases too
    origin_aliases = []
    if route.origin.aliases:
        origin_aliases = [a.strip().lower() for a in route.origin.aliases.split(",") if a.strip()]

    dest_aliases = []
    if route.destination.aliases:
        dest_aliases = [a.strip().lower() for a in route.destination.aliases.split(",") if a.strip()]

    # Does pickup match destination? → reverse
    dest_candidates = [dest_name] + dest_aliases
    for candidate in dest_candidates:
        if candidate and candidate in pickup:
            return "reverse"

    # Does pickup match origin? → forward
    origin_candidates = [origin_name] + origin_aliases
    for candidate in origin_candidates:
        if candidate and candidate in pickup:
            return "forward"

    return "forward"  # default


def _find_rate(driver, route, vehicle, direction):
    """Find the best matching DriverPayRate.

    Lookup priority:
      1. Exact direction match (forward/reverse) with vehicle
      2. 'both' direction with vehicle
      3. Exact direction match, all vehicles
      4. 'both' direction, all vehicles
    """
    from drivers.models import DriverPayRate

    base_qs = DriverPayRate.objects.filter(driver=driver, route=route)

    # With specific vehicle
    if vehicle:
        # Exact direction + vehicle
        rate = base_qs.filter(vehicle=vehicle, direction=direction).first()
        if rate:
            return rate.base_pay

        # Both directions + vehicle
        rate = base_qs.filter(vehicle=vehicle, direction="both").first()
        if rate:
            return rate.base_pay

    # All vehicles (vehicle=NULL)
    # Exact direction
    rate = base_qs.filter(vehicle__isnull=True, direction=direction).first()
    if rate:
        return rate.base_pay

    # Both directions, all vehicles
    rate = base_qs.filter(vehicle__isnull=True, direction="both").first()
    if rate:
        return rate.base_pay

    return None


def calculate_driver_pay(leg):
    """Calculate base_pay for a leg based on driver, route, vehicle.

    Lookup chain:
      INHOUSE:
        1. DriverPayRate(driver, route) → driver override
        2. Route.inhouse_base_pay → default

      AFFILIATE:
        1. DriverPayRate with exact direction + vehicle
        2. DriverPayRate with 'both' + vehicle
        3. DriverPayRate with exact direction + all vehicles
        4. DriverPayRate with 'both' + all vehicles
        5. None → manual entry needed

    Returns Decimal or None.
    """
    driver = leg.driver
    if not driver:
        return None

    route = leg.route
    if not route:
        return None

    direction = _determine_direction(leg)

    # Get vehicle from reservation
    vehicle = None
    if leg.reservation and leg.reservation.vehicle:
        vehicle = leg.reservation.vehicle

    if driver.driver_type == "inhouse":
        # Check for driver-specific override
        rate = _find_rate(driver, route, vehicle=None, direction="both")
        if rate:
            return rate

        # Default: Route.inhouse_base_pay
        return route.inhouse_base_pay  # May be None

    elif driver.driver_type == "affiliate":
        return _find_rate(driver, route, vehicle, direction)

    return None


def calculate_night_bonus(driver, pickup_time):
    """Return driver's night bonus if pickup is 10:01 PM - 5:59 AM, else $0."""
    if not pickup_time or not driver:
        return Decimal("0.00")

    is_night = pickup_time >= time(22, 1) or pickup_time <= time(5, 59)
    if is_night:
        return driver.night_bonus
    return Decimal("0.00")
