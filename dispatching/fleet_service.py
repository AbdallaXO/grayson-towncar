"""
The Service board: every unit's maintenance intervals on one screen.

Why this exists. Setting an interval used to mean opening one vehicle page at a
time and finding the panel near the bottom, so in practice intervals went
unset — and an odometer we collect every three minutes had nothing to feed.
This module builds the whole fleet's intervals as one grid: units down, service
types across, so a gap is visible as a gap.

DB-only and pure, like every other fleet loader: nothing here calls Samsara.

WHO DECIDES WHAT. Due-ness is not recomputed here. ``fleet_health.service_findings``
answers "is this due", exactly as the vehicle list and the desk ask it, and this
module takes the level it returns verbatim — so a cell and a readiness chip can
never disagree about the same car. What this module owns is only what the cell
SAYS: the wording, and the thousands separators a dense grid needs.
"""
from __future__ import annotations

from decimal import Decimal

from django.urls import reverse

from dispatching import fleet_capacity, fleet_health
from drivers.models import VehicleServiceSchedule

# The four every car in this fleet actually gets. They are always columns, even
# when no car has one yet — a missing oil interval is the thing this page is for
# showing, and it cannot show it if the column only appears once it is set.
EVERYDAY_SERVICES = ("oil", "tires", "brakes", "inspection")

SERVICE_LABELS = dict(VehicleServiceSchedule.SERVICE_TYPE_CHOICES)


def _interval_text(schedule):
    """
    '5,000 mi · 180d' — whichever halves this interval has.

    Terse because four of these sit side by side in the grid. "180 days"
    spelled out is what pushed the last column off a laptop screen.
    """
    parts = []
    if schedule.interval_miles:
        parts.append(f"{schedule.interval_miles:,} mi")
    if schedule.interval_days:
        parts.append(f"{schedule.interval_days}d")
    return " · ".join(parts)


def _fmt_date(day):
    """'Mar 4' — short, no leading zero, no year for a date this close."""
    return f"{day:%b} {day.day}"


def _remaining(schedule, odometer, today):
    """
    Miles and days left before this falls due. Either may be None.

    Miles are None when the car has never reported an odometer — which is NOT
    the same as being at zero miles left, and is why this returns None rather
    than a number the grid would happily colour red.
    """
    miles = days = None
    due_miles = schedule.due_at_odometer_miles
    if due_miles is not None and odometer is not None:
        miles = int(due_miles - Decimal(odometer))
    due_date = schedule.due_on_date
    if due_date is not None:
        days = (due_date - today).days
    return miles, days


def _due_text(schedule, odometer, today):
    """
    What the second line of a cell reads.

    The thresholds mirror ``fleet_health``'s, and deliberately so: this picks the
    wording for a state that module has already decided. If they ever disagree
    the state wins — it is what colours the cell and what the strip counts.
    """
    miles, days = _remaining(schedule, odometer, today)

    if miles is not None and miles <= 0:
        return f"{abs(miles):,} mi past due"
    if days is not None and days < 0:
        return f"{abs(days)}d past due"
    if miles is not None and miles <= fleet_health.SERVICE_DUE_MILES:
        return f"in {miles:,} mi"
    if days is not None and days <= fleet_health.SERVICE_DUE_DAYS:
        return f"in {days}d"

    # Not due for a while. Say where the line is rather than how far off it is:
    # "55,000 mi" is something you can check against the dash; "in 4,000 miles"
    # is a number you would have to do arithmetic on to use. One "due" for both
    # halves — the column already says what kind of service this is.
    bits = []
    if schedule.due_at_odometer_miles is not None:
        bits.append(f"{int(schedule.due_at_odometer_miles):,} mi")
    if schedule.due_on_date is not None:
        bits.append(_fmt_date(schedule.due_on_date))
    return "due " + " · ".join(bits)


def _urgency(schedule, odometer, today):
    """
    How far past due, as one comparable number, so the strip can rank a car
    overdue on miles against one overdue on days.

    Each half is measured against its own threshold — 500 miles or 14 days — and
    the worse half wins. A car 5,000 miles past an oil change scores 10; one 100
    miles past scores 0.2. It is a ranking, not a measurement, and it exists only
    to decide what a fleet manager reads first.
    """
    miles, days = _remaining(schedule, odometer, today)
    scores = [0.0]
    if miles is not None and miles <= 0:
        scores.append(abs(miles) / fleet_health.SERVICE_DUE_MILES)
    if days is not None and days < 0:
        scores.append(abs(days) / fleet_health.SERVICE_DUE_DAYS)
    return max(scores)


def _cell(service_type, schedule, odometer, today):
    """One square of the grid."""
    base = {
        "service_type": service_type,
        "label": SERVICE_LABELS.get(service_type, service_type),
        "schedule": schedule,
    }

    if schedule is None:
        # Nothing set. Said out loud rather than left blank, because a blank
        # square in a grid of numbers reads as "fine", and this one means
        # "nothing here can ever come due".
        return {**base, "state": "unset", "interval_text": "", "due_text": ""}

    if schedule.due_at_odometer_miles is None and schedule.due_on_date is None:
        # An interval with nothing to count from. Honest and inert: a made-up
        # baseline is worse than none, because a due date computed from it
        # looks exactly like one computed from a real service.
        return {**base, "state": "no_baseline",
                "interval_text": _interval_text(schedule),
                "due_text": "no baseline"}

    findings = fleet_health.service_findings(schedule, odometer, today)
    return {
        **base,
        "state": findings[0]["level"] if findings else "ok",
        "interval_text": _interval_text(schedule),
        "due_text": _due_text(schedule, odometer, today),
    }


def cell_payload(schedule, vehicle, today=None):
    """
    One square, as JSON, for the board to repaint after a save.

    Built by the same ``_cell`` the page is rendered from, so a square that has
    just been edited says exactly what it would say on a fresh page load. The
    vehicle detail page ignores this and reloads; only the grid uses it.
    """
    from django.utils import timezone

    cell = _cell(schedule.service_type, schedule, vehicle.odometer_miles,
                 today or timezone.localdate())
    return {
        "service_type": cell["service_type"],
        "state": cell["state"],
        "interval_text": cell["interval_text"],
        "due_text": cell["due_text"],
        "schedule_id": schedule.id,
    }


def load_service_board(today=None):
    """
    Everything the Service page renders, as one dict.

    Keys: ``columns``, ``rows``, ``due_now``, ``due_soon``, ``no_intervals``,
    ``no_baseline``.
    """
    from django.utils import timezone

    today = today or timezone.localdate()

    units = sorted(
        fleet_capacity.fleet_units(),
        key=lambda u: fleet_capacity.natural_unit_key(u.vehicle_number),
    )

    by_unit = {}
    for schedule in VehicleServiceSchedule.objects.filter(
        vehicle__in=units, is_active=True
    ):
        by_unit.setdefault(schedule.vehicle_id, {})[schedule.service_type] = schedule

    # Columns: the everyday four, plus any rarer type a car actually has, in the
    # model's own order. A fleet with no transmission intervals does not carry an
    # empty transmission column across nineteen rows.
    in_use = {t for schedules in by_unit.values() for t in schedules}
    extra = [t for t, _ in VehicleServiceSchedule.SERVICE_TYPE_CHOICES
             if t not in EVERYDAY_SERVICES and t in in_use]
    columns = [{"service_type": t, "label": SERVICE_LABELS[t]}
               for t in list(EVERYDAY_SERVICES) + extra]

    rows, due_now, due_soon = [], [], []
    no_intervals = no_baseline = 0

    for unit in units:
        schedules = by_unit.get(unit.id, {})
        if not schedules:
            no_intervals += 1

        odometer = unit.odometer_miles
        cells = []
        for column in columns:
            service_type = column["service_type"]
            cell = _cell(service_type, schedules.get(service_type), odometer, today)
            cells.append(cell)

            if cell["state"] == "no_baseline":
                no_baseline += 1
            elif cell["state"] in (fleet_health.CRITICAL, fleet_health.WARN):
                item = {
                    "unit": unit.vehicle_number,
                    "href": reverse("fleet_detail", args=[unit.pk]),
                    "label": cell["label"],
                    "detail": cell["due_text"],
                    "urgency": _urgency(schedules[service_type], odometer, today),
                }
                (due_now if cell["state"] == fleet_health.CRITICAL
                 else due_soon).append(item)

        rows.append({
            "vehicle": unit,
            # None stays None: an unknown odometer renders as an em-dash, never
            # as 0, which would make a dead gateway look like a new car.
            "odometer": odometer,
            "odometer_estimated": unit.odometer_is_estimate,
            "cells": cells,
        })

    due_now.sort(key=lambda i: -i["urgency"])
    due_soon.sort(key=lambda i: i["detail"])

    return {
        "columns": columns,
        "rows": rows,
        "due_now": due_now,
        "due_soon": due_soon,
        "no_intervals": no_intervals,
        "no_baseline": no_baseline,
    }


# ════════════════════════════════════════════════════════════════════════════
# Forecast — when each service falls due at the rate the car is actually working
# ════════════════════════════════════════════════════════════════════════════

# How much history the mileage rate is averaged over. The same window the desk
# and the vehicle list use, so a date here and a date there cannot disagree.
RATE_WINDOW_DAYS = 30

HORIZONS = (30, 60, 90)
DEFAULT_HORIZON = 60

# Why an interval cannot be projected, in the order a person would want to hear
# them — commonest and most fixable first. Phrased here rather than stitched
# together by template tags, which left a space before the full stop.
BLIND_SPOT_REASONS = (
    ("no_baseline", "with no last service to count from"),
    ("no_rate", "with no recent mileage"),
    ("not_moving", "on a car that is not moving"),
    ("no_odometer", "with no odometer reading"),
)


def _rate_by_vehicle(unit_ids, today):
    """Average miles per known day over the rate window, per unit.

    Deliberately the same aggregate as ``fleet_desk``: a NULL day is unknown and
    leaves the denominator, a 0 day is a car that provably sat and stays in it.
    """
    from datetime import timedelta

    from django.db.models import Count, Q, Sum
    from drivers.models import VehicleDayReading

    window_start = today - timedelta(days=RATE_WINDOW_DAYS)
    rates = {}
    for row in (VehicleDayReading.objects
                .filter(vehicle_id__in=unit_ids, date__gte=window_start, date__lte=today)
                .values("vehicle_id")
                .annotate(miles=Sum("miles_driven"),
                          known=Count("id", filter=Q(miles_driven__isnull=False)))):
        if row["miles"] is not None and row["known"]:
            rates[row["vehicle_id"]] = (
                Decimal(row["miles"]) / row["known"]).quantize(Decimal("0.1"))
    return rates


def _bucket(day, today):
    """Which heading a projected date sits under."""
    days = (day - today).days
    if days < 0:
        return "Overdue"
    if days < 7:
        return "This week"
    if days < 14:
        return "Next week"
    return f"{day:%B}" if day.year == today.year else f"{day:%B %Y}"


def _project(schedule, odometer, per_day, today):
    """
    When this interval falls due: ``(date, basis)``, or ``(None, reason)``.

    Whichever comes first, miles or days — the model's own rule. A mileage half
    needs a baseline, a current odometer AND a rate the car is actually working
    at; ``days_to_cover`` refuses a parked car rather than returning a date
    years out, because someone will book a shop day against whatever it says.
    """
    from datetime import timedelta

    from dispatching.mileage import days_to_cover

    by_date = schedule.due_on_date
    due_miles = schedule.due_at_odometer_miles

    by_miles = None
    miles_reason = None
    if due_miles is None:
        miles_reason = None if by_date is not None else "no_baseline"
    elif odometer is None:
        miles_reason = "no_odometer"
    elif per_day is None:
        miles_reason = "no_rate"
    else:
        days_out = days_to_cover(due_miles - Decimal(odometer), per_day)
        if days_out is None:
            miles_reason = "not_moving"
        else:
            by_miles = today + timedelta(days=days_out)

    if by_miles is not None and by_date is not None:
        return (by_miles, "rate") if by_miles <= by_date else (by_date, "date")
    if by_miles is not None:
        return by_miles, "rate"
    if by_date is not None:
        return by_date, "date"
    return None, (miles_reason or "no_baseline")


def load_forecast(today=None, horizon_days=DEFAULT_HORIZON):
    """
    Every service the fleet will need inside the horizon, soonest first.

    Keys: ``items``, ``unprojectable`` (counts by reason), ``blind_spots``
    (those counts as phrases), ``unprojectable_total``, ``horizon_days``,
    ``horizons``.

    An interval that cannot be projected is COUNTED, never silently dropped.
    Omitting them would turn "we do not know" into what reads as a quiet
    quarter, which is the one way this page could do real damage.
    """
    from datetime import timedelta

    from django.utils import timezone

    today = today or timezone.localdate()
    if horizon_days not in HORIZONS:
        horizon_days = DEFAULT_HORIZON
    limit = today + timedelta(days=horizon_days)

    units = fleet_capacity.fleet_units()
    by_id = {u.id: u for u in units}
    rates = _rate_by_vehicle(list(by_id), today)

    items = []
    unprojectable = {"no_baseline": 0, "no_odometer": 0, "no_rate": 0, "not_moving": 0}

    for schedule in (VehicleServiceSchedule.objects
                     .filter(vehicle__in=units, is_active=True)):
        unit = by_id[schedule.vehicle_id]
        day, basis = _project(
            schedule, unit.odometer_miles, rates.get(unit.id), today)

        if day is None:
            if basis in unprojectable:
                unprojectable[basis] += 1
            continue
        if day > limit:
            continue

        # Already past due projects to TODAY, not to a date in the past —
        # days_to_cover returns 0 for a negative remainder and leaves the
        # wording to its caller. Whether it IS past due comes from
        # fleet_health, the same call that colours the square red, so the two
        # views cannot disagree about one car.
        overdue = any(
            f["level"] == fleet_health.CRITICAL
            for f in fleet_health.service_findings(schedule, unit.odometer_miles, today)
        )

        items.append({
            "unit": unit.vehicle_number,
            "vehicle": unit,
            "href": reverse("fleet_detail", args=[unit.pk]),
            "label": SERVICE_LABELS.get(schedule.service_type, schedule.service_type),
            "service_type": schedule.service_type,
            "date": day,
            "days_out": (day - today).days,
            "bucket": "Overdue" if overdue else _bucket(day, today),
            "overdue": overdue,
            # "rate" is an ESTIMATE off how hard the car is working; "date" is
            # a real deadline off the calendar. The page must not print them
            # the same way.
            "basis": basis,
            "per_day": rates.get(unit.id) if basis == "rate" else None,
        })

    items.sort(key=lambda i: (not i["overdue"], i["date"],
                              fleet_capacity.natural_unit_key(i["unit"])))

    return {
        "items": items,
        "unprojectable": unprojectable,
        "blind_spots": [f"{unprojectable[key]} {phrase}"
                        for key, phrase in BLIND_SPOT_REASONS if unprojectable[key]],
        "unprojectable_total": sum(unprojectable.values()),
        "horizon_days": horizon_days,
        "horizons": HORIZONS,
    }
