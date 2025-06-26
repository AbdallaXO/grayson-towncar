from django.contrib import messages
from django.shortcuts import redirect
from django.conf import settings
import requests
from .forms import (
    ReservationForm,
    CustomerForm,
    LegForm,
    FlightForm,
)
from decimal import Decimal
from datetime import time
import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)


def get_form_details(request, rate):
    """returns a trip type and returns a price based on the trip_type, if trip_type not valid
    redirects to the rates page"""
    trip_type = request.GET.get("round")
    if trip_type == "1":
        price = rate.oneway_price
        trip_type = "one_way"
    elif trip_type == "2":
        price = rate.round_trip_price
        trip_type = "round_trip"
    else:
        price = rate.round_trip_price
        trip_type = "round_trip"
    return trip_type, price


def _initalize_form(trip_type, rate, price):
    """Initializes the forms for the GET request and returns forms for customer, reservation,
    flight1,leg1, and if trip_type is round_trip, it returns a flight2 form and a leg2 form"""
    customer_form = CustomerForm(label_suffix="*")
    reservation_form = ReservationForm(
        initial={
            "vehicle": rate.vehicle,
            "base_price": price,
            "total_price": price,
            "route": rate.route,
        },
        rate=rate,
        label_suffix="",
    )
    flight1_form = FlightForm(prefix="flight1")
    leg1_form = LegForm(prefix="leg1", label_suffix="*")
    # conditional forms if its a roundtrip
    flight2_form = FlightForm(prefix="flight2") if trip_type == "round_trip" else None
    leg2_form = (
        LegForm(prefix="leg2", label_suffix="*") if trip_type == "round_trip" else None
    )

    return (
        customer_form,
        reservation_form,
        flight1_form,
        leg1_form,
        flight2_form,
        leg2_form,
    )


def returns_post_form(request, trip_type, rate):
    """Returns Forms with Posted Data, just to Avoid Redundancy of repeating everything in the view
    returns customer,reservatiom,flight1,leg1, flight 2 and leg 2 if trip_type == 2, else oneway"""
    customer_form = CustomerForm(request.POST)
    reservation_form = ReservationForm(request.POST, rate=rate)
    flight1_form = FlightForm(request.POST, prefix="flight1")
    leg1_form = LegForm(request.POST, prefix="leg1")
    flight2_form = (
        FlightForm(request.POST, prefix="flight2")
        if trip_type == "round_trip"
        else None
    )
    leg2_form = (
        LegForm(request.POST, prefix="leg2") if trip_type == "round_trip" else None
    )
    return (
        customer_form,
        reservation_form,
        flight1_form,
        leg1_form,
        flight2_form,
        leg2_form,
    )


def validate_forms(
    customer_form,
    reservation_form,
    flight1_form,
    leg1_form,
    flight2_form,
    leg2_form,
    trip_type,
):
    """Validated all the submitted forms based on trip_type
    received forms for customer, reservation, flight1, leg1, flight2 if round_trip, leg2 if round_trip
    returns true if all forms are valid
    if trip_type is oneway will always return true for oneway+ roundtrip"""
    customer_valid = customer_form.is_valid()
    reservation_valid = reservation_form.is_valid()
    flight1_valid = flight1_form.is_valid()
    leg1_valid = leg1_form.is_valid()

    if trip_type != "round_trip":
        leg2_valid = True
        flight2_valid = True
    else:
        leg2_valid = leg2_form.is_valid()
        flight2_valid = flight2_form.is_valid()
    forms_valid = all(
        [
            customer_valid,
            reservation_valid,
            flight1_valid,
            leg1_valid,
            flight2_valid,
            leg2_valid,
        ]
    )

    return forms_valid


AIRLINES = [
    "American Airlines",
    "Delta Air Lines",
    "United Airlines",
    "JetBlue Airways",
    "Southwest Airlines",
    "Spirit Airlines",
    "Alaska Airlines",
    "Frontier Airlines",
]


def extra_charges(reservation):
    total_extra = Decimal(0)
    for leg in reservation.legs.all():
        if leg.pickup_time >= time(22, 0) or leg.pickup_time < time(6, 0):
            total_extra += Decimal(20.00)
            logger.info(
                f"Added ${total_extra} on Reservation #{reservation.id} for {reservation.customer.get_full_name()}"
            )

    reservation.additional_charges = total_extra
    reservation.total_price = reservation.base_price + total_extra
    reservation.base_price = reservation.total_price
    reservation.save(update_fields=["additional_charges", "total_price", "base_price"])
    return total_extra


def send_ntfy_notification(title, message, priority="default", tags=None):
    """
    Send a notification via ntfy
    
    Args:
        title (str): Notification title
        message (str): Notification message
        priority (str): Priority level (min, low, default, high, urgent)
        tags (list): List of tags for the notification
    """
    if not getattr(settings, 'NTFY_ENABLED', False):
        logger.info("NTFY notifications are disabled")
        return
    
    try:
        topic = getattr(settings, 'NTFY_TOPIC', 'grayson-leads')
        server = getattr(settings, 'NTFY_SERVER', 'https://ntfy.sh')
        
        url = f"{server}/{topic}"
        
        headers = {
            'Content-Type': 'text/plain',
        }
        
        if priority != "default":
            headers['Priority'] = priority
            
        if tags:
            headers['Tags'] = ','.join(tags)
        
        response = requests.post(url, data=message, headers=headers, timeout=10)
        
        if response.status_code == 200:
            logger.info(f"NTFY notification sent successfully: {title}")
        else:
            logger.error(f"Failed to send NTFY notification. Status: {response.status_code}")
            
    except Exception as e:
        logger.error(f"Error sending NTFY notification: {str(e)}")


def send_lead_notification(lead):
    """
    Send a notification for a new lead
    
    Args:
        lead: Lead object with customer and trip details
    """
    try:
        # Format pickup and dropoff locations
        pickup_location = lead.pickup_location or "N/A"
        dropoff_location = lead.dropoff_location or "N/A"
        
        title = f"🚗 New Lead: {lead.get_full_name}"
        message = f"""
New lead received!

Customer: {lead.get_full_name}
Phone: {lead.phone or 'N/A'}
Email: {lead.email or 'N/A'}

From: {pickup_location}
To: {dropoff_location}
Date: {lead.pickup_date or 'N/A'}
Vehicle: {lead.vehicle or 'N/A'}
Trip Type: {lead.trip_type or 'N/A'}
Estimated Price: ${lead.estimated_price or 'N/A'}

Lead ID: #{lead.id}
Priority: {lead.priority}
        """.strip()
        
        tags = ["car", "money", "new"]
        
        send_ntfy_notification(title, message, priority="high", tags=tags)
        
    except Exception as e:
        logger.error(f"Error sending lead notification: {str(e)}")


def add_utm_to_metadata(metadata: Dict[str, Any], reservation) -> Dict[str, Any]:
    """
    Add UTM parameters from a reservation to Stripe metadata.
    
    Args:
        metadata: Existing Stripe metadata dictionary
        reservation: Reservation object with UTM fields
        
    Returns:
        Updated metadata dictionary with UTM parameters
    """
    utm_params = ['gclid', 'utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content']
    
    for param in utm_params:
        value = getattr(reservation, param, None)
        if value:
            metadata[param] = value
            logger.info(f"Added UTM parameter to Stripe metadata: {param} = {value}")
    
    return metadata
