"""
Message template renderer for follow-up SMS automation.

Safely renders message templates with lead data, handling missing values gracefully.
"""

import logging
from collections import defaultdict

logger = logging.getLogger(__name__)


class SafeFormatDict(defaultdict):
    """Dict that returns the placeholder name for missing keys instead of raising."""
    def __missing__(self, key):
        return f"{{{key}}}"


def render_follow_up_message(template_str, lead):
    """
    Render a follow-up message template with lead data.

    Available variables:
    - {first_name}
    - {pickup_location}
    - {dropoff_location}
    - {pickup_date} (formatted as "March 15")
    - {estimated_price} (formatted as "$125")
    - {vehicle_name}
    - {trip_type_display}

    Missing values produce the placeholder name rather than errors.
    """
    # Format pickup_date nicely
    pickup_date_str = ""
    if lead.pickup_date:
        try:
            pickup_date_str = lead.pickup_date.strftime("%B %-d")
        except ValueError:
            # Windows doesn't support %-d, use %d and strip leading zero
            pickup_date_str = lead.pickup_date.strftime("%B %d").replace(" 0", " ")

    # Format estimated price
    price_str = ""
    if lead.estimated_price:
        price_str = f"${lead.estimated_price:,.0f}"

    # Get vehicle name
    vehicle_name = ""
    if lead.vehicle:
        vehicle_name = str(lead.vehicle)

    # Get trip type display
    trip_type_display = ""
    if lead.trip_type:
        trip_type_display = lead.get_trip_type_display()

    # Build the substitution dict
    values = SafeFormatDict(str, {
        "first_name": lead.first_name or "there",
        "pickup_location": lead.pickup_location or "",
        "dropoff_location": lead.dropoff_location or "",
        "pickup_date": pickup_date_str,
        "estimated_price": price_str,
        "vehicle_name": vehicle_name,
        "trip_type_display": trip_type_display,
    })

    try:
        return template_str.format_map(values)
    except Exception as e:
        logger.error(f"Error rendering template for lead #{lead.id}: {e}")
        return template_str
