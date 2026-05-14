"""
Revenue & source-attribution KPI helpers.

Single source of truth for the math behind the revenue dashboard. The
"revenue" definition is **cash-basis** — money actually received during
the window — with two payment paths:

  • A reservation contributes to revenue when EITHER:
        - it has at least one Payment row with status="paid"
          (the Stripe path; is_paid=True / paid_amount populated), OR
        - its status is "completed" (the ride happened — operator
          collected cash, invoice, ACH, or some other means we don't
          model row-by-row).

  • Revenue amount per reservation:
        - Stripe-tracked  → paid_amount (net of refunds)
        - non-Stripe done → total_price (the contracted amount)

  • Revenue anchor date (which window the revenue lands in):
        - Stripe-tracked  → first_paid_at  (when Stripe credited us)
        - non-Stripe done → earliest non-cancelled leg's pickup_date
                            (cash-on-pickup is the dominant non-Stripe
                            pattern — the trip date is when the cash
                            actually changes hands)
        - legless edge    → created_at fallback

This is intentionally NOT the accrual report — pickup_date is only used
as a cash-receipt proxy for non-Stripe trips. Stripe trips still anchor
on the actual payment-receipt date, even when that's months before the
trip. If you need accrual semantics (revenue when service was delivered,
regardless of payment) use the separate `accrual_revenue_report` page.
"""
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.db.models import (
    Avg, Case, Count, DateTimeField, DecimalField, Exists, F, OuterRef, Q,
    Subquery, Sum, Value, When,
)
from django.db.models.functions import Coalesce, TruncDate
from django.utils import timezone

from reservations.models import Leg, Reservation


ZERO = Decimal("0.00")

# Earliest non-cancelled leg's pickup_date — used as the cash-receipt proxy
# for non-Stripe completed trips, since most are paid at pickup.
_earliest_active_pickup_subq = (
    Leg.objects.filter(reservation=OuterRef("pk"))
    .exclude(status="cancelled")
    .order_by("pickup_date", "pickup_time")
    .values("pickup_date")[:1]
)

# Catches round-trip reservations stuck in "confirmed" with one leg already
# completed (operator updated the leg but not the parent reservation).
_has_completed_leg_subq = Exists(
    Leg.objects.filter(reservation=OuterRef("pk"), status="completed")
)

# Net revenue per row: Stripe = paid_amount (net of refunds); non-Stripe = total_price.
_REVENUE_AMOUNT_EXPR = Case(
    When(is_paid=True, then=F("paid_amount")),
    default=F("total_price"),
    output_field=DecimalField(max_digits=12, decimal_places=2),
)

# Gross revenue per row: Stripe = gross_paid (before refunds); non-Stripe = total_price.
_REVENUE_GROSS_EXPR = Case(
    When(is_paid=True, then=F("gross_paid")),
    default=F("total_price"),
    output_field=DecimalField(max_digits=12, decimal_places=2),
)

# Cash-basis anchor: when did we actually receive the money?
# COALESCE returns the first non-NULL value, so for Stripe-paid rows
# first_paid_at wins (real receipt date), and for cash-only rows the
# subquery returns the earliest non-cancelled leg pickup_date.
_REVENUE_ANCHOR_EXPR = Coalesce(
    "first_paid_at",
    Subquery(_earliest_active_pickup_subq),
    "created_at",
    output_field=DateTimeField(),
)


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
            dt = datetime.combine(value, time.max if end_of_day else time.min)
        if timezone.is_naive(dt):
            dt = timezone.make_aware(dt, tz)
        return dt

    if start or end:
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
    """Booking-creation window — used for volume metrics."""
    return qs.filter(created_at__gte=start, created_at__lt=end)


def revenue_qs(start, end):
    """
    The cash-basis revenue queryset for [start, end). Every other helper
    in this module builds on top of this — guarantees a consistent
    definition of 'paid revenue' across the dashboard.

    A reservation contributes when ANY of these is true:
      - is_paid=True (Stripe path)
      - status="completed" (whole-trip cash collection)
      - at least one Leg has status="completed" (partial collection on
        round-trips where operator hasn't flipped the parent reservation)

    Each row carries:
      - revenue_amount: net revenue (Decimal)
      - revenue_gross:  pre-refund gross (Decimal)
      - revenue_at:     date money was received (DateTime)
    """
    return (
        Reservation.objects
        .annotate(
            has_completed_leg=_has_completed_leg_subq,
            revenue_at=_REVENUE_ANCHOR_EXPR,
            revenue_amount=_REVENUE_AMOUNT_EXPR,
            revenue_gross=_REVENUE_GROSS_EXPR,
        )
        .filter(
            Q(is_paid=True) | Q(status="completed") | Q(has_completed_leg=True)
        )
        .filter(revenue_at__gte=start, revenue_at__lt=end)
    )


def overview(start, end) -> dict:
    """
    Headline cards. Volume (Bookings Created) is anchored on created_at;
    every revenue metric is cash-basis on revenue_at (first_paid_at or
    created_at fallback for non-Stripe completed trips).
    """
    qs_created = _scoped(Reservation.objects.all(), start, end)
    paid_qs = revenue_qs(start, end)

    created = qs_created.count()
    paid = paid_qs.count()

    agg = paid_qs.aggregate(
        net=Sum("revenue_amount"),
        gross=Sum("revenue_gross"),
        # total_refunded is a Stripe-only field; for non-Stripe rows it's 0/null.
        refunded=Sum("total_refunded"),
        avg_ticket=Avg("revenue_amount"),
    )
    paid_revenue = agg["net"] or ZERO
    gross_paid = agg["gross"] or ZERO
    refunded = agg["refunded"] or ZERO
    avg_ticket = agg["avg_ticket"] or ZERO

    return {
        "bookings_created": created,
        "bookings_paid": paid,
        # mixed-cohort rate matching the headline cards
        "paid_conv_rate": round(_safe_div(paid, created) * 100, 1),
        "paid_revenue": paid_revenue,
        "gross_paid": gross_paid,
        "refunded": refunded,
        "avg_ticket": avg_ticket,
        "repeat_revenue": (
            paid_qs.filter(is_repeat_booking=True)
            .aggregate(s=Sum("revenue_amount"))["s"] or ZERO
        ),
    }


def by_source(start, end):
    """
    Per-channel breakdown. Created column is anchored on created_at; all
    paid columns use the cash-basis revenue_at anchor.
    """
    label_map = dict(Reservation.BOOKING_SOURCE_CHOICES)

    created_rows = (
        _scoped(Reservation.objects.all(), start, end)
        .values("booking_source")
        .annotate(created=Count("id"))
    )
    by_src_created = {r["booking_source"]: r["created"] for r in created_rows}

    paid_rows = (
        revenue_qs(start, end)
        .values("booking_source")
        .annotate(
            paid=Count("id"),
            paid_revenue=Coalesce(
                Sum("revenue_amount"),
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
    """Top travel agents by paid revenue in the window (cash-basis)."""
    return list(
        revenue_qs(start, end)
        .filter(travel_agent__isnull=False)
        .values(
            "travel_agent_id",
            "travel_agent__agent_name",
            "travel_agent__agency_name",
        )
        .annotate(
            paid_bookings=Count("id"),
            paid_revenue=Sum("revenue_amount"),
            commission=Sum("commission_amount"),
            avg_ticket=Avg("revenue_amount"),
        )
        .order_by("-paid_revenue")[:limit]
    )


def travel_agent_totals(start, end) -> dict:
    """Combined totals across all travel-agent bookings — used for the leaderboard footer."""
    agg = (
        revenue_qs(start, end)
        .filter(travel_agent__isnull=False)
        .aggregate(
            paid_bookings=Count("id"),
            paid_revenue=Sum("revenue_amount"),
            commission=Sum("commission_amount"),
        )
    )
    return {
        "paid_bookings": agg["paid_bookings"] or 0,
        "paid_revenue": agg["paid_revenue"] or ZERO,
        "commission": agg["commission"] or ZERO,
    }


def by_route(start, end, limit: int = 15):
    """Top revenue-generating routes (cash-basis)."""
    return list(
        revenue_qs(start, end)
        .values(
            "rate__route__origin__name",
            "rate__route__destination__name",
        )
        .annotate(
            paid_bookings=Count("id"),
            paid_revenue=Sum("revenue_amount"),
        )
        .order_by("-paid_revenue")[:limit]
    )


def by_vehicle(start, end):
    """Paid revenue by vehicle type (cash-basis)."""
    return list(
        revenue_qs(start, end)
        .values("vehicle__vehicle_type")
        .annotate(
            paid_bookings=Count("id"),
            paid_revenue=Sum("revenue_amount"),
            avg_ticket=Avg("revenue_amount"),
        )
        .order_by("-paid_revenue")
    )


def revenue_trend(start, end):
    """
    Daily paid-revenue series. Keyed on revenue_at (cash-basis anchor).
    """
    return list(
        revenue_qs(start, end)
        .annotate(day=TruncDate("revenue_at"))
        .values("day")
        .annotate(
            paid_revenue=Sum("revenue_amount"),
            bookings=Count("id"),
        )
        .order_by("day")
    )
