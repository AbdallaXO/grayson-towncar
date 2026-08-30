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


def calculate_driver_pay(leg, locations=None):
    """Calculate base_pay for a leg based on driver, route, vehicle.

    Lookup chain:
      INHOUSE:
        1. DriverPayRate(driver, route) → driver override
        2. Route.inhouse_base_pay → this pair overrides its zone
        3. ZoneRate for the two endpoints' pay zones → the normal case
        4. None → outside the service area, needs a human

      AFFILIATE (route required — negotiated card rates, no zone concept):
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

    if driver.driver_type == "inhouse":
        # 1. Driver-specific override on this exact route.
        if route is not None:
            rate = _find_rate(driver, route, vehicle=None, direction="both")
            if rate is not None:
                return rate
            # 2. The route's own price. A Route is an OVERRIDE on its zone, so
            #    it wins whenever someone has set one.
            if route.inhouse_base_pay is not None:
                return route.inhouse_base_pay
        # 3. The zone rate. Most trips have no Route row and never will — the
        #    table only ever held the pairs someone got round to entering. Two
        #    endpoints in known zones is enough to price the trip.
        return _zone_base_pay(leg, locations=locations)

    if route is None:
        # Affiliates are paid negotiated per-route card rates, so an unrouted
        # leg genuinely has no affiliate price. Zones are an in-house concept.
        return None

    direction = _determine_direction(leg)

    # Get vehicle from reservation
    vehicle = None
    if leg.reservation and leg.reservation.vehicle:
        vehicle = leg.reservation.vehicle

    if driver.driver_type == "affiliate":
        rate = _find_rate(driver, route, vehicle, direction)
        if rate is None and (leg.effective_vehicle_type or "") == "mini_van":
            # minivan == SUV pricing equivalence (founder rule — see the farm-out optimizer's
            # loud header): an affiliate with no minivan row is paid their SUV rate. FALLBACK
            # only — an explicit minivan row above always wins. Keeps the booked pay equal to
            # the farm-out page's quote for per-vehicle-carded affiliates.
            from rates.models import Vehicle
            suv = Vehicle.objects.filter(vehicle_type="suv").first()
            if suv is not None:
                rate = _find_rate(driver, route, suv, direction)
        return rate

    return None


def _zone_base_pay(leg, locations=None):
    """In-house base pay from the pay zones of the leg's two endpoints.

    None when either endpoint is somewhere we do not serve (Tampa, Miami) or
    has no zone set — those legs stay unpriced on purpose and surface on the
    driver-pay page as needing a price.
    """
    from rates.models import ZoneRate

    origin, destination = leg._resolve_location_endpoints(locations=locations)
    if not (origin and destination):
        return None
    return ZoneRate.pay_for(origin.pay_zone_id, destination.pay_zone_id)


def calculate_night_bonus(driver, pickup_time):
    """Return driver's night bonus if pickup is 10:01 PM - 5:59 AM, else $0."""
    if not pickup_time or not driver:
        return Decimal("0.00")

    is_night = pickup_time >= time(22, 1) or pickup_time <= time(5, 59)
    if is_night:
        return driver.night_bonus
    return Decimal("0.00")
