"""
Lead segmentation engine.

Classifies leads by trip type based on pickup/dropoff locations,
vehicle type, pricing, and customer history.
"""

import logging

logger = logging.getLogger(__name__)

# Keywords for segment detection (case-insensitive matching)
AIRPORT_KEYWORDS = {"mco", "airport", "sfb", "oia", "orlando international", "sanford international"}
CRUISE_KEYWORDS = {"port canaveral", "cruise", "terminal", "cruise port"}
THEME_PARK_KEYWORDS = {"disney", "universal", "seaworld", "legoland", "theme park", "magic kingdom", "epcot", "hollywood studios", "animal kingdom", "islands of adventure"}
LARGE_GROUP_VEHICLES = {"van 14", "van14", "sprinter"}
LARGE_GROUP_PRICE_THRESHOLD = 300


def classify_lead(lead):
    """
    Classify a lead into a segment based on trip details and customer history.

    Priority order:
    1. Repeat customer (phone/email matches existing Customer with reservations)
    2. Abandoned quote (has expired quote, no active sequence)
    3. Airport transfer (location keywords)
    4. Cruise transfer (location keywords)
    5. Theme park (location keywords)
    6. Large group (vehicle type or high price)
    7. General (default)

    Returns a segment string matching Lead.SegmentChoices values.
    """
    from reservations.models import Customer

    locations = f"{lead.pickup_location or ''} {lead.dropoff_location or ''}".lower()

    # 1. Check for repeat customer
    try:
        if lead.phone or lead.email:
            customer_match = Customer.objects.none()
            if lead.email:
                customer_match = Customer.objects.filter(
                    email__iexact=lead.email, reservation_count__gte=1
                )
            if not customer_match.exists() and lead.phone:
                customer_match = Customer.objects.filter(
                    phone_number__iexact=lead.phone, reservation_count__gte=1
                )
            if customer_match.exists():
                return "repeat_customer"
    except Exception as e:
        logger.warning(f"Error checking repeat customer for lead #{lead.id}: {e}")

    # 2. Check for abandoned quote
    try:
        if hasattr(lead, 'quotes') and lead.quotes.filter(status="expired").exists():
            if not lead.sequence_active:
                return "abandoned_quote"
    except Exception as e:
        logger.warning(f"Error checking abandoned quote for lead #{lead.id}: {e}")

    # 3. Airport transfer
    if any(kw in locations for kw in AIRPORT_KEYWORDS):
        return "airport_transfer"

    # 4. Cruise transfer
    if any(kw in locations for kw in CRUISE_KEYWORDS):
        return "cruise_transfer"

    # 5. Theme park
    if any(kw in locations for kw in THEME_PARK_KEYWORDS):
        return "theme_park"

    # 6. Large group (vehicle or price)
    try:
        vehicle_name = ""
        if lead.vehicle:
            vehicle_name = str(lead.vehicle).lower()
        if any(v in vehicle_name for v in LARGE_GROUP_VEHICLES):
            return "large_group"
        if lead.estimated_price and lead.estimated_price > LARGE_GROUP_PRICE_THRESHOLD:
            return "large_group"
    except Exception as e:
        logger.warning(f"Error checking large group for lead #{lead.id}: {e}")

    # 7. Default
    return "general"
