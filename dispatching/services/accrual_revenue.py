"""
Accrual Revenue Report — service module.

Builds accrual-basis revenue figures: revenue is recognized when the *ride was
fulfilled* (Leg.pickup_date in the selected range, Leg.status != 'cancelled'),
regardless of when the customer paid.

This module is the single source of truth for the report. The page view and the
CSV/TXT exports all call build_report() so their numbers always reconcile.

Inclusion rule (a leg is included iff ALL of):
    - Leg.pickup_date in [start_date, end_date]
    - Leg.status != "cancelled"
    - Leg.exclude_from_analytics is False
    - Reservation.status != "cancelled"

Notes on inclusion:
- Statuses 'in-progress', 'confirmed', 'on-the-way', 'on-location', 'picked-up'
  are included (and flagged FLAG_PENDING_STATUS) because in production data
  many rides genuinely ran but the dispatcher never tapped 'completed'. Filter
  by flag to exclude them if you want a stricter view.

Revenue per leg: smart fallback.
- If sum(Leg.revenue_share over non-cancelled legs of the parent) reconciles
  to Reservation.total_price (within $0.02), use Leg.revenue_share directly.
- Otherwise, allocate Reservation.total_price across non-cancelled legs:
    - If any non-cancelled leg has leg_base_price set, use those as weights
      (mirrors Reservation.recalculate_leg_revenue_shares logic).
    - Otherwise, equal split.
- The allocated amount is computed at report time only — Leg.revenue_share is
  never modified in the database.

Stripe processing fees: OMITTED. There is no source-of-truth field on Payment.

Refunds: never reduce gross. Two views surfaced:
- "Refunds tied to in-range reservations": sum(Reservation.total_refunded) for
  any reservation that has at least one included leg, regardless of refund date.
- "Refund payments in window": sum(Payment.refunded_amount) where Payment.status
  = 'refunded' and Payment.updated_at falls in the date window.

Gratuity is included in headline gross (it's part of Reservation.total_price).
A separate "Gross excluding gratuity" line is also published since drivers
receive ~98% of customer-paid gratuity in production data — accountant's call.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Optional

from django.db.models import Prefetch, Sum
from django.utils import timezone

from reservations.models import Leg, Reservation
from payment.models import Payment
from dispatching.analytics import categorize_location


ZERO = Decimal("0.00")
CENT = Decimal("0.01")
RECONCILE_TOLERANCE = Decimal("0.02")  # allow 2¢ rounding before falling back


# ── Anomaly flag tags ─────────────────────────────────────────────────────────

FLAG_REALLOCATED = "reallocated"                  # rev_share didn't reconcile, fallback used
FLAG_ZERO_OR_NEGATIVE = "zero_or_negative"        # allocated revenue <= 0
FLAG_SPLIT_RANGE = "split_range"                  # reservation has legs both in and out of range
FLAG_REFUNDED = "refunded"                        # reservation.total_refunded > 0
FLAG_CANCELLED_RES = "cancelled_with_completed"   # reservation cancelled but leg in non-cancelled status
FLAG_UNPAID = "unpaid"                            # leg/reservation not paid (A/R note)
FLAG_PENDING_STATUS = "pending_status"            # leg in non-completed, non-cancelled status

FLAG_LABELS = {
    FLAG_REALLOCATED: "Revenue reallocated — leg.revenue_share did not reconcile to reservation.total_price; allocated from total_price",
    FLAG_ZERO_OR_NEGATIVE: "Zero or negative fare (reservation total_price is also zero)",
    FLAG_SPLIT_RANGE: "Split-range — reservation has legs both inside and outside the range",
    FLAG_REFUNDED: "Refunded reservation — review refund period vs accrual range",
    FLAG_CANCELLED_RES: "Reservation is cancelled but a leg is non-cancelled — data inconsistency",
    FLAG_UNPAID: "Unpaid — accounts receivable (still in accrual since service was rendered)",
    FLAG_PENDING_STATUS: "Pending status — leg not marked 'completed'. Verify the ride actually ran.",
}


# Allocation methods (stored on each LegRow.allocation_method)
ALLOC_REVENUE_SHARE = "revenue_share"           # used Leg.revenue_share (reconciled)
ALLOC_WEIGHTED = "weighted_total_price"         # allocated total_price by leg_base_price weights
ALLOC_EQUAL = "equal_split"                     # allocated total_price equally across non-cancelled legs


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
    trip_type: str
    pickup_location: str
    dropoff_location: str
    leg_status: str
    vehicle: str
    booking_source: str
    reservation_payment_status: str
    leg_payment_status: str
    gross_fare_in_range: Decimal
    allocation_method: str  # one of ALLOC_*
    tip_allocated: Decimal
    additional_charges_allocated: Decimal
    base_price_allocated: Decimal
    reservation_total_price: Decimal
    reservation_total_refunded: Decimal
    driver_pay: Decimal
    driver_name: str
    route_category: str
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
    reallocated: list[LegRow] = field(default_factory=list)
    zero_or_negative: list[LegRow] = field(default_factory=list)
    split_range: list[LegRow] = field(default_factory=list)
    refunded: list[LegRow] = field(default_factory=list)
    cancelled_with_completed: list[LegRow] = field(default_factory=list)
    pending_status: list[LegRow] = field(default_factory=list)


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
    gross_excluding_gratuity: Decimal

    # Refund views (gross is never reduced; net lines computed for display)
    refunds_for_inrange_reservations: Decimal
    refund_payments_in_window: Decimal
    net_after_inrange_refunds: Decimal
    net_after_window_refund_payments: Decimal

    # Cost informational
    driver_pay_total: Decimal
    estimated_margin: Decimal  # gross - driver_pay_total (informational)

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

    # Backwards-compat alias kept so existing template `net_after_refunds` works.
    @property
    def net_after_refunds(self) -> Decimal:
        return self.net_after_inrange_refunds


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
    """
    if end_date < start_date:
        start_date, end_date = end_date, start_date

    # ── 1. Pull every in-range, non-cancelled leg ─────────────────────────
    legs_qs = (
        Leg.objects.filter(
            pickup_date__gte=start_date,
            pickup_date__lte=end_date,
            exclude_from_analytics=False,
        )
        .exclude(status="cancelled")
        .exclude(reservation__status="cancelled")
        .select_related(
            "reservation",
            "reservation__customer",
            "reservation__vehicle",
            "vehicle",
            "driver",
        )
        .prefetch_related(
            # ALL legs of each in-range reservation (siblings for allocation + split-range)
            "reservation__legs",
            Prefetch(
                "reservation__payments",
                queryset=Payment.objects.order_by("-created_at"),
            ),
        )
        .order_by("pickup_date", "pickup_time")
    )

    if vehicle_id:
        from django.db.models import Q
        legs_qs = legs_qs.filter(
            Q(vehicle_id=vehicle_id)
            | Q(vehicle__isnull=True, reservation__vehicle_id=vehicle_id)
        )
    if booking_source:
        legs_qs = legs_qs.filter(reservation__booking_source=booking_source)

    in_range_legs = list(legs_qs)

    # ── 2. Group in-range legs by reservation; allocate per reservation ───
    by_res: dict = defaultdict(list)
    for leg in in_range_legs:
        by_res[leg.reservation_id].append(leg)

    leg_revenue: dict[int, Decimal] = {}
    leg_method: dict[int, str] = {}
    for res_id, in_range_for_res in by_res.items():
        res = in_range_for_res[0].reservation
        # Siblings come from prefetch — includes the in-range legs and any others.
        all_valid_legs = [l for l in res.legs.all() if l.status != "cancelled"]
        method, alloc_map = _allocate_reservation(res, in_range_for_res, all_valid_legs)
        leg_revenue.update(alloc_map)
        for leg_id in alloc_map:
            leg_method[leg_id] = method

    # ── 3. Build LegRow objects ───────────────────────────────────────────
    rows: list[LegRow] = []
    for leg in in_range_legs:
        revenue = leg_revenue.get(leg.id, ZERO)
        method = leg_method.get(leg.id, ALLOC_REVENUE_SHARE)
        rows.append(_build_leg_row(leg, revenue, method, start_date, end_date))

    # Post-fetch filter on reservation.payment_status (it's a @cached_property).
    if payment_status:
        rows = [r for r in rows if r.reservation_payment_status == payment_status]

    # ── 4. Refund payments dated in the window (separate from per-reservation refunds) ─
    window_end_exclusive = datetime.combine(end_date + timedelta(days=1), time.min)
    window_start_inclusive = datetime.combine(start_date, time.min)
    if timezone.is_aware(timezone.now()):
        tz = timezone.get_current_timezone()
        window_end_exclusive = timezone.make_aware(window_end_exclusive, tz)
        window_start_inclusive = timezone.make_aware(window_start_inclusive, tz)
    refund_payments_in_window = (
        Payment.objects.filter(
            status="refunded",
            refunded_amount__isnull=False,
            updated_at__gte=window_start_inclusive,
            updated_at__lt=window_end_exclusive,
        ).aggregate(s=Sum("refunded_amount"))["s"]
        or ZERO
    )

    return _aggregate(
        rows,
        start_date,
        end_date,
        Decimal(refund_payments_in_window or 0),
        {
            "vehicle_id": vehicle_id,
            "booking_source": booking_source,
            "payment_status": payment_status,
        },
    )


# ── Allocation ────────────────────────────────────────────────────────────────


def _allocate_reservation(
    res: Reservation,
    in_range_legs: list[Leg],
    all_valid_legs: list[Leg],
) -> tuple[str, dict[int, Decimal]]:
    """
    Decide each in-range leg's revenue contribution for `res`.

    Returns (allocation_method, {leg_id: Decimal}).

    Smart-fallback rules:
      1. If sum(Leg.revenue_share over non-cancelled siblings) reconciles to
         Reservation.total_price within RECONCILE_TOLERANCE AND > 0:
         use Leg.revenue_share for each in-range leg.
      2. Else allocate Reservation.total_price across non-cancelled siblings:
         - If any non-cancelled leg has leg_base_price set: weighted by
           leg_base_price (with default = base_price/n_valid for unset legs).
         - Else: equal split.

    Reservation.total_price is never modified.
    """
    total = Decimal(res.total_price or 0)

    # Path 1 — revenue_share reconciles
    sum_share = sum(
        Decimal(l.revenue_share or 0) for l in all_valid_legs
    )
    if sum_share > 0 and abs(sum_share - total) <= RECONCILE_TOLERANCE:
        return ALLOC_REVENUE_SHARE, {
            l.id: Decimal(l.revenue_share or 0).quantize(CENT)
            for l in in_range_legs
        }

    n_valid = len(all_valid_legs)
    if n_valid == 0 or total <= 0:
        # Pathological: no valid legs (shouldn't happen since we have in_range_legs)
        # or total_price is 0/negative. Return zeros and let zero_or_negative flag fire.
        return ALLOC_EQUAL, {l.id: ZERO for l in in_range_legs}

    # Path 2a — weighted by leg_base_price
    has_weights = any(l.leg_base_price is not None for l in all_valid_legs)
    if has_weights:
        base = Decimal(res.base_price or 0)
        default = (base / Decimal(n_valid)).quantize(CENT) if base else ZERO
        total_weight = sum(
            Decimal(l.leg_base_price if l.leg_base_price is not None else default)
            for l in all_valid_legs
        )
        if total_weight > 0:
            return ALLOC_WEIGHTED, {
                l.id: (
                    total
                    * Decimal(l.leg_base_price if l.leg_base_price is not None else default)
                    / total_weight
                ).quantize(CENT)
                for l in in_range_legs
            }

    # Path 2b — equal split
    share = (total / Decimal(n_valid)).quantize(CENT)
    return ALLOC_EQUAL, {l.id: share for l in in_range_legs}


def _allocate_field(
    leg_revenue: Decimal, reservation_total: Decimal, field_value: Decimal
) -> Decimal:
    """
    Proportionally allocate a reservation-level money field (gratuity_amount,
    additional_charges, base_price) to one leg using the same weight as its
    revenue contribution: leg_alloc = field * (leg_revenue / total_price).
    """
    if not field_value:
        return ZERO
    if reservation_total and reservation_total > 0:
        ratio = Decimal(leg_revenue) / Decimal(reservation_total)
        return (Decimal(field_value) * ratio).quantize(CENT)
    return ZERO


# ── Row construction ──────────────────────────────────────────────────────────


def _build_leg_row(
    leg: Leg,
    revenue: Decimal,
    method: str,
    start_date: date,
    end_date: date,
) -> LegRow:
    res = leg.reservation
    res_total = Decimal(res.total_price) if (res and res.total_price) else ZERO
    revenue = Decimal(revenue or 0).quantize(CENT)

    tip_alloc = _allocate_field(revenue, res_total, Decimal(res.gratuity_amount or 0)) if res else ZERO
    addl_alloc = _allocate_field(revenue, res_total, Decimal(res.additional_charges or 0)) if res else ZERO
    base_alloc = _allocate_field(revenue, res_total, Decimal(res.base_price or 0)) if res else ZERO

    driver_pay = ZERO
    try:
        driver_pay = Decimal(leg.total_driver_pay or 0)
    except Exception:
        driver_pay = ZERO

    eff_vehicle = leg.effective_vehicle
    vehicle_str = eff_vehicle.get_vehicle_type_display() if eff_vehicle else ""

    if res and getattr(res, "customer", None):
        cust = res.customer
        customer_name = (
            f"{getattr(cust, 'first_name', '') or ''} {getattr(cust, 'last_name', '') or ''}".strip()
            or getattr(cust, "email", "") or ""
        )
    else:
        customer_name = ""

    res_pay_status = ""
    if res:
        try:
            res_pay_status = res.payment_status or ""
        except Exception:
            res_pay_status = ""

    flags: list[str] = []
    if method != ALLOC_REVENUE_SHARE:
        flags.append(FLAG_REALLOCATED)
    if revenue <= ZERO:
        flags.append(FLAG_ZERO_OR_NEGATIVE)
    if leg.status not in ("completed", "cancelled"):
        flags.append(FLAG_PENDING_STATUS)
    if res:
        for sibling in res.legs.all():
            if sibling.pickup_date < start_date or sibling.pickup_date > end_date:
                flags.append(FLAG_SPLIT_RANGE)
                break
        if Decimal(res.total_refunded or 0) > ZERO:
            flags.append(FLAG_REFUNDED)
        if res.status == "cancelled":
            flags.append(FLAG_CANCELLED_RES)
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
        trip_type=(res.get_trip_type_display() if res else "") or "",
        pickup_location=leg.pickup_location or "",
        dropoff_location=leg.dropoff_location or "",
        leg_status=leg.status or "",
        vehicle=vehicle_str,
        booking_source=(res.booking_source if res else "") or "",
        reservation_payment_status=res_pay_status,
        leg_payment_status=leg.payment_status or "",
        gross_fare_in_range=revenue,
        allocation_method=method,
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


def _aggregate(
    rows: list[LegRow],
    start_date: date,
    end_date: date,
    refund_payments_in_window: Decimal,
    filters: dict,
) -> AccrualReport:
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

        if r.reservation_id and r.reservation_id not in refunds_seen_for_res:
            refunds_seen_for_res[r.reservation_id] = r.reservation_total_refunded

        if FLAG_REALLOCATED in r.flags:
            anomalies.reallocated.append(r)
        if FLAG_ZERO_OR_NEGATIVE in r.flags:
            anomalies.zero_or_negative.append(r)
        if FLAG_SPLIT_RANGE in r.flags:
            anomalies.split_range.append(r)
        if FLAG_REFUNDED in r.flags:
            anomalies.refunded.append(r)
        if FLAG_CANCELLED_RES in r.flags:
            anomalies.cancelled_with_completed.append(r)
        if FLAG_PENDING_STATUS in r.flags:
            anomalies.pending_status.append(r)

    refunds_total = sum(refunds_seen_for_res.values(), ZERO)

    avg_per_leg = (gross / Decimal(total_legs)).quantize(CENT) if total_legs else ZERO
    avg_per_res = (
        (gross / Decimal(len(unique_res_ids))).quantize(CENT)
        if unique_res_ids
        else ZERO
    )

    by_day_rows = [
        DayRow(day=d, leg_count=v["count"], revenue=v["rev"].quantize(CENT))
        for d, v in sorted(by_day.items())
    ]
    by_vehicle_rows = sorted(
        [BreakdownRow(label=k, leg_count=v["count"], revenue=v["rev"].quantize(CENT)) for k, v in by_vehicle.items()],
        key=lambda x: x.revenue, reverse=True,
    )
    by_source_rows = sorted(
        [BreakdownRow(label=k, leg_count=v["count"], revenue=v["rev"].quantize(CENT)) for k, v in by_source.items()],
        key=lambda x: x.revenue, reverse=True,
    )
    by_route_rows = sorted(
        [BreakdownRow(label=k, leg_count=v["count"], revenue=v["rev"].quantize(CENT)) for k, v in by_route.items()],
        key=lambda x: x.revenue, reverse=True,
    )
    by_pay_rows = sorted(
        [BreakdownRow(label=k, leg_count=v["count"], revenue=v["rev"].quantize(CENT)) for k, v in by_pay.items()],
        key=lambda x: x.revenue, reverse=True,
    )

    top_legs = sorted(rows, key=lambda r: r.gross_fare_in_range, reverse=True)[:10]

    refund_payments_in_window = Decimal(refund_payments_in_window or 0).quantize(CENT)
    refunds_total_q = refunds_total.quantize(CENT)
    gross_q = gross.quantize(CENT)

    return AccrualReport(
        start_date=start_date,
        end_date=end_date,
        timezone_name="America/New_York",
        filters=filters,
        total_legs=total_legs,
        total_reservations=len(unique_res_ids),
        gross_accrual_revenue=gross_q,
        avg_fare_per_leg=avg_per_leg,
        avg_fare_per_reservation=avg_per_res,
        tips_allocated=tips_total.quantize(CENT),
        additional_charges_allocated=addl_total.quantize(CENT),
        base_price_allocated=base_total.quantize(CENT),
        gross_excluding_gratuity=(gross - tips_total).quantize(CENT),
        refunds_for_inrange_reservations=refunds_total_q,
        refund_payments_in_window=refund_payments_in_window,
        net_after_inrange_refunds=(gross_q - refunds_total_q),
        net_after_window_refund_payments=(gross_q - refund_payments_in_window),
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
    """Map a quick-filter token to (start_date, end_date) in America/New_York."""
    if not quick or quick == "custom":
        return None
    today = today or timezone.localdate()

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
