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
    if not getattr(settings, "NTFY_ENABLED", False):
        logger.info("NTFY notifications are disabled")
        return

    try:
        topic = getattr(settings, "NTFY_TOPIC", "grayson-leads")
        server = getattr(settings, "NTFY_SERVER", "https://ntfy.sh")

        url = f"{server}/{topic}"

        headers = {
            "Content-Type": "text/plain",
        }

        if priority != "default":
            headers["Priority"] = priority

        if tags:
            headers["Tags"] = ",".join(tags)

        response = requests.post(url, data=message, headers=headers, timeout=10)

        if response.status_code == 200:
            logger.info(f"NTFY notification sent successfully: {title}")
        else:
            logger.error(
                f"Failed to send NTFY notification. Status: {response.status_code}"
            )

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
Phone: {lead.phone or "N/A"}
Email: {lead.email or "N/A"}

From: {pickup_location}
To: {dropoff_location}
Date: {lead.pickup_date or "N/A"}
Vehicle: {lead.vehicle or "N/A"}
Trip Type: {lead.trip_type or "N/A"}
Estimated Price: ${lead.estimated_price or "N/A"}

Lead ID: #{lead.id}
Priority: {lead.priority}
        """.strip()

        tags = ["car", "money", "new"]

        send_ntfy_notification(title, message, priority="high", tags=tags)

    except Exception as e:
        logger.error(f"Error sending lead notification: {str(e)}")


def send_driver_status_notification(leg, old_status=None, new_status=None):
    """
    Send a notification for driver leg status updates

    Args:
        leg: Leg object with driver and reservation details
        old_status: Previous status (optional)
        new_status: New status (optional, defaults to leg.status)
    """
    try:
        if not leg.driver:
            logger.warning(f"No driver assigned to leg {leg.id}")
            return

        if new_status is None:
            new_status = leg.status

        # Get status display information
        status_info = get_driver_status_info(new_status)
        
        # Format customer information
        customer_name = leg.reservation.customer.get_full_name()
        driver_name = leg.driver.__str__()
        
        # Format pickup/dropoff locations
        pickup_location = leg.pickup_location or "N/A"
        dropoff_location = leg.dropoff_location or "N/A"
        
        # Format date and time
        pickup_datetime = f"{leg.pickup_date} at {leg.pickup_time}"
        
        # Create natural notification title and message
        title = f"{status_info['emoji']} {driver_name} {status_info['action_phrase']}"
        
        # Create a natural message based on status
        if new_status == "on-location":
            message = f"{driver_name} is on location for {customer_name}'s pickup"
        elif new_status == "on-the-way":
            message = f"{driver_name} is on the way to pickup {customer_name}"
        elif new_status == "picked-up":
            message = f"{driver_name} has picked up {customer_name}"
        elif new_status == "confirmed":
            message = f"{driver_name} has confirmed the job for {customer_name}"
        elif new_status == "completed":
            message = f"{driver_name} has completed the trip for {customer_name}"
        else:
            message = f"{driver_name} - {status_info['label']} for {customer_name}"
        
        # Add trip details
        message += f"\n\n📍 {pickup_location} → {dropoff_location}"
        message += f"\n🕐 {pickup_datetime}"
        message += f"\n🚗 {leg.reservation.vehicle or 'Vehicle TBD'}"
        message += f"\n📋 Reservation #{leg.reservation.id}"

        # Add status change info if old status provided
        if old_status and old_status != new_status:
            old_status_info = get_driver_status_info(old_status)
            message += f"\n\nChanged from: {old_status_info['label']}"

        # Set appropriate tags and priority
        tags = ["car", "driver", status_info['tag']]
        priority = status_info['priority']

        # Send notification to driver-specific topic
        send_driver_ntfy_notification(title, message, priority=priority, tags=tags)

    except Exception as e:
        logger.error(f"Error sending driver status notification: {str(e)}")


def get_driver_status_info(status):
    """
    Get display information for driver status
    
    Args:
        status: Driver status string
        
    Returns:
        dict: Status information including emoji, label, tag, and priority
    """
    status_map = {
        "in-progress": {
            "emoji": "⏳",
            "label": "In Progress",
            "action_phrase": "is working on",
            "tag": "in_progress",
            "priority": "default"
        },
        "confirmed": {
            "emoji": "✅",
            "label": "Confirmed",
            "action_phrase": "confirmed",
            "tag": "confirmed",
            "priority": "high"
        },
        "on-the-way": {
            "emoji": "🚗",
            "label": "On the Way",
            "action_phrase": "is on the way",
            "tag": "on_the_way",
            "priority": "high"
        },
        "on-location": {
            "emoji": "📍",
            "label": "On Location",
            "action_phrase": "is on location",
            "tag": "on_location",
            "priority": "high"
        },
        "picked-up": {
            "emoji": "👥",
            "label": "Picked Up",
            "action_phrase": "picked up",
            "tag": "picked_up",
            "priority": "high"
        },
        "completed": {
            "emoji": "🎉",
            "label": "Completed",
            "action_phrase": "completed",
            "tag": "completed",
            "priority": "default"
        }
    }
    
    return status_map.get(status, {
        "emoji": "❓",
        "label": status.title(),
        "action_phrase": "updated status to",
        "tag": "unknown",
        "priority": "default"
    })


def send_driver_ntfy_notification(title, message, priority="default", tags=None):
    """
    Send a notification via ntfy to the driver topic

    Args:
        title (str): Notification title
        message (str): Notification message
        priority (str): Priority level (min, low, default, high, urgent)
        tags (list): List of tags for the notification
    """
    if not getattr(settings, "NTFY_ENABLED", False):
        logger.info("NTFY notifications are disabled")
        return

    try:
        # Use driver-specific topic
        topic = getattr(settings, "NTFY_DRIVER_TOPIC", "grayson-driver-noti")
        server = getattr(settings, "NTFY_SERVER", "https://ntfy.sh")

        url = f"{server}/{topic}"

        headers = {
            "Content-Type": "text/plain",
        }

        if priority != "default":
            headers["Priority"] = priority

        if tags:
            headers["Tags"] = ",".join(tags)

        response = requests.post(url, data=message, headers=headers, timeout=10)

        if response.status_code == 200:
            logger.info(f"Driver NTFY notification sent successfully: {title}")
        else:
            logger.error(
                f"Failed to send driver NTFY notification. Status: {response.status_code}"
            )

    except Exception as e:
        logger.error(f"Error sending driver NTFY notification: {str(e)}")


def add_utm_to_metadata(metadata: Dict[str, Any], reservation) -> Dict[str, Any]:
    """
    Add UTM parameters from a reservation to Stripe metadata.

    Args:
        metadata: Existing Stripe metadata dictionary
        reservation: Reservation object with UTM fields

    Returns:
        Updated metadata dictionary with UTM parameters
    """
    utm_params = [
        "gclid",
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
    ]

    for param in utm_params:
        value = getattr(reservation, param, None)
        if value:
            metadata[param] = value
            logger.info(f"Added UTM parameter to Stripe metadata: {param} = {value}")

    return metadata