"""
Revenue & source-attribution KPI helpers.

Single source of truth for the math behind the revenue dashboard. Revenue
is **transaction-level cash-basis**: every figure is built from individual
``payment.Payment`` rows (the row-by-row mirror of Stripe), so the numbers
reconcile directly against the Stripe dashboard.

  • A revenue event is one ``Payment`` with status="paid".

  • Revenue amount per event:
        net   = amount − refunded_amount   (partial refunds netted out)
        gross = amount                     (before refunds)
    Fully-refunded charges carry status="refunded" and are excluded — the
    +charge and −refund net to zero, so dropping the row is correct.

  • Revenue anchor date (which window the cash lands in):
        Payment.created_at — when Stripe actually credited us. A deposit and
        its later balance are SEPARATE events on their own dates, so a balance
        collected this month counts this month even if the deposit was earlier.

Why not read Reservation.paid_amount / first_paid_at? Those denormalized
columns (a) drift out of sync with the Payment rows, and (b) collapse every
payment a reservation ever received onto the *first* payment's date — which
misattributes balance cash to the deposit month. Summing the Payment rows
directly avoids both problems.

Caveat: trips paid purely in cash with no Payment row are not counted here
(this is Stripe-reconcilable cash-basis, not total contracted value). Use the
separate ``accrual_revenue_report`` page for service-delivered (accrual)
semantics.

Reservation attributes (booking_source, travel_agent, route, vehicle) are
reached by joining Payment → reservation; per-reservation breakdowns count
DISTINCT reservations to avoid multiplying a reservation by its payment count.
"""
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.db.models import (
    Case, CharField, Count, DecimalField, F, Sum, Value, When,
)
from django.db.models.functions import Coalesce, TruncDate
from django.utils import timezone

from payment.models import Payment
from reservations.models import Reservation


ZERO = Decimal("0.00")
_DEC = DecimalField(max_digits=12, decimal_places=2)

# Net cash for one Payment row: amount minus any partial refund recorded on it.
_NET = F("amount") - Coalesce("refunded_amount", Value(ZERO), output_field=_DEC)


def _sum_net(field_alias="net"):
    """Coalesced Sum() of the per-row net cash expression, as a Decimal."""
    return Coalesce(Sum(_NET), Value(ZERO), output_field=_DEC)


def payment_revenue_qs(start, end):
    """
    The transaction-level cash-basis revenue queryset for [start, end).
    One row per successful Stripe payment, anchored on Payment.created_at.
    Every other helper builds on this so the revenue definition stays
    consistent across the whole dashboard.

    Each row is annotated with ``net`` (amount − partial refund).
    Join to the parent reservation with the ``reservation__…`` path.
    """
    return (
        Payment.objects
        .filter(status="paid", created_at__gte=start, created_at__lt=end)
        .annotate(net=_NET)
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


def overview(start, end) -> dict:
    """
    Headline cards. 'Bookings Created' is volume anchored on created_at;
    every revenue figure is transaction-level cash-basis — the sum of paid
    Payment rows charged in the window (net of partial refunds), anchored on
    Payment.created_at. 'Bookings Paid' counts DISTINCT reservations that
    received at least one such payment.
    """
    pay = payment_revenue_qs(start, end)

    created = _scoped(Reservation.objects.all(), start, end).count()

    agg = pay.aggregate(
        net=_sum_net(),
        gross=Coalesce(Sum("amount"), Value(ZERO), output_field=_DEC),
        refunded=Coalesce(Sum("refunded_amount"), Value(ZERO), output_field=_DEC),
        txns=Count("id"),
        reservations=Count("reservation", distinct=True),
    )
    paid_revenue = agg["net"]
    paid_res = agg["reservations"]
    avg_ticket = (paid_revenue / paid_res) if paid_res else ZERO

    repeat_revenue = pay.filter(
        reservation__is_repeat_booking=True
    ).aggregate(s=_sum_net())["s"]

    return {
        "bookings_created": created,
        "bookings_paid": paid_res,
        "paid_txns": agg["txns"],
        "paid_conv_rate": round(_safe_div(paid_res, created) * 100, 1),
        "paid_revenue": paid_revenue,
        "gross_paid": agg["gross"],
        "refunded": agg["refunded"],
        "avg_ticket": avg_ticket,
        "repeat_revenue": repeat_revenue,
    }


def by_source(start, end):
    """
    Per-channel breakdown. Created column is anchored on created_at; all
    paid columns use the cash-basis revenue_at anchor.

    **Effective source — travel agent always wins.** A reservation linked to a
    travel_agent is counted under 'travel_agent' regardless of any ad/UTM
    params (gclid/fbclid/utm_source) it also carries, so agent bookings never
    double-count under Google/Meta. This mirrors the Reservation Sources page,
    and is independent of the stored ``booking_source`` column (which drifts —
    it isn't recomputed when an agent is linked after creation).

    Each row also carries ``pct_of_bookings`` (share of all bookings created in
    the window) and ``pct_of_revenue`` (share of paid revenue).
    """
    from reservations.attribution import channel_label

    # travel_agent FK overrides the (drift-prone) stored booking_source.
    eff_created = Case(
        When(travel_agent__isnull=False, then=Value("travel_agent")),
        default=F("booking_source"),
        output_field=CharField(),
    )
    eff_paid = Case(
        When(reservation__travel_agent__isnull=False, then=Value("travel_agent")),
        default=F("reservation__booking_source"),
        output_field=CharField(),
    )

    created_rows = (
        _scoped(Reservation.objects.all(), start, end)
        .annotate(eff_source=eff_created)
        .values("eff_source")
        .annotate(created=Count("id"))
    )
    by_src_created = {r["eff_source"]: r["created"] for r in created_rows}

    paid_rows = (
        payment_revenue_qs(start, end)
        .annotate(eff_source=eff_paid)
        .values("eff_source")
        .annotate(
            paid=Count("reservation", distinct=True),
            paid_revenue=_sum_net(),
        )
    )
    by_src_paid = {
        r["eff_source"]: {
            "paid": r["paid"],
            "paid_revenue": r["paid_revenue"],
        }
        for r in paid_rows
    }

    all_sources = set(by_src_created) | set(by_src_paid)
    rows = []
    for src in all_sources:
        created = by_src_created.get(src, 0)
        p = by_src_paid.get(src, {"paid": 0, "paid_revenue": ZERO})
        rows.append({
            "booking_source": src,
            "label": channel_label(src),
            "created": created,
            "paid": p["paid"],
            "paid_revenue": p["paid_revenue"] or ZERO,
        })

    total_revenue = sum((r["paid_revenue"] for r in rows), ZERO)
    total_created = sum(r["created"] for r in rows)
    for r in rows:
        r["conv_rate"] = round(_safe_div(r["paid"], r["created"]) * 100, 1)
        r["pct_of_revenue"] = round(_safe_div(r["paid_revenue"], total_revenue) * 100, 1)
        r["pct_of_bookings"] = round(_safe_div(r["created"], total_created) * 100, 1)
    rows.sort(key=lambda r: (r["paid_revenue"], r["created"]), reverse=True)
    return rows


def _commission_by_reservation_ids(res_ids):
    """
    Commission is a per-reservation amount, so it must be summed over DISTINCT
    reservations — never over the payment rows (which would multiply a
    reservation's commission by its number of payments). Returns
    {travel_agent_id: total_commission}.
    """
    return dict(
        Reservation.objects
        .filter(id__in=res_ids, travel_agent__isnull=False)
        .values_list("travel_agent_id")
        .annotate(c=Sum("commission_amount"))
        .values_list("travel_agent_id", "c")
    )


def by_travel_agent(start, end, limit: int = 25):
    """Top travel agents by paid revenue in the window (cash-basis)."""
    pay = payment_revenue_qs(start, end).filter(
        reservation__travel_agent__isnull=False
    )
    raw = list(
        pay.values(
            "reservation__travel_agent_id",
            "reservation__travel_agent__agent_name",
            "reservation__travel_agent__agency_name",
        )
        .annotate(
            paid_bookings=Count("reservation", distinct=True),
            paid_revenue=_sum_net(),
        )
        .order_by("-paid_revenue")[:limit]
    )
    res_ids = list(pay.values_list("reservation_id", flat=True).distinct())
    comm_by_agent = _commission_by_reservation_ids(res_ids)

    rows = []
    for r in raw:
        aid = r["reservation__travel_agent_id"]
        pb = r["paid_bookings"]
        pr = r["paid_revenue"] or ZERO
        rows.append({
            # keys kept identical to the old shape so the template is untouched
            "travel_agent_id": aid,
            "travel_agent__agent_name": r["reservation__travel_agent__agent_name"],
            "travel_agent__agency_name": r["reservation__travel_agent__agency_name"],
            "paid_bookings": pb,
            "paid_revenue": pr,
            "commission": comm_by_agent.get(aid, ZERO) or ZERO,
            "avg_ticket": (pr / pb) if pb else ZERO,
        })
    return rows


def travel_agent_totals(start, end) -> dict:
    """Combined totals across all travel-agent bookings — leaderboard footer."""
    pay = payment_revenue_qs(start, end).filter(
        reservation__travel_agent__isnull=False
    )
    agg = pay.aggregate(
        paid_bookings=Count("reservation", distinct=True),
        paid_revenue=_sum_net(),
    )
    res_ids = list(pay.values_list("reservation_id", flat=True).distinct())
    commission = (
        Reservation.objects.filter(id__in=res_ids)
        .aggregate(c=Sum("commission_amount"))["c"] or ZERO
    )
    return {
        "paid_bookings": agg["paid_bookings"] or 0,
        "paid_revenue": agg["paid_revenue"] or ZERO,
        "commission": commission,
    }


def by_route(start, end, limit: int = 15):
    """
    Top revenue-generating routes (cash-basis). Each row carries its share of
    paid revenue (``pct_of_revenue``) and of paid bookings (``pct_of_bookings``).
    Shares are computed against ALL paid routes in the window, not just the
    top N shown, so the percentages reflect the true route mix.
    """
    raw = list(
        payment_revenue_qs(start, end)
        .values(
            "reservation__rate__route__origin__name",
            "reservation__rate__route__destination__name",
        )
        .annotate(
            paid_bookings=Count("reservation", distinct=True),
            paid_revenue=_sum_net(),
        )
        .order_by("-paid_revenue")
    )
    total_revenue = sum((r["paid_revenue"] or ZERO for r in raw), ZERO)
    total_bookings = sum(r["paid_bookings"] for r in raw)

    rows = []
    for r in raw[:limit]:
        pr = r["paid_revenue"] or ZERO
        pb = r["paid_bookings"]
        rows.append({
            "rate__route__origin__name": r["reservation__rate__route__origin__name"],
            "rate__route__destination__name": r["reservation__rate__route__destination__name"],
            "paid_bookings": pb,
            "paid_revenue": pr,
            "pct_of_revenue": round(_safe_div(pr, total_revenue) * 100, 1),
            "pct_of_bookings": round(_safe_div(pb, total_bookings) * 100, 1),
        })
    return rows


def by_vehicle(start, end):
    """Paid revenue by vehicle type (cash-basis)."""
    raw = (
        payment_revenue_qs(start, end)
        .values("reservation__vehicle__vehicle_type")
        .annotate(
            paid_bookings=Count("reservation", distinct=True),
            paid_revenue=_sum_net(),
        )
        .order_by("-paid_revenue")
    )
    rows = []
    for r in raw:
        pb = r["paid_bookings"]
        pr = r["paid_revenue"] or ZERO
        rows.append({
            "vehicle__vehicle_type": r["reservation__vehicle__vehicle_type"],
            "paid_bookings": pb,
            "paid_revenue": pr,
            "avg_ticket": (pr / pb) if pb else ZERO,
        })
    return rows


def revenue_trend(start, end):
    """Daily paid-revenue series, keyed on Payment.created_at (cash-basis)."""
    return list(
        payment_revenue_qs(start, end)
        .annotate(day=TruncDate("created_at"))
        .values("day")
        .annotate(
            paid_revenue=_sum_net(),
            bookings=Count("reservation", distinct=True),
        )
        .order_by("day")
    )
