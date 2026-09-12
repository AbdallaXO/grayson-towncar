"""Passenger lookup for the schedule board.

The board shows one day. A dispatcher with a guest on the phone knows a name, a
phone number, an email or the 50-number on the confirmation — almost never the
date. Before this, finding them meant leaving the board for the reservations
list, reading the date off a row, then coming back and paging the board to it.

This endpoint answers the question the dispatcher actually has — *which day is
this person on?* — and hands back, per trip, the board link that opens that day
with the trip lit up. Cancelled trips are returned too, marked, because "it's
not on the board" is itself the answer sometimes.

Matching, in one pass:
  * name          — "george", "george harrison", "harrison"
  * email         — any fragment
  * phone         — typed any way; 4075551234, (407) 555-1234, 555-1234
  * 50-number     — 501234 or the bare reservation id

Read by [dispatching/templates/dispatching/schedule_board.html]'s search box.
"""

from datetime import date, timedelta

from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.http import JsonResponse
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET

from business.datefmt import time12, weekday_day_month
from reservations.models import Customer, Leg

from .farmout_report import _short_loc

# How far back a guest's history is worth showing. A dispatcher looking someone
# up is working on the trip that is coming, or the one that just happened —
# anything older belongs to the reservation record, not to the board.
HISTORY_DAYS = 180

# Ceilings. The panel is a dropdown over a live board, not a report.
MAX_PASSENGERS = 6
MAX_TRIPS_EACH = 6
MAX_UPCOMING_EACH = 4
# Candidates pulled before ranking — a common surname matches more people than
# the panel shows, and we want the ones with a trip coming, not the first six
# rows the database happened to hand back.
CANDIDATE_POOL = 24


def _digits(s):
    return "".join(ch for ch in s if ch.isdigit())


def _loose_phone_regex(digits):
    r"""'4075551234' -> '4\D*0\D*7\D*...' so the match survives any formatting.

    Stored numbers are a museum of formats — +1 407-555-1234, (407) 555-1234,
    4075551234 — because they arrive from the booking form, the agent portal
    and hand-entry. Comparing digit-to-digit is the only way a dispatcher
    typing what's on their screen finds the guest.
    """
    return r"\D*".join(digits)


# What a phone number or a trip number is allowed to be made of. Anything else
# in the box — a letter — means the dispatcher is typing a name or an email,
# even if there are digits in it ("nkf2014@gmail.com" is not a phone number).
_NUMBER_CHARS = set("0123456789 ()-.+")


def _is_number_query(query, digits):
    """True when the whole box reads as a phone number or a 50-number."""
    return len(digits) >= 4 and not (set(query) - _NUMBER_CHARS)


def _customer_matches(query):
    """The Q object behind one search box, over name / email / phone / 50-number."""
    digits = _digits(query)
    parts = query.split()

    if len(parts) >= 2:
        # "george harrison" — first + last together, and either word alone, so a
        # guest stored as "George Harrison Jr" is still found.
        first_part, last_part = parts[0], " ".join(parts[1:])
        conditions = (
            Q(first_name__icontains=first_part, last_name__icontains=last_part)
            | Q(first_name__icontains=query)
            | Q(last_name__icontains=query)
        )
    else:
        conditions = Q(first_name__icontains=query) | Q(last_name__icontains=query)

    conditions |= Q(email__icontains=query)

    if _is_number_query(query, digits):
        conditions |= Q(phone_number__icontains=query)
        conditions |= Q(phone_number__icontains=digits)
        # Only for something long enough to be a real phone fragment — a loose
        # scan on "40" would sweep the whole table for nothing.
        if len(digits) >= 7:
            conditions |= Q(phone_number__regex=_loose_phone_regex(digits))

        # The 50-number off a confirmation, and the bare id behind it.
        res_ids = {int(digits)} if len(digits) <= 9 else set()
        if digits.startswith("50") and len(digits) > 2:
            res_ids.add(int(digits[2:]))
        if res_ids:
            conditions |= Q(reservation__id__in=res_ids)

    return conditions


def _trip_row(leg, today):
    """One trip, said the way a dispatcher would say it out loud."""
    reservation = leg.reservation
    driver = leg.driver
    cancelled = leg.status == "cancelled" or (
        reservation and reservation.status == "cancelled"
    )
    # Affiliate jobs live on their own board — sending a dispatcher to the
    # in-house board for a farmed-out trip would show them an empty lane.
    board_view = (
        "affiliate"
        if driver is not None and driver.driver_type == "affiliate"
        else "inhouse"
    )

    row = {
        "leg_id": leg.id,
        "date": leg.pickup_date.isoformat() if leg.pickup_date else "",
        "date_label": weekday_day_month(leg.pickup_date),
        "year": leg.pickup_date.year if leg.pickup_date else None,
        "time_label": time12(leg.pickup_time),
        "route": f"{_short_loc(leg.pickup_location)} → {_short_loc(leg.dropoff_location)}",
        "driver": str(driver) if driver else "",
        "board": board_view,
        "cancelled": cancelled,
        "res_number": reservation.display_number if reservation else "",
        "res_url": (
            reverse("reservation_details", args=[reservation.uuid])
            if reservation
            else ""
        ),
        "board_url": "",
        "when": "past",
    }

    if leg.pickup_date:
        if leg.pickup_date == today:
            row["when"] = "today"
        elif leg.pickup_date > today:
            row["when"] = "upcoming"

    # A cancelled trip was never drawn on the board, so it gets no board link —
    # the reservation is the only honest place to send someone.
    if not cancelled and leg.pickup_date:
        row["board_url"] = (
            f"{reverse('schedule_board')}?date={leg.pickup_date.isoformat()}"
            f"&view={board_view}&focus={leg.id}"
        )

    return row


def _iso_ordinal(iso):
    """'2026-09-09' -> a day number, so dates can be ranked in both directions."""
    year, month, day = (int(part) for part in iso.split("-"))
    return date(year, month, day).toordinal()


def _rank_key(passenger):
    """Nearest trip first: soonest ahead, then most recent behind, then nothing.

    A dispatcher searching a name is almost always holding the phone for the
    trip closest to now.
    """
    dated = [t["date"] for t in passenger["trips"] if t["date"]]
    ahead = [
        t["date"] for t in passenger["trips"]
        if t["date"] and t["when"] in ("today", "upcoming")
    ]
    if ahead:
        return (0, min(_iso_ordinal(d) for d in ahead), passenger["name"])
    if dated:
        return (1, -max(_iso_ordinal(d) for d in dated), passenger["name"])
    return (2, 0, passenger["name"])


@require_GET
@login_required(login_url="login")
def board_passenger_search(request):
    """Find a guest by name, phone, email or 50-number; answer with their trips."""
    if not request.user.is_staff:
        return JsonResponse({"error": "Staff only."}, status=403)

    query = (request.GET.get("q") or "").strip()
    if len(query) < 2:
        return JsonResponse({"query": query, "results": [], "too_short": True})

    today = timezone.localdate()

    customers = list(
        Customer.objects.filter(_customer_matches(query))
        .distinct()
        .order_by("-created_at")[:CANDIDATE_POOL]
    )
    if not customers:
        return JsonResponse({"query": query, "results": []})

    customer_ids = [c.id for c in customers]

    legs = (
        Leg.objects.filter(
            reservation__customer_id__in=customer_ids,
            pickup_date__gte=today - timedelta(days=HISTORY_DAYS),
        )
        .select_related("reservation", "reservation__customer", "driver", "driver__profile")
        .order_by("pickup_date", "pickup_time")
    )

    trips_by_customer = {}
    for leg in legs:
        trips_by_customer.setdefault(leg.reservation.customer_id, []).append(leg)

    # Whole history, so the panel can say "3 of 11 trips" instead of implying
    # the window it just applied is everything on file.
    totals = {
        row["reservation__customer_id"]: row["n"]
        for row in Leg.objects.filter(reservation__customer_id__in=customer_ids)
        .values("reservation__customer_id")
        .annotate(n=Count("id"))
    }

    results = []
    for customer in customers:
        legs_for_customer = trips_by_customer.get(customer.id, [])
        ahead = [l for l in legs_for_customer if l.pickup_date and l.pickup_date >= today]
        behind = [l for l in legs_for_customer if not l.pickup_date or l.pickup_date < today]

        chosen = ahead[:MAX_UPCOMING_EACH]
        # Backfill with the most recent past trips — reversed, because the
        # closest one behind is the one they're being asked about.
        chosen += list(reversed(behind))[: MAX_TRIPS_EACH - len(chosen)]

        results.append({
            "customer_id": customer.id,
            "name": customer.get_full_name(),
            "phone": customer.phone_number or "",
            "email": customer.email or "",
            "trip_count": totals.get(customer.id, 0),
            "shown_count": len(chosen),
            "trips": [_trip_row(leg, today) for leg in chosen],
        })

    results.sort(key=_rank_key)
    return JsonResponse({"query": query, "results": results[:MAX_PASSENGERS]})
