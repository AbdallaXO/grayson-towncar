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

from django.utils import timezone

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

# How far out the page offers to look. A week, because a fleet manager plans a
# week — but only the first few days of it are a BOARD. Measured 2026-09-15:
# 97% of today's trips carried a chauffeur, 87% tomorrow, 62% the day after and
# 0% for the four days beyond, against 162, 256, 193 and 154 booked trips.
#
# So the page runs two row treatments, and which one a day gets is decided by
# that day's own coverage, never by how far away it is. A built day gets the
# car-by-car clock. A day with no chauffeurs on it gets its DEMAND and nothing
# else — its trips are real and worth planning around, but no statement about
# any individual car would be true. Raising this number without that split is
# what would have every unit reading "free all day" on a Saturday carrying 256
# trips, which is the exact lie this page was built to stop telling.
DAY_CHOICES = 7


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


def _venue(address):
    """'Disney's Old Key West Resort' out of the full booked address.

    Bookings store the whole postal string, and on a phone row that turns one
    trip into four lines of ", Lake Buena Vista, FL, USA". The venue is the part
    before the first comma, which is how these addresses are always written.
    Kept for DISPLAY only — every link and every route still carries the full
    address, because Google resolves that one accurately.
    """
    text = (address or "").strip()
    if not text:
        return ""
    head = text.split(",")[0].strip()
    # A house number on its own says nothing; keep the street with it.
    if len(head) < 5 and "," in text:
        head = ",".join(text.split(",")[:2]).strip()
    return head or text


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
            # Venue-only, for the phone list. The tooltip keeps the full address.
            "pickup_short": _venue(slot.pickup_location),
            "dropoff_short": _venue(slot.dropoff_location),
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
            # Set below: the first and last block of a row always carry a time,
            # printed OUTSIDE the block when it is too narrow to hold one.
            "edge": "",
        })

    # Without this most of a row is unlabelled marks, and reading when a car's
    # day starts and ends means hovering. The two that anchor the row get their
    # time whatever their width — outside the block if it will not fit, where
    # there is nothing to collide with because it is the end of the row.
    if jobs:
        for job, side in ((jobs[0], "start"), (jobs[-1], "end")):
            if not job["show_label"]:
                job["edge"] = side

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

    # Most room first. The page exists to find a hole big enough to put a car
    # in, and the old order led with the busiest unit and buried the two cars
    # standing still under seventeen rows nobody can act on.
    order = {"open": 0, "unknown": 1, "working": 2, "down": 3}
    rows.sort(key=lambda r: (order.get(r["state"], 9), -_room_minutes(r),
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


def _room_minutes(row):
    """The longest genuinely USABLE hole in this car's day, in minutes.

    Usable, not longest — deliberately the same reading the inspection round
    takes. A three-hour gap that opens behind an airport arrival is not three
    hours you can hold a car for, and sorting on the raw hole would put a car
    with no real window above one that has one.
    """
    return max((gap["minutes"] for gap in (row.get("usable_windows") or [])), default=0)


def _natural(vehicle_number):
    """'#2' before '#10'. Same sort the desk uses."""
    number = (vehicle_number or "").strip()
    digits = "".join(ch for ch in number if ch.isdigit())
    return (0, int(digits), number) if digits else (1, 0, number)


def car_today(unit, day, *, now=None):
    """This one car's day, for the "take it off the road" confirmation.

    The desk used to ask only what the FLEET loses — "19 needed at 10:36 AM, 18
    left" — and never what this car is in the middle of. A unit can be halfway
    through seven trips with a named chauffeur on it, and the panel would say
    "move the job", singular, without naming either.

    Scoped to one unit and its own holders, but through the same estimator
    ``build_day`` uses, so this sentence and that car's row on The day can never
    disagree about when its last trip ends.

    Returns ``{"line", "trips", "left", "drivers", "last_label", "free"}``.
    """
    from dispatching import scheduler
    from drivers.models import DriverVehicleAssignment
    from reservations.models import Leg

    # Every datetime this module builds is NAIVE LOCAL — slot_datetimes combines
    # a date with a clock time and the axis is drawn from those. Comparing one
    # against an aware "now" raises, so the clock is normalised to match rather
    # than the slots being made aware.
    if now is None:
        now = timezone.localtime()
    if timezone.is_aware(now):
        now = timezone.localtime(now).replace(tzinfo=None)
    number = f"#{unit.vehicle_number}"

    dva_rows = [
        a for a in (DriverVehicleAssignment.objects
                    .filter(date=day, vehicle=unit)
                    .select_related("driver", "driver__profile", "vehicle"))
        if a.driver and a.driver.is_active and a.driver.driver_type == "inhouse"
    ]
    drivers = {a.driver_id: a.driver for a in dva_rows}
    # Capitalise the first letter only — .title() would turn "MiguelT" into
    # "Miguelt". A Driver with no surname also str()s with a trailing space,
    # which printed "Roberto 's".
    names = [_name(d) for d in drivers.values()]
    if not drivers:
        # "Nobody is on this car" and "nobody is on ANY car yet" look identical
        # from one unit's rows and mean opposite things — the first is a free
        # car, the second is a day dispatch has not reached. One cheap indexed
        # existence check separates them, and only on the ambiguous path.
        day_built = DriverVehicleAssignment.objects.filter(date=day).exists()
        line = (f"No chauffeur is on {number} today, so taking it off the road "
                f"now costs Dispatch nothing."
                if day_built else
                f"{strf(day, '%a %-d %b')} has no chauffeurs on it yet, so what "
                f"{number} picks up then is not known.")
        return {"line": line, "trips": 0, "left": 0, "drivers": [],
                "last_label": "", "free": True, "day_built": day_built}

    legs = list(
        Leg.objects.filter(pickup_date=day, driver_id__in=list(drivers))
        .exclude(reservation__status__in=("cancelled", "canceled"))
        .exclude(status="cancelled")
        .select_related("driver", "reservation", "reservation__customer",
                        "reservation__vehicle", "vehicle", "flight_information")
        .prefetch_related("legflight_set__flight", "legstop_set",
                          "reservation__payments")
        .order_by("pickup_time", "id")
    )
    who = _and_list(names)
    if not legs:
        return {"line": f"{who} {'have' if len(names) > 1 else 'has'} {number} today "
                        f"but nothing is booked on it, so nothing moves.",
                "trips": 0, "left": 0, "drivers": names, "last_label": "", "free": True, "day_built": True}

    preloaded_here = scheduler._timing_cache is None
    if preloaded_here:
        scheduler.preload_timing_cache()
    try:
        schedules = scheduler.build_driver_schedules(
            legs, list(drivers.values()), day, dva_rows=dva_rows)
    finally:
        if preloaded_here:
            scheduler.clear_timing_cache()

    spans = []
    for schedule in schedules.values():
        for slot in schedule.slots:
            spans.append(slot_datetimes(slot, day))
    spans.sort()
    if not spans:
        return {"line": f"{who} {'have' if len(names) > 1 else 'has'} {number} today "
                        f"but nothing is booked on it, so nothing moves.",
                "trips": 0, "left": 0, "drivers": names, "last_label": "", "free": True, "day_built": True}

    last_end = max(end for _s, end in spans)
    # "Left" is measured against the clock only when the day in question is
    # today — a takedown booked for a future date moves all of it.
    if day == now.date():
        remaining = [pair for pair in spans if pair[1] > now]
    else:
        remaining = list(spans)
    trips, left = len(spans), len(remaining)
    last_label = _fmt(last_end)

    if not left:
        return {"line": f"{number} has finished for the day — {who} ran "
                        f"{trips} trip{'s' if trips != 1 else ''}, the last ending "
                        f"{last_label}. Nothing is left to move.",
                "trips": trips, "left": 0, "drivers": names,
                "last_label": last_label, "free": True, "day_built": True}

    plural = "s" if trips != 1 else ""
    if left == trips:
        lead = f"{number} still has all {trips} of {who}'s trip{plural}"
    else:
        lead = f"{number} has {left} of {who}'s {trips} trip{plural}"
    moves = "that one" if left == 1 else f"all {left}"
    return {
        "line": (f"{lead} still to run, the last ending {last_label}. "
                 f"Taking it off now moves {moves}."),
        "trips": trips, "left": left, "drivers": names,
        "last_label": last_label, "free": False, "day_built": True,
    }


def car_range(unit, start, back, *, now=None, detail_days=7):
    """Every day a takedown would block, and what this car is carrying on each.

    ``back`` is the first day the car is usable AGAIN — the downtime ledger's
    own rule — so the blocked days are ``start`` up to but not including it.

    The confirmation used to assume "off now, back tomorrow" and look only at
    today. A car booked solid on Wednesday would be taken off until Thursday
    without Wednesday ever being mentioned, which is the whole of what makes
    the decision reversible-by-surprise rather than informed.

    Cost is bounded by how much of the range is actually BUILT: a day with no
    chauffeur on this car returns before it loads any legs, and the board is
    only assigned about three days out.
    """
    # An open-ended downtime has no return date yet, so there is no span to
    # count. Look as far as the board is ever built and say it that way —
    # "across those 7 days" would be inventing a length nobody chose.
    open_ended = back is None
    if open_ended:
        back = start + timedelta(days=detail_days)
    elif back <= start:
        back = start + timedelta(days=1)
    span = (back - start).days

    days, total_left, unbuilt = [], 0, []
    for offset in range(min(span, detail_days)):
        day = start + timedelta(days=offset)
        row = car_today(unit, day, now=now)
        label = "today" if day == _today_of(now) else strf(day, "%a %-d %b")
        days.append({"date": day.isoformat(), "label": label,
                     "trips": row["trips"], "left": row["left"],
                     "drivers": row["drivers"],
                     "line": row["line"], "free": row["free"]})
        total_left += row["left"]
        if not row.get("day_built", True):
            unbuilt.append(label)

    booked = [d for d in days if d["left"]]
    if not booked:
        head = f"#{unit.vehicle_number} has nothing of its own to move"
        if open_ended:
            head += " over the days the board is built for."
        else:
            head += " on that day." if span == 1 else f" across those {span} days."
    else:
        parts = []
        for d in booked:
            word = "trip" if d["left"] == 1 else "trips"
            parts.append(f"{d['left']} {word} {d['label']}")
        # "move to another car", not just "move": the impact line beside this one
        # can correctly say no job gets FARMED OUT — the rest of the fleet covers
        # them — and the two read as a contradiction unless this one says what
        # actually happens to the work.
        head = (f"#{unit.vehicle_number} has {_and_list(parts)} — "
                f"{total_left} job{'s' if total_left != 1 else ''} "
                f"{'that' if total_left == 1 else 'that'} would have to move to "
                f"another car.")
        # Whose work it is, when the answer is short enough to be worth saying.
        # A chauffeur's name is what turns "12 jobs" into a phone call.
        who = sorted({name for d in booked for name in d["drivers"]})
        if len(who) == 1:
            head += f" {who[0]} is on it."
        elif 1 < len(who) <= 3:
            head += f" {_and_list(who)} are on it."

    # Days nobody has assigned yet are NOT "no trips on this car" — the board
    # simply has not reached them, and saying nothing here would read as a
    # promise the page cannot make.
    if unbuilt:
        # Naming five dates is noise; the point is only that the board has not
        # reached them. str.capitalize() would lowercase the month names too.
        if len(unbuilt) > 2:
            head += (f" The other {len(unbuilt)} days are not assigned yet, so "
                     f"what this car picks up then is not known.")
        else:
            named = _and_list(unbuilt)
            head += (f" {named[:1].upper()}{named[1:]} "
                     f"{'is' if len(unbuilt) == 1 else 'are'} not assigned yet, so "
                     f"what this car picks up then is not known.")
    if not open_ended and span > detail_days:
        head += f" The window runs {span} days in all."

    return {"line": head, "days": days, "total_left": total_left,
            "span_days": None if open_ended else span,
            "open_ended": open_ended, "free": total_left == 0}


def _today_of(now):
    if now is None:
        return timezone.localdate()
    return now.date() if hasattr(now, "date") else now


def _name(driver):
    text = str(driver).strip()
    return f"{text[:1].upper()}{text[1:]}" if text else ""


def _and_list(names):
    if not names:
        return "Nobody"
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


def week_pulse(start, days=DAY_CHOICES):
    """``{date: {"trips": n, "assigned": n, "holders": n}}`` for a whole week.

    Two aggregate queries for the entire range — cheap enough to run on every
    page load, which is the point: the week strip has to state each day's
    coverage, and paying ``load_car_day`` seven times to print seven numbers
    would put the whole board's cost on a control.

    Same two exclusions as ``load_car_day``, so a day's trip count here and its
    row count there can never disagree.
    """
    from django.db.models import Count, Q

    from drivers.models import DriverVehicleAssignment
    from reservations.models import Leg

    end = start + timedelta(days=days - 1)
    out = {start + timedelta(days=i): {"trips": 0, "assigned": 0, "holders": 0}
           for i in range(days)}

    legs = (Leg.objects
            .filter(pickup_date__range=(start, end))
            .exclude(reservation__status__in=("cancelled", "canceled"))
            .exclude(status="cancelled")
            .values("pickup_date")
            .annotate(trips=Count("id"),
                      assigned=Count("id", filter=Q(driver_id__isnull=False))))
    for row in legs:
        day = out.get(row["pickup_date"])
        if day is not None:
            day["trips"] = row["trips"]
            day["assigned"] = row["assigned"]

    holders = (DriverVehicleAssignment.objects
               .filter(date__range=(start, end), vehicle__isnull=False,
                       driver__is_active=True, driver__driver_type="inhouse")
               .values("date")
               .annotate(n=Count("id")))
    for row in holders:
        day = out.get(row["date"])
        if day is not None:
            day["holders"] = row["n"]
    return out


def is_built(pulse_row):
    """Has dispatch started putting chauffeurs on this day?

    The same reading ``build_day`` takes — somebody holds a car AND at least one
    trip has a chauffeur — so the strip and the board never disagree about which
    treatment a day gets.
    """
    return bool(pulse_row["holders"]) and pulse_row["assigned"] > 0


def day_options(today, pulse=None):
    """The week control: seven days, each carrying what is known about it.

    Every option states its own coverage so the choice itself is honest — a day
    four out reads "0% assigned · 256 trips" before it is opened, not after.
    """
    labels = ["Today", "Tomorrow"]
    pulse = pulse if pulse is not None else week_pulse(today)
    out = []
    for offset in range(DAY_CHOICES):
        value = today + timedelta(days=offset)
        row = pulse.get(value) or {"trips": 0, "assigned": 0, "holders": 0}
        built = is_built(row)
        ratio = (row["assigned"] / row["trips"]) if row["trips"] else 1.0
        out.append({
            "date": value,
            "value": value.isoformat(),
            "label": labels[offset] if offset < len(labels) else strf(value, "%a %-d"),
            "trips": row["trips"],
            "assigned": row["assigned"],
            "built": built,
            "confident": built and ratio >= CONFIDENT_COVERAGE,
            "coverage_pct": int(round(ratio * 100)) if row["trips"] else 0,
        })
    return out


def demand_only(day, pulse_row, typical_units=None):
    """What an unbuilt day can honestly say about itself.

    No car rows: with no chauffeur on any trip there is nothing true to draw
    against a unit. What IS real is the demand — those trips are booked — and
    what that weekday normally takes to run, which is the pair a shop day gets
    planned against.
    """
    when = strf(day, "%A %-d %B")
    trips = pulse_row["trips"]
    if not trips:
        return {
            "day": day, "trips": 0, "typical_units": typical_units,
            "headline": f"Nothing booked for {when} yet.",
            "detail": "Nothing to plan around on this day.",
        }
    detail = (f"{trips} trip{'s' if trips != 1 else ''} are on the books and none of them "
              f"has a chauffeur yet, so no car on this date can be called free.")
    if typical_units:
        detail += (f" A {strf(day, '%A')} normally runs {typical_units} cars — "
                   f"plan a shop day against that, not against an empty board.")
    return {
        "day": day,
        "trips": trips,
        "typical_units": typical_units,
        "headline": f"Dispatch has not built {when} yet.",
        "detail": detail,
    }


def parse_day(raw, today):
    """``?date=YYYY-MM-DD`` → a date, falling back to today on anything odd."""
    if not raw:
        return today
    try:
        return _date.fromisoformat(str(raw).strip())
    except (TypeError, ValueError):
        return today
