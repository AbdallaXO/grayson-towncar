"""
Next-day confirmation SMS: build rows from legs, generate message by trip type, send via Twilio.
Used after validating flights; can also export CSV for external workflows.
"""
import csv
import io
import logging
from django.conf import settings
from django.utils import timezone

from reservations.models import Leg

logger = logging.getLogger(__name__)


def _format_time(t):
    """Format time as '3:30 AM' (12-hour, no leading zero on hour)."""
    if t is None:
        return ""
    h, m = t.hour, t.minute
    ampm = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return f"{h12}:{m:02d} {ampm}"


# Office contact for automated-message footer
OFFICE_PHONE = "407-212-7190"
AUTOMATED_FOOTER = (
    "\n"
    f"Questions or changes? Call/text our office: {OFFICE_PHONE}\n"
    "This is an automated message — please do not reply to this number."
)

# Trip type -> CSV/label (e.g. "Departure" for return if you prefer)
TRIP_TYPE_LABELS = {
    "arrival": "Arrival",
    "return": "Departure",
    "cruise": "Cruise Transfer",
    "other": "Other",
}


def get_legs_for_confirmation(target_date):
    """Legs for the given date, excluding refunded reservations, ordered by pickup time."""
    return (
        Leg.objects.filter(pickup_date=target_date)
        .exclude(reservation__refund_status="completed")
        .select_related(
            "reservation",
            "reservation__customer",
            "reservation__vehicle",
            "reservation__travel_agent",
            "flight_information",
            "cruise_information",
        )
        .order_by("pickup_time")
    )


def _format_datetime(dt):
    """Format datetime as '5:00 AM' (12-hour) in local time."""
    if dt is None:
        return ""
    if timezone.is_aware(dt):
        dt = timezone.make_naive(dt, timezone.get_current_timezone())
    return _format_time(dt.time())


def leg_to_row(leg):
    """Build one CSV-style row (dict) for a leg."""
    r = leg.reservation
    c = r.customer
    vehicle = r.vehicle
    trip_type = leg.get_trip_type()
    cruise_direction = leg.get_cruise_direction()  # 'to_cruise', 'from_cruise', or None
    is_airport_pickup = leg.is_airport_pickup()
    flight = leg.flight_information
    cruise = leg.cruise_information
    travel_agent = r.travel_agent

    # Landing time for arrival OR airport→cruise legs (scheduled gate arrival or runway arrival)
    landing_time = ""
    if flight:
        landing_time = _format_datetime(
            getattr(flight, "scheduled_gate_arrival_local", None)
            or getattr(flight, "scheduled_arrival_local", None)
        )

    # Car seat details only if reservation has car seats (e.g. "1 Booster seat")
    car_seats_detail = None
    if r.need_carseats and r.display_carseats():
        raw = r.display_carseats()
        car_seats_detail = (
            raw.replace("Rear-Facing", "Rear-Facing seat")
            .replace("Forward-Facing", "Forward-Facing seat")
            .replace("Booster", "Booster seat")
        )

    # Finer-grained cruise label for display
    if trip_type == "cruise" and cruise_direction == "to_cruise" and is_airport_pickup:
        display_trip_type = "Cruise (MCO)"
    elif trip_type == "cruise" and cruise_direction == "to_cruise":
        display_trip_type = "Cruise (To Port)"
    elif trip_type == "cruise" and cruise_direction == "from_cruise":
        display_trip_type = "Cruise (From Port)"
    else:
        display_trip_type = TRIP_TYPE_LABELS.get(trip_type, "Other")

    return {
        "leg_id": leg.id,
        "guest_name": c.get_full_name() if c else "",
        "phone_number": c.phone_number if c else "",
        "pickup_date": leg.pickup_date.strftime("%Y-%m-%d") if leg.pickup_date else "",
        "pickup_time": _format_time(leg.pickup_time) if leg.pickup_time else "",
        "pickup_location": leg.pickup_location or "",
        "dropoff_location": leg.dropoff_location or "",
        "trip_type": display_trip_type,
        "vehicle_type": vehicle.get_vehicle_type_display() if vehicle else "",
        "passenger_count": r.passenger_count or 0,
        "suitcase_count": r.luggage_count or 0,
        "car_seats": r.display_carseats() if r.need_carseats else "No car seats",
        "car_seats_detail": car_seats_detail,
        "grocery_stop": "Yes" if r.store_stop else "No",
        "travel_agent": travel_agent.agent_name if travel_agent else "",
        "airline": (flight.airline or "") if flight else "",
        "airline_display": (flight.airline_display_name or flight.airline or "").strip() if flight else "",
        "flight_number": (flight.flight_number or "").strip() if flight else "",
        "landing_time": landing_time,
        "cruise_line": (cruise.cruise_line or "").strip() if cruise else "",
        "cruise_direction": cruise_direction,
        "is_airport_pickup": is_airport_pickup,
        "first_name": (c.first_name or "").strip() if c else "Guest",
    }


def get_confirmation_message(leg, row):
    """
    Generate confirmation SMS per templates:
    - Template 1: Departure (hotel → airport)
    - Template 2: Arrival (airport → hotel), with car seats / Publix only when applicable
    - Template 3: Port Canaveral pickup (cruise terminal)
    - Template 4: Other (generic)
    """
    first_name = row.get("first_name") or "Guest"
    pickup_time = row.get("pickup_time", "")
    pickup_location = row.get("pickup_location", "")
    dropoff_location = row.get("dropoff_location", "")
    car_seats_detail = row.get("car_seats_detail")
    airline_display = row.get("airline_display", "").strip()
    flight_number = (row.get("flight_number") or "").strip()
    landing_time = (row.get("landing_time") or "").strip()
    cruise_line = (row.get("cruise_line") or "").strip()
    store_stop = row.get("grocery_stop") == "Yes"

    # Build "Southwest Airlines flight WN3577" or partial
    airline_flight = ""
    if airline_display and flight_number:
        airline_flight = f"{airline_display} flight {flight_number}"
    elif flight_number:
        airline_flight = f"flight {flight_number}"
    elif airline_display:
        airline_flight = airline_display

    trip_type = leg.get_trip_type()

    # --- DEPARTURE: Hotel → Airport ---
    if trip_type == "return":
        msg = (
            f"Hi {first_name}, Grayson Towncar confirming your transfer from {pickup_location} to the airport tomorrow.\n"
            f"Pickup Time: {pickup_time}\n"
            "Meet: Main lobby entrance — your chauffeur will be waiting out front."
        )
        if car_seats_detail:
            msg += f"\nCar seats: {car_seats_detail} will be installed."
        msg += "\nSafe travels!" + AUTOMATED_FOOTER
        return msg

    # --- ARRIVAL: Airport → Hotel ---
    # Landing time comes from leg.flight_information; refresh flights on the legs dashboard to populate.
    if trip_type == "arrival":
        msg = (
            f"Hi {first_name}, Grayson Towncar confirming your transfer from MCO to {dropoff_location} tomorrow.\n"
            "\n"
        )
        if landing_time:
            tracking_line = f"We'll be tracking your flight (landing {landing_time}). "
        else:
            tracking_line = "We'll be tracking your flight. "
        msg += (
            tracking_line
            + "If your flight is delayed, no need to call — we'll adjust automatically.\n"
            "\n"
            "Please make sure your phone is off airplane mode when you land — "
            "your chauffeur will contact you and meet you inside baggage claim with a name sign."
        )
        if car_seats_detail:
            msg += f"\nCar seats: {car_seats_detail} will be installed."
        if store_stop:
            msg += (
                "\nGrocery stop: We'll stop at Publix (9930 Universal Blvd) on the way. "
                "We recommend pre-ordering online for a quicker pickup."
            )
        msg += "\n\nSafe travels — we look forward to welcoming you!" + AUTOMATED_FOOTER
        return msg

    # --- CRUISE TRANSFERS ---
    if trip_type == "cruise":
        cruise_direction = row.get("cruise_direction")
        is_airport_pickup = row.get("is_airport_pickup", False)
        terminal = f"{cruise_line} terminal at Port Canaveral" if cruise_line else "Port Canaveral"

        # Airport → Cruise Port (arrival-style: flight tracking, baggage claim, then drive to port)
        if cruise_direction == "to_cruise" and is_airport_pickup:
            msg = (
                f"Hi {first_name}, Grayson Towncar confirming your transfer from MCO to {terminal} tomorrow.\n"
                "\n"
            )
            if landing_time:
                tracking_line = f"We'll be tracking your flight (landing {landing_time}). "
            else:
                tracking_line = "We'll be tracking your flight. "
            msg += (
                tracking_line
                + "If your flight is delayed, no need to call — we'll adjust automatically.\n"
                "\n"
                "Please make sure your phone is off airplane mode when you land — "
                "your chauffeur will contact you and meet you inside baggage claim with a name sign."
            )
            if car_seats_detail:
                msg += f"\nCar seats: {car_seats_detail} will be installed."
            msg += "\n\nSafe travels — we look forward to welcoming you!" + AUTOMATED_FOOTER
            return msg

        # Hotel/Resort → Cruise Port (departure-style: pickup at lobby, drive to port)
        if cruise_direction == "to_cruise":
            msg = (
                f"Hi {first_name}, Grayson Towncar confirming your transfer from {pickup_location} to {terminal} tomorrow.\n"
                f"Pickup Time: {pickup_time}\n"
                "Meet: Main lobby entrance — your chauffeur will be waiting out front."
            )
            if car_seats_detail:
                msg += f"\nCar seats: {car_seats_detail} will be installed."
            msg += "\nSafe travels!" + AUTOMATED_FOOTER
            return msg

        # Cruise Port → Hotel (from_cruise: chauffeur meets at terminal)
        msg = (
            f"Hi {first_name}, Grayson Towncar confirming your transfer from the {terminal} to {dropoff_location} tomorrow.\n"
            "\n"
            "Your chauffeur will call/text you once they arrive at the terminal."
        )
        if car_seats_detail:
            msg += f"\nCar seats: {car_seats_detail} will be installed."
        msg += "\n\nSafe travels!" + AUTOMATED_FOOTER
        return msg

    # --- OTHER / GENERIC ---
    msg = (
        f"Hi {first_name}, Grayson Towncar confirming your ride tomorrow.\n"
        "\n"
        f"Pickup: {pickup_time} at {pickup_location}\n"
        f"Dropoff: {dropoff_location}"
    )
    if car_seats_detail:
        msg += f"\nCar seats: {car_seats_detail} will be installed."
    msg += "\n\nSafe travels!" + AUTOMATED_FOOTER
    return msg


def export_confirmations_csv(target_date):
    """Return CSV bytes (UTF-8) for legs on target_date. Columns match your existing CSV format."""
    legs = get_legs_for_confirmation(target_date)
    if not legs:
        return None

    fieldnames = [
        "guest_name",
        "phone_number",
        "pickup_date",
        "pickup_time",
        "pickup_location",
        "dropoff_location",
        "trip_type",
        "vehicle_type",
        "passenger_count",
        "suitcase_count",
        "car_seats",
        "grocery_stop",
        "travel_agent",
        "airline",
        "flight_number",
    ]
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for leg in legs:
        row = leg_to_row(leg)
        writer.writerow(row)
    return output.getvalue().encode("utf-8")


def send_confirmation_via_twilio(leg, row, message):
    """
    Send one SMS via Twilio. Returns (success: bool, error_message: str or None).
    """
    sid = (getattr(settings, "TWILIO_ACCOUNT_SID", None) or "").strip()
    auth = (getattr(settings, "TWILIO_AUTH_TOKEN", None) or "").strip()
    from_number = (getattr(settings, "TWILIO_PHONE_NUMBER", None) or "").strip()

    if not sid or not auth or not from_number:
        return False, "Twilio not configured (TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER)"

    # Twilio expects E.164; if US 10-digit, prepend +1
    if from_number and len(from_number) == 10 and from_number.isdigit():
        from_number = "+1" + from_number
    elif from_number and not from_number.startswith("+"):
        from_number = "+1" + from_number.lstrip("1")

    to = (row.get("phone_number") or "").strip()
    if not to:
        return False, "No phone number"

    try:
        from twilio.rest import Client
        client = Client(sid, auth)
        client.messages.create(
            body=message,
            from_=from_number,
            to=to,
        )
        return True, None
    except Exception as e:
        logger.exception("Twilio send failed for leg %s", leg.id)
        return False, str(e)


def send_confirmations_for_date(target_date, skip_already_sent=True, excluded_leg_ids=None):
    """
    For each leg on target_date, generate message and send via Twilio.
    Returns dict: sent=int, failed=int, skipped=int, errors=[(leg_id, msg), ...].
    If skip_already_sent=True, legs with confirmation_sms_sent_at set are skipped.
    If excluded_leg_ids is provided, those legs are skipped.
    """
    legs = get_legs_for_confirmation(target_date)
    sent = 0
    failed = 0
    skipped = 0
    errors = []

    for leg in legs:
        if excluded_leg_ids and leg.id in excluded_leg_ids:
            skipped += 1
            continue
        if skip_already_sent and getattr(leg, "confirmation_sms_sent_at", None):
            skipped += 1
            continue

        row = leg_to_row(leg)
        message = get_confirmation_message(leg, row)
        ok, err = send_confirmation_via_twilio(leg, row, message)
        if ok:
            sent += 1
            if hasattr(leg, "confirmation_sms_sent_at"):
                leg.confirmation_sms_sent_at = timezone.now()
                leg.save(update_fields=["confirmation_sms_sent_at"])
        else:
            failed += 1
            errors.append((leg.id, err or "Unknown error"))

    return {"sent": sent, "failed": failed, "skipped": skipped, "errors": errors}


def twilio_configured():
    """Check that all three Twilio env vars are set and non-empty."""
    sid = (getattr(settings, "TWILIO_ACCOUNT_SID", None) or "").strip()
    auth = (getattr(settings, "TWILIO_AUTH_TOKEN", None) or "").strip()
    phone = (getattr(settings, "TWILIO_PHONE_NUMBER", None) or "").strip()
    return bool(sid and auth and phone)