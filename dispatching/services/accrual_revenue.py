"""
Accrual Revenue Report — service module.

Builds accrual-basis revenue figures: revenue is recognized when the *ride was
fulfilled* (Leg.status='completed' AND Leg.pickup_date in the selected range),
regardless of when the customer paid.

This module is the single source of truth for the report. The page view and the
CSV/TXT exports all call build_report() so their numbers always reconcile.

Inclusion rule (a leg is included iff ALL of):
    - Leg.status == "completed"
    - Leg.pickup_date in [start_date, end_date]
    - Leg.exclude_from_analytics is False
    - Leg.reservation.status != "cancelled"

Revenue per leg = Leg.revenue_share (already split from Reservation.total_price,
which includes gratuity + additional_charges). When revenue_share is NULL the
report falls back to an equal split of Reservation.total_price and flags the
leg as estimated.

Stripe fees are intentionally OMITTED — there is no source-of-truth field on
Payment for actual fees charged.

Refunds are tied to in-range completed reservations regardless of refund date,
per business decision. Gross is NEVER reduced by refunds; net-after-refunds is
shown separately.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Iterable, Optional

from django.db.models import Prefetch
from django.utils import timezone

from reservations.models import Leg, Reservation
from payment.models import Payment
from dispatching.analytics import categorize_location


ZERO = Decimal("0.00")
CENT = Decimal("0.01")


# ── Anomaly flag tags ─────────────────────────────────────────────────────────

FLAG_ESTIMATED_SHARE = "estimated_share"          # revenue_share was NULL; equal-split fallback used
FLAG_ZERO_OR_NEGATIVE = "zero_or_negative"        # revenue_share <= 0
FLAG_SPLIT_RANGE = "split_range"                  # reservation has legs both in and out of range
FLAG_REFUNDED = "refunded"                        # reservation.total_refunded > 0
FLAG_CANCELLED_RES = "cancelled_with_completed"   # reservation cancelled but leg completed
FLAG_UNPAID = "unpaid"                            # leg/reservation not paid (A/R note)

FLAG_LABELS = {
    FLAG_ESTIMATED_SHARE: "Estimated revenue (equal-split fallback — leg.revenue_share was NULL)",
    FLAG_ZERO_OR_NEGATIVE: "Zero or negative fare",
    FLAG_SPLIT_RANGE: "Split-range — reservation has legs both inside and outside the range",
    FLAG_REFUNDED: "Refunded reservation — review refund period vs accrual range",
    FLAG_CANCELLED_RES: "Reservation is cancelled but leg is completed — data inconsistency",
    FLAG_UNPAID: "Unpaid — accounts receivable (still in accrual since service was rendered)",
}


# ── Result dataclasses ────────────────────────────────────────────────────────


@dataclass
class LegRow:
    """One row in the audit table / CSV."""
    leg_id: int
    reservation_id: str
    customer_name: str
    pickup_date: date
    pickup_time: time
    status_changed_at: Optional[datetime]
    trip_type: str  # one_way / round_trip
    pickup_location: str
    dropoff_location: str
    leg_status: str
    vehicle: str
    booking_source: str
    reservation_payment_status: str  # cached property on Reservation
    leg_payment_status: str
    gross_fare_in_range: Decimal
    revenue_share_was_null: bool
    tip_allocated: Decimal
    additional_charges_allocated: Decimal
    base_price_allocated: Decimal
    reservation_total_price: Decimal
    reservation_total_refunded: Decimal
    driver_pay: Decimal
    driver_name: str
    route_category: str  # categorize(pickup) → categorize(dropoff)
    flags: list[str] = field(default_factory=list)


@dataclass
class BreakdownRow:
    label: str
    leg_count: int
    revenue: Decimal


@dataclass
class DayRow:
    day: date
    leg_count: int
    revenue: Decimal


@dataclass
class AnomalyBucket:
    """Lists of LegRow grouped by flag."""
    estimated_share: list[LegRow] = field(default_factory=list)
    zero_or_negative: list[LegRow] = field(default_factory=list)
    split_range: list[LegRow] = field(default_factory=list)
    refunded: list[LegRow] = field(default_factory=list)
    cancelled_with_completed: list[LegRow] = field(default_factory=list)


@dataclass
class AccrualReport:
    # Inputs (echoed for templates/exports)
    start_date: date
    end_date: date
    timezone_name: str
    filters: dict

    # Headline
    total_legs: int
    total_reservations: int
    gross_accrual_revenue: Decimal
    avg_fare_per_leg: Decimal
    avg_fare_per_reservation: Decimal

    # Breakdown (informational — never subtracted from gross)
    tips_allocated: Decimal
    additional_charges_allocated: Decimal
    base_price_allocated: Decimal
    refunds_for_inrange_reservations: Decimal
    net_after_refunds: Decimal
    driver_pay_total: Decimal
    estimated_margin: Decimal  # gross - driver_pay (informational)

    # Group rows
    by_day: list[DayRow]
    by_vehicle: list[BreakdownRow]
    by_source: list[BreakdownRow]
    by_route_category: list[BreakdownRow]
    by_payment_status: list[BreakdownRow]
    top_legs: list[LegRow]

    # Audit
    legs: list[LegRow]
    anomalies: AnomalyBucket


# ── Public entry point ────────────────────────────────────────────────────────


def build_report(
    start_date: date,
    end_date: date,
    *,
    vehicle_id: Optional[int] = None,
    booking_source: Optional[str] = None,
    payment_status: Optional[str] = None,
) -> AccrualReport:
    """
    Build the accrual revenue report for the given inclusive date range.

    Args:
        start_date, end_date: bounds of the service-date window (inclusive).
            Treated as America/New_York calendar days.
        vehicle_id: optional Vehicle PK filter (matches effective_vehicle).
        booking_source: optional Reservation.booking_source filter.
        payment_status: optional Reservation.payment_status (cached property)
            filter — applied post-fetch since it's a property, not a column.

    Returns:
        AccrualReport with all summary/breakdown/audit data populated.
    """
    if end_date < start_date:
        start_date, end_date = end_date, start_date

    legs_qs = (
        Leg.objects.filter(
            status="completed",
            pickup_date__gte=start_date,
            pickup_date__lte=end_date,
            exclude_from_analytics=False,
        )
        .exclude(reservation__status="cancelled")
        .select_related(
            "reservation",
            "reservation__customer",
            "reservation__vehicle",
            "vehicle",
            "driver",
        )
        .prefetch_related(
            # for split-range detection — need ALL legs of each in-range reservation
            "reservation__legs",
            # for refund/payment_status calculation
            Prefetch(
                "reservation__payments",
                queryset=Payment.objects.order_by("-created_at"),
            ),
        )
        .order_by("pickup_date", "pickup_time")
    )

    if vehicle_id:
        # Match either leg-level override or reservation-level vehicle.
        from django.db.models import Q
        legs_qs = legs_qs.filter(
            Q(vehicle_id=vehicle_id) | Q(vehicle__isnull=True, reservation__vehicle_id=vehicle_id)
        )
    if booking_source:
        legs_qs = legs_qs.filter(reservation__booking_source=booking_source)

    legs = list(legs_qs)

    # Build LegRow objects with all derived fields.
    rows: list[LegRow] = []
    for leg in legs:
        rows.append(_build_leg_row(leg, start_date, end_date))

    # Post-fetch filter on reservation.payment_status (it's a @cached_property).
    if payment_status:
        rows = [r for r in rows if r.reservation_payment_status == payment_status]

    return _aggregate(rows, start_date, end_date, {
        "vehicle_id": vehicle_id,
        "booking_source": booking_source,
        "payment_status": payment_status,
    })


# ── Row construction ──────────────────────────────────────────────────────────


def _leg_revenue(leg: Leg) -> tuple[Decimal, bool]:
    """
    Return (revenue, was_null).
    If leg.revenue_share is NULL, fall back to equal-split of reservation total
    and signal that an estimate was used.
    """
    if leg.revenue_share is not None:
        return Decimal(leg.revenue_share), False
    res = leg.reservation
    if not res or res.total_price is None:
        return ZERO, True
    leg_count = len(res.legs.all()) or 1  # prefetched
    share = (Decimal(res.total_price) / Decimal(leg_count)).quantize(CENT)
    return share, True


def _allocate_field(leg_share: Decimal, reservation_total: Decimal, field_value: Decimal) -> Decimal:
    """
    Proportionally allocate a reservation-level money field (gratuity_amount,
    additional_charges, base_price) to one leg based on its revenue_share weight.

    leg_alloc = field_value * (leg_share / reservation_total)

    Falls back to equal split if reservation_total is zero.
    """
    if not field_value:
        return ZERO
    if reservation_total and reservation_total > 0:
        ratio = Decimal(leg_share) / Decimal(reservation_total)
        return (Decimal(field_value) * ratio).quantize(CENT)
    return Decimal(field_value).quantize(CENT)


def _build_leg_row(leg: Leg, start_date: date, end_date: date) -> LegRow:
    res = leg.reservation
    revenue, was_null = _leg_revenue(leg)
    res_total = Decimal(res.total_price) if (res and res.total_price) else ZERO

    tip_alloc = _allocate_field(revenue, res_total, Decimal(res.gratuity_amount or 0)) if res else ZERO
    addl_alloc = _allocate_field(revenue, res_total, Decimal(res.additional_charges or 0)) if res else ZERO
    base_alloc = _allocate_field(revenue, res_total, Decimal(res.base_price or 0)) if res else ZERO

    # Driver pay: prefer total_driver_pay cached property (sums base+grat+additional).
    driver_pay = ZERO
    try:
        driver_pay = Decimal(leg.total_driver_pay or 0)
    except Exception:
        driver_pay = ZERO

    # Effective vehicle display
    eff_vehicle = leg.effective_vehicle
    vehicle_str = eff_vehicle.get_vehicle_type_display() if eff_vehicle else ""

    # Customer name
    if res and getattr(res, "customer", None):
        cust = res.customer
        customer_name = (
            f"{getattr(cust, 'first_name', '') or ''} {getattr(cust, 'last_name', '') or ''}".strip()
            or getattr(cust, "email", "") or ""
        )
    else:
        customer_name = ""

    # Reservation payment_status is a @cached_property — accessing it triggers
    # the computation, but payments are prefetched so no extra queries.
    res_pay_status = ""
    if res:
        try:
            res_pay_status = res.payment_status or ""
        except Exception:
            res_pay_status = ""

    # Flags
    flags: list[str] = []
    if was_null:
        flags.append(FLAG_ESTIMATED_SHARE)
    if revenue is not None and revenue <= ZERO:
        flags.append(FLAG_ZERO_OR_NEGATIVE)
    if res:
        # Split-range: any sibling leg whose pickup_date is outside the window.
        for sibling in res.legs.all():
            if sibling.pickup_date < start_date or sibling.pickup_date > end_date:
                flags.append(FLAG_SPLIT_RANGE)
                break
        if Decimal(res.total_refunded or 0) > ZERO:
            flags.append(FLAG_REFUNDED)
        if res.status == "cancelled":
            flags.append(FLAG_CANCELLED_RES)
        # Unpaid A/R: leg unpaid AND reservation not paid.
        if leg.payment_status == "unpaid" and not getattr(res, "is_paid", False):
            flags.append(FLAG_UNPAID)

    route_cat = ""
    try:
        pickup_cat = categorize_location(leg.pickup_location or "")
        drop_cat = categorize_location(leg.dropoff_location or "")
        route_cat = f"{pickup_cat} → {drop_cat}"
    except Exception:
        route_cat = ""

    return LegRow(
        leg_id=leg.id,
        reservation_id=str(res.id) if res else "",
        customer_name=customer_name,
        pickup_date=leg.pickup_date,
        pickup_time=leg.pickup_time,
        status_changed_at=leg.status_changed_at,
        trip_type=(res.get_trip_type_display() if res else "") if res else "",
        pickup_location=leg.pickup_location or "",
        dropoff_location=leg.dropoff_location or "",
        leg_status=leg.status or "",
        vehicle=vehicle_str,
        booking_source=(res.booking_source if res else "") or "",
        reservation_payment_status=res_pay_status,
        leg_payment_status=leg.payment_status or "",
        gross_fare_in_range=revenue.quantize(CENT) if revenue is not None else ZERO,
        revenue_share_was_null=was_null,
        tip_allocated=tip_alloc,
        additional_charges_allocated=addl_alloc,
        base_price_allocated=base_alloc,
        reservation_total_price=res_total,
        reservation_total_refunded=Decimal(res.total_refunded or 0) if res else ZERO,
        driver_pay=driver_pay,
        driver_name=str(leg.driver) if leg.driver else "",
        route_category=route_cat,
        flags=flags,
    )


# ── Aggregation ───────────────────────────────────────────────────────────────


def _aggregate(rows: list[LegRow], start_date: date, end_date: date, filters: dict) -> AccrualReport:
    total_legs = len(rows)
    unique_res_ids: set[str] = set()
    gross = ZERO
    tips_total = ZERO
    addl_total = ZERO
    base_total = ZERO
    driver_pay_total = ZERO

    by_day: dict[date, dict] = defaultdict(lambda: {"count": 0, "rev": ZERO})
    by_vehicle: dict[str, dict] = defaultdict(lambda: {"count": 0, "rev": ZERO})
    by_source: dict[str, dict] = defaultdict(lambda: {"count": 0, "rev": ZERO})
    by_route: dict[str, dict] = defaultdict(lambda: {"count": 0, "rev": ZERO})
    by_pay: dict[str, dict] = defaultdict(lambda: {"count": 0, "rev": ZERO})

    anomalies = AnomalyBucket()
    refunds_seen_for_res: dict[str, Decimal] = {}

    for r in rows:
        unique_res_ids.add(r.reservation_id)
        gross += r.gross_fare_in_range
        tips_total += r.tip_allocated
        addl_total += r.additional_charges_allocated
        base_total += r.base_price_allocated
        driver_pay_total += r.driver_pay

        d = by_day[r.pickup_date]
        d["count"] += 1
        d["rev"] += r.gross_fare_in_range

        v = by_vehicle[r.vehicle or "(no vehicle)"]
        v["count"] += 1
        v["rev"] += r.gross_fare_in_range

        s = by_source[r.booking_source or "(unknown)"]
        s["count"] += 1
        s["rev"] += r.gross_fare_in_range

        rc = by_route[r.route_category or "(uncategorized)"]
        rc["count"] += 1
        rc["rev"] += r.gross_fare_in_range

        p = by_pay[r.reservation_payment_status or "(unknown)"]
        p["count"] += 1
        p["rev"] += r.gross_fare_in_range

        # Refund total — only count each reservation's refund once.
        if r.reservation_id and r.reservation_id not in refunds_seen_for_res:
            refunds_seen_for_res[r.reservation_id] = r.reservation_total_refunded

        # Anomaly bucketing
        if FLAG_ESTIMATED_SHARE in r.flags:
            anomalies.estimated_share.append(r)
        if FLAG_ZERO_OR_NEGATIVE in r.flags:
            anomalies.zero_or_negative.append(r)
        if FLAG_SPLIT_RANGE in r.flags:
            anomalies.split_range.append(r)
        if FLAG_REFUNDED in r.flags:
            anomalies.refunded.append(r)
        if FLAG_CANCELLED_RES in r.flags:
            anomalies.cancelled_with_completed.append(r)

    refunds_total = sum(refunds_seen_for_res.values(), ZERO)

    avg_per_leg = (gross / Decimal(total_legs)).quantize(CENT) if total_legs else ZERO
    avg_per_res = (gross / Decimal(len(unique_res_ids))).quantize(CENT) if unique_res_ids else ZERO

    by_day_rows = [
        DayRow(day=d, leg_count=v["count"], revenue=v["rev"].quantize(CENT))
        for d, v in sorted(by_day.items())
    ]
    by_vehicle_rows = sorted(
        [BreakdownRow(label=k, leg_count=v["count"], revenue=v["rev"].quantize(CENT)) for k, v in by_vehicle.items()],
        key=lambda x: x.revenue,
        reverse=True,
    )
    by_source_rows = sorted(
        [BreakdownRow(label=k, leg_count=v["count"], revenue=v["rev"].quantize(CENT)) for k, v in by_source.items()],
        key=lambda x: x.revenue,
        reverse=True,
    )
    by_route_rows = sorted(
        [BreakdownRow(label=k, leg_count=v["count"], revenue=v["rev"].quantize(CENT)) for k, v in by_route.items()],
        key=lambda x: x.revenue,
        reverse=True,
    )
    by_pay_rows = sorted(
        [BreakdownRow(label=k, leg_count=v["count"], revenue=v["rev"].quantize(CENT)) for k, v in by_pay.items()],
        key=lambda x: x.revenue,
        reverse=True,
    )

    top_legs = sorted(rows, key=lambda r: r.gross_fare_in_range, reverse=True)[:10]

    return AccrualReport(
        start_date=start_date,
        end_date=end_date,
        timezone_name="America/New_York",
        filters=filters,
        total_legs=total_legs,
        total_reservations=len(unique_res_ids),
        gross_accrual_revenue=gross.quantize(CENT),
        avg_fare_per_leg=avg_per_leg,
        avg_fare_per_reservation=avg_per_res,
        tips_allocated=tips_total.quantize(CENT),
        additional_charges_allocated=addl_total.quantize(CENT),
        base_price_allocated=base_total.quantize(CENT),
        refunds_for_inrange_reservations=refunds_total.quantize(CENT),
        net_after_refunds=(gross - refunds_total).quantize(CENT),
        driver_pay_total=driver_pay_total.quantize(CENT),
        estimated_margin=(gross - driver_pay_total).quantize(CENT),
        by_day=by_day_rows,
        by_vehicle=by_vehicle_rows,
        by_source=by_source_rows,
        by_route_category=by_route_rows,
        by_payment_status=by_pay_rows,
        top_legs=top_legs,
        legs=rows,
        anomalies=anomalies,
    )


# ── Quick-filter helper ───────────────────────────────────────────────────────


def resolve_quick_filter(quick: str, today: Optional[date] = None) -> Optional[tuple[date, date]]:
    """
    Map a quick-filter token to (start_date, end_date) in America/New_York.
    Returns None if quick is empty/unknown/'custom'.
    """
    if not quick or quick == "custom":
        return None
    today = today or timezone.localdate()  # already America/New_York since USE_TZ=True

    if quick == "today":
        return today, today
    if quick == "yesterday":
        y = today - timedelta(days=1)
        return y, y
    if quick == "this_month":
        first = today.replace(day=1)
        return first, today
    if quick == "last_month":
        first_this = today.replace(day=1)
        last_prev = first_this - timedelta(days=1)
        first_prev = last_prev.replace(day=1)
        return first_prev, last_prev
    if quick == "ytd":
        return today.replace(month=1, day=1), today
    return None
