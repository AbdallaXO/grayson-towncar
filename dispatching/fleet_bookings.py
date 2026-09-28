"""
Vehicle bookings — fleet claiming a few hours of one car's day.

Fleet → The day shows every car's trips and the holes between them. A booking
is fleet putting something IN one of those holes: "#006, ten to twelve, rear
tyres". The model is ``drivers.VehicleBooking``; this module is the one place
that decides what a booking means to everyone else.

SOFT BY DEFAULT, AND WHAT THAT MEANS
────────────────────────────────────────────────────────────────────────────
A soft booking makes dispatch AWARE. The car stays in every pool, stays
draggable, stays selectable — and wherever a trip is about to land on top of
the booking, the dispatcher is told exactly what is booked and asked to
continue or cancel BEFORE anything is written. Nothing is taken off the road
by a soft booking.

A hard booking is fleet saying the car physically cannot run in that window.
It refuses any trip that would overlap it, and there is NO override at the
point of assignment — the way through is to move, soften or cancel the booking,
which on a hard one only a fleet manager or a superuser may do
(``can_manage``). Whole days off the road stay ``VehicleDowntime``'s job.

WHERE THE CHECK RUNS
────────────────────────────────────────────────────────────────────────────
Putting a trip on a chauffeur (checked against the car they hold that day):
  * ``assignment.set_leg_driver`` — THE front door — raises
    ``HardBookingRefused`` for a hard clash on every path that goes through it
    (dropdowns, board drag, swaps, takeback, Smart Builder, advisors, farm-out).
  * Paths that write ``leg.driver`` themselves (auto-assign apply, draft
    publish, snapshot restore) run ``pair_clashes`` over their whole batch.
  * A SOFT clash is a question, not a refusal: the interactive endpoints
    answer 409 ``refusal(...)`` with ``can_override`` and write only when the
    caller comes back with ``override_booking``.
Handing a car to a chauffeur for the day (pool drop, Day Setup, copy
yesterday's cars) — ``driver_clash``, against that chauffeur's trips already
on the day.
Fleet saving a booking — against the trips already on the car, so fleet
knows before dispatch does. The day draws a booking a trip has since landed
on as a conflict, worked out on read.

A leg has no FK to a physical car (see ``fleet_day``): the car is whatever the
chauffeur holds that day, so every check goes leg → driver → that day's
``DriverVehicleAssignment`` → the car's bookings.

Trip ends are the same p75 planning estimate The day draws its blocks with
(``scheduler.estimate_job_end_time``, stretched by ``stop_aware_end`` for a
charter's booked hours and extra stops), so a block drawn touching a booking
and the warning a dispatcher gets can never disagree. Touching ends do not
overlap — a trip clearing at 10:00 and a booking from 10:00 are compatible
(``car_share.intervals_overlap``).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from business.datefmt import strf
from dispatching.car_share import intervals_overlap

# Leg statuses that put a trip on a car. The same exclusions The day uses.
CANCELLED_RESERVATION = ("cancelled", "canceled")

# One short word per booking type, for a block too narrow for the full name —
# a one-hour booking on the schedule board is ~45px wide. Seven letters or
# fewer, so it fits there.
TYPE_SHORT = {
    "service": "Service",
    "maintenance": "Maint.",
    "inspection": "Inspect",
    "detailing": "Detail",
    "repair": "Repair",
    "transfer": "Pickup",
    "fleet_use": "Fleet",
    "training": "Train",
    "permit": "Permit",
    "reserved": "Held",
    "other": "Booked",
}


class HardBookingRefused(Exception):
    """Putting this trip on this chauffeur lands it inside a HARD booking.

    Raised by ``assignment.set_leg_driver`` before anything is staged or
    written. ``clash`` is the ``booking_clash`` dict; ``refusal(exc.clash)`` is
    the 409 body every endpoint answers with.
    """

    def __init__(self, clash):
        self.clash = clash
        super().__init__(clash["text"])


# ════════════════════════════════════════════════════════════════════════════
# Who may do what
# ════════════════════════════════════════════════════════════════════════════

def can_manage_hard(user) -> bool:
    """Only fleet (or a superuser) may make, move, soften or cancel a HARD
    booking. A soft one is a note to dispatch and any staff member may keep it
    current; a hard one takes a car away, and the people who decided that are
    the ones who get to undo it."""
    if not getattr(user, "is_authenticated", False) or not user.is_staff:
        return False
    if user.is_superuser:
        return True
    profile = getattr(user, "profile", None)
    return bool(profile and getattr(profile, "is_fleet_manager", False))


def can_manage(user, booking=None, *, making_hard=False) -> bool:
    """May ``user`` save this booking (or a new one) in its new shape?"""
    if not getattr(user, "is_authenticated", False) or not user.is_staff:
        return False
    touches_hard = making_hard or bool(booking is not None and booking.is_hard)
    return can_manage_hard(user) if touches_hard else True


# ════════════════════════════════════════════════════════════════════════════
# Pure helpers
# ════════════════════════════════════════════════════════════════════════════

def span(booking):
    """(start, end) as naive local datetimes — the clock every slot uses."""
    return (datetime.combine(booking.date, booking.start_time),
            datetime.combine(booking.date, booking.end_time))


def overlapping(bookings, start, end):
    """The bookings in ``bookings`` that [start, end) runs into, hard first."""
    hits = [b for b in bookings if intervals_overlap(start, end, *span(b))]
    hits.sort(key=lambda b: (not b.is_hard, b.start_time))
    return hits


def kind_word(booking) -> str:
    return "hard-booked" if booking.is_hard else "booked"


def clash_text(booking, *, subject="This trip"):
    """The sentence a dispatcher reads — the founder's own wording. Names the
    car, the window, the reason, and — for a hard one — the way through,
    because "no" with no next step just sends them hunting."""
    head = (f"Vehicle #{booking.vehicle.vehicle_number} is {kind_word(booking)} "
            f"from {booking.window_label()} for {booking.title()}.")
    if booking.is_hard:
        return (f"{head} {subject} overlaps it, and a hard booking means the car "
                f"can't be used then. Put the trip on another car, or ask fleet "
                f"to move or lift the booking.")
    return f"{head} {subject} overlaps with the vehicle booking."


def trip_phrase(trip):
    """'the 10:30 AM trip (Miguel)' for a clash list."""
    who = f" ({trip['driver']})" if trip.get("driver") else ""
    return f"the {trip['start_label']} trip{who}"


def and_list(items):
    items = list(items)
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


def refusal(clash, **extra) -> dict:
    """The 409 body every endpoint answers a booking clash with.

    ``can_override`` is the whole contract: True (soft) means "ask the
    dispatcher, and resend with ``override_booking: true`` if they continue";
    False (hard) means there is nothing to continue — show the text and stop.
    Accepts a ``booking_clash`` or a ``driver_clash`` dict.
    """
    booking = clash.get("booking") or (clash.get("bookings") or [None])[0]
    body = {
        "success": False,
        "error": clash["text"],
        "booking_conflict": True,
        "hard": bool(clash["hard"]),
        "can_override": not clash["hard"],
    }
    if booking is not None:
        body["booking_id"] = booking.id
        body["vehicle_number"] = booking.vehicle.vehicle_number
    body.update(extra)
    return body


def booking_payload(booking, *, trips=(), can_edit=True):
    """Everything a page needs to draw a booking and open it for editing."""
    return {
        "id": booking.id,
        "vehicle_id": booking.vehicle_id,
        "number": booking.vehicle.vehicle_number,
        "date": booking.date.isoformat(),
        "start": booking.start_time.strftime("%H:%M"),
        "end": booking.end_time.strftime("%H:%M"),
        "start_label": strf(booking.start_time, "%-I:%M %p"),
        "end_label": strf(booking.end_time, "%-I:%M %p"),
        "window": booking.window_label(),
        "type": booking.booking_type,
        "type_label": booking.get_booking_type_display(),
        "title": booking.title(),
        "reason": booking.reason,
        "location": booking.location,
        "notes": booking.notes,
        "is_hard": booking.is_hard,
        "label": booking.label(),
        "clashes": [dict(t) for t in trips],
        "can_edit": can_edit,
    }


def chip(booking, trips=(), *, compact=False):
    """The tag every dispatch surface prints beside a car: the pools, the
    assigned-car chips, the schedule board, the trip dropdowns.

    ``trips`` are the trip spans already on the car (or on the chauffeur
    holding it). A soft booking nothing lands on reads as plain information —
    "Booked 10:00 AM–12:00 PM · Tire service" with a calendar icon — and only
    turns into a ⚠ when a trip actually sits inside it; a hard one always
    carries the lock. ``compact`` uses '10a–12p' for tags squeezed beside a
    name.
    """
    window = booking.short_window() if compact else booking.window_label()
    hits = trips_in(*span(booking), trips) if trips else []
    lead = "Hard-booked" if booking.is_hard else "Booked"
    conflict_text = ""
    if hits:
        conflict_text = (f"Booking conflict — "
                         f"{and_list([trip_phrase(t) for t in hits])} "
                         f"{'is' if len(hits) == 1 else 'are'} inside it.")
    return {
        "id": booking.id,
        "short": f"{lead} {window} · {booking.title()}",
        "label": booking.label(),
        "window": booking.window_label(),
        "purpose": booking.title(),
        "hard": booking.is_hard,
        "conflict": bool(hits),
        "conflict_text": conflict_text,
        "icon": ("lock-fill" if booking.is_hard
                 else "exclamation-triangle-fill" if hits else "calendar-event"),
        "title": (f"{booking.title()} — {booking.window_label()}. "
                  + ("Hard booking: no trips on this car in that window."
                     if booking.is_hard else
                     "Soft booking: the car can still be used; dispatch is "
                     "asked first if a trip lands on it.")
                  + (f" {conflict_text}" if conflict_text else "")),
    }


# ════════════════════════════════════════════════════════════════════════════
# Loading
# ════════════════════════════════════════════════════════════════════════════

def bookings_for(day, vehicle_ids=None):
    """``{vehicle_id: [booking, ...]}`` for one date, active only, one query."""
    from drivers.models import VehicleBooking

    qs = VehicleBooking.active().filter(date=day).select_related("vehicle")
    if vehicle_ids is not None:
        qs = qs.filter(vehicle_id__in=list(vehicle_ids))
    out = defaultdict(list)
    for booking in qs.order_by("start_time", "id"):
        out[booking.vehicle_id].append(booking)
    return dict(out)


def _warm_timing():
    """Warm the route-timing cache if it is cold; return whether we did, so
    the caller can put it back the way it found it (fleet_day's courtesy)."""
    from dispatching import scheduler

    if scheduler._timing_cache is None:
        scheduler.preload_timing_cache()
        return True
    return False


def _cool_timing(warmed):
    if warmed:
        from dispatching import scheduler
        scheduler.clear_timing_cache()


def stop_aware_end(leg, day, base_end):
    """``base_end`` stretched by what the leg's stops actually hold the car for.

    The planning estimate (``scheduler.estimate_job_end_time``) is pickup +
    drive and never reads ``LegStop``: a four-hour charter would be measured as
    a twelve-minute transfer and could never clash with anything. A charter
    stop keeps the car from its start (or the pickup) for its booked hours; any
    other stop adds its dwell to the drive. Uses the prefetched stops when the
    caller prefetched them.
    """
    if leg.pickup_time is None or base_end is None:
        return base_end
    try:
        stops = list(leg.legstop_set.all())
    except Exception:
        return base_end
    if not stops:
        return base_end
    pickup = datetime.combine(day, leg.pickup_time)
    dwell = 0
    end = base_end
    for stop in stops:
        minutes = stop.duration_minutes or 0
        if stop.stop_type == "charter":
            begin = datetime.combine(day, stop.start_time) if stop.start_time else pickup
            if begin < pickup:
                begin = pickup
            end = max(end, begin + timedelta(minutes=minutes))
        else:
            dwell += minutes
    return max(end, base_end + timedelta(minutes=dwell))


def stamp_ends(legs, day):
    """Set ``_estimated_end_dt`` (honoured by ``build_driver_schedules``) to
    the stop-aware end on each leg, so The day's blocks and every booking check
    measure a charter the same way. Call with the timing cache warm."""
    from dispatching import scheduler

    for leg in legs:
        if leg.pickup_time is None:
            continue
        try:
            base = scheduler.estimate_job_end_time(leg, day)
        except Exception:
            continue
        leg._estimated_end_dt = stop_aware_end(leg, day, base)


def _leg_window(leg, day):
    """(start, end) with the timing cache already warm."""
    from dispatching import scheduler

    start = datetime.combine(day, leg.pickup_time)
    try:
        end = stop_aware_end(leg, day, scheduler.estimate_job_end_time(leg, day))
    except Exception:
        end = None
    if end is None or end <= start:
        end = start + timedelta(minutes=30)
    return start, end


def leg_span(leg, day=None):
    """(start, end) of one trip on the planning clock, or None without a time."""
    day = day or leg.pickup_date
    if leg.pickup_time is None or day is None:
        return None
    warmed = _warm_timing()
    try:
        return _leg_window(leg, day)
    finally:
        _cool_timing(warmed)


def trip_spans(day, driver_ids, *, exclude_leg_id=None):
    """Every trip ``driver_ids`` run on ``day``, as the blocks The day draws.

    Same two cancelled exclusions and the same estimator as
    ``fleet_day.load_car_day``, so "this trip overlaps the booking" here and a
    block drawn over the booking there are the same fact.
    """
    from dispatching import scheduler
    from dispatching.fleet_day import slot_datetimes
    from drivers.models import Driver
    from reservations.models import Leg

    driver_ids = [d for d in driver_ids if d]
    if not driver_ids:
        return []
    legs = (Leg.objects.filter(pickup_date=day, driver_id__in=driver_ids)
            .exclude(reservation__status__in=CANCELLED_RESERVATION)
            .exclude(status="cancelled")
            .select_related("driver", "reservation", "reservation__customer",
                            "reservation__vehicle", "vehicle", "flight_information")
            .prefetch_related("legflight_set__flight", "legstop_set",
                              "reservation__payments")
            .order_by("pickup_time", "id"))
    if exclude_leg_id:
        legs = legs.exclude(id=exclude_leg_id)
    legs = list(legs)
    if not legs:
        return []
    drivers = list(Driver.objects.filter(id__in=driver_ids).select_related("profile"))
    warmed = _warm_timing()
    try:
        stamp_ends(legs, day)
        schedules = scheduler.build_driver_schedules(legs, drivers, day)
    finally:
        _cool_timing(warmed)

    names = {d.id: str(d).strip() for d in drivers}
    out = []
    for driver_id, schedule in schedules.items():
        for slot in schedule.slots:
            start, end = slot_datetimes(slot, day)
            out.append({
                "leg_id": slot.leg_id,
                "driver_id": driver_id,
                "driver": names.get(driver_id, ""),
                "start": start,
                "end": end,
                "start_label": strf(start, "%-I:%M %p"),
                "end_label": strf(end, "%-I:%M %p"),
                "customer": slot.customer_name,
            })
    out.sort(key=lambda t: (t["start"], t["leg_id"]))
    return out


def unit_holder_ids(vehicle, day):
    """Active in-house chauffeurs holding ``vehicle`` on ``day``."""
    from drivers.models import DriverVehicleAssignment

    return [a.driver_id for a in
            DriverVehicleAssignment.objects.filter(date=day, vehicle=vehicle)
            .select_related("driver")
            if a.driver and a.driver.is_active and a.driver.driver_type == "inhouse"]


def unit_trip_spans(vehicle, day):
    """Every trip on one car on one day, across all of its holders."""
    return trip_spans(day, unit_holder_ids(vehicle, day))


def trips_in(start, end, trips):
    """The trips out of ``trips`` that [start, end) runs into."""
    return [t for t in trips if intervals_overlap(start, end, t["start"], t["end"])]


def day_vehicle(driver, day):
    """The car ``driver`` holds on ``day``, or None."""
    from drivers.models import DriverVehicleAssignment

    if driver is None or getattr(driver, "driver_type", None) != "inhouse":
        return None
    row = (DriverVehicleAssignment.objects.filter(driver=driver, date=day)
           .select_related("vehicle").first())
    return row.vehicle if row and row.vehicle_id else None


# ════════════════════════════════════════════════════════════════════════════
# The checks dispatch runs
# ════════════════════════════════════════════════════════════════════════════

def _clash_dict(booking):
    return {
        "booking": booking,
        "hard": booking.is_hard,
        "code": "vehicle_booking_hard" if booking.is_hard else "vehicle_booking",
        "text": clash_text(booking),
    }


def booking_clash(leg, driver=None, *, vehicle=None, day=None):
    """Would putting ``leg`` on ``driver`` land it on a booked window?

    Returns ``{"booking", "hard", "text", "code"}`` for the worst clash (a hard
    one before a soft one), or None. Costs one indexed query when the car has
    no bookings that day — which is almost every car on almost every day — and
    only estimates the trip's end when there is something to measure against.
    """
    day = day or leg.pickup_date
    if day is None or leg.pickup_time is None:
        return None
    if vehicle is None:
        vehicle = day_vehicle(driver, day)
    if vehicle is None:
        return None
    bookings = bookings_for(day, [vehicle.id]).get(vehicle.id, [])
    if not bookings:
        return None
    window = leg_span(leg, day)
    if window is None:
        return None
    hits = overlapping(bookings, *window)
    if not hits:
        return None
    return _clash_dict(hits[0])


def check_assign(leg, driver):
    """The front door's rule: raise ``HardBookingRefused`` when ``leg`` on
    ``driver`` lands inside a HARD booking; return the SOFT clash (or None) so
    an interactive caller can ask first."""
    if driver is None:
        return None
    clash = booking_clash(leg, driver)
    if clash and clash["hard"]:
        raise HardBookingRefused(clash)
    return clash


def pair_clashes(pairs):
    """``{leg_id: clash}`` for every (leg, driver) pair in a batch that lands on
    a booking — for the paths that write ``leg.driver`` in bulk (auto-assign
    apply, draft publish, snapshot restore) and the previews that list what an
    apply would refuse.

    Three queries per date however long the batch: the chauffeurs' cars, those
    cars' bookings, and nothing else unless a car is actually booked — only
    then is a trip's end estimated.
    """
    from drivers.models import DriverVehicleAssignment

    by_day = defaultdict(list)
    for leg, driver in pairs:
        if (driver is None or leg.pickup_time is None or leg.pickup_date is None
                or getattr(driver, "driver_type", None) != "inhouse"):
            continue
        by_day[leg.pickup_date].append((leg, driver))

    out = {}
    for day, day_pairs in by_day.items():
        cars = {
            a.driver_id: a.vehicle
            for a in DriverVehicleAssignment.objects.filter(
                date=day, driver_id__in={d.id for _, d in day_pairs},
                vehicle__isnull=False).select_related("vehicle")
        }
        booked = bookings_for(day, {v.id for v in cars.values()})
        if not booked:
            continue
        warmed = _warm_timing()
        try:
            for leg, driver in day_pairs:
                car = cars.get(driver.id)
                bookings = booked.get(car.id) if car is not None else None
                if not bookings:
                    continue
                hits = overlapping(bookings, *_leg_window(leg, day))
                if hits:
                    out[leg.id] = _clash_dict(hits[0])
        finally:
            _cool_timing(warmed)
    return out


def driver_clash(driver, vehicle, day):
    """Handing ``vehicle`` to ``driver`` for ``day``: which of the chauffeur's
    trips land on the car's bookings?

    Returns ``{"hard", "text", "bookings": [...]}`` or None. Only this
    chauffeur's trips are counted — a co-driver's trips were already on the car
    before this assignment and are not this dispatcher's doing.
    """
    bookings = bookings_for(day, [vehicle.id]).get(vehicle.id, [])
    if not bookings:
        return None
    trips = trip_spans(day, [driver.id])
    if not trips:
        return None
    found = []
    for booking in bookings:
        hit = trips_in(*span(booking), trips)
        if hit:
            found.append((booking, hit))
    if not found:
        return None
    found.sort(key=lambda pair: (not pair[0].is_hard, pair[0].start_time))
    hard = any(b.is_hard for b, _ in found)
    unit = f"Vehicle #{vehicle.vehicle_number}"
    who = str(driver).strip() or "This chauffeur"
    parts = []
    for booking, hit in found:
        times = and_list([t["start_label"] for t in hit])
        parts.append(f"from {booking.window_label()} for {booking.title()}"
                     f"{' (hard booking)' if booking.is_hard else ''} — "
                     f"{who}'s {times} trip{'s' if len(hit) != 1 else ''} "
                     f"{'fall' if len(hit) != 1 else 'falls'} inside it")
    text = f"{unit} is booked {'; and '.join(parts)}."
    if hard:
        text += (" A hard booking means the car can't be used then — move those "
                 "trips, pick another car, or ask fleet to move or lift the booking.")
    return {"hard": hard, "text": text, "bookings": [b for b, _ in found]}


def pool_rows(vehicles, day):
    """``{vehicle_id: [chip, ...]}`` — the booking tags every vehicle pool
    prints beside a car. A car somebody already holds is measured against its
    holders' trips, so a tag turns to ⚠ only when a trip really sits inside
    the booking. One query when nothing is booked."""
    by_unit = bookings_for(day, [v.id for v in vehicles])
    if not by_unit:
        return {}
    from drivers.models import DriverVehicleAssignment

    holders = defaultdict(list)
    for a in (DriverVehicleAssignment.objects
              .filter(date=day, vehicle_id__in=list(by_unit))
              .select_related("driver")):
        if a.driver and a.driver.is_active and a.driver.driver_type == "inhouse":
            holders[a.vehicle_id].append(a.driver_id)
    trips = trip_spans(day, sorted({d for ids in holders.values() for d in ids}))
    out = {}
    for vehicle_id, bookings in by_unit.items():
        on_car = [t for t in trips if t["driver_id"] in holders.get(vehicle_id, ())]
        out[vehicle_id] = [chip(b, on_car) for b in bookings]
    return out


# ════════════════════════════════════════════════════════════════════════════
# Fleet-side summaries
# ════════════════════════════════════════════════════════════════════════════

def conflict_clause(trips, title):
    """'the 10:30 AM trip (Miguel) was put on this car during Tire service.'
    Which trip, whose, and on what. ``trips`` need ``start_label`` and
    ``driver``."""
    listed = and_list([trip_phrase(t) for t in trips])
    return (f"{listed} {'was' if len(trips) == 1 else 'were'} "
            f"put on this car during {title}.")


def conflict_sentence(trips, title):
    """'Booking conflict — the 10:30 AM trip (Miguel) was put on this car
    during Tire service.' What fleet reads on a booking a trip has landed on."""
    return f"Booking conflict — {conflict_clause(trips, title)}"


def conflicts_between(start, end):
    """Every active booking from ``start`` to ``end`` (inclusive) that a trip
    has landed on, soonest first.

    Returns ``[{"booking", "id", "date", "number", "window", "title",
    "is_hard", "trips", "text", "detail"}]`` — ``detail`` is ``text`` without
    its "Booking conflict —" lead, for a list already headed that way. The
    desk's band and The day's week control read this,
    so a trip dispatch puts on Thursday's tyre slot on Tuesday night is in front
    of fleet on Wednesday morning without anyone opening Thursday to look.

    The same trip clock as the strip (``trip_spans``), so the desk and The day
    can never count a different clash. One query when nothing is booked; trips
    are built only for a date where a booked car is actually held by somebody.
    """
    from drivers.models import DriverVehicleAssignment, VehicleBooking

    bookings = list(VehicleBooking.active()
                    .filter(date__range=(start, end))
                    .select_related("vehicle")
                    .order_by("date", "start_time", "id"))
    if not bookings:
        return []

    holders = defaultdict(list)
    for a in (DriverVehicleAssignment.objects
              .filter(date__range=(start, end),
                      vehicle_id__in={b.vehicle_id for b in bookings})
              .select_related("driver")):
        if a.driver and a.driver.is_active and a.driver.driver_type == "inhouse":
            holders[(a.date, a.vehicle_id)].append(a.driver_id)

    by_day = defaultdict(list)
    for booking in bookings:
        if holders.get((booking.date, booking.vehicle_id)):
            by_day[booking.date].append(booking)

    out = []
    warmed = _warm_timing()
    try:
        for day in sorted(by_day):
            day_bookings = by_day[day]
            trips = trip_spans(day, sorted({d for b in day_bookings
                                            for d in holders[(day, b.vehicle_id)]}))
            for booking in day_bookings:
                on_car = [t for t in trips
                          if t["driver_id"] in holders[(day, booking.vehicle_id)]]
                hits = trips_in(*span(booking), on_car)
                if not hits:
                    continue
                out.append({
                    "booking": booking,
                    "id": booking.id,
                    "date": day,
                    "number": booking.vehicle.vehicle_number,
                    "window": booking.window_label(),
                    "title": booking.title(),
                    "is_hard": booking.is_hard,
                    "trips": hits,
                    "text": conflict_sentence(hits, booking.title()),
                    "detail": _capital(conflict_clause(hits, booking.title())),
                })
    finally:
        _cool_timing(warmed)
    return out


def conflict_counts(conflicts):
    """``{date: n}`` out of ``conflicts_between`` — for the week control."""
    counts = defaultdict(int)
    for row in conflicts:
        counts[row["date"]] += 1
    return dict(counts)


def _capital(text):
    """First letter only — str.capitalize() would lowercase 'AM' and names."""
    return f"{text[:1].upper()}{text[1:]}" if text else text
