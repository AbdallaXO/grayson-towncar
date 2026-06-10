"""
Fleet Capacity Intelligence — read-only economics + farm-out classification.

Phase A of ``docs/fleet-capacity-intelligence/README.md``. This module is **strictly
read-only**: it performs NO model writes and requires NO migrations. It reuses the existing
scheduling engine (``dispatching/scheduler.py`` + ``feasibility_guards.py`` +
``swap_optimizer.py``) to decide *why* each farmed-out leg could not be served in-house.

Key facts the design rests on (see the design doc):

* **Farm-out is derived**, not stored: ``leg.driver.driver_type == 'affiliate'`` (mirrors
  ``scheduler.get_coverage_stats``). No ``is_farmed_out`` flag exists.
* **Affiliate cost** is the leg's ``driver_base_pay`` (the affiliate's rate, looked up from
  ``DriverPayRate`` at save). **In-house counterfactual cost** is ``route.inhouse_base_pay``
  (what a generic in-house driver would have been paid for that route).
* **Recovered margin (driver-pay-only v1)** = affiliate base − in-house counterfactual base.
  Gratuity is a customer pass-through and ``driver_additional`` (night bonus, wait time) is
  situational, so both are EXCLUDED by default (tunable via ``INCLUDE_ADDITIONAL_PAY``).

Confidence caveat: classification leans on ``feasibility_guards`` driver windows, which are
STUB-backed today (``USE_STUB_WINDOWS=True``) and only cover a subset of drivers. Treat
DRIVER_IDLE vs UNIT_CAPACITY splits as indicative, not authoritative, until real shifts land.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Dict, List, Optional

from dispatching import feasibility_guards as fg
from dispatching.scheduler import (
    DriverDaySchedule,
    build_driver_schedules,
    check_feasibility,
    estimate_job_end_time,
    get_compatible_vehicle_types,
    load_all_driver_vtypes,
    preload_timing_cache,
)

ZERO = Decimal("0.00")

# Tunables -------------------------------------------------------------------
# Include driver_additional (night bonus / wait time) in the cost comparison?
# Off by default: it is situational and not part of the route "rate".
INCLUDE_ADDITIONAL_PAY = False
# Per-leg swap recovery. OFF by default ON PURPOSE: find_swaps answers "could THIS leg be
# squeezed in, holding everything else fixed?" — and on a busy day it says yes for almost
# every farmed leg *individually*, because they all compete for the SAME finite slack. Summing
# those into "preventable" double-counts (the marginal-vs-total fallacy the design doc warns
# about). The honest per-leg binding constraint comes from direct feasibility + failure reasons;
# the TRUE absorbable count comes from the Phase C +1-vehicle simulation. When enabled, a
# swap-only-recoverable leg is reported as SCHEDULING_PROCESS_LEAK — an UPPER BOUND, not a promise.
USE_SWAP_RECOVERY = False
_SWAP_MAX_DEPTH = 3
_SWAP_TIME_LIMIT_MS = 1500
_SWAP_MAX_ITER = 2000


# ── Fulfillment ─────────────────────────────────────────────────────────────
IN_HOUSE = "in_house"
FARM_OUT = "farm_out"
UNASSIGNED = "unassigned"


def fulfillment_of(leg) -> str:
    """Derive fulfillment from the assigned driver (mirrors get_coverage_stats)."""
    if not leg.driver_id:
        return UNASSIGNED
    # ``driver`` is select_related in our querysets; .driver_type is the discriminator.
    return IN_HOUSE if leg.driver.driver_type == "inhouse" else FARM_OUT


# ── Reason codes ────────────────────────────────────────────────────────────
VEHICLE_TYPE_SHORTAGE = "vehicle_type_shortage"
UNIT_CAPACITY_SHORTAGE = "unit_capacity_shortage"
DRIVER_IDLE_OR_OFF_SHIFT = "driver_idle_or_off_shift"
POSITIONING_ISSUE = "positioning_issue"
FLIGHT_DELAY_LEAK = "flight_delay_leak"
SCHEDULING_PROCESS_LEAK = "scheduling_process_leak"
DISPATCH_LEAK = "dispatch_leak"
SMART_FARM_OUT = "smart_farm_out"
UNKNOWN = "unknown"

# Higher-level family for KPI rollups (capacity vs driver vs process vs strategic).
REASON_FAMILY = {
    VEHICLE_TYPE_SHORTAGE: "capacity",
    UNIT_CAPACITY_SHORTAGE: "capacity",
    DRIVER_IDLE_OR_OFF_SHIFT: "driver",
    POSITIONING_ISSUE: "process",
    FLIGHT_DELAY_LEAK: "process",
    SCHEDULING_PROCESS_LEAK: "process",
    DISPATCH_LEAK: "process",
    SMART_FARM_OUT: "strategic",
    UNKNOWN: "unknown",
}

REASON_LABEL = {
    VEHICLE_TYPE_SHORTAGE: "Vehicle type shortage",
    UNIT_CAPACITY_SHORTAGE: "Unit capacity shortage (all units busy)",
    DRIVER_IDLE_OR_OFF_SHIFT: "Driver idle / off-shift",
    POSITIONING_ISSUE: "Positioning / reachability",
    FLIGHT_DELAY_LEAK: "Flight-delay leak",
    SCHEDULING_PROCESS_LEAK: "Scheduling/process leak (swap-recoverable)",
    DISPATCH_LEAK: "Dispatch leak (driver was free)",
    SMART_FARM_OUT: "Smart / strategic farm-out",
    UNKNOWN: "Unknown",
}

REASON_REMEDY = {
    VEHICLE_TYPE_SHORTAGE: "No in-house vehicle of this type was deployed — candidate for adding that vehicle type (validate buy math).",
    UNIT_CAPACITY_SHORTAGE: "Every car+driver unit was busy — add a unit (buy or hire) only if repeatable.",
    DRIVER_IDLE_OR_OFF_SHIFT: "A vehicle existed but no driver was on shift — driver coverage / scheduling fix.",
    POSITIONING_ISSUE: "A free driver couldn't reach the pickup in time — chaining / positioning / earlier planning.",
    FLIGHT_DELAY_LEAK: "An upstream flight delay broke the turn — earlier monitoring / reassignment / buffers.",
    SCHEDULING_PROCESS_LEAK: "A swap/reassignment could have covered it in-house — better dispatch logic/training.",
    DISPATCH_LEAK: "A feasible in-house driver was free — preventable process leak.",
    SMART_FARM_OUT: "Likely a correct farm-out that protected better work.",
    UNKNOWN: "Insufficient data to classify (missing vehicle type / route / window).",
}

# Capacity (needs buy/hire) and strategic farm-outs are NOT preventable by dispatch alone.
_NON_PREVENTABLE = {VEHICLE_TYPE_SHORTAGE, UNIT_CAPACITY_SHORTAGE, SMART_FARM_OUT, UNKNOWN}


def is_preventable(reason: str) -> bool:
    """A farm-out is process-preventable if dispatch/scheduling could have avoided it."""
    return reason not in _NON_PREVENTABLE


# ── Founder-facing ACTION buckets — each binding constraint maps to a decision ──
ACT_PREVENTABLE = "preventable"  # a free in-house driver could have done it, but it was farmed
ACT_HIRE = "hire"                # nobody was available -> hire / schedule more drivers
ACT_DELAY = "delay"              # a flight delay broke the turn -> overnight / standby coverage
ACT_BUY = "buy"                  # no in-house vehicle of that type -> consider buying
ACT_POSITION = "position"        # a free driver couldn't reach in time -> positioning / chaining
ACT_REVIEW = "review"            # uncertain / needs a human look

REASON_ACTION = {
    DISPATCH_LEAK: ACT_PREVENTABLE,
    SCHEDULING_PROCESS_LEAK: ACT_PREVENTABLE,
    DRIVER_IDLE_OR_OFF_SHIFT: ACT_HIRE,
    UNIT_CAPACITY_SHORTAGE: ACT_HIRE,
    FLIGHT_DELAY_LEAK: ACT_DELAY,
    VEHICLE_TYPE_SHORTAGE: ACT_BUY,
    POSITIONING_ISSUE: ACT_POSITION,
    SMART_FARM_OUT: ACT_REVIEW,
    UNKNOWN: ACT_REVIEW,
}

ACTION_LABEL = {
    ACT_PREVENTABLE: "Preventable — a driver was free",
    ACT_HIRE: "Hire / schedule — nobody was available",
    ACT_DELAY: "Delay — a flight broke the turn",
    ACT_BUY: "Buy — no in-house vehicle of this type",
    ACT_POSITION: "Positioning — driver free but couldn't reach in time",
    ACT_REVIEW: "Review",
}

# Display order: the dispatcher-accountability bucket first.
ACTION_ORDER = [ACT_PREVENTABLE, ACT_HIRE, ACT_DELAY, ACT_POSITION, ACT_BUY, ACT_REVIEW]


# ═══════════════════════════════════════════════════════════════════════════
# ECONOMICS  (driver-pay-only, v1 — gratuity/additional excluded by default)
# ═══════════════════════════════════════════════════════════════════════════
def leg_revenue(leg) -> Decimal:
    """This leg's share of the reservation price (round-trip split already solved)."""
    rev = leg.revenue_share
    if rev is None:
        try:
            rev = leg.calculate_revenue_share()
        except Exception:
            rev = ZERO
    return rev or ZERO


def _base_plus_optional_additional(base, additional) -> Optional[Decimal]:
    if base is None:
        return None
    total = base
    if INCLUDE_ADDITIONAL_PAY and additional is not None:
        total = total + additional
    return total


def affiliate_base_cost(leg) -> Optional[Decimal]:
    """What we actually pay the affiliate for this leg (base rate; gratuity excluded).

    Returns None if the affiliate rate was never captured (no DriverPayRate match → manual
    entry left blank). None means 'uncomputable', NOT zero.
    """
    return _base_plus_optional_additional(leg.driver_base_pay, leg.driver_additional)


def inhouse_counterfactual_cost(leg) -> Optional[Decimal]:
    """What a generic in-house driver WOULD have been paid for this leg's route.

    Uses ``route.inhouse_base_pay`` (the route default for in-house drivers). Driver-specific
    overrides are intentionally ignored — this is a generic counterfactual. Returns None when
    the leg has no matched route or the route has no in-house base pay configured.
    """
    route = leg.route if leg.route_id else None
    if route is None:
        return None
    return route.inhouse_base_pay  # may be None


def recovered_margin(leg) -> dict:
    """Recovered margin for a farmed leg = affiliate base − in-house counterfactual.

    Returns a dict with ``available`` (both sides known), ``margin``, ``positive`` and
    ``negative`` splits, and the two component costs. Positive => we'd likely have made more
    in-house; negative => the affiliate was cheaper (farm-out validated).
    """
    aff = affiliate_base_cost(leg)
    inh = inhouse_counterfactual_cost(leg)
    if aff is None or inh is None:
        return {
            "available": False,
            "margin": None,
            "positive": ZERO,
            "negative": ZERO,
            "affiliate_cost": aff,
            "inhouse_cost": inh,
        }
    m = (aff - inh)
    return {
        "available": True,
        "margin": m,
        "positive": m if m > 0 else ZERO,
        "negative": m if m < 0 else ZERO,
        "affiliate_cost": aff,
        "inhouse_cost": inh,
    }


# ═══════════════════════════════════════════════════════════════════════════
# CLASSIFICATION  (replays the day's in-house board, read-only)
# ═══════════════════════════════════════════════════════════════════════════
@dataclass
class DayContext:
    """Pre-built per-date scheduling context (board + vehicle types) for classification."""

    day: date
    board: Dict[int, DriverDaySchedule]
    dvtypes: Dict[int, str]
    legs_by_id: Dict[int, object]
    inhouse_drivers: list


def build_day_context(day: date, day_legs, inhouse_drivers) -> DayContext:
    """Build the in-house board for one date. ``build_driver_schedules`` only places legs
    assigned to the given (in-house) drivers, so affiliate/unassigned legs are excluded."""
    board = build_driver_schedules(day_legs, inhouse_drivers, day)
    dvtypes = load_all_driver_vtypes(day)
    legs_by_id = {l.id: l for l in day_legs}
    return DayContext(day, board, dvtypes, legs_by_id, list(inhouse_drivers))


def _reason_category(reason: str) -> str:
    r = reason or ""
    if "Outside driver window" in r:
        return "window"
    if "Needs" in r or "Conflicts with next job" in r:
        return "turnaround"
    return "other"


def _idle_at(sched: DriverDaySchedule, leg, day: date) -> bool:
    """True if the driver has NO job overlapping this leg's time window (free at that moment)."""
    try:
        leg_start = datetime.combine(day, leg.pickup_time)
        leg_end = estimate_job_end_time(leg, day)
    except Exception:
        return False
    for s in sched.slots:
        s_start = datetime.combine(day, s.pickup_time)
        s_end = s.estimated_end_time
        if s_start < leg_end and leg_start < s_end:  # intervals overlap
            return False
    return True


def _is_flight_delay_driven(leg) -> bool:
    """The conflict is plausibly flight-delay-driven if this is an arrival whose flight time
    no longer matches the booked pickup."""
    try:
        if leg.get_trip_type() != "arrival":
            return False
        return leg.has_flight_time_mismatch(threshold_minutes=30)
    except Exception:
        return False


def _swap_can_recover(leg, ctx: DayContext, use_swaps: bool) -> bool:
    if not use_swaps:
        return False
    try:
        from dispatching.swap_optimizer import find_swaps

        result = find_swaps(
            leg, ctx.board, ctx.legs_by_id, ctx.dvtypes, ctx.day,
            max_depth=_SWAP_MAX_DEPTH, time_limit_ms=_SWAP_TIME_LIMIT_MS,
            max_iterations=_SWAP_MAX_ITER,
        )
        return bool(result.solutions)
    except Exception:
        return False


def classify_farmout(leg, ctx: DayContext, *, use_swaps: bool = USE_SWAP_RECOVERY,
                     detail: bool = False) -> dict:
    """Classify a single farmed-out leg's binding constraint by replaying the day's board.

    Returns a dict: ``reason``, ``family``, ``action``, ``preventable``, ``smart_farm_out``,
    ``confidence``, plus the candidate/feasibility tallies and ``feasible_drivers`` (the in-house
    drivers who COULD have taken it — the proof behind a "preventable" verdict). With ``detail=True``
    it also returns ``trace`` (every candidate driver's verdict) and does not stop at the first
    feasible driver.

    ``use_swaps`` enables the bounded swap search (SCHEDULING_PROCESS_LEAK). See the note on
    ``USE_SWAP_RECOVERY`` — it is an UPPER BOUND on dispatch-recoverable legs, not additive.
    """
    required_type = leg.effective_vehicle_type

    # Candidate = in-house driver with a tier-compatible vehicle deployed that day.
    candidates = []
    for drv in ctx.inhouse_drivers:
        dvtype = ctx.dvtypes.get(drv.id)
        if dvtype is None:
            continue  # no FleetVehicle assigned that day → not a deployable unit
        if required_type and required_type not in get_compatible_vehicle_types(dvtype):
            continue
        candidates.append(drv)

    base = {
        "reason": UNKNOWN,
        "family": "unknown",
        "preventable": False,
        "smart_farm_out": False,  # intent not captured in v1 — always False (see design doc §6)
        "confidence": "low" if fg.USE_STUB_WINDOWS else "medium",
        "candidates": len(candidates),
        "feasible": 0,
        "window_fail": 0,
        "turnaround_fail": 0,
    }

    if not candidates:
        result = _finish(base, VEHICLE_TYPE_SHORTAGE)
        result["feasible_drivers"] = []
        if detail:
            result["trace"] = []
        return result

    feasible_drivers = []
    trace = []
    window_fail = turn_fail = idle_turn_fail = 0
    for drv in candidates:
        sched = ctx.board.get(drv.id) or DriverDaySchedule(drv.id, str(drv), "inhouse")
        # enforce_cap=False: retrospective farm-out analytics must keep their shipped,
        # pre-Span-Governor capacity semantics — the new duty-span cap would silently
        # shift the absorbable counts.
        window = fg.get_effective_window(drv.id, configured=None, enforce_cap=False)
        fr = check_feasibility(sched, leg, ctx.day, driver_window=window)
        rec = {
            "driver_id": drv.id, "driver": str(drv),
            "vehicle_type": ctx.dvtypes.get(drv.id),
            "feasible": fr.feasible, "buffer_minutes": fr.buffer_minutes,
            "reason": fr.reason, "category": _reason_category(fr.reason),
            "n_jobs": len(sched.slots),
        }
        if detail:
            trace.append(rec)
        if fr.feasible:
            feasible_drivers.append(rec)
            if not detail:
                break
        else:
            cat = rec["category"]
            if cat == "window":
                window_fail += 1
            elif cat == "turnaround":
                turn_fail += 1
                if _idle_at(sched, leg, ctx.day):
                    idle_turn_fail += 1

    feasible_any = bool(feasible_drivers)
    base["feasible"] = len(feasible_drivers)
    base["window_fail"] = window_fail
    base["turnaround_fail"] = turn_fail

    if feasible_any:
        reason = DISPATCH_LEAK
    elif _swap_can_recover(leg, ctx, use_swaps):
        reason = SCHEDULING_PROCESS_LEAK
    elif turn_fail and idle_turn_fail:
        reason = POSITIONING_ISSUE
    elif turn_fail:
        reason = UNIT_CAPACITY_SHORTAGE
    elif window_fail:
        reason = DRIVER_IDLE_OR_OFF_SHIFT
    else:
        reason = UNKNOWN

    # Flight-delay overlay: a timing/positioning leak driven by a delayed arrival flight.
    if reason in (POSITIONING_ISSUE, UNIT_CAPACITY_SHORTAGE, DISPATCH_LEAK) and _is_flight_delay_driven(leg):
        reason = FLIGHT_DELAY_LEAK

    result = _finish(base, reason)
    result["feasible_drivers"] = feasible_drivers
    if detail:
        result["trace"] = trace
    return result


def _finish(base: dict, reason: str) -> dict:
    base["reason"] = reason
    base["family"] = REASON_FAMILY.get(reason, "unknown")
    base["action"] = REASON_ACTION.get(reason, ACT_REVIEW)
    base["preventable"] = is_preventable(reason)
    base["detail"] = REASON_REMEDY.get(reason, "")
    return base


# ═══════════════════════════════════════════════════════════════════════════
# QUERY + AGGREGATION
# ═══════════════════════════════════════════════════════════════════════════
def legs_for_range(start: date, end: date, *, performed_only: bool = True,
                   exclude_bad_data: bool = True):
    """Return a leg queryset for [start, end] by ``pickup_date`` with relations prefetched.

    ``performed_only`` excludes cancelled legs (rides actually performed).
    ``exclude_bad_data`` excludes legs flagged ``exclude_from_analytics``.
    """
    from reservations.models import Leg

    qs = (
        Leg.objects.filter(pickup_date__gte=start, pickup_date__lte=end)
        .select_related(
            "driver", "driver_assigned_by", "reservation", "reservation__customer",
            "reservation__vehicle", "vehicle", "route", "route__origin",
            "route__destination", "flight_information",
        )
        .prefetch_related("legstop_set", "legflight_set", "status_history")
    )
    if performed_only:
        qs = qs.exclude(status="cancelled")
    if exclude_bad_data:
        qs = qs.exclude(exclude_from_analytics=True)
    return qs


def _zone(location_text: str) -> str:
    from dispatching.analytics import categorize_location

    return categorize_location(location_text or "") or "Other"


def _acc(d: dict, key, *, margin=None, available=False, spend=None, inhouse=None):
    """Accumulate per-group count, what-we-paid (spend), in-house counterfactual, and the
    recovered-margin splits into d[key]. ``spend``/``inhouse`` accrue whenever known (even if the
    margin itself is uncomputable), so 'what we paid' is always complete."""
    slot = d.setdefault(key, {"count": 0, "net": ZERO, "positive": ZERO, "negative": ZERO,
                              "available": 0, "spend": ZERO, "inhouse": ZERO})
    slot["count"] += 1
    if spend is not None:
        slot["spend"] += spend
    if inhouse is not None:
        slot["inhouse"] += inhouse
    if available and margin is not None:
        slot["available"] += 1
        slot["net"] += margin
        slot["positive"] += margin if margin > 0 else ZERO
        slot["negative"] += margin if margin < 0 else ZERO


def fleet_size_by_type() -> Dict[str, int]:
    """Count physical fleet vehicles grouped by vehicle type (FleetVehicle has no is_active)."""
    from drivers.models import FleetVehicle

    out: Dict[str, int] = defaultdict(int)
    for fv in FleetVehicle.objects.filter(is_active=True).select_related("vehicle_type"):
        vt = fv.vehicle_type.vehicle_type if fv.vehicle_type else "unknown"
        out[vt] += 1
    return dict(out)


def summarize_range(start: date, end: date, *, classify: bool = True,
                    use_swaps: bool = USE_SWAP_RECOVERY) -> dict:
    """Compute the full Fleet Capacity Intelligence KPI bundle for [start, end].

    Read-only. Groups legs by service date; builds each day's in-house board ONCE; classifies
    every farmed-out leg via the engine. Returns nested dicts ready for a service/dashboard.

    ``use_swaps`` enables the swap-recovery upper bound (see ``USE_SWAP_RECOVERY``).
    """
    from drivers.models import Driver

    legs = list(legs_for_range(start, end))
    inhouse_drivers = list(Driver.objects.filter(driver_type="inhouse", is_active=True))

    op = {"total": 0, "in_house": 0, "farm_out": 0, "unassigned": 0}
    fin = {"revenue_total": ZERO, "in_house_revenue": ZERO, "farm_out_revenue": ZERO,
           "affiliate_cost_total": ZERO, "inhouse_counterfactual_total": ZERO,
           "recovered_net": ZERO, "recovered_positive": ZERO, "recovered_negative": ZERO,
           "farm_legs": 0, "counterfactual_available": 0}
    by_vehicle: dict = {}
    by_reason: dict = {}
    by_family: dict = {}
    by_zone_pickup: dict = {}
    by_zone_dropoff: dict = {}
    by_dow: dict = {}
    by_hour: dict = {}
    by_affiliate: dict = {}

    if classify:
        preload_timing_cache()

    legs_by_date: Dict[date, list] = defaultdict(list)
    for leg in legs:
        legs_by_date[leg.pickup_date].append(leg)

    for day, day_legs in legs_by_date.items():
        ctx = build_day_context(day, day_legs, inhouse_drivers) if classify else None
        for leg in day_legs:
            op["total"] += 1
            ff = fulfillment_of(leg)
            rev = leg_revenue(leg)
            fin["revenue_total"] += rev

            if ff == IN_HOUSE:
                op["in_house"] += 1
                fin["in_house_revenue"] += rev
                continue
            if ff == UNASSIGNED:
                op["unassigned"] += 1
                continue

            # ── Farm-out leg ──
            op["farm_out"] += 1
            fin["farm_out_revenue"] += rev
            fin["farm_legs"] += 1

            rm = recovered_margin(leg)
            aff_cost = rm["affiliate_cost"]
            inh_cost = rm["inhouse_cost"]
            if aff_cost is not None:
                fin["affiliate_cost_total"] += aff_cost
            if inh_cost is not None:
                fin["inhouse_counterfactual_total"] += inh_cost
            margin = rm["margin"]
            available = rm["available"]
            if available:
                fin["counterfactual_available"] += 1
                fin["recovered_net"] += margin
                fin["recovered_positive"] += rm["positive"]
                fin["recovered_negative"] += rm["negative"]

            cls = (classify_farmout(leg, ctx, use_swaps=use_swaps) if classify
                   else {"reason": UNKNOWN, "family": "unknown"})

            vt = leg.effective_vehicle_type or "unknown"
            kw = dict(margin=margin, available=available, spend=aff_cost, inhouse=inh_cost)
            _acc(by_vehicle, vt, **kw)
            _acc(by_reason, cls["reason"], **kw)
            _acc(by_family, cls["family"], **kw)
            _acc(by_zone_pickup, _zone(leg.pickup_location), **kw)
            _acc(by_zone_dropoff, _zone(leg.dropoff_location), **kw)
            _acc(by_dow, leg.pickup_date.strftime("%A"), **kw)
            _acc(by_hour, leg.pickup_time.hour if leg.pickup_time else -1, **kw)
            _acc(by_affiliate, str(leg.driver), **kw)

    total = op["total"] or 1
    farm_legs = fin["farm_legs"] or 1
    op["farm_out_rate"] = round(op["farm_out"] / total * 100, 1)
    op["in_house_rate"] = round(op["in_house"] / total * 100, 1)
    fin["counterfactual_coverage_pct"] = round(fin["counterfactual_available"] / farm_legs * 100, 1)

    return {
        "range": {"start": start, "end": end,
                  "days": (end - start).days + 1},
        "operational": op,
        "financial": fin,
        "by_vehicle_type": by_vehicle,
        "by_reason": by_reason,
        "by_family": by_family,
        "by_zone_pickup": by_zone_pickup,
        "by_zone_dropoff": by_zone_dropoff,
        "by_day_of_week": by_dow,
        "by_hour": by_hour,
        "by_affiliate": by_affiliate,
        "fleet_size_by_type": fleet_size_by_type(),
        "reason_labels": REASON_LABEL,
        "reason_remedies": REASON_REMEDY,
    }


def _assigned_by_name(leg) -> Optional[str]:
    """Who assigned the affiliate (the dispatcher who farmed it), if recorded."""
    try:
        u = leg.driver_assigned_by
        if u is None:
            return None
        return u.get_full_name() or u.get_username()
    except Exception:
        return None


def collect_leaks(start: date, end: date, *, use_swaps: bool = USE_SWAP_RECOVERY,
                  actions=None) -> dict:
    """Per-leg LEAK report for the drill-down: every farmed leg with the evidence behind its
    classification, grouped into founder-facing ACTION buckets (preventable / hire / delay / buy /
    positioning). Read-only. ``actions`` optionally filters to a set of action keys.

    Each item carries: economics (paid / in-house cost / margin), the computed reason + action,
    WHO farmed it (``assigned_by``), the in-house drivers who could have taken it
    (``feasible_drivers``), and the full per-driver ``trace``.
    """
    from drivers.models import Driver

    legs = list(legs_for_range(start, end))
    inhouse_drivers = list(Driver.objects.filter(driver_type="inhouse", is_active=True))
    preload_timing_cache()

    legs_by_date: Dict[date, list] = defaultdict(list)
    for leg in legs:
        legs_by_date[leg.pickup_date].append(leg)

    items = []
    buckets: dict = {}
    for day, day_legs in legs_by_date.items():
        ctx = build_day_context(day, day_legs, inhouse_drivers)
        for leg in day_legs:
            if fulfillment_of(leg) != FARM_OUT:
                continue
            rm = recovered_margin(leg)
            cls = classify_farmout(leg, ctx, use_swaps=use_swaps, detail=True)
            action = cls["action"]
            if actions and action not in actions:
                continue
            customer = ""
            if leg.reservation_id and leg.reservation.customer:
                customer = leg.reservation.customer.get_full_name()
            items.append({
                "leg_id": leg.id,
                "date": leg.pickup_date,
                "time": leg.pickup_time,
                "customer": customer,
                "pickup": leg.pickup_location,
                "dropoff": leg.dropoff_location,
                "pickup_zone": _zone(leg.pickup_location),
                "vehicle_type": leg.effective_vehicle_type or "unknown",
                "affiliate": str(leg.driver),
                "assigned_by": _assigned_by_name(leg),
                "paid": rm["affiliate_cost"],
                "inhouse_cost": rm["inhouse_cost"],
                "margin": rm["margin"],
                "available": rm["available"],
                "reason": cls["reason"],
                "reason_label": REASON_LABEL.get(cls["reason"], cls["reason"]),
                "family": cls["family"],
                "action": action,
                "action_label": ACTION_LABEL.get(action, action),
                "confidence": cls["confidence"],
                "candidates": cls["candidates"],
                "feasible_drivers": cls["feasible_drivers"],
                "trace": cls.get("trace", []),
            })
            b = buckets.setdefault(action, {"count": 0, "spend": ZERO, "net": ZERO})
            b["count"] += 1
            if rm["affiliate_cost"] is not None:
                b["spend"] += rm["affiliate_cost"]
            if rm["available"]:
                b["net"] += rm["margin"]

    order = {a: i for i, a in enumerate(ACTION_ORDER)}
    items.sort(key=lambda it: (order.get(it["action"], 99),
                               -(it["margin"] if it["margin"] is not None else ZERO)))
    return {
        "items": items,
        "buckets": buckets,
        "action_order": ACTION_ORDER,
        "action_labels": ACTION_LABEL,
        "range": {"start": start, "end": end, "days": (end - start).days + 1},
    }
