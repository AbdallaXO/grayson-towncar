"""
Revenue & source-attribution KPI helpers.

Single source of truth for the math behind the revenue dashboard. Every
function here returns paid-only data (Reservation.is_paid=True) unless its
docstring explicitly says otherwise. Volume metrics that must include
unpaid bookings (e.g. conversion rate) are computed inside `overview()`.

If "paid" ever needs to mean something different (e.g. only after the trip
is completed), change it in one place: PAID below.
"""
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.db.models import Avg, Count, DecimalField, Q, Sum
from django.db.models.functions import Coalesce, TruncDate
from django.utils import timezone

from reservations.models import Reservation


# Filter constant — using is_paid=True keeps the query at the DB layer
# instead of falling through to the @cached_property payment_status.
PAID = Q(is_paid=True)

ZERO = Decimal("0.00")


def resolve_range(start=None, end=None, days: int = 30):
    """
    Normalize the various ways the dashboard can request a window into a
    single (start_dt, end_dt) tuple of timezone-aware datetimes.

    Accepts:
      - explicit start/end (date or datetime)  -> custom range, end inclusive
      - days only                              -> rolling N-day window ending now
    The end of an inclusive date range is bumped to the next day at 00:00 so
    callers can always use `__lt=end_dt` (i.e. half-open intervals).
    """
    tz = timezone.get_current_timezone()

    def _to_aware(value, end_of_day=False):
        if value is None:
            return None
        if isinstance(value, datetime):
            dt = value
        else:
            # plain date — pin to start or end of day
            dt = datetime.combine(value, time.max if end_of_day else time.min)
        if timezone.is_naive(dt):
            dt = timezone.make_aware(dt, tz)
        return dt

    if start or end:
        # Custom range. Default missing side to a sensible bound.
        start_dt = _to_aware(start) if start else _to_aware(date(2000, 1, 1))
        if end:
            end_date = end.date() if isinstance(end, datetime) else end
            end_dt = _to_aware(end_date + timedelta(days=1))  # exclusive
        else:
            end_dt = timezone.now()
    else:
        end_dt = timezone.now()
        start_dt = end_dt - timedelta(days=days)
    return start_dt, end_dt


def _safe_div(num, den):
    if not den:
        return 0.0
    return float(num) / float(den)


def _scoped(qs, start, end):
    """Apply the booking-creation window (created_at) — used for volume metrics."""
    return qs.filter(created_at__gte=start, created_at__lt=end)


def _scoped_paid(qs, start, end):
    """
    Apply the payment-receipt window (first_paid_at) — used for cash-basis
    revenue metrics so a booking made in March but paid in April lands in
    April's totals. Caller is responsible for also applying PAID.
    """
    return qs.filter(first_paid_at__gte=start, first_paid_at__lt=end)


def overview(start, end) -> dict:
    """
    Headline cards. Volume (Bookings Created) is anchored on created_at; every
    paid metric is anchored on first_paid_at so the page reflects money that
    actually landed during the window.
    """
    qs_created = _scoped(Reservation.objects.all(), start, end)
    paid_qs = _scoped_paid(Reservation.objects.filter(PAID), start, end)

    created = qs_created.count()
    paid = paid_qs.count()
    paid_revenue = paid_qs.aggregate(s=Sum("paid_amount"))["s"] or ZERO
    gross_paid = paid_qs.aggregate(s=Sum("gross_paid"))["s"] or ZERO
    refunded = paid_qs.aggregate(s=Sum("total_refunded"))["s"] or ZERO
    avg_ticket = paid_qs.aggregate(a=Avg("paid_amount"))["a"] or ZERO

    return {
        "bookings_created": created,
        "bookings_paid": paid,
        # paid/created — both anchored on their own date, so this rate is
        # "for every booking created in the window we collected on N payments
        # in the same window" (mixed-cohort, but matches the headline cards).
        "paid_conv_rate": round(_safe_div(paid, created) * 100, 1),
        "paid_revenue": paid_revenue,        # net of refunds (the headline)
        "gross_paid": gross_paid,            # before refunds
        "refunded": refunded,
        "avg_ticket": avg_ticket,
        "repeat_revenue": (
            paid_qs.filter(is_repeat_booking=True)
            .aggregate(s=Sum("paid_amount"))["s"] or ZERO
        ),
    }


def by_source(start, end):
    """
    Per-channel breakdown — Created column is anchored on created_at, all
    paid columns on first_paid_at. Two grouped queries merged in Python on
    booking_source so each metric uses the right window anchor.
    """
    label_map = dict(Reservation.BOOKING_SOURCE_CHOICES)

    created_rows = (
        _scoped(Reservation.objects.all(), start, end)
        .values("booking_source")
        .annotate(created=Count("id"))
    )
    by_src_created = {r["booking_source"]: r["created"] for r in created_rows}

    paid_rows = (
        _scoped_paid(Reservation.objects.filter(PAID), start, end)
        .values("booking_source")
        .annotate(
            paid=Count("id"),
            paid_revenue=Coalesce(
                Sum("paid_amount"),
                ZERO,
                output_field=DecimalField(max_digits=12, decimal_places=2),
            ),
        )
    )
    by_src_paid = {r["booking_source"]: r for r in paid_rows}

    all_sources = set(by_src_created) | set(by_src_paid)
    rows = []
    for src in all_sources:
        created = by_src_created.get(src, 0)
        p = by_src_paid.get(src, {"paid": 0, "paid_revenue": ZERO})
        rows.append({
            "booking_source": src,
            "label": label_map.get(src, src or "—"),
            "created": created,
            "paid": p["paid"],
            "paid_revenue": p["paid_revenue"] or ZERO,
        })

    total_revenue = sum((r["paid_revenue"] for r in rows), ZERO)
    for r in rows:
        r["conv_rate"] = round(_safe_div(r["paid"], r["created"]) * 100, 1)
        r["pct_of_revenue"] = round(_safe_div(r["paid_revenue"], total_revenue) * 100, 1)
    rows.sort(key=lambda r: (r["paid_revenue"], r["created"]), reverse=True)
    return rows


def by_travel_agent(start, end, limit: int = 25):
    """Top travel agents by paid revenue in the window (cash-basis: first_paid_at)."""
    return list(
        _scoped_paid(Reservation.objects.filter(PAID, travel_agent__isnull=False), start, end)
        .values(
            "travel_agent_id",
            "travel_agent__agent_name",
            "travel_agent__agency_name",
        )
        .annotate(
            paid_bookings=Count("id"),
            paid_revenue=Sum("paid_amount"),
            commission=Sum("commission_amount"),
            avg_ticket=Avg("paid_amount"),
        )
        .order_by("-paid_revenue")[:limit]
    )


def travel_agent_totals(start, end) -> dict:
    """Combined totals across all travel-agent bookings — used for the leaderboard footer."""
    agg = _scoped_paid(
        Reservation.objects.filter(PAID, travel_agent__isnull=False),
        start, end,
    ).aggregate(
        paid_bookings=Count("id"),
        paid_revenue=Sum("paid_amount"),
        commission=Sum("commission_amount"),
    )
    return {
        "paid_bookings": agg["paid_bookings"] or 0,
        "paid_revenue": agg["paid_revenue"] or ZERO,
        "commission": agg["commission"] or ZERO,
    }


def by_route(start, end, limit: int = 15):
    """Top revenue-generating routes (cash-basis: first_paid_at)."""
    return list(
        _scoped_paid(Reservation.objects.filter(PAID), start, end)
        .values(
            "rate__route__origin__name",
            "rate__route__destination__name",
        )
        .annotate(
            paid_bookings=Count("id"),
            paid_revenue=Sum("paid_amount"),
        )
        .order_by("-paid_revenue")[:limit]
    )


def by_vehicle(start, end):
    """Paid revenue by vehicle type (cash-basis: first_paid_at)."""
    return list(
        _scoped_paid(Reservation.objects.filter(PAID), start, end)
        .values("vehicle__vehicle_type")
        .annotate(
            paid_bookings=Count("id"),
            paid_revenue=Sum("paid_amount"),
            avg_ticket=Avg("paid_amount"),
        )
        .order_by("-paid_revenue")
    )


def revenue_trend(start, end):
    """
    Daily paid-revenue series, keyed on first_paid_at (when the money
    actually landed) — not on created_at, since a booking can be created on
    one day and paid on another.
    """
    return list(
        Reservation.objects.filter(
            PAID,
            first_paid_at__gte=start,
            first_paid_at__lt=end,
        )
        .annotate(day=TruncDate("first_paid_at"))
        .values("day")
        .annotate(
            paid_revenue=Sum("paid_amount"),
            bookings=Count("id"),
        )
        .order_by("day")
    )
