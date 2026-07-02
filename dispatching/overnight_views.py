"""
Views for the overnight-arrival date confirmation flow.

- `overnight_flight_check` — public AJAX endpoint the booking form's overnight
  popup calls BEFORE a reservation exists: guest's takeoff date in, derived
  landing/pickup date out. Never blocks booking — the form treats any error as
  "we'll confirm by email later".
- `overnight_confirm_public` — the one-tap landing page from the backstop
  email. GET shows the two takeoff choices (preselected via ?choice=), POST
  commits: confirms the booked date, or moves the pickup a day forward when
  the guest says they take off ON the booked date.
"""

import json
import logging
from datetime import date, timedelta

from django.core.cache import cache
from django.core.signing import BadSignature, SignatureExpired
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_POST, require_http_methods

from reservations.models import Leg

from .overnight_arrival import (
    apply_overnight_answer,
    derive_arrival_for_takeoff,
    fmt_date,
    fmt_time,
    is_overnight_pickup_time,
    parse_overnight_token,
)

logger = logging.getLogger(__name__)

# Public unauthenticated endpoint that triggers an AeroAPI call — keep a
# simple per-IP throttle so a scraper can't burn the API quota.
_CHECK_RATE_LIMIT = 10          # calls
_CHECK_RATE_WINDOW_SECONDS = 60


def _client_ip(request):
    xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "unknown")


@require_POST
def overnight_flight_check(request):
    """Booking-form popup: derive landing date from the guest's takeoff date."""
    key = f"overnight_check_{_client_ip(request)}"
    calls = cache.get(key, 0)
    if calls >= _CHECK_RATE_LIMIT:
        return JsonResponse(
            {"success": False, "error": "Too many checks — please try again in a minute."},
            status=429,
        )
    cache.set(key, calls + 1, _CHECK_RATE_WINDOW_SECONDS)

    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)

    airline = (data.get("airline") or "").strip()
    flight_number = (data.get("flight_number") or "").strip()
    takeoff_raw = (data.get("takeoff_date") or "").strip()
    pickup_raw = (data.get("pickup_date") or "").strip()

    try:
        takeoff_date = date.fromisoformat(takeoff_raw)
    except ValueError:
        return JsonResponse({"success": False, "error": "Invalid takeoff date"}, status=400)
    try:
        booked_pickup = date.fromisoformat(pickup_raw) if pickup_raw else None
    except ValueError:
        booked_pickup = None

    # Sanity bounds: the popup only ever offers pickup_date-1 / pickup_date,
    # and never for past dates. Refuse anything outside a generous range.
    today = timezone.localdate()
    if takeoff_date < today - timedelta(days=1) or takeoff_date > today + timedelta(days=400):
        return JsonResponse({"success": False, "error": "Takeoff date out of range"}, status=400)

    derived = derive_arrival_for_takeoff(airline, flight_number, takeoff_date)
    status = derived.get("status")

    if status == "found":
        arrival_date = derived["arrival_date"]
        return JsonResponse(
            {
                "success": True,
                "found": True,
                "flight_label": derived["flight_label"],
                "origin": derived["origin"],
                "destination": derived["destination"],
                "takeoff_date": takeoff_date.isoformat(),
                "arrival_date": arrival_date.isoformat(),
                "arrival_date_human": fmt_date(arrival_date),
                "arrival_time_human": fmt_time(derived["arrival_local"].time()),
                "matches_booked": bool(booked_pickup and arrival_date == booked_pickup),
            }
        )

    if status == "rate_limited":
        return JsonResponse(
            {"success": False, "error": "Flight system briefly busy — continuing without the check."},
            status=429,
        )

    # not_found / not_found_on_date / not_orlando / bad_airline / error:
    # the form falls back to "thanks — we'll double-check by email".
    return JsonResponse(
        {
            "success": True,
            "found": False,
            "message": derived.get("error", "Flight not found for that date."),
        }
    )


def _page_ctx(leg, *, status="", error=""):
    """Context for the public one-tap page."""
    pickup_date = leg.pickup_date
    prev_day = pickup_date - timedelta(days=1)
    next_day = pickup_date + timedelta(days=1)
    flight = leg.flight_information
    customer = leg.reservation.customer if leg.reservation_id else None

    airline_display = (
        getattr(flight, "airline_display_name", "") or getattr(flight, "airline", "") or ""
    ) if flight else ""
    flight_number = getattr(flight, "flight_number", "") if flight else ""

    return {
        "customer_first_name": (getattr(customer, "first_name", "") or "").strip().title() or "there",
        "flight_label": f"{airline_display} {flight_number}".strip() or "your flight",
        "pickup_date_str": fmt_date(pickup_date),
        "pickup_time_str": fmt_time(leg.pickup_time),
        "prev_day_str": fmt_date(prev_day),
        "same_day_str": fmt_date(pickup_date),
        "next_day_str": fmt_date(next_day),
        "already_confirmed": bool(leg.overnight_confirmed_at),
        "status": status,
        "error": error,
    }


def _render_bad_link(request, message):
    return render(
        request,
        "users/overnight_confirm_page.html",
        {
            "customer_first_name": "there",
            "flight_label": "",
            "pickup_date_str": "",
            "pickup_time_str": "",
            "prev_day_str": "",
            "same_day_str": "",
            "next_day_str": "",
            "already_confirmed": False,
            "status": "error",
            "error": message,
        },
        status=400,
    )


@require_http_methods(["GET", "POST"])
def overnight_confirm_public(request, token):
    """One-tap landing page. Email links land on GET with ?choice= preselected;
    the actual commit is the POST button (so inbox link-prefetchers can never
    confirm or move a pickup date by accident)."""
    try:
        leg_id = parse_overnight_token(token)
    except SignatureExpired:
        return _render_bad_link(
            request,
            "This confirmation link has expired. Please reply to our email and "
            "we'll confirm your date by hand.",
        )
    except BadSignature:
        return _render_bad_link(
            request,
            "This confirmation link is invalid. Please reply to our email so we "
            "can confirm your date by hand.",
        )

    try:
        leg = (
            Leg.objects.select_related("reservation__customer", "flight_information")
            .get(id=leg_id)
        )
    except Leg.DoesNotExist:
        return _render_bad_link(
            request, "We couldn't find this reservation. Please reply to our email."
        )

    if not leg.pickup_date:
        return _render_bad_link(
            request, "This reservation has no pickup date on file. Please reply to our email."
        )

    ctx = _page_ctx(leg)
    ctx["token"] = token

    if request.method == "GET":
        ctx["preselect"] = request.GET.get("choice", "")
        if ctx["preselect"] not in ("prev", "same"):
            ctx["preselect"] = ""
        if leg.overnight_confirmed_at:
            ctx["status"] = "done"
        return render(request, "users/overnight_confirm_page.html", ctx)

    # POST — commit the guest's answer. Idempotent: a repeat POST after
    # confirmation just re-renders the success page.
    choice = request.POST.get("choice", "")
    if choice not in ("prev", "same"):
        ctx["error"] = "Please pick which date you take off."
        return render(request, "users/overnight_confirm_page.html", ctx, status=400)

    if leg.overnight_confirmed_at:
        ctx["status"] = "done"
        return render(request, "users/overnight_confirm_page.html", ctx)

    result = apply_overnight_answer(leg, choice, source="one_tap")

    ctx = _page_ctx(leg)
    ctx["token"] = token
    ctx["status"] = "done"
    ctx["just_confirmed"] = True
    ctx["moved"] = result["moved"]
    ctx["old_pickup_date_str"] = fmt_date(result["old_pickup_date"])
    ctx["takeoff_str"] = fmt_date(result["takeoff"])
    return render(request, "users/overnight_confirm_page.html", ctx)


@require_POST
def overnight_staff_confirm(request):
    """Dispatcher board action: record the guest's takeoff-date answer after a
    text/call, or right after a backend booking. One click flips the amber
    'Which night?' callout green — replaces the manual reservation note.

    POST JSON: {"leg_id": int, "choice": "prev" | "same"}"""
    if not request.user.is_authenticated or not request.user.is_staff:
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)

    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)

    choice = data.get("choice", "")
    if choice not in ("prev", "same"):
        return JsonResponse({"success": False, "error": "choice must be prev|same"}, status=400)

    try:
        leg = (
            Leg.objects.select_related("reservation__customer", "flight_information")
            .get(id=int(data.get("leg_id")))
        )
    except (Leg.DoesNotExist, TypeError, ValueError):
        return JsonResponse({"success": False, "error": "Leg not found"}, status=404)

    if not leg.pickup_date:
        return JsonResponse({"success": False, "error": "Leg has no pickup date"}, status=400)
    if not is_overnight_pickup_time(leg.pickup_time):
        return JsonResponse(
            {"success": False, "error": "Not an overnight (12 AM–6 AM) pickup"}, status=400
        )
    if leg.overnight_confirmed_at:
        return JsonResponse({"success": True, "already_confirmed": True, "moved": False})

    result = apply_overnight_answer(
        leg, choice, source="staff", resolved_by=request.user, notify_office=False
    )
    logger.info(
        f"overnight staff confirm: leg={leg.id} choice={choice} by={request.user.username} "
        f"moved={result['moved']}"
    )
    return JsonResponse(
        {
            "success": True,
            "moved": result["moved"],
            "takeoff": result["takeoff"].isoformat(),
            "new_pickup_date": result["new_pickup_date"].isoformat(),
        }
    )
