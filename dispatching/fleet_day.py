"""
What each car has for the day — the fleet manager's read of the dispatch board.

The desk answers "which car needs a decision" and the shop-window finder answers
"which HOUR is cheapest fleet-wide". Neither of them can answer the question the
fleet manager actually asks while standing in the yard: *what is this particular
car doing today, and when is it sitting still?* Dispatch has known that all along
— it is the board — but the board is a per-driver screen behind a nav the fleet
role does not have. This module turns the same rows the board reads into one row
per CAR.

THE CHAIN, and why each link is the one it is
────────────────────────────────────────────────────────────────────────────
A ``Leg`` has no FK to a physical car. ``Leg.vehicle`` is a ``rates.Vehicle``,
which is a vehicle *class* used for pricing (SUV / Sprinter), not the unit with
a number on it. The only thing that ties a job to a physical car is the
chauffeur holding that car that day:

    FleetVehicle → DriverVehicleAssignment(date) → Driver → that driver's Legs

``DriverVehicleAssignment`` is unique on (driver, date), NOT on (vehicle, date),
so one car legitimately carries several chauffeurs in a day — the Day Setup
AM/PM share. Every unit therefore merges the slots of ALL its holders, and the
seam between two chauffeurs' work is a HANDOFF, not a turnaround.

WHAT THIS MODULE REFUSES TO DO
────────────────────────────────────────────────────────────────────────────
1. It never says "free" from an absence of data. An unbuilt day and an empty car
   look identical in the database — both are "no rows" — and calling that second
   one free is the exact lie the Phase 3 audit caught on the desk. The board
   fills in as a GRADIENT, not a switch (measured: 95% of today's legs carry a
   driver, 85% tomorrow, 61% the day after, 0% beyond), so a boolean "is it
   built" is not enough either. Every page carries its assignment coverage and
   every car-row's wording is chosen against it.
2. It never gates anything. ``fleet_health`` and ``day_setup`` record the
   founder's ruling that only the human-entered downtime ledger removes a unit
   from the pool; Guard A was built and pulled for false positives. A car
   reading "sitting still" here informs a decision and subtracts no capacity.
3. It prints no turnaround verdict. Labelling a gap "tight" or "critical" is the
   feasibility engine's job and must come from ``required_turnaround`` slack, not
   from raw clock minutes. This page states the gap's LENGTH — a fact — and
   marks only whether a window is long enough for shop work, padded for drift.

PARITY. Slot ends come from ``scheduler.estimate_job_end_time``, the same
estimator ``fleet_windows.hourly_need`` uses, so a car's strip here and the shop
grid there can never disagree about when a job is over. That estimator is a p75
planning number and is never used for feasibility anywhere in this codebase —
the same rule applies here.

DB-only, like every fleet page: nothing here calls Samsara.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date as _date, datetime, time, timedelta

from business.datefmt import strf
from dispatching import car_share, fleet_capacity

# A shop job worth moving a car for. The audit's cadence finding: at this
# fleet's mileage an oil service falls due roughly every business day, and a
# service is ~90 minutes — so the useful question is "is there a 90-minute hole
# in this car's day", not "is the car free all day".
MIN_WINDOW_MINUTES = 90

# Getting the car to the bay and back. Charged against every window.
WINDOW_PAD_MINUTES = 30

# Flight drift, charged ON TOP and only where it is real: a window that opens
# after an airport ARRIVAL starts whenever the aircraft lands, so the clock
# overstates it. A departure carries a flight too but leaves on a fixed time.
FLIGHT_PAD_MINUTES = 60

# Below this share of the day's legs carrying a chauffeur, "no trips on this
# car" means "dispatch has not got to it yet", not "this car is free".
CONFIDENT_COVERAGE = 0.90

# How far out the page offers to look. The board is genuinely built about three
# days ahead; past that every row would read "not built yet".
DAY_CHOICES = 3


# ════════════════════════════════════════════════════════════════════════════
# Loading — every query in this module lives here
# ════════════════════════════════════════════════════════════════════════════

def load_car_day(day):
    """Everything one day's car-rows need, in five queries.

    Kept separate from ``fleet_desk.load_desk`` on purpose: that loader is
    undated and always means "today", and this page is dated. The pure/impure
    line is the same one the rest of the subsystem draws — this function
    touches the DB, everything below it takes objects.
    """
    from dispatching import scheduler
    from drivers.models import DriverVehicleAssignment
    from reservations.models import Leg

    units = fleet_capacity.fleet_units()
    unit_ids = [u.id for u in units]

    # Holders. select_related the VEHICLE as well as the driver: build_vehicle_caps
    # reads dva.vehicle.max_passenger_capacity per row, so leaving it out costs a
    # query per assignment.
    dva_rows = list(
        DriverVehicleAssignment.objects
        .filter(date=day, vehicle_id__in=unit_ids, vehicle__isnull=False)
        .select_related("driver", "driver__profile", "vehicle")
    )
    # A departed chauffeur can keep a stale row. Day Setup filters these; so do we,
    # or a driver who left in June appears to be holding a car today.
    dva_rows = [a for a in dva_rows
                if a.driver and a.driver.is_active and a.driver.driver_type == "inhouse"]

    # Legs. Our own query rather than fleet_capacity.load_legs_by_day, because
    # build_driver_schedules dereferences leg.driver, leg.reservation.customer and
    # leg.flight_information, none of which that shared query loads — and widening
    # it would change the query shape for the Outlook and the finder too. Same two
    # exclusions, which are the non-negotiable part.
    legs = list(
        Leg.objects.filter(pickup_date=day)
        .exclude(reservation__status__in=("cancelled", "canceled"))
        .exclude(status="cancelled")
        .select_related("driver", "reservation", "reservation__customer",
                        "reservation__vehicle", "vehicle", "flight_information")
        # Three per-leg N+1s inside build_driver_schedules, each one query per leg
        # on a 160-leg day: legstop_set and legflight_set get COUNTed, and
        # board_pay_state reads Reservation.payment_status, which walks
        # reservation.payments unless it finds them prefetched.
        .prefetch_related("legflight_set__flight", "legstop_set",
                          "reservation__payments")
        .order_by("pickup_time", "id")
    )

    holders = car_share.holders_by_unit((a.driver_id, a.vehicle_id) for a in dva_rows)
    drivers_by_id = {a.driver_id: a.driver for a in dva_rows}

    # Warm the route-timing cache only if we found it cold, and put it back the way
    # we found it — the courtesy fleet_windows observes for the same estimator.
    preloaded_here = scheduler._timing_cache is None
    if preloaded_here:
        scheduler.preload_timing_cache()
    try:
        schedules = scheduler.build_driver_schedules(
            legs, list(drivers_by_id.values()), day, dva_rows=dva_rows)
    finally:
        if preloaded_here:
            scheduler.clear_timing_cache()

    return {
        "day": day,
        "units": units,
        "dva_rows": dva_rows,
        "legs": legs,
        "holders": holders,
        "drivers_by_id": drivers_by_id,
        "schedules": schedules,
    }


# ════════════════════════════════════════════════════════════════════════════
# Pure builders
# ════════════════════════════════════════════════════════════════════════════

def _fmt(value):
    """'7:15 AM' — Windows-safe, and the same clock the rest of dispatch prints."""
    if value is None:
        return ""
    return strf(value, "%-I:%M %p")


def _span(minutes):
    """'3h 10m' / '45m' — a duration a person reads without doing arithmetic."""
    minutes = int(round(minutes))
    if minutes < 60:
        return f"{minutes}m"
    hours, rest = divmod(minutes, 60)
    return f"{hours}h" if not rest else f"{hours}h {rest}m"


def coverage(legs):
    """How much of this day dispatch has actually assigned.

    Returns (assigned, total, ratio). This is the honesty number the whole page
    hangs off: with no legs at all there is nothing to be wrong about, so the
    ratio is 1.0 rather than 0.
    """
    total = len(legs)
    if not total:
        return 0, 0, 1.0
    assigned = sum(1 for leg in legs if leg.driver_id)
    return assigned, total, assigned / total


def slot_datetimes(slot, day):
    """(start, end) as datetimes. Never `.hour` arithmetic — a 23:44 pickup ends
    after midnight, and hour-of-day maths silently wraps it to the start of the
    page."""
    start = datetime.combine(day, slot.pickup_time)
    end = slot.estimated_end_time
    if end is None or end <= start:
        end = start + timedelta(minutes=30)
    return start, end


def day_axis(day, spans):
    """The page's left and right edge, as datetimes floored/ceilinged to the hour.

    Deliberately NOT fleet_windows' 7a–5p shop axis: real days in this fleet run
    04:30 to 22:30, and reusing the shop axis would crop the first and last runs
    off the page without saying so.
    """
    default_start = datetime.combine(day, time(6, 0))
    default_end = datetime.combine(day, time(20, 0))
    if not spans:
        return default_start, default_end
    first = min(s for s, _ in spans)
    last = max(e for _, e in spans)
    start = min(first, default_start).replace(minute=0, second=0, microsecond=0)
    end = max(last, default_end)
    if end.minute or end.second:
        end = (end + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    return start, end


def axis_hours(axis_start, axis_end):
    """Tick labels for the strip header, one per hour, thinned on long days."""
    total = int((axis_end - axis_start).total_seconds() // 3600)
    step = 1 if total <= 12 else 2
    ticks = []
    for index in range(0, total + 1, step):
        moment = axis_start + timedelta(hours=index)
        ticks.append({
            "label": strf(moment, "%-I%p").replace("AM", "a").replace("PM", "p"),
            "pct": round(100.0 * index / total, 4) if total else 0.0,
            # Every other tick is dropped on a narrow screen rather than letting
            # the labels overprint each other into a smear.
            "minor": bool((index // step) % 2),
        })
    return ticks


def _place(start, end, axis_start, axis_end):
    """Left offset and width as percentages of the axis, from datetimes."""
    total = (axis_end - axis_start).total_seconds()
    if total <= 0:
        return 0.0, 0.0
    left = max(0.0, (start - axis_start).total_seconds() / total * 100.0)
    width = min(100.0 - left, (end - start).total_seconds() / total * 100.0)
    return round(left, 4), round(max(width, 0.6), 4)


def gaps_between(entries, day):
    """The holes in one car's day, with the honest caveats attached.

    ``entries`` is the unit's merged, time-ordered list of (driver_id, slot).
    A gap between two slots of the SAME chauffeur is idle time on that shift;
    a gap across two chauffeurs is a handoff, and the car may well be changing
    hands rather than sitting. Both are reported, labelled differently.

    ``usable`` means "long enough for shop work after padding both ends for
    flight drift" — it is the only judgement this module makes, and it is about
    the WINDOW, never about the trip.
    """
    out = []
    for (prev_driver, prev_slot), (next_driver, next_slot) in zip(entries, entries[1:]):
        _, prev_end = slot_datetimes(prev_slot, day)
        next_start, _ = slot_datetimes(next_slot, day)
        minutes = (next_start - prev_end).total_seconds() / 60.0
        if minutes <= 0:
            continue
        handoff = prev_driver != next_driver
        # An ARRIVAL's pickup time is the flight time and moves with it, so a window
        # that opens after one is softer than the clock says. A departure carries a
        # flight too, but its pickup is a fixed clock time — flagging those as well
        # would light the marker on nearly every gap and mean nothing.
        flight_dependent = (prev_slot.trip_type or "") == "arrival"
        needed = (MIN_WINDOW_MINUTES + WINDOW_PAD_MINUTES
                  + (FLIGHT_PAD_MINUTES if flight_dependent else 0))
        out.append({
            "start": prev_end,
            "end": next_start,
            "minutes": int(round(minutes)),
            "span": _span(minutes),
            "from_label": _fmt(prev_end),
            "to_label": _fmt(next_start),
            "handoff": handoff,
            "flight_dependent": bool(flight_dependent),
            "needed": needed,
            "usable": (not handoff) and minutes >= needed,
        })
    return out


def car_row(unit, day, holder_ids, drivers_by_id, schedules, axis_start, axis_end,
            confident=True):
    """One car's day. Pure — every argument is already loaded."""
    downtime = unit.downtime_on(day)
    names = [str(drivers_by_id[d]) for d in holder_ids if d in drivers_by_id]

    entries = []
    for driver_id in holder_ids:
        schedule = schedules.get(driver_id)
        if schedule is None:
            continue
        for slot in schedule.slots:
            entries.append((driver_id, slot))
    entries.sort(key=lambda pair: (pair[1].pickup_time, pair[1].leg_id))

    jobs = []
    for driver_id, slot in entries:
        start, end = slot_datetimes(slot, day)
        left, width = _place(start, end, axis_start, axis_end)
        jobs.append({
            "slot": slot,
            "driver_id": driver_id,
            "driver": str(drivers_by_id.get(driver_id, "")),
            "start": start,
            "end": end,
            "start_label": _fmt(start),
            # Inside the block, the meridiem is dead weight: the hour gridlines
            # and the axis already say which half of the day this is, and
            # "11:18 AM" needs half again the width of "11:18" to avoid being
            # clipped to "11:18 A".
            "short_label": strf(start, "%-I:%M"),
            "end_label": _fmt(end),
            "left": left,
            "width": width,
            "pickup": slot.pickup_location,
            "dropoff": slot.dropoff_location,
            "customer": slot.customer_name,
            "trip_type": slot.trip_type,
            "status": slot.status,
            "has_flight": slot.has_flight,
            "flight_info": slot.flight_info,
            "is_sanford": getattr(slot, "is_sanford", False),
            # A narrow block cannot hold a time; clipped text reads as a
            # rendering fault, and the row is more legible with a clean mark than
            # with "11:18 A". Measured against the short label at 0.66rem inside
            # 12px of padding, ~8% of a typical axis is where it starts to fit.
            "show_label": width >= 8.0,
        })

    gaps = gaps_between(entries, day)
    for gap in gaps:
        gap["left"], gap["width"] = _place(gap["start"], gap["end"], axis_start, axis_end)

    # Two chauffeurs' legs cannot both be in one car at once. If they overlap the
    # board has a problem worth saying out loud rather than drawing over.
    overlap = False
    for (_, a), (_, b) in zip(entries, entries[1:]):
        a_start, a_end = slot_datetimes(a, day)
        b_start, b_end = slot_datetimes(b, day)
        if car_share.intervals_overlap(a_start, a_end, b_start, b_end):
            overlap = True
            break

    longest = max(gaps, key=lambda g: g["minutes"]) if gaps else None
    usable = [g for g in gaps if g["usable"]]

    # ── The sentence. Four states that must never collapse into each other. ──
    if downtime is not None:
        state, note = "down", (unit.out_of_service_label(day) or "In the shop")
    elif not holder_ids and not jobs:
        if confident:
            state, note = "open", "No chauffeur on it — free all day."
        else:
            state, note = "unknown", "Not assigned yet."
    elif not jobs:
        if confident:
            state, note = "open", f"{names[0] if names else 'Assigned'} has it, nothing booked on it."
        else:
            state, note = "unknown", (
                f"{names[0] if names else 'Assigned'} has it, nothing on it yet.")
    else:
        state = "working"
        first, last = jobs[0]["start_label"], jobs[-1]["end_label"]
        note = f"{len(jobs)} trip{'s' if len(jobs) != 1 else ''} · {first} – {last}"

    return {
        "unit": unit,
        "number": unit.vehicle_number,
        "vehicle_type": fleet_capacity.type_label(fleet_capacity.unit_type(unit)),
        "state": state,
        "note": note,
        "drivers": names,
        "shared": len(names) > 1,
        "jobs": jobs,
        "trips": len(jobs),
        "gaps": gaps,
        "usable_windows": usable,
        "longest_gap": longest,
        "overlap": overlap,
        "downtime": downtime,
        "downtime_notice": unit.downtime_notice(day) if downtime is None else "",
        "first_label": jobs[0]["start_label"] if jobs else "",
        "last_label": jobs[-1]["end_label"] if jobs else "",
        "href": f"/dispatching/fleet/{unit.id}/",
    }


def build_day(loaded):
    """The whole page, from one ``load_car_day`` payload."""
    day = loaded["day"]
    units, legs = loaded["units"], loaded["legs"]
    holders = loaded["holders"]
    drivers_by_id, sched = loaded["drivers_by_id"], loaded["schedules"]

    assigned, total, ratio = coverage(legs)
    built = bool(loaded["dva_rows"]) and assigned > 0
    confident = built and ratio >= CONFIDENT_COVERAGE

    spans = []
    for schedule in sched.values():
        for slot in schedule.slots:
            spans.append(slot_datetimes(slot, day))
    axis_start, axis_end = day_axis(day, spans)

    rows = [
        car_row(unit, day, holders.get(unit.id, []), drivers_by_id, sched,
                axis_start, axis_end, confident=confident)
        for unit in units
    ]

    order = {"working": 0, "open": 1, "unknown": 2, "down": 3}
    rows.sort(key=lambda r: (order.get(r["state"], 9), -r["trips"],
                             _natural(r["number"])))

    on_a_car = sum(r["trips"] for r in rows)
    return {
        "day": day,
        "rows": rows,
        "built": built,
        "confident": confident,
        "assigned": assigned,
        "total_legs": total,
        "coverage_pct": int(round(ratio * 100)) if total else 0,
        "unplaced": max(0, total - on_a_car),
        "axis_start": axis_start,
        "axis_end": axis_end,
        "hours": axis_hours(axis_start, axis_end),
        # One hour as a percentage of the axis, so the board's gridlines land on
        # the same hours the header labels do however long the day turns out.
        "hour_pct": round(100.0 / max(1, int((axis_end - axis_start).total_seconds() // 3600)), 4),
        "working": sum(1 for r in rows if r["state"] == "working"),
        "open_units": sum(1 for r in rows if r["state"] == "open"),
        "down": sum(1 for r in rows if r["state"] == "down"),
        "headline": _headline(built, confident, ratio, total, day),
    }


def _headline(built, confident, ratio, total, day):
    """What the page says about its own trustworthiness, before any car-row."""
    when = strf(day, "%A %-d %B")
    if not built:
        if total:
            return (f"Dispatch has not built {when} yet. {total} trips are booked, "
                    f"and no car is assigned to any of them — nothing on this page "
                    f"is a free car.")
        return f"Nothing booked for {when} yet."
    if not confident:
        return (f"{when} is still being built — {int(round(ratio * 100))}% of its "
                f"trips have a chauffeur. A car showing nothing may just not be "
                f"assigned yet.")
    return f"{when} is built — {int(round(ratio * 100))}% of trips have a chauffeur."


def _natural(vehicle_number):
    """'#2' before '#10'. Same sort the desk uses."""
    number = (vehicle_number or "").strip()
    digits = "".join(ch for ch in number if ch.isdigit())
    return (0, int(digits), number) if digits else (1, 0, number)


def day_options(today):
    """The Today / Tomorrow / +2 control. Capped at what dispatch actually builds."""
    labels = ["Today", "Tomorrow"]
    out = []
    for offset in range(DAY_CHOICES):
        value = today + timedelta(days=offset)
        out.append({
            "date": value,
            "value": value.isoformat(),
            "label": labels[offset] if offset < len(labels) else strf(value, "%a %-d"),
        })
    return out


def parse_day(raw, today):
    """``?date=YYYY-MM-DD`` → a date, falling back to today on anything odd."""
    if not raw:
        return today
    try:
        return _date.fromisoformat(str(raw).strip())
    except (TypeError, ValueError):
        return today
