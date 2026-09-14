"""
Demand-aware fleet capacity: which days are slow, which are busy, and whether
taking a unit off the road on a given day leaves dispatch short.

This is the module that lets fleet and dispatch plan from the same facts. It
reads only what the system already holds — booked legs, the active fleet, the
downtime ledger, and the vehicle-assignment history — and answers three
questions the fleet manager used to answer by phoning dispatch:

  * How busy is each of the next N days, and at what moment?
  * If #007 is on a lift Tuesday and Wednesday, is anyone left short?
  * Where is the best gap to put a two-day job?

Two measures, deliberately, because each is wrong on its own:

  BOOKED PEAK — the founder's roster-sizing rule ("13 legs in flight at 09:30
  means 13 cars, never legs-per-driver"): the most legs in flight at once,
  per vehicle tier, from ``day_setup.peak_concurrency``. Exact for what is
  booked, but bookings keep arriving right up to the day, so three weeks out
  it is a floor, not a forecast.

  TYPICAL USE — how many distinct units the board actually ran on that weekday
  over the last eight weeks (DriverVehicleAssignment history). Blind to what is
  booked, but it knows a Saturday needs sixteen cars before Saturday's
  bookings do.

A day's verdict is the worse of the two. "Clear" means both leave a car spare;
"tight" means one of them lands exactly on the fleet; "conflict" means one of
them needs more cars than would be left. It is advice for a person choosing a
shop day — it never blocks a downtime from being saved, because the founder's
standing rule is that humans decide and the system informs (see
``VehicleDowntime`` and the Guard A history in docs/fleet-management.md).

Tier arithmetic follows the scheduler's nested compatibility (a Sprinter can
run an SUV job, an SUV cannot run a Sprinter job): for every tier t, the legs
needing tier >= t must fit in the units of tier >= t. Nested sets make that
check both necessary and sufficient at any instant.

Vocabulary: a unit's "tier" is ``rates.Vehicle.vehicle_type`` read through
``scheduler.VEHICLE_TIER_ORDER``; an untyped unit counts toward the overall
headcount only.
"""
from __future__ import annotations

import logging
from collections import Counter, defaultdict
from datetime import timedelta
from statistics import median

from django.core.cache import cache
from django.utils import timezone

from business.datefmt import strf
from dispatching.scheduler import VEHICLE_TIER_ORDER, get_vehicle_tier

logger = logging.getLogger(__name__)

# Booked legs change slowly at the horizon this is read at; the outlook is
# recomputed at most this often. Supply (the downtime ledger) is never cached —
# the page that edits it must see its own edit.
OUTLOOK_CACHE_SECONDS = 15 * 60
TYPICAL_USE_CACHE_SECONDS = 60 * 60
TYPICAL_USE_WEEKS = 8
# A weekday needs at least this many observed days before its typical use is
# trusted; one odd Sunday must not set the bar for every Sunday.
TYPICAL_USE_MIN_DAYS = 3

# Busyness bands: booked peak as a share of the units available that day.
QUIET_UTILISATION = 0.60
BUSY_UTILISATION = 0.85

DEFAULT_OUTLOOK_DAYS = 28
# An open-ended downtime ("no return date") is checked over this many days so
# the verdict still says something.
OPEN_ENDED_CHECK_DAYS = 7

LEVEL_RANK = {"clear": 0, "tight": 1, "conflict": 2}

# Human names for the rates.Vehicle keys, in the words the office uses.
TYPE_LABELS = {
    "towncar": "Towncar",
    "mini_van": "Metris",
    "suv": "SUV",
    "van": "Van",
    "Van(14 Pax)": "Sprinter",
}


def type_label(vehicle_type):
    if not vehicle_type:
        return "Untyped"
    return TYPE_LABELS.get(vehicle_type, str(vehicle_type).title())


def unit_type(unit):
    """The rates.Vehicle key for a FleetVehicle, or None when untyped."""
    vt = getattr(unit, "vehicle_type", None)
    return vt.vehicle_type if vt is not None else None


def unit_tier(unit):
    return get_vehicle_tier(unit_type(unit))


# ════════════════════════════════════════════════════════════════════════════
# Demand — what is booked
# ════════════════════════════════════════════════════════════════════════════

def load_legs_by_day(start, days):
    """Every live leg from ``start`` for ``days`` days, grouped by pickup date.

    Same exclusions as Day Setup's demand query: both spellings of a cancelled
    reservation, and cancelled legs. Unpaid legs count — staffing for a
    maybe-paid leg errs safe.
    """
    from reservations.models import Leg

    end = start + timedelta(days=days - 1)
    legs = (
        Leg.objects.filter(pickup_date__gte=start, pickup_date__lte=end)
        .exclude(reservation__status__in=("cancelled", "canceled"))
        .exclude(status="cancelled")
        .select_related("reservation__vehicle", "vehicle", "reservation",
                        "flight_information")
        .prefetch_related("legflight_set__flight")
    )
    by_day = defaultdict(list)
    for leg in legs:
        by_day[leg.pickup_date].append(leg)
    return by_day


def day_demand(day, legs):
    """Booked demand for one day: totals per type and the in-flight peaks.

    ``cumulative`` is keyed by the types present in the legs and holds the
    peak of in-flight legs whose tier is >= that type's tier — the coverage
    measure under nested compatibility (see ``day_setup.peak_concurrency``).
    """
    from dispatching.day_setup import peak_concurrency

    if not legs:
        return {
            "date": day, "total": 0, "per_type": {}, "peak": 0, "peak_at": "",
            "cumulative": {}, "cumulative_at": {},
        }
    pc = peak_concurrency(day, legs)
    per_type = Counter(
        str(leg.effective_vehicle_type) for leg in legs if leg.effective_vehicle_type
    )
    overall_n, overall_at = pc["overall"]
    return {
        "date": day,
        "total": pc["total_legs"],
        "per_type": dict(per_type),
        "peak": overall_n,
        "peak_at": _clock(overall_at),
        "cumulative": {t: n for t, (n, _at) in pc["cumulative"].items()},
        "cumulative_at": {t: _clock(at) for t, (_n, at) in pc["cumulative"].items()},
    }


def demand_outlook(start, days=DEFAULT_OUTLOOK_DAYS, *, use_cache=True):
    """``[day_demand, ...]`` for ``days`` days from ``start``. Cached briefly."""
    key = f"fleet_demand_outlook:{start.isoformat()}:{days}"
    if use_cache:
        cached = cache.get(key)
        if cached is not None:
            return cached

    import dispatching.scheduler as sch

    preloaded_here = sch._timing_cache is None
    by_day = load_legs_by_day(start, days)
    out = []
    for offset in range(days):
        day = start + timedelta(days=offset)
        out.append(day_demand(day, by_day.get(day, [])))
    if preloaded_here:
        # Same courtesy the capacity planner extends: don't leave the whole
        # RouteTimingMetric table sitting in a module global after a page load.
        sch.clear_timing_cache()

    if use_cache:
        cache.set(key, out, OUTLOOK_CACHE_SECONDS)
    return out


def invalidate_outlook_cache():
    """Nothing to do — the cache key carries the start date and expires on its
    own. Kept as the one place to change if a keyed invalidation is ever added."""
    return None


# ════════════════════════════════════════════════════════════════════════════
# Supply — what is on the road
# ════════════════════════════════════════════════════════════════════════════

def fleet_units():
    """Active units with their open downtimes prefetched — one query for the
    pool, one for the ledger, however many days are then asked about."""
    from drivers.models import FleetVehicle

    return list(
        FleetVehicle.objects.filter(is_active=True)
        .select_related("vehicle_type")
        .with_open_downtimes()
        .order_by("vehicle_number")
    )


def supply_on(day, units, extra_down_ids=()):
    """Split ``units`` into those available on ``day`` and those down.

    ``extra_down_ids`` lets a caller ask "and what if THIS one were down too"
    without writing anything — the whole point of the demand check.
    """
    available, down = [], []
    for unit in units:
        downtime = unit.downtime_on(day)
        if downtime is not None:
            down.append({"unit": unit, "downtime": downtime, "hypothetical": False})
        elif unit.id in extra_down_ids:
            down.append({"unit": unit, "downtime": None, "hypothetical": True})
        else:
            available.append(unit)
    return {"available": available, "down": down}


def have_at_or_above(available):
    """Units of tier >= t, for every tier t. Untyped units count for nothing
    here — they are still bodies in ``len(available)``."""
    counts = {}
    for vtype in VEHICLE_TIER_ORDER:
        k = get_vehicle_tier(vtype)
        counts[vtype] = sum(1 for unit in available if unit_tier(unit) >= k)
    return counts


def need_at_or_above(demand):
    """Peak in-flight legs of tier >= t, for every tier t.

    ``demand["cumulative"]`` is keyed by the types actually booked that day.
    For a tier with nothing booked, the need is the cumulative of the next
    booked tier above it (the same legs, nothing in between), so the max over
    booked tiers >= t is exactly right.
    """
    cumulative = demand.get("cumulative") or {}
    out = {}
    for vtype in VEHICLE_TIER_ORDER:
        k = get_vehicle_tier(vtype)
        candidates = [n for booked, n in cumulative.items() if get_vehicle_tier(booked) >= k]
        out[vtype] = max(candidates) if candidates else 0
    return out


def typical_units_by_weekday(today, *, weeks=TYPICAL_USE_WEEKS, use_cache=True):
    """Median distinct units the board ran, per weekday, over the last ``weeks``.

    Read from DriverVehicleAssignment — the only job-to-physical-car link — so
    it is what dispatch actually did, not what it planned. Weekdays with fewer
    than ``TYPICAL_USE_MIN_DAYS`` observations are omitted rather than guessed.
    """
    key = f"fleet_typical_units:{today.isoformat()}:{weeks}"
    if use_cache:
        cached = cache.get(key)
        if cached is not None:
            return cached

    from django.db.models import Count
    from drivers.models import DriverVehicleAssignment

    start = today - timedelta(days=7 * weeks)
    rows = (
        DriverVehicleAssignment.objects
        .filter(date__gte=start, date__lt=today, vehicle__isnull=False)
        .values("date")
        .annotate(n=Count("vehicle_id", distinct=True))
    )
    by_weekday = defaultdict(list)
    for row in rows:
        by_weekday[row["date"].weekday()].append(row["n"])
    out = {
        weekday: int(round(median(values)))
        for weekday, values in by_weekday.items()
        if len(values) >= TYPICAL_USE_MIN_DAYS
    }
    if use_cache:
        cache.set(key, out, TYPICAL_USE_CACHE_SECONDS)
    return out


# ════════════════════════════════════════════════════════════════════════════
# The verdict
# ════════════════════════════════════════════════════════════════════════════

def judge_day(demand, supply, typical_units=None):
    """Pure: one day's demand + one day's supply -> a verdict a person can act on.

    Returns::

        {
          "date", "level": clear|tight|conflict, "busyness": quiet|normal|busy|over,
          "peak", "peak_at", "legs", "available", "down": [...],
          "typical_units", "utilisation",
          "rows": [{type, label, need, have, slack, at}],   # per tier
          "binding": row or None,                            # the tier that decided it
          "reasons": ["Sprinters: 5 needed at 9:30 AM, 4 left", ...],
        }
    """
    available = supply["available"]
    have = have_at_or_above(available)
    need = need_at_or_above(demand)
    total_available = len(available)

    rows, ranked_reasons = [], []
    level, binding = "clear", None
    tiers = list(VEHICLE_TIER_ORDER)
    for index, vtype in enumerate(tiers):
        n, h = need[vtype], have[vtype]
        if n == 0 and h == 0:
            continue
        slack = h - n
        row = {
            "type": vtype, "label": type_label(vtype), "need": n, "have": h,
            "slack": slack, "at": demand.get("cumulative_at", {}).get(vtype, ""),
        }
        rows.append(row)
        row_level = "conflict" if slack < 0 else ("tight" if slack == 0 and n else "clear")
        if LEVEL_RANK[row_level] > LEVEL_RANK[level]:
            level, binding = row_level, row
        # A reason is worth a sentence only where this tier itself adds
        # demand — "any car: 2 needed" when the two legs are Sprinter jobs
        # restates the Sprinter line in vaguer words.
        above = need[tiers[index + 1]] if index + 1 < len(tiers) else 0
        if row_level != "clear" and n and n > above:
            when = f" at {row['at']}" if row.get("at") else ""
            ranked_reasons.append(
                (LEVEL_RANK[row_level], f"{_tier_phrase(index, n)}: {n} needed{when}, {h} left"))
    ranked_reasons.sort(key=lambda r: -r[0])
    reasons = [text for _rank, text in ranked_reasons]

    # Whole-fleet check against booked peak, for the days when no tier is
    # short but the headcount is — untyped legs count here and nowhere else.
    peak = demand.get("peak", 0)
    if peak > total_available:
        level = "conflict"
        reasons.append(f"{peak} legs in flight at {demand.get('peak_at') or 'the peak'}, "
                       f"{total_available} cars left")
    elif peak == total_available and peak and LEVEL_RANK[level] < LEVEL_RANK["tight"]:
        level = "tight"
        reasons.append(f"{peak} legs in flight at {demand.get('peak_at') or 'the peak'}, "
                       f"exactly {total_available} cars left")

    # History check: what this weekday has actually needed lately.
    if typical_units is not None and total_available:
        if total_available < typical_units:
            level = "conflict"
            reasons.append(
                f"{_weekday_name(demand['date'])}s have run {typical_units} cars lately, "
                f"{total_available} would be left")
        elif total_available == typical_units and LEVEL_RANK[level] < LEVEL_RANK["tight"]:
            level = "tight"
            reasons.append(
                f"{_weekday_name(demand['date'])}s have run {typical_units} cars lately — "
                f"no spare")

    if total_available:
        utilisation = peak / total_available
    else:
        utilisation = 1.0 if peak else 0.0
    if utilisation > 1.0:
        busyness = "over"
    elif utilisation >= BUSY_UTILISATION:
        busyness = "busy"
    elif utilisation >= QUIET_UTILISATION:
        busyness = "normal"
    else:
        busyness = "quiet"

    per_type = demand.get("per_type", {}) or {}
    return {
        "date": demand["date"],
        "level": level,
        "busyness": busyness,
        "peak": peak,
        "peak_at": demand.get("peak_at", ""),
        "legs": demand.get("total", 0),
        "per_type": per_type,
        # In tier order, with the office's names: "34 Towncar · 20 SUV".
        "per_type_rows": [
            (type_label(t), per_type[t]) for t in VEHICLE_TIER_ORDER if per_type.get(t)
        ] + [(type_label(t), n) for t, n in per_type.items() if t not in VEHICLE_TIER_ORDER],
        "available": total_available,
        "down": supply["down"],
        "typical_units": typical_units,
        "utilisation": round(utilisation, 2),
        "rows": rows,
        "binding": binding,
        "reasons": reasons,
    }


def outlook(start, days, units, *, today=None, extra_down_ids=(), use_cache=True):
    """One judged row per day from ``start`` — the strip the desk and the
    outlook page draw, and the thing every window check is a slice of."""
    today = today or timezone.localdate()
    demands = demand_outlook(start, days, use_cache=use_cache)
    typical = typical_units_by_weekday(today, use_cache=use_cache)
    rows = []
    for demand in demands:
        day = demand["date"]
        supply = supply_on(day, units, extra_down_ids=extra_down_ids)
        rows.append(judge_day(demand, supply, typical.get(day.weekday())))
    return rows


def check_window(unit, starts_on, expected_back_on, units, *, today=None,
                 use_cache=True, ignore_downtime_id=None):
    """Would taking ``unit`` down from ``starts_on`` until ``expected_back_on``
    (exclusive; None = open-ended) leave dispatch short on any of those days?

    ``ignore_downtime_id`` excludes an existing row from the supply picture so
    editing a downtime is judged against everything EXCEPT its old self.

    Returns ``{"level", "days": [judged rows], "summary": str, "worst": row}``.
    """
    today = today or timezone.localdate()
    if expected_back_on is not None and expected_back_on > starts_on:
        days = (expected_back_on - starts_on).days
    else:
        days = OPEN_ENDED_CHECK_DAYS
    days = max(1, min(days, 60))

    if ignore_downtime_id is not None:
        units = _without_downtime(units, ignore_downtime_id)

    # Judged twice: once as the fleet stands, once with this unit gone. The
    # difference is the answer — a Saturday that is short with every car is not
    # this car's fault, and saying "short" about it teaches nothing.
    baseline = outlook(starts_on, days, units, today=today, use_cache=use_cache)
    rows = outlook(starts_on, days, units, today=today,
                   extra_down_ids={unit.id}, use_cache=use_cache)
    for base, row in zip(baseline, rows):
        row["baseline_level"] = base["level"]
        row["worsened"] = LEVEL_RANK[row["level"]] > LEVEL_RANK[base["level"]]
    worst = max(rows, key=lambda r: (LEVEL_RANK[r["level"]], r["utilisation"]))
    level = worst["level"]
    worsened = [r for r in rows if r["worsened"]]
    caused = max((LEVEL_RANK[r["level"]] for r in worsened), default=0)
    return {
        "level": level,
        # True when THIS unit turns some day short — the case worth a tick box.
        "caused": caused >= LEVEL_RANK["conflict"],
        "worsened_days": worsened,
        "days": rows,
        "worst": worst,
        "summary": _window_summary(level, rows, worst, expected_back_on is None, worsened),
    }


def suggest_windows(unit, length_days, units, *, today=None, horizon_days=DEFAULT_OUTLOOK_DAYS,
                    earliest=None, limit=3, use_cache=True):
    """The ``limit`` best places to put a ``length_days`` job for ``unit`` in
    the next ``horizon_days``: clear first, then the quietest, then the soonest.
    Never proposes a window that starts before ``earliest`` (default tomorrow)."""
    today = today or timezone.localdate()
    earliest = earliest or (today + timedelta(days=1))
    length_days = max(1, length_days)
    rows = outlook(earliest, horizon_days, units, today=today,
                   extra_down_ids={unit.id}, use_cache=use_cache)
    candidates = []
    for i in range(0, len(rows) - length_days + 1):
        window = rows[i:i + length_days]
        worst = max(LEVEL_RANK[r["level"]] for r in window)
        load = sum(r["utilisation"] for r in window) / length_days
        candidates.append({
            "starts_on": window[0]["date"],
            "expected_back_on": window[0]["date"] + timedelta(days=length_days),
            "level": [k for k, v in LEVEL_RANK.items() if v == worst][0],
            "utilisation": round(load, 2),
            "days": window,
        })
    candidates.sort(key=lambda c: (LEVEL_RANK[c["level"]], c["utilisation"], c["starts_on"]))
    return candidates[:limit]


# ════════════════════════════════════════════════════════════════════════════
# helpers
# ════════════════════════════════════════════════════════════════════════════

def _clock(dt):
    if dt is None:
        return ""
    return strf(dt, "%-I:%M %p")


def _weekday_name(day):
    return strf(day, "%A")


def _plural(vehicle_type, n):
    """'Sprinter' -> 'Sprinters', 'Metris' stays 'Metris', 'SUV' -> 'SUVs'."""
    label = type_label(vehicle_type)
    if n == 1 or label.endswith("s"):
        return label
    return label + "s"


def _tier_phrase(index, n):
    """How the office would say 'units of tier >= t': the bottom tier is any
    car at all, the top tier is just its name, the rest are 'X or bigger'."""
    vtype = VEHICLE_TIER_ORDER[index]
    if index == 0:
        return "Any car"
    if index == len(VEHICLE_TIER_ORDER) - 1:
        return _plural(vtype, n)
    return f"{type_label(vtype)} or bigger"


def _without_downtime(units, downtime_id):
    """Copy of ``units`` whose cached open-downtime lists omit one row."""
    out = []
    for unit in units:
        kept = [d for d in unit.open_downtimes() if d.id != downtime_id]
        if len(kept) != len(unit.open_downtimes()):
            unit._open_downtimes = kept
        out.append(unit)
    return out


def _window_summary(level, rows, worst, open_ended, worsened=()):
    n = len(rows)
    span = f"{n} day{'s' if n != 1 else ''}"
    if open_ended:
        span = f"the next {n} days"
    if level == "clear":
        return f"Clear — dispatch keeps a spare car on every one of {span}."

    def _day(r):
        return strf(r["date"], "%a %b %-d")

    def _why(r):
        return r["reasons"][0] if r["reasons"] else "no spare car"

    worsened = list(worsened)
    if worsened:
        # This unit is what tips the day over.
        tip = max(worsened, key=lambda r: (LEVEL_RANK[r["level"]], r["utilisation"]))
        if tip["level"] == "conflict":
            return (f"Short on {_day(tip)} because of this car — {_why(tip)}. "
                    f"Dispatch would need to farm out or move the job.")
        return (f"Tight on {_day(tip)} because of this car — {_why(tip)}. "
                f"Nothing to spare if a booking lands.")

    # Every non-clear day was already non-clear with the whole fleet.
    already = [r for r in rows if r["level"] != "clear"]
    if level == "conflict":
        days = ", ".join(_day(r) for r in already if r["level"] == "conflict")
        return (f"Already short on {days} with every car ({_why(worst)}) — dispatch "
                f"farms those days out regardless. This car doesn't change that.")
    days = ", ".join(_day(r) for r in already)
    return (f"Already tight on {days} with every car — this car makes no day worse, "
            f"but there is nothing to spare if a booking lands.")
