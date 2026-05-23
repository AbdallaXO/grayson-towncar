"""
Views for the guest-facing flight-verification flow.

- `send_flight_verification_email_ajax` — staff endpoint, fired from the
  Flight Refresh Review modal's per-row button.
- `flight_verification_public` — public page the guest reaches via the
  signed link in that email; lets them confirm or correct airline +
  flight number.
"""

import json
import logging

from django.contrib.auth.decorators import login_required
from django.core.signing import BadSignature, SignatureExpired
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_POST, require_http_methods

from reservations.models import Leg
from reservations.utils import (
    normalize_airline,
    normalize_flight_number,
    get_flightaware_code,
    get_airline_display_name,
)
from .aeroapi_service import AeroAPIService, ORLANDO_AIRPORT_CODES
from .flight_verify_email import (
    parse_verify_token,
    send_flight_verification_email,
)

logger = logging.getLogger(__name__)


@login_required
@require_POST
def send_flight_verification_email_ajax(request):
    """Trigger a verification email to the leg's guest. Staff-only."""
    if not request.user.is_staff:
        return JsonResponse(
            {"success": False, "error": "Permission denied"}, status=403
        )

    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)

    leg_id = data.get("leg_id")
    if not leg_id:
        return JsonResponse(
            {"success": False, "error": "leg_id is required"}, status=400
        )

    try:
        leg = (
            Leg.objects.select_related("reservation__customer", "flight_information")
            .get(id=int(leg_id))
        )
    except (Leg.DoesNotExist, ValueError, TypeError):
        return JsonResponse({"success": False, "error": "Leg not found"}, status=404)

    result = send_flight_verification_email(
        leg, sent_by=request.user, request=request
    )
    if not result.get("success"):
        return JsonResponse(result, status=400)
    return JsonResponse(result)


def _ctx_from_leg(leg, *, form_airline=None, form_flight_number=None, token=""):
    """Shared template context for the public GET / POST renders."""
    flight = leg.flight_information
    customer = leg.reservation.customer
    try:
        trip_type = leg.get_trip_type()
    except Exception:
        trip_type = "other"

    airline_display = (
        getattr(flight, "airline_display_name", "")
        or getattr(flight, "airline", "")
        or ""
    ) if flight else ""
    flight_number = getattr(flight, "flight_number", "") if flight else ""
    booked_label = f"{airline_display} {flight_number}".strip() or "(no flight on file)"

    return {
        "token": token,
        "customer_first_name": (customer.first_name or "").strip().title() or "there",
        "booked_label": booked_label,
        "pickup_location": leg.pickup_location or "",
        "dropoff_location": leg.dropoff_location or "",
        "pickup_date": leg.pickup_date,
        "pickup_time": leg.pickup_time,
        "is_arrival": trip_type == "arrival",
        "is_return": trip_type == "return",
        "form_airline": form_airline if form_airline is not None else airline_display,
        "form_flight_number": form_flight_number if form_flight_number is not None else flight_number,
        "status": "",
        "error": "",
        "updated_label": "",
    }


def _render_expired(request, message):
    """Render a minimal version of the page in an error state."""
    return render(
        request,
        "users/flight_verification_form.html",
        {
            "customer_first_name": "there",
            "booked_label": "—",
            "pickup_location": "",
            "dropoff_location": "",
            "pickup_date": None,
            "pickup_time": None,
            "is_arrival": False,
            "is_return": False,
            "form_airline": "",
            "form_flight_number": "",
            "status": "done",   # hide the form
            "error": message,
            "updated_label": "—",
        },
        status=400,
    )


@require_http_methods(["GET", "POST"])
def flight_verification_public(request, token):
    """
    Guest-facing page. GET renders the form pre-filled with the booked
    flight; POST validates, updates the Flight, kicks off a refresh, and
    shows a confirmation.
    """
    try:
        leg_id = parse_verify_token(token)
    except SignatureExpired:
        return _render_expired(
            request,
            "This verification link has expired. Please reply to the email we sent and "
            "we'll get you sorted manually."
        )
    except BadSignature:
        return _render_expired(
            request,
            "This verification link is invalid. Please reply to the email we sent so we "
            "can confirm your flight by hand."
        )

    try:
        leg = (
            Leg.objects
            .select_related("reservation__customer", "flight_information")
            .get(id=leg_id)
        )
    except Leg.DoesNotExist:
        return _render_expired(
            request,
            "We couldn't find this reservation. Please reply to the email we sent."
        )

    if request.method == "GET":
        return render(request, "users/flight_verification_form.html", _ctx_from_leg(leg, token=token))

    # POST
    raw_airline = (request.POST.get("airline") or "").strip()
    raw_flight = (request.POST.get("flight_number") or "").strip()

    if not raw_airline or not raw_flight:
        ctx = _ctx_from_leg(leg, form_airline=raw_airline, form_flight_number=raw_flight, token=token)
        ctx["error"] = "Please enter both the airline and the flight number."
        return render(request, "users/flight_verification_form.html", ctx, status=400)

    # Hand off to Flight.save() — it normalizes airline → IATA, sets the
    # display name, strips airline prefix from the flight number, and cleans
    # non-digits. Importing here avoids a circular import at module load.
    flight = leg.flight_information
    if flight is None:
        ctx = _ctx_from_leg(leg, form_airline=raw_airline, form_flight_number=raw_flight, token=token)
        ctx["error"] = (
            "This reservation has no flight record yet. Please reply to the email "
            "we sent and we'll add it manually."
        )
        return render(request, "users/flight_verification_form.html", ctx, status=400)

    # Snapshot the booked flight identity BEFORE we mutate it, so the
    # post-update notifications can show "Old: X → New: Y".
    old_airline_display = (flight.airline_display_name or flight.airline or "").strip()
    old_flight_number = (flight.flight_number or "").strip()
    old_flight_label = f"{old_airline_display} {old_flight_number}".strip() or "(none on file)"

    try:
        flight.airline = raw_airline.upper()
        flight.airline_display_name = ""  # let save() refill from normalized code
        flight.flight_number = raw_flight
        # Clear stale tracking fields so the next refresh repopulates them cleanly
        flight.flight_iata = ""
        flight.status = ""
        flight.scheduled_arrival_local = None
        flight.estimated_arrival_local = None
        flight.scheduled_gate_arrival_local = None
        flight.estimated_gate_arrival_local = None
        flight.actual_arrival_local = None
        flight.actual_gate_arrival_local = None
        flight.terminal = ""
        flight.gate = ""
        flight.baggage_claim = ""
        flight.save()
    except Exception as e:
        logger.error(f"flight_verification_public: failed to save flight for leg {leg.id}: {e}")
        ctx = _ctx_from_leg(leg, form_airline=raw_airline, form_flight_number=raw_flight, token=token)
        ctx["error"] = "Something went wrong saving that. Please reply to the email so a human can fix it."
        return render(request, "users/flight_verification_form.html", ctx, status=500)

    # Refresh AeroAPI data synchronously so we can auto-align the pickup time
    # against the new flight before showing the success page. (Background-thread
    # mode would make the data unavailable in time for this render.)
    refresh_ok = False
    try:
        from .views import _refresh_one_flight
        aeroapi = AeroAPIService()
        res = _refresh_one_flight(flight, leg, aeroapi)
        refresh_ok = bool(res and res.get("success"))
    except Exception as e:
        logger.warning(f"flight_verification_public: sync refresh failed for leg {leg.id}: {e}")

    # If the new flight arrival lands on the same pickup date but at a different
    # time, slide the pickup to match. Only safe to auto-adjust for arrivals —
    # returns need a driveTime offset that dispatchers compute manually.
    flight.refresh_from_db()
    pickup_adjusted = False
    old_pickup_time_str = ""
    new_pickup_time_str = ""
    pickup_shift_minutes = None
    flight_arrives_different_date = False

    try:
        trip_type = leg.get_trip_type()
    except Exception:
        trip_type = "other"

    if refresh_ok and trip_type == "arrival" and leg.pickup_date and leg.pickup_time:
        new_arrival = (
            flight.scheduled_gate_arrival_local
            or flight.scheduled_arrival_local
        )
        if new_arrival is not None:
            try:
                from zoneinfo import ZoneInfo
                if timezone.is_aware(new_arrival):
                    arrival_local = new_arrival.astimezone(ZoneInfo("America/New_York"))
                else:
                    arrival_local = new_arrival.replace(tzinfo=ZoneInfo("America/New_York"))

                if arrival_local.date() != leg.pickup_date:
                    flight_arrives_different_date = True
                else:
                    new_time = arrival_local.time().replace(microsecond=0)
                    old_time = leg.pickup_time
                    old_min = old_time.hour * 60 + old_time.minute
                    new_min = new_time.hour * 60 + new_time.minute
                    diff_min = abs(new_min - old_min)
                    # 15-minute threshold — avoids flapping on AeroAPI second-level rounding,
                    # still catches material schedule changes.
                    if diff_min >= 15:
                        Leg.objects.filter(id=leg.id).update(pickup_time=new_time)
                        pickup_adjusted = True
                        pickup_shift_minutes = diff_min
                        old_pickup_time_str = old_time.strftime("%-I:%M %p")
                        new_pickup_time_str = new_time.strftime("%-I:%M %p")
            except Exception as e:
                logger.warning(
                    f"flight_verification_public: pickup auto-adjust failed for leg {leg.id}: {e}"
                )

    # Guest has acted on the verification link — clear the "email sent" marker
    # so any future flight-verify cycle (e.g. airline changes the flight again)
    # surfaces a fresh button instead of a stale "Sent X ago" badge.
    try:
        Leg.objects.filter(pk=leg.pk).update(flight_verification_email_sent_at=None)
    except Exception as e:
        logger.warning(
            f"flight_verification_public: clear-sent-at failed for leg {leg.id}: {e}"
        )

    # Close any open FLIGHT_VERIFICATION ops task — the guest has acted.
    try:
        from ops.models import OperationalTask
        from ops.services import close_task
        open_tasks = OperationalTask.objects.filter(
            leg=leg,
            task_type=OperationalTask.TaskType.FLIGHT_VERIFICATION,
            status__in=list(OperationalTask.OPEN_STATUSES),
        )
        note_suffix = ""
        if pickup_adjusted:
            note_suffix = f" — pickup auto-adjusted {old_pickup_time_str} → {new_pickup_time_str}"
        elif flight_arrives_different_date:
            note_suffix = " — flight arrives a different day; pickup left as-is for dispatcher review"
        for task in open_tasks:
            close_task(
                task,
                resolved_by=None,
                resolution_notes=f"Guest confirmed flight via self-service link: {flight}{note_suffix}",
            )
    except Exception as e:
        logger.warning(f"flight_verification_public: close-task failed for leg {leg.id}: {e}")

    leg.refresh_from_db()
    new_flight_label = str(flight).strip() or f"{raw_airline} {raw_flight}".strip()

    # Fire confirmation-to-guest + heads-up-to-office emails. Fire-and-forget;
    # any failure here must not break the success page render.
    try:
        from .flight_verify_email import send_flight_updated_notifications
        send_flight_updated_notifications(
            leg=leg,
            old_flight_label=old_flight_label,
            new_flight_label=new_flight_label,
            pickup_adjusted=pickup_adjusted,
            old_pickup_time_str=old_pickup_time_str,
            new_pickup_time_str=new_pickup_time_str,
            flight_arrives_different_date=flight_arrives_different_date,
            request=request,
        )
    except Exception as e:
        logger.warning(f"flight_verification_public: post-update emails failed for leg {leg.id}: {e}")

    ctx = _ctx_from_leg(leg, token=token)
    ctx["status"] = "done"
    ctx["updated_label"] = new_flight_label
    ctx["old_flight_label"] = old_flight_label
    ctx["new_flight_origin"] = (flight.origin or "").strip()
    ctx["new_flight_destination"] = (flight.destination or "").strip()
    ctx["pickup_adjusted"] = pickup_adjusted
    ctx["old_pickup_time_str"] = old_pickup_time_str
    ctx["new_pickup_time_str"] = new_pickup_time_str
    ctx["flight_arrives_different_date"] = flight_arrives_different_date
    return render(request, "users/flight_verification_form.html", ctx)


@require_POST
def flight_verification_check(request, token):
    """
    Pre-flight verification step. Called via AJAX from the public form
    before the guest commits. Looks up the entered airline + flight number
    in AeroAPI for the leg's pickup date, returns a JSON preview so the
    guest can confirm the flight is real and arrives at the right airport.

    Returns shapes:
      success=False, error                                   — bad input / link
      success=True,  found=False, error                      — AeroAPI not_found
      success=True,  found=True,  wrong_direction=True/False — preview payload
    """
    try:
        leg_id = parse_verify_token(token)
    except SignatureExpired:
        return JsonResponse(
            {"success": False, "error": "This verification link has expired."},
            status=400,
        )
    except BadSignature:
        return JsonResponse(
            {"success": False, "error": "This verification link is invalid."},
            status=400,
        )

    try:
        leg = (
            Leg.objects.select_related("flight_information").get(id=leg_id)
        )
    except Leg.DoesNotExist:
        return JsonResponse(
            {"success": False, "error": "Reservation not found."}, status=404
        )

    raw_airline = (request.POST.get("airline") or "").strip()
    raw_flight_input = (request.POST.get("flight_number") or "").strip()
    flight_digits = normalize_flight_number(raw_flight_input) or ""

    if not raw_airline or not flight_digits:
        return JsonResponse(
            {"success": False, "error": "Please enter both an airline and a flight number."},
            status=400,
        )

    iata_code = normalize_airline(raw_airline)
    # Guard: normalize_airline returns the bare input string when it can't
    # match a known airline (typos like "Alliegant"). Real IATA codes are
    # 2-3 alphanumeric chars; anything else means we'd send garbage to
    # AeroAPI and get a 400. Surface a clear error to the user instead.
    if not iata_code or len(iata_code) > 3 or not iata_code.isalnum():
        return JsonResponse(
            {
                "success": False,
                "error": (
                    f"Airline \"{raw_airline}\" isn't recognized. "
                    "Try the full name (e.g. \"Allegiant\", \"Delta\") "
                    "or the IATA code (e.g. \"G4\", \"DL\")."
                ),
            },
            status=400,
        )
    airline_display = get_airline_display_name(iata_code) or iata_code or raw_airline
    fa_code = get_flightaware_code(iata_code) or iata_code
    flight_ident = f"{fa_code}{flight_digits}"
    label = f"{airline_display} {flight_digits}".strip()

    flight_date_iso = leg.pickup_date.isoformat() if leg.pickup_date else None
    try:
        trip_type = leg.get_trip_type()
    except Exception:
        trip_type = None
    aero_trip_type = trip_type if trip_type in ("arrival", "return") else None

    pickup_date_human = (
        leg.pickup_date.strftime("%a, %b %-d") if leg.pickup_date else "the booked date"
    )

    aeroapi = AeroAPIService()
    try:
        data = aeroapi.get_flight_data(
            flight_ident, flight_date=flight_date_iso, trip_type=aero_trip_type
        )
    except Exception as e:
        logger.error(f"flight_verification_check: AeroAPI call failed leg={leg.id}: {e}")
        return JsonResponse(
            {"success": False, "error": "Our flight system hit a snag. Try again in a minute."},
            status=502,
        )

    status = data.get("status")
    if status == "rate_limited":
        return JsonResponse(
            {
                "success": False,
                "error": "Our flight system is briefly rate-limited. Please try again in a minute.",
            },
            status=429,
        )

    if status == "not_found":
        return JsonResponse(
            {
                "success": True,
                "found": False,
                "error": (
                    f"We couldn't find {label} on {pickup_date_human}. "
                    "Double-check the number — if your airline changed the flight, "
                    "the new number will be on your latest itinerary email."
                ),
            }
        )

    if status != "success":
        # AeroAPI hit a hard error (key missing, upstream 5xx, parse failure, etc.).
        # Don't tell the guest "we couldn't find your flight" — that's misleading
        # when the real issue is on our side. Log details and ask them to reply.
        upstream_err = data.get("error", "(no error message)")
        logger.warning(
            f"flight_verification_check: AeroAPI hard error for {flight_ident} "
            f"on {flight_date_iso} (leg={leg.id}): status={status} err={upstream_err}"
        )
        return JsonResponse(
            {
                "success": False,
                "error": (
                    "Our flight-check system is temporarily unavailable. Please reply to "
                    "the email we sent with your airline and flight number — we'll update "
                    "your reservation manually within the hour."
                ),
            },
            status=503,
        )

    origin = data.get("origin") or ""
    destination = data.get("destination") or ""
    orig_code = (origin.split(" - ")[0] if origin else "").strip().upper()
    dest_code = (destination.split(" - ")[0] if destination else "").strip().upper()

    arrives_orlando = dest_code in ORLANDO_AIRPORT_CODES
    departs_orlando = orig_code in ORLANDO_AIRPORT_CODES

    is_arrival = trip_type == "arrival"
    is_return = trip_type == "return"

    if is_arrival:
        wrong_direction = not arrives_orlando
    elif is_return:
        wrong_direction = not departs_orlando
    else:
        wrong_direction = not (arrives_orlando or departs_orlando)

    sched_dt = (
        data.get("scheduled_gate_arrival_local")
        or data.get("scheduled_arrival_local")
        or data.get("scheduled_runway_arrival_local")
    )
    if sched_dt is not None:
        try:
            scheduled_str = sched_dt.strftime("%a, %b %-d at %-I:%M %p")
        except Exception:
            scheduled_str = str(sched_dt)
    else:
        scheduled_str = "—"

    payload = {
        "success": True,
        "found": True,
        "wrong_direction": wrong_direction,
        "flight_label": label,
        "flight_iata": data.get("flight_iata", ""),
        "origin": origin,
        "destination": destination,
        "scheduled_arrival": scheduled_str,
        "scheduled_arrival_label": "Lands" if is_arrival else "Departs",
        "trip_type": trip_type or "other",
    }

    if wrong_direction:
        if is_arrival:
            payload["warning"] = (
                f"We found this flight ({origin or '?'} → {destination or '?'}), but it doesn't land in Orlando. "
                "Please give us the final flight landing at MCO or Sanford (SFB). "
                "If you're connecting, that's usually the second leg of your trip — not the first."
            )
        elif is_return:
            payload["warning"] = (
                f"We found this flight ({origin or '?'} → {destination or '?'}), but it doesn't depart from Orlando. "
                "Please give us the flight leaving MCO or Sanford (SFB) on the pickup date."
            )
        else:
            payload["warning"] = (
                f"We found this flight ({origin or '?'} → {destination or '?'}), but it doesn't touch Orlando. "
                "Double-check the number."
            )

    return JsonResponse(payload)

    threading.Thread(target=runner, daemon=True).start()
