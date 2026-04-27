from django.contrib import messages
from django.shortcuts import redirect
from django.conf import settings
import requests
import re
import threading
from .forms import (
    ReservationForm,
    CustomerForm,
    LegForm,
    FlightForm,
    CruiseForm,
)
from decimal import Decimal
from datetime import time, date, datetime
import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)


def _run_in_background(func, *args, **kwargs):
    """Run a function in a background daemon thread to avoid blocking the request."""
    def _wrapper():
        try:
            func(*args, **kwargs)
        except Exception as e:
            logger.error(f"Background task error in {func.__name__}: {e}")
    thread = threading.Thread(target=_wrapper, daemon=True)
    thread.start()


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
    flight1,leg1, cruise1, and if trip_type is round_trip, it returns a flight2 form, leg2 form, and cruise2 form"""
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
    cruise1_form = CruiseForm(prefix="cruise1")
    leg1_form = LegForm(prefix="leg1", label_suffix="*", route=rate.route)
    # conditional forms if its a roundtrip
    flight2_form = FlightForm(prefix="flight2") if trip_type == "round_trip" else None
    cruise2_form = CruiseForm(prefix="cruise2") if trip_type == "round_trip" else None
    leg2_form = (
        LegForm(prefix="leg2", label_suffix="*", route=rate.route) if trip_type == "round_trip" else None
    )

    return (
        customer_form,
        reservation_form,
        flight1_form,
        cruise1_form,
        leg1_form,
        flight2_form,
        cruise2_form,
        leg2_form,
    )


def returns_post_form(request, trip_type, rate):
    """Returns Forms with Posted Data, just to Avoid Redundancy of repeating everything in the view
    returns customer,reservatiom,flight1,cruise1,leg1, flight2, cruise2 and leg2 if trip_type == round_trip, else oneway"""
    customer_form = CustomerForm(request.POST)
    reservation_form = ReservationForm(request.POST, rate=rate)
    flight1_form = FlightForm(request.POST, prefix="flight1")
    cruise1_form = CruiseForm(request.POST, prefix="cruise1")
    leg1_form = LegForm(request.POST, prefix="leg1", route=rate.route)
    flight2_form = (
        FlightForm(request.POST, prefix="flight2")
        if trip_type == "round_trip"
        else None
    )
    cruise2_form = (
        CruiseForm(request.POST, prefix="cruise2")
        if trip_type == "round_trip"
        else None
    )
    leg2_form = (
        LegForm(request.POST, prefix="leg2", route=rate.route) if trip_type == "round_trip" else None
    )
    return (
        customer_form,
        reservation_form,
        flight1_form,
        cruise1_form,
        leg1_form,
        flight2_form,
        cruise2_form,
        leg2_form,
    )


def validate_forms(
    customer_form,
    reservation_form,
    flight1_form,
    cruise1_form,
    leg1_form,
    flight2_form,
    cruise2_form,
    leg2_form,
    trip_type,
):
    """Validated all the submitted forms based on trip_type
    received forms for customer, reservation, flight1, cruise1, leg1, flight2, cruise2 if round_trip, leg2 if round_trip
    returns true if all forms are valid
    if trip_type is oneway will always return true for oneway+ roundtrip"""
    customer_valid = customer_form.is_valid()
    reservation_valid = reservation_form.is_valid()
    flight1_valid = flight1_form.is_valid()
    cruise1_valid = cruise1_form.is_valid()
    leg1_valid = leg1_form.is_valid()

    if trip_type != "round_trip":
        leg2_valid = True
        flight2_valid = True
        cruise2_valid = True
    else:
        leg2_valid = leg2_form.is_valid()
        flight2_valid = flight2_form.is_valid()
        cruise2_valid = cruise2_form.is_valid()
    forms_valid = all(
        [
            customer_valid,
            reservation_valid,
            flight1_valid,
            cruise1_valid,
            leg1_valid,
            flight2_valid,
            cruise2_valid,
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
    "Air Canada",
    "Allegiant Air",
    "Avelo Airlines",
    "Breeze Airways",
    "British Airways",
    "Sun Country Airlines",
    "Virgin Atlantic",
]

CRUISE_LINES = [
    "Disney Cruise Line",
    "Royal Caribbean",
    "Carnival Cruise Line",
    "Norwegian Cruise Line",
    "MSC Cruises",
    "Celebrity Cruises",
    "Princess Cruises",
    "Holland America Line",
    "Cunard Line",
    "Virgin Voyages",
]


def normalize_flight_number(flight_number_input):
    """
    Normalize flight number by removing letters and keeping only digits.
    
    Args:
        flight_number_input (str): Raw flight number input (e.g., "WN1234", "1234", "AA-5678")
        
    Returns:
        str: Cleaned flight number with only digits (e.g., "1234", "5678")
    """
    if not flight_number_input:
        return ""
    
    # Remove all non-digit characters
    flight_number = ''.join(c for c in str(flight_number_input) if c.isdigit())
    
    return flight_number


def extract_airline_from_flight_number(flight_number_input):
    """
    Extract airline IATA code from flight number if present.
    
    Examples:
        "WN1234" -> "WN"
        "B61234" -> "B6"
        "AA5678" -> "AA"
        "1234" -> None (no airline code)
    
    Args:
        flight_number_input (str): Flight number that might contain airline code
        
    Returns:
        str or None: Extracted airline code, or None if not found
    """
    if not flight_number_input:
        return None
    
    # Convert to uppercase and strip
    flight_num = str(flight_number_input).strip().upper()
    
    # List of all known IATA codes (2 letters)
    known_codes = ['AA', 'DL', 'UA', 'WN', 'B6', 'NK', 'F9', 'AS', 'HA', 'G4', 
                   'AC', 'XP', 'MX', 'BA', 'SY', 'VS', 'WS']
    
    # Check if flight number starts with a known 2-letter code
    if len(flight_num) >= 2:
        potential_code = flight_num[:2]
        if potential_code in known_codes:
            return potential_code
    
    return None


def normalize_airline(airline_input):
    """
    Normalize airline input to a standard IATA code format.
    
    Handles various input formats:
    - Full airline names: "JetBlue", "Southwest Airlines"
    - Variations with spaces: "jet blue", "south west"
    - IATA codes: "B6", "WN"
    - Combinations: "jet blue b6", "southwest wn"
    - Case variations: "JETBLUE", "jetBlue", "JET BLUE"
    - Common misspellings/abbreviations: "wna" for "wn"
    
    Args:
        airline_input (str): Raw airline input from user
        
    Returns:
        str: Normalized IATA code (e.g., "B6", "WN", "AA") or original input if not recognized
    """
    if not airline_input:
        return ""
    
    # Convert to uppercase and strip whitespace
    airline = airline_input.strip().upper()
    
    # Remove extra spaces and normalize
    airline = ' '.join(airline.split())
    
    # Comprehensive airline mapping
    # Maps various airline name formats and codes to standard IATA codes
    airline_mapping = {
        # JetBlue variations
        'JETBLUE': 'B6',
        'JET BLUE': 'B6',
        'JETBLUE AIRWAYS': 'B6',
        'JET BLUE AIRWAYS': 'B6',
        'B6': 'B6',
        
        # Southwest variations
        'SOUTHWEST': 'WN',
        'SOUTHWEST AIRLINES': 'WN',
        'SOUTH WEST': 'WN',
        'SOUTH WEST AIRLINES': 'WN',
        'WN': 'WN',
        'WNA': 'WN',  # Common typo
        
        # American Airlines variations
        'AMERICAN': 'AA',
        'AMERICAN AIRLINES': 'AA',
        'AMERICAN AIR': 'AA',
        'AA': 'AA',
        
        # Delta variations
        'DELTA': 'DL',
        'DELTA AIRLINES': 'DL',
        'DELTA AIR LINES': 'DL',
        'DELTA AIR': 'DL',
        'DL': 'DL',
        
        # United variations
        'UNITED': 'UA',
        'UNITED AIRLINES': 'UA',
        'UNITED AIR': 'UA',
        'UA': 'UA',
        
        # Spirit variations
        'SPIRIT': 'NK',
        'SPIRIT AIRLINES': 'NK',
        'SPIRIT AIR': 'NK',
        'NK': 'NK',
        
        # Frontier variations
        'FRONTIER': 'F9',
        'FRONTIER AIRLINES': 'F9',
        'FRONTIER AIR': 'F9',
        'F9': 'F9',
        
        # Alaska variations
        'ALASKA': 'AS',
        'ALASKA AIRLINES': 'AS',
        'ALASKA AIR': 'AS',
        'AS': 'AS',
                
        # Allegiant variations
        'ALLEGIANT': 'G4',
        'ALLEGIANT AIR': 'G4',
        'ALLEGIANT AIRLINES': 'G4',
        'G4': 'G4',
        
        # Air Canada variations
        'AIR CANADA': 'AC',
        'AIR CANADA AIRLINES': 'AC',
        'AC': 'AC',
        
        # Avelo variations
        'AVELO': 'XP',
        'AVELO AIRLINES': 'XP',
        'AVELO AIR': 'XP',
        'XP': 'XP',
        
        # Breeze variations
        'BREEZE': 'MX',
        'BREEZE AIRWAYS': 'MX',
        'BREEZE AIR': 'MX',
        'MX': 'MX',
        
        # British Airways variations
        'BRITISH AIRWAYS': 'BA',
        'BRITISH': 'BA',
        'BA': 'BA',
        
        # Sun Country variations
        'SUN COUNTRY': 'SY',
        'SUN COUNTRY AIRLINES': 'SY',
        'SUN COUNTRY AIR': 'SY',
        'SY': 'SY',
        
        # Virgin Atlantic variations
        'VIRGIN ATLANTIC': 'VS',
        'VIRGIN ATLANTIC AIRWAYS': 'VS',
        'VIRGIN': 'VS',
        'VS': 'VS',
        
        # WestJet variations
        'WESTJET': 'WS',
        'WEST JET': 'WS',
        'WESTJET AIRLINES': 'WS',
        'WEST JET AIRLINES': 'WS',
        'WS': 'WS',
        'WJA': 'WS',  # FlightAware code, but normalize to IATA
    }
    
    # First, try exact match
    if airline in airline_mapping:
        return airline_mapping[airline]
    
    # Check if input contains a known IATA code (2 characters)
    # This handles cases like "jet blue b6" or "southwest wn"
    # Look for 2-letter codes at the end or standalone
    code_pattern = r'\b([A-Z]{2})\b'
    codes_found = re.findall(code_pattern, airline)
    
    for code in codes_found:
        if code in airline_mapping.values():
            return code
    
    # Check if input contains airline name keywords
    # This handles partial matches like "jet blue" in "jet blue airways"
    for key, value in airline_mapping.items():
        # Remove common suffixes for matching
        key_clean = key.replace(' AIRLINES', '').replace(' AIR', '').replace(' AIRWAYS', '')
        airline_clean = airline.replace(' AIRLINES', '').replace(' AIR', '').replace(' AIRWAYS', '')
        
        if key_clean in airline_clean or airline_clean in key_clean:
            return value
    
    # If no match found, return original input (normalized to uppercase)
    # This allows for airlines we don't have in our mapping
    return airline


def get_airline_display_name(iata_code):
    """
    Get the full display name for an airline from its IATA code.
    
    Args:
        iata_code (str): IATA airline code (e.g., "DL", "WN", "B6")
        
    Returns:
        str: Full airline name (e.g., "Delta Airlines", "Southwest Airlines") or IATA code if not found
    """
    if not iata_code:
        return ""
    
    iata_code = iata_code.strip().upper()
    
    # Mapping from IATA codes to display names
    display_name_mapping = {
        'AA': 'American Airlines',
        'DL': 'Delta Airlines',
        'UA': 'United Airlines',
        'WN': 'Southwest Airlines',
        'B6': 'JetBlue Airways',
        'NK': 'Spirit Airlines',
        'F9': 'Frontier Airlines',
        'AS': 'Alaska Airlines',
        'G4': 'Allegiant Air',
        'AC': 'Air Canada',
        'XP': 'Avelo Airlines',
        'MX': 'Breeze Airways',
        'BA': 'British Airways',
        'SY': 'Sun Country Airlines',
        'VS': 'Virgin Atlantic',
        'WS': 'WestJet',
    }
    
    return display_name_mapping.get(iata_code, iata_code)


def get_flightaware_code(iata_code):
    """
    Convert IATA airline code to FlightAware/AeroAPI code.
    Most airlines use the same code, but some have different codes.
    
    Args:
        iata_code (str): IATA airline code (e.g., "B6", "F9", "G4")
        
    Returns:
        str: FlightAware code for API calls (e.g., "JBU", "FFT", "AAY")
    """
    if not iata_code:
        return ""
    
    iata_code = iata_code.strip().upper()
    
    # Mapping from IATA codes to FlightAware codes
    # Most airlines use the same code, only special cases are mapped
    flightaware_mapping = {
        'B6': 'JBU',  # JetBlue: IATA is B6, FlightAware uses JBU
        'F9': 'FFT',  # Frontier: IATA is F9, FlightAware uses FFT
        'G4': 'AAY',  # Allegiant: IATA is G4, FlightAware uses AAY
        'AS': 'ASA',  # Alaska Airlines: IATA is AS, FlightAware uses ASA
        'XP': 'VXP',  # Avelo: IATA is XP, FlightAware uses VXP
        'MX': 'MXY',  # Breeze: IATA is MX, FlightAware uses MXY
        'WS': 'WJA',  # WestJet: IATA is WS, FlightAware uses WJA
        # All others use the same code
    }
    
    # Return FlightAware code if mapped, otherwise return IATA code
    return flightaware_mapping.get(iata_code, iata_code)


def adjust_reservation_for_stop_fee_delta(reservation, fee_delta):
    """Apply a stop-fee delta (positive or negative) to the reservation's
    additional_charges and total_price. Leaves all OTHER fee components
    (late-night, carseats, gratuity) untouched — preserves whatever was set
    at booking time.

    Use from inline LegStop CRUD: capture the old fee before mutating,
    then call this with (new_fee - old_fee). Adding a $40 stop → delta=+40.
    Deleting a $15 stop → delta=-15. Editing $40 → $50 → delta=+10.
    """
    if fee_delta is None or fee_delta == 0:
        return reservation.total_price
    delta = Decimal(str(fee_delta)).quantize(Decimal("0.01"))
    reservation.additional_charges = (
        (reservation.additional_charges or Decimal("0.00")) + delta
    ).quantize(Decimal("0.01"))
    reservation.total_price = (
        (reservation.total_price or Decimal("0.00")) + delta
    ).quantize(Decimal("0.01"))
    reservation.save(update_fields=["additional_charges", "total_price"])
    return reservation.total_price


def extra_charges(reservation):
    total_extra = Decimal(0)
    for leg in reservation.legs.all():
        if leg.pickup_time >= time(22, 0) or leg.pickup_time < time(6, 0):
            total_extra += Decimal(20.00)
            logger.info(
                f"Added ${total_extra} on Reservation #{reservation.id} for {reservation.customer.get_full_name()}"
            )

    # Extra car seat / booster fees (per-leg, using effective vehicle for each leg)
    extra_seat_fee = Decimal(0)
    for leg in reservation.legs.all():
        leg_vehicle = leg.effective_vehicle
        leg_extra_cs = leg.effective_extra_carseats or 0
        leg_extra_bs = leg.effective_extra_boosters or 0
        if leg_vehicle and (leg_extra_cs or leg_extra_bs):
            extra_seat_fee += Decimal(leg_extra_cs) * leg_vehicle.extra_carseat_fee
            extra_seat_fee += Decimal(leg_extra_bs) * leg_vehicle.extra_booster_fee
    if extra_seat_fee:
        total_extra += extra_seat_fee
        logger.info(
            f"Added ${extra_seat_fee} extra seat fee on Reservation #{reservation.id} for {reservation.customer.get_full_name()}"
        )

    # Calculate gratuity if specified
    gratuity_amount = Decimal(0)
    if reservation.gratuity_percentage and reservation.gratuity_percentage > 0:
        gratuity_amount = (reservation.base_price * reservation.gratuity_percentage) / Decimal(100)
        reservation.gratuity_amount = gratuity_amount
        logger.info(
            f"Added ${gratuity_amount} gratuity ({reservation.gratuity_percentage}%) on Reservation #{reservation.id} for {reservation.customer.get_full_name()}"
        )

    reservation.additional_charges = total_extra
    reservation.total_price = reservation.base_price + total_extra + gratuity_amount

    # Always add gratuity note to reservation special_requests
    if gratuity_amount > 0:
        gratuity_note = f"{int(reservation.gratuity_percentage)}% Gratuity Included (${gratuity_amount:.2f})"
        if reservation.special_requests:
            reservation.special_requests += f"\n{gratuity_note}"
        else:
            reservation.special_requests = gratuity_note

    reservation.save(update_fields=["additional_charges", "total_price", "gratuity_amount", "special_requests"])

    # Always add per-leg gratuity to private_notes (split for multi-leg)
    if gratuity_amount > 0:
        legs = list(reservation.legs.all())
        leg_count = len(legs)
        gratuity_per_leg = (gratuity_amount / Decimal(leg_count)).quantize(Decimal("0.01")) if leg_count > 1 else gratuity_amount
        for leg in legs:
            note = f"${gratuity_per_leg:.2f} Gratuity Included"
            if leg.private_notes:
                leg.private_notes = f"{leg.private_notes}\n{note}"
            else:
                leg.private_notes = note
            leg.save(update_fields=["private_notes"])

    return total_extra


def send_ntfy_notification(title, message, priority="default", tags=None):
    """
    Send a notification via ntfy (runs in background thread to avoid blocking).

    Args:
        title (str): Notification title
        message (str): Notification message
        priority (str): Priority level (min, low, default, high, urgent)
        tags (list): List of tags for the notification
    """
    if not getattr(settings, "NTFY_ENABLED", False):
        logger.info("NTFY notifications are disabled")
        return

    def _do_send():
        try:
            topic = getattr(settings, "NTFY_TOPIC", "grayson-leads")
            server = getattr(settings, "NTFY_SERVER", "https://ntfy.sh")

            url = f"{server}/{topic}"

            headers = {
                "Content-Type": "text/plain; charset=utf-8",
            }

            if title:
                headers["Title"] = title.encode("utf-8")

            if priority != "default":
                headers["Priority"] = priority

            if tags:
                headers["Tags"] = ",".join(tags)

            response = requests.post(
                url, data=message.encode("utf-8"), headers=headers, timeout=10
            )

            if response.status_code == 200:
                logger.info(f"NTFY notification sent successfully: {title}")
            else:
                logger.error(
                    f"Failed to send NTFY notification. Status: {response.status_code}"
                )

        except Exception as e:
            logger.error(f"Error sending NTFY notification: {str(e)}")

    _run_in_background(_do_send)


def send_dispatch_alert_notification(title, message, priority="urgent", tags=None):
    """
    Send a notification to the dispatch alerts channel (grayson-dispatch-alerts).
    Separate from leads and driver topics — for urgent dispatcher warnings.
    Runs in background thread to avoid blocking.
    """
    if not getattr(settings, "NTFY_ENABLED", False):
        logger.info("NTFY notifications are disabled")
        return

    def _do_send():
        try:
            topic = getattr(settings, "NTFY_DISPATCH_ALERT_TOPIC", "grayson-dispatch-alerts")
            server = getattr(settings, "NTFY_SERVER", "https://ntfy.sh")

            url = f"{server}/{topic}"

            headers = {
                "Content-Type": "text/plain; charset=utf-8",
            }

            if title:
                headers["Title"] = title.encode("utf-8")

            if priority != "default":
                headers["Priority"] = priority

            if tags:
                headers["Tags"] = ",".join(tags)

            response = requests.post(
                url, data=message.encode("utf-8"), headers=headers, timeout=10
            )

            if response.status_code == 200:
                logger.info(f"Dispatch alert sent successfully: {title}")
            else:
                logger.error(
                    f"Failed to send dispatch alert. Status: {response.status_code}"
                )

        except Exception as e:
            logger.error(f"Error sending dispatch alert: {str(e)}")

    _run_in_background(_do_send)


def send_lead_notification(lead):
    """
    Send a notification for a new lead with urgency based on pickup date proximity.
    """
    try:
        pickup_location = lead.pickup_location or "Unknown"
        dropoff_location = lead.dropoff_location or "Unknown"
        customer_name = lead.get_full_name
        trip_type_display = dict(lead.TripTypeChoices.choices).get(lead.trip_type, lead.trip_type or "N/A")
        vehicle_name = str(lead.vehicle) if lead.vehicle else "No Vehicle"

        # Determine urgency based on how soon the trip is
        urgency = ""
        priority = "high"
        tags = ["car", "money", "new"]
        days_until = None

        if lead.pickup_date:
            days_until = (lead.pickup_date - date.today()).days

            if days_until < 0:
                urgency = "PAST DATE"
                priority = "urgent"
                tags = ["rotating_light", "car", "money"]
            elif days_until == 0:
                urgency = "URGENT - TODAY"
                priority = "urgent"
                tags = ["rotating_light", "car", "money"]
            elif days_until == 1:
                urgency = "URGENT - TOMORROW"
                priority = "urgent"
                tags = ["rotating_light", "car", "money"]
            elif days_until <= 3:
                urgency = f"URGENT - In {days_until} days"
                priority = "urgent"
                tags = ["warning", "car", "money"]
            elif days_until <= 7:
                urgency = f"Soon - In {days_until} days"
                priority = "high"

        # Build title
        if urgency:
            title = f"{urgency} | {customer_name} - {pickup_location} to {dropoff_location}"
        else:
            title = f"New Lead: {customer_name} - {pickup_location} to {dropoff_location}"

        # Build message body — lead with natural sentence, then details
        date_str = lead.pickup_date.strftime("%a %b %d, %Y") if lead.pickup_date else "No date"
        price_str = f"${lead.estimated_price}" if lead.estimated_price else "No quote"

        # Human-readable opening line
        if days_until is not None and days_until == 0:
            message = f"New lead from {customer_name} needs a {trip_type_display} TODAY from {pickup_location} to {dropoff_location}"
        elif days_until is not None and days_until == 1:
            message = f"New lead from {customer_name} needs a {trip_type_display} TOMORROW from {pickup_location} to {dropoff_location}"
        elif days_until is not None and days_until <= 3:
            message = f"New lead from {customer_name} needs a {trip_type_display} in {days_until} days from {pickup_location} to {dropoff_location}"
        else:
            message = f"New lead from {customer_name} requesting a {trip_type_display} from {pickup_location} to {dropoff_location}"

        message += f"\n\nDate: {date_str}"
        if days_until is not None and days_until > 3:
            message += f" ({days_until} days away)"
        message += f"\nVehicle: {vehicle_name}"
        message += f"\nPrice: {price_str}"

        if lead.phone:
            message += f"\nPhone: {lead.phone}"
        if lead.email:
            message += f"\nEmail: {lead.email}"

        send_ntfy_notification(title, message, priority=priority, tags=tags)

    except Exception as e:
        logger.error(f"Error sending lead notification: {str(e)}")


def send_driver_status_notification(leg, old_status=None, new_status=None):
    """
    Send a notification for driver leg status updates with descriptive titles.
    Title format: "Driver action_phrase to Customer for Time - Trip Type from Location to Location"
    """
    try:
        if not leg.driver:
            logger.warning(f"No driver assigned to leg {leg.id}")
            return

        if new_status is None:
            new_status = leg.status

        status_info = get_driver_status_info(new_status)

        customer_name = leg.reservation.customer.get_full_name()
        driver_name = str(leg.driver)
        pickup_location = leg.pickup_location or "Unknown"
        dropoff_location = leg.dropoff_location or "Unknown"
        trip_type = leg.get_trip_type().title()  # Arrival, Return, Cruise, Other
        vehicle_name = str(leg.reservation.vehicle) if leg.reservation.vehicle else "Vehicle TBD"

        # Format time nicely (e.g., "2:30 PM")
        if leg.pickup_time:
            hour = leg.pickup_time.hour
            minute = leg.pickup_time.minute
            ampm = "AM" if hour < 12 else "PM"
            display_hour = hour if 1 <= hour <= 12 else (hour - 12 if hour > 12 else 12)
            time_str = f"{display_hour}:{minute:02d} {ampm}"
        else:
            time_str = "TBD"

        # Build descriptive title per status
        # e.g., "Yovanny on the way to John for his 2:30 PM Arrival from MCO to Disney"
        if new_status == "on-the-way":
            title = f"{driver_name} on the way to {customer_name} for {time_str} {trip_type} - {pickup_location} to {dropoff_location}"
        elif new_status == "on-location":
            title = f"{driver_name} on location for {customer_name}'s {time_str} {trip_type} at {pickup_location}"
        elif new_status == "picked-up":
            title = f"{driver_name} picked up {customer_name} - {trip_type} to {dropoff_location}"
        elif new_status == "confirmed":
            title = f"{driver_name} confirmed {customer_name}'s {time_str} {trip_type} - {pickup_location} to {dropoff_location}"
        elif new_status == "completed":
            title = f"{driver_name} completed {customer_name}'s {trip_type} - {dropoff_location}"
        else:
            title = f"{driver_name} {status_info['action_phrase']} - {customer_name}'s {time_str} {trip_type}"

        # Message body — lead with the same human-readable context, then details
        if new_status == "on-the-way":
            message = f"{driver_name} is on the way to {customer_name} for the {time_str} {trip_type} from {pickup_location} to {dropoff_location}"
        elif new_status == "on-location":
            message = f"{driver_name} is on location waiting for {customer_name} at {pickup_location} for the {time_str} {trip_type}"
        elif new_status == "picked-up":
            message = f"{driver_name} has picked up {customer_name} and is heading to {dropoff_location}"
        elif new_status == "confirmed":
            message = f"{driver_name} confirmed the {time_str} {trip_type} for {customer_name} from {pickup_location} to {dropoff_location}"
        elif new_status == "completed":
            message = f"{driver_name} completed {customer_name}'s {trip_type} to {dropoff_location}"
        else:
            message = f"{driver_name} — {status_info['label']} for {customer_name}'s {time_str} {trip_type}"

        message += f"\n\n{pickup_location} -> {dropoff_location}"
        message += f"\n{leg.pickup_date.strftime('%a %b %d')} at {time_str} | {vehicle_name}"
        message += f"\nRes #{leg.reservation.id}"

        if old_status and old_status != new_status:
            old_info = get_driver_status_info(old_status)
            message += f"\nPrev: {old_info['label']}"

        tags = ["car", "driver", status_info['tag']]
        priority = status_info['priority']

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
    Send a notification via ntfy to the driver topic.
    Runs in background thread to avoid blocking.

    Args:
        title (str): Notification title
        message (str): Notification message
        priority (str): Priority level (min, low, default, high, urgent)
        tags (list): List of tags for the notification
    """
    if not getattr(settings, "NTFY_ENABLED", False):
        logger.info("NTFY notifications are disabled")
        return

    def _do_send():
        try:
            # Use driver-specific topic
            topic = getattr(settings, "NTFY_DRIVER_TOPIC", "grayson-driver-noti")
            server = getattr(settings, "NTFY_SERVER", "https://ntfy.sh")

            url = f"{server}/{topic}"

            headers = {
                "Content-Type": "text/plain; charset=utf-8",
            }

            if title:
                headers["Title"] = title.encode("utf-8")

            if priority != "default":
                headers["Priority"] = priority

            if tags:
                headers["Tags"] = ",".join(tags)

            response = requests.post(
                url, data=message.encode("utf-8"), headers=headers, timeout=10
            )

            if response.status_code == 200:
                logger.info(f"Driver NTFY notification sent successfully: {title}")
            else:
                logger.error(
                    f"Failed to send driver NTFY notification. Status: {response.status_code}"
                )

        except Exception as e:
            logger.error(f"Error sending driver NTFY notification: {str(e)}")

    _run_in_background(_do_send)


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
        "fbclid",  # Facebook Click ID
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