"""
Hour-level fleet capacity: the numbers behind "when can I take a car down?"

``fleet_capacity`` judges whole days — it is what the downtime form checks and
what gets stored on the ledger. This module is the hour-by-hour twin of the
same arithmetic, for the shop-window finder on the Fleet desk and the day-by-
day grid on the Outlook. Both screens read ONE payload built here, so a window
recommended on the desk is the same window the outlook shows; the ranking and
the copy live in ``templates/dispatching/includes/_fleet_windows_js.html`` and
run once, on the numbers this module hands over.

What a cell holds: for one day, one shop hour and one vehicle tier, the most
legs in flight at any moment of that hour that need a unit of that tier OR
BIGGER — the scheduler's nested compatibility (a Sprinter can run an SUV job,
an SUV cannot run a Sprinter job), exactly as ``day_setup.peak_concurrency``
counts the day's peak. Leg ends come from the same estimator the planner
uses, so a cell and the planner can never disagree about when a job is over.

Demand is what is booked. A day three weeks out looks light because bookings
keep arriving; the whole-day verdict that sits beside each row still comes from
``fleet_capacity.judge_day`` (booked peak AND what that weekday has actually
run lately), with one unit of the tier removed — the same check the downtime
form runs when a window is saved, so the badge and the save never contradict
each other.

DB-only, like every fleet page: nothing here calls Samsara.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta

from django.core.cache import cache
from django.utils import timezone

from business.datefmt import strf
from dispatching import fleet_capacity
from dispatching.scheduler import VEHICLE_TIER_ORDER, get_vehicle_tier

# The shop day: ten one-hour squares, 7:00 AM to 5:00 PM. A window may start
# in any square where the whole window still ends by 5:00 PM.
SHOP_START_HOUR = 7
SHOP_END_HOUR = 17
SLOTS = SHOP_END_HOUR - SHOP_START_HOUR

# The days a shop will take the car. Monday-Friday: the yards this fleet uses are
# shut at the weekend, so a Saturday window is not a cheap window, it is a closed
# one — and Saturday is this fleet's busiest day anyway, which is exactly why the
# demand arithmetic alone kept recommending it.
#
# This governs what the finder OFFERS. It deliberately does not touch the downtime
# ledger: a car that breaks down on a Sunday still has to be recordable, and a shop
# visit booked for Friday may well run through the weekend.
SHOP_WEEKDAYS = (0, 1, 2, 3, 4)


def shop_is_open(day):
    return day.weekday() in SHOP_WEEKDAYS
HOUR_LABELS = ["7a", "8", "9", "10", "11", "12", "1p", "2", "3", "4"]
DURATIONS = (2, 3, 4, 6, 8)

DESK_DAYS = 7
OUTLOOK_HORIZONS = (14, 28)
HOURLY_CACHE_SECONDS = fleet_capacity.OUTLOOK_CACHE_SECONDS

# How the office says "a unit of this tier or bigger". The top tier is just
# its name; the bottom tier is any car at all.
_TIER_WORDS = {
    "towncar": ("car", "cars"),
    "mini_van": ("Metris-or-bigger car", "Metris-or-bigger cars"),
    "suv": ("SUV-or-bigger car", "SUV-or-bigger cars"),
    "van": ("van-or-bigger unit", "van-or-bigger units"),
    "Van(14 Pax)": ("Sprinter", "Sprinters"),
}


def tier_words(vehicle_type):
    """('Sprinter', 'Sprinters') — singular and plural for the pool a unit of
    this type competes in."""
    return _TIER_WORDS.get(vehicle_type, ("car", "cars"))


def tier_phrase(vehicle_type):
    """'Sprinters' / 'SUV or bigger' / 'Any car' — the row label the outlook
    uses for a tier, in the words ``fleet_capacity`` already uses."""
    index = get_vehicle_tier(vehicle_type)
    if index < 0:
        return "Untyped"
    return fleet_capacity._tier_phrase(index, 2)


# ════════════════════════════════════════════════════════════════════════════
# Demand by the hour
# ════════════════════════════════════════════════════════════════════════════

def slot_of(moment, day):
    """Which shop square a datetime falls in on ``day``, or None outside the
    shop day (or on another date)."""
    if moment is None or moment.date() != day:
        return None
    if SHOP_START_HOUR <= moment.hour < SHOP_END_HOUR:
        return moment.hour - SHOP_START_HOUR
    return None


def hourly_need(day, legs):
    """Pure: peak in-flight legs per shop square, per tier (nested).

    Returns ``{"tiers": {vtype: [10 ints]}, "overall": [10 ints], "peak": n,
    "peak_at": datetime|None}``. ``tiers[t][i]`` is the most legs needing a
    unit of tier >= t that are under way at once during square ``i``;
    ``overall`` counts every leg, typed or not (a body is still needed).
    Same event ordering as ``day_setup.peak_concurrency`` — arrivals before
    departures at ties, the conservative reading — so the day's peak here is
    the peak the planner reports.
    """
    import dispatching.scheduler as sch

    tiers = list(VEHICLE_TIER_ORDER)
    need = {t: [0] * SLOTS for t in tiers}
    overall = [0] * SLOTS
    if not legs:
        return {"tiers": need, "overall": overall, "peak": 0, "peak_at": None}

    if sch._timing_cache is None:
        sch.preload_timing_cache()

    events = []
    for leg in legs:
        vt = leg.effective_vehicle_type
        start = datetime.combine(day, leg.pickup_time)
        end = sch.estimate_job_end_time(leg, day)
        if end <= start:
            end = start + timedelta(minutes=1)
        tier = get_vehicle_tier(str(vt)) if vt else -1
        events.append((start, 1, tier))
        events.append((end, -1, tier))
    events.sort(key=lambda e: (e[0], -e[1]))

    bounds = [datetime.combine(day, time(SHOP_START_HOUR + i)) for i in range(SLOTS)]
    cur = [0] * len(tiers)     # cur[k] = legs in flight needing tier >= k
    cur_all = 0
    peak, peak_at = 0, None

    def record(slot):
        for k, t in enumerate(tiers):
            if cur[k] > need[t][slot]:
                need[t][slot] = cur[k]
        if cur_all > overall[slot]:
            overall[slot] = cur_all

    for i, (moment, delta, tier) in enumerate(events):
        cur_all += delta
        if tier >= 0:
            for k in range(tier + 1):
                cur[k] += delta
        if cur_all > peak:
            peak, peak_at = cur_all, moment
        slot = slot_of(moment, day)
        if slot is not None:
            record(slot)
        # The state after this event holds until the next one; every square
        # that starts inside that stretch inherits it.
        nxt = events[i + 1][0] if i + 1 < len(events) else None
        for s, b in enumerate(bounds):
            if b > moment and (nxt is None or b < nxt):
                record(s)
    return {"tiers": need, "overall": overall, "peak": peak, "peak_at": peak_at}


def hourly_outlook(start, days, *, use_cache=True):
    """``[{"date", "need", "overall", "trips", "peak", "peak_at", "peak_slot"}, ...]``
    for ``days`` days from ``start``. Cached as briefly as the day outlook."""
    key = f"fleet_hourly_outlook:{start.isoformat()}:{days}"
    if use_cache:
        cached = cache.get(key)
        if cached is not None:
            return cached

    import dispatching.scheduler as sch

    preloaded_here = sch._timing_cache is None
    by_day = fleet_capacity.load_legs_by_day(start, days)
    out = []
    for offset in range(days):
        day = start + timedelta(days=offset)
        legs = by_day.get(day, [])
        h = hourly_need(day, legs)
        peak_slot = slot_of(h["peak_at"], day)
        out.append({
            "date": day,
            # The whole-day picture judge_day reads, from the same legs in the
            # same pass — the payload never loads a day twice.
            "demand": fleet_capacity.day_demand(day, legs),
            "need": h["tiers"],
            "overall": h["overall"],
            "trips": len(legs),
            "peak": h["peak"],
            "peak_at": fleet_capacity._clock(h["peak_at"]),
            "peak_slot": -1 if peak_slot is None else peak_slot,
        })
    if preloaded_here:
        sch.clear_timing_cache()
    if use_cache:
        cache.set(key, out, HOURLY_CACHE_SECONDS)
    return out


# ════════════════════════════════════════════════════════════════════════════
# The payload both screens read
# ════════════════════════════════════════════════════════════════════════════

def _verdict(row):
    return {"level": row["level"], "reasons": list(row.get("reasons") or [])}


def window_payload(start, days, units, *, today=None, now=None, use_cache=True,
                   unit_extras=None):
    """Everything the window finder needs, JSON-ready.

    ``unit_extras`` is ``{vehicle_id: {...}}`` merged onto each unit — the
    desk uses it to say what a booking would be for (a repair with these
    codes, or plain maintenance) without the JS re-deriving it.

    Per day and per tier: the hourly need, how many units of that tier or
    bigger are available (the ledger already applied), and the whole-day
    verdict if one more unit of that tier were taken down — computed by the
    same ``judge_day`` the downtime form runs, so the badge on the grid and
    the answer at save time are one answer.
    """
    now = now or timezone.now()
    today = today or timezone.localdate(now)
    unit_extras = unit_extras or {}
    tiers = list(VEHICLE_TIER_ORDER)

    hourly = hourly_outlook(start, days, use_cache=use_cache)
    typical = fleet_capacity.typical_units_by_weekday(today, use_cache=use_cache)

    day_rows = []
    for idx, h in enumerate(hourly):
        day = h["date"]
        demand = h["demand"]
        supply = fleet_capacity.supply_on(day, units)
        have = fleet_capacity.have_at_or_above(supply["available"])
        typical_units = typical.get(day.weekday())
        baseline = fleet_capacity.judge_day(demand, supply, typical_units)
        by_tier = {}
        for t in tiers:
            candidate = next(
                (u for u in supply["available"] if fleet_capacity.unit_type(u) == t), None)
            if candidate is None:
                if_down = baseline
            else:
                if_down = fleet_capacity.judge_day(
                    demand, fleet_capacity.supply_on(day, units, extra_down_ids={candidate.id}),
                    typical_units)
            verdict = _verdict(if_down)
            # Does THIS tier's unit tip the day, or was the day already there
            # with every car? A Saturday that is short with the whole fleet is
            # dispatch's Saturday, not this decision — the badge must not cry
            # wolf on it (same reading as fleet_capacity.check_window).
            verdict["worsened"] = (fleet_capacity.LEVEL_RANK[if_down["level"]]
                                   > fleet_capacity.LEVEL_RANK[baseline["level"]])
            by_tier[t] = {
                "need": h["need"][t],
                "have": have[t],
                "if_down": verdict,
            }
        day_rows.append({
            "idx": idx,
            "date": day.isoformat(),
            "wd": strf(day, "%a"),
            "dom": strf(day, "%b %-d"),
            "short": strf(day, "%-d"),
            "is_today": day == today,
            "weekend": day.weekday() >= 5,
            # Rendered, but never offered — see SHOP_WEEKDAYS.
            "shop_open": shop_is_open(day),
            "trips": h["trips"],
            "peak": h["peak"],
            "peak_at": h["peak_at"],
            "peak_slot": h["peak_slot"],
            "overall": h["overall"],
            "available": len(supply["available"]),
            "down": [f"#{d['unit'].vehicle_number}" for d in supply["down"]],
            "typical_units": typical_units,
            "baseline": _verdict(baseline),
            "by_tier": by_tier,
        })

    unit_rows = []
    for u in units:
        vtype = fleet_capacity.unit_type(u)
        if get_vehicle_tier(vtype) < 0:
            # An untyped unit competes in no pool; there is nothing to plan
            # around, so it is not offered in the finder.
            continue
        down_dates = [r["date"] for r in day_rows
                      if u.downtime_on(datetime.fromisoformat(r["date"]).date()) is not None]
        planned = next((d for d in u.open_downtimes() if d.is_planned(today)), None)
        row = {
            "id": u.id,
            "number": u.vehicle_number,
            "label": f"#{u.vehicle_number} · {u.year} {u.make} {u.model}".strip(),
            "tier": vtype,
            "down": down_dates,
            "booked": ({"id": planned.id, "day": strf(planned.starts_on, "%a %b %-d")}
                       if planned is not None else None),
            "category": "maintenance",
            "codes": [],
        }
        row.update(unit_extras.get(u.id, {}))
        unit_rows.append(row)

    owned = fleet_capacity.have_at_or_above(units)

    local_now = timezone.localtime(now)
    if local_now.date() != today:
        now_slot = -1
    elif local_now.hour < SHOP_START_HOUR:
        now_slot = -1
    elif local_now.hour >= SHOP_END_HOUR:
        now_slot = SLOTS
    else:
        now_slot = local_now.hour - SHOP_START_HOUR

    return {
        "slots": SLOTS,
        "hours": list(HOUR_LABELS),
        "shop_start": SHOP_START_HOUR,
        "durations": list(DURATIONS),
        "today": today.isoformat(),
        "now_slot": now_slot,
        "tiers": [{
            "key": t,
            "index": i,
            "one": tier_words(t)[0],
            "many": tier_words(t)[1],
            "phrase": tier_phrase(t),
            "label": fleet_capacity.type_label(t),
            "owned": owned[t],
        } for i, t in enumerate(tiers)],
        "units": unit_rows,
        "days": day_rows,
    }
