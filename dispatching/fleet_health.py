"""
Vehicle readiness and service-due findings.

ADVISORY ONLY. Nothing here may gate an assignment, remove a unit from a pool,
or subtract capacity. Guard A — an assignment-time per-vehicle capacity check —
was built and then deliberately removed for firing false positives off stale
per-unit data (dispatching/feasibility_guards.py:140-144), and the founder rule
in dispatching/day_setup.py:33-36 is explicit that "there is no such thing as a
car not working today". These chips tell a dispatcher something useful at 6am;
they never make the decision.

Pure functions over already-loaded data — no queries, no clock reads except the
`now` a caller passes in — so they are cheap inside a render loop and trivial to
unit-test. Same contract as pickup_policy.py.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

# ── Thresholds ──────────────────────────────────────────────────────────────

# Fuel: a 4:30am airport run leaving on a quarter tank is a dispatcher's problem,
# not a crisis. Two bands so "top it up tonight" and "do not leave" read apart.
FUEL_LOW_PCT = 25
FUEL_CRITICAL_PCT = 12

# Starter battery. Below ~12.2V a cold-morning no-start becomes plausible; below
# 11.8V it is likely. Chauffeur cars sit overnight, which is when this bites.
BATTERY_LOW_MV = 12_200
BATTERY_CRITICAL_MV = 11_800

# Telemetry older than this means the gateway is offline or unplugged — the
# readiness data below it is not trustworthy and must be labelled, not hidden.
TELEMETRY_STALE_HOURS = 12

# Compliance paperwork: how far ahead to start nagging.
EXPIRY_WARN_DAYS = 30

# Preventive maintenance: how close to due before it is worth surfacing.
SERVICE_DUE_MILES = 500
SERVICE_DUE_DAYS = 14

CRITICAL, WARN, INFO = "critical", "warn", "info"


def _chip(level, label, detail=""):
    return {"level": level, "label": label, "detail": detail}


def fuel_reading(vehicle, now):
    """
    One vehicle's fuel level, ready to render. None when the car has never
    reported one — an un-onboarded unit shows an em-dash, not an empty gauge.

    Bands come from the same two constants the readiness chip uses, so the
    column and the chip can never tell a dispatcher different stories about the
    same tank.

    ``stale`` matters more here than anywhere else on the page: fuel is the one
    reading that changes while nobody is looking. A gateway that went quiet
    yesterday afternoon reports the level it saw then, and a car that has since
    done two MCO runs is nowhere near it. The number is still worth showing —
    it's a floor, not a fiction — but it has to be labelled.
    """
    percent = vehicle.samsara_fuel_percent
    if percent is None:
        return None

    if percent <= FUEL_CRITICAL_PCT:
        level = CRITICAL
    elif percent <= FUEL_LOW_PCT:
        level = WARN
    else:
        level = INFO

    # Same age the "GPS stale" chip is computed from, so one reading can't be
    # called stale in the chip column and fresh in the fuel column.
    stale_hours = _telemetry_age_hours(vehicle, now)
    stale = stale_hours is not None and stale_hours >= TELEMETRY_STALE_HOURS
    return {
        "percent": percent,
        "level": level,
        "stale": stale,
        "detail": (
            f"{percent}% as of {_compact_hours(stale_hours)} ago — the gateway has "
            f"gone quiet, so the tank may be lower now"
            if stale else f"{percent}% at the last reading"
        ),
    }


def vehicle_readiness(vehicle, now, *, open_fault_count=None, in_shop=False):
    """
    Readiness chips for one vehicle, worst first.

    Returns [] for a car with nothing to say — an un-onboarded unit renders no
    chips at all rather than a row of grey "unknown" badges. Partial Samsara
    coverage is the designed steady state, and the UI must never punish a car
    for not having a gateway.
    """
    chips = []

    if in_shop:
        # A label, never a pool removal. See the module docstring.
        chips.append(_chip(WARN, "In shop", "scheduled out-of-service window"))

    if not vehicle.samsara_vehicle_id:
        return chips  # nothing telematic to say; stay quiet

    stale = _telemetry_age_hours(vehicle, now)
    if stale is not None and stale >= TELEMETRY_STALE_HOURS:
        chips.append(_chip(
            WARN, "GPS stale",
            f"no sample in {_compact_hours(stale)} — readings below may be old",
        ))

    faults = open_fault_count
    if faults is None:
        faults = vehicle.samsara_open_fault_count
    if faults:
        chips.append(_chip(
            CRITICAL if faults > 1 else WARN,
            f"{faults} fault{'s' if faults != 1 else ''}",
            "engine fault reported by the vehicle",
        ))

    fuel = vehicle.samsara_fuel_percent
    if fuel is not None:
        if fuel <= FUEL_CRITICAL_PCT:
            chips.append(_chip(CRITICAL, f"Fuel {fuel}%", "fuel before first pickup"))
        elif fuel <= FUEL_LOW_PCT:
            chips.append(_chip(WARN, f"Fuel {fuel}%", "top up tonight"))

    mv = vehicle.samsara_battery_millivolts
    if mv is not None:
        volts = Decimal(mv) / 1000
        if mv <= BATTERY_CRITICAL_MV:
            chips.append(_chip(CRITICAL, f"Battery {volts:.1f}V", "no-start risk"))
        elif mv <= BATTERY_LOW_MV:
            chips.append(_chip(WARN, f"Battery {volts:.1f}V", "battery running low"))

    return sorted(chips, key=lambda c: {CRITICAL: 0, WARN: 1, INFO: 2}[c["level"]])


def compliance_findings(vehicle, today):
    """Registration / insurance / inspection dates that are due or past."""
    findings = []
    for field, label in (
        ("registration_expires_on", "Registration"),
        ("insurance_expires_on", "Insurance"),
        ("next_inspection_on", "Inspection"),
    ):
        due = getattr(vehicle, field, None)
        if due is None:
            continue
        days = (due - today).days
        if days < 0:
            findings.append(_chip(CRITICAL, f"{label} expired", f"{abs(days)}d ago"))
        elif days <= EXPIRY_WARN_DAYS:
            findings.append(_chip(WARN, f"{label} due", f"in {days}d"))
    return findings


def service_findings(schedule, current_odometer_miles, today):
    """
    Is this maintenance interval due?

    Whichever comes first, miles or days. A schedule with no usable baseline
    returns nothing rather than guessing — an invented due date is worse than
    none, because someone will plan a shop day around it.
    """
    findings = []

    due_miles = schedule.due_at_odometer_miles
    if due_miles is not None and current_odometer_miles is not None:
        remaining = due_miles - Decimal(current_odometer_miles)
        label = schedule.get_service_type_display()
        if remaining <= 0:
            findings.append(_chip(
                CRITICAL, f"{label} overdue", f"{abs(int(remaining))} mi past due"))
        elif remaining <= SERVICE_DUE_MILES:
            findings.append(_chip(WARN, f"{label} due", f"in {int(remaining)} mi"))

    due_date = schedule.due_on_date
    if due_date is not None:
        days = (due_date - today).days
        label = schedule.get_service_type_display()
        if days < 0:
            findings.append(_chip(CRITICAL, f"{label} overdue", f"{abs(days)}d past due"))
        elif days <= SERVICE_DUE_DAYS:
            findings.append(_chip(WARN, f"{label} due", f"in {days}d"))

    # Miles and days can both fire; keep the more urgent one only.
    if len(findings) == 2:
        return [f for f in findings if f["level"] == CRITICAL][:1] or findings[:1]
    return findings


def feed_health(state, now):
    """
    One line describing whether Samsara data is arriving at all.

    The single most important thing this module renders. The integration was
    silently dead for ~25 days because the env var name did not match what
    settings read, and nothing anywhere said so.
    """
    if state is None or state.last_success_at is None:
        return {
            "level": CRITICAL,
            "label": "No Samsara data",
            "detail": "the feed has never reported a successful sync",
        }

    age = now - state.last_success_at
    minutes = int(age.total_seconds() // 60)
    if minutes <= 15:
        return {"level": "ok", "label": "Live",
                "detail": f"synced {_compact_minutes(minutes)}"}
    if minutes <= 60:
        return {"level": WARN, "label": "Lagging",
                "detail": f"last sync {_compact_minutes(minutes)}"}
    return {
        "level": CRITICAL,
        "label": "Feed down",
        "detail": (
            f"last successful sync {_compact_minutes(minutes)}"
            + (f" — {state.consecutive_failures} failures in a row"
               if state.consecutive_failures else "")
        ),
    }


def is_in_shop(service_records, today):
    """True when any record's out-of-service window covers `today`."""
    for record in service_records:
        start = record.out_of_service_from
        end = record.out_of_service_to
        if start and start <= today and (end is None or today <= end):
            return True
    return False


# ── helpers ─────────────────────────────────────────────────────────────────

def _telemetry_age_hours(vehicle, now):
    stamp = vehicle.samsara_last_seen_at or vehicle.samsara_odometer_at
    if stamp is None:
        return None
    return (now - stamp).total_seconds() / 3600


def _compact_hours(hours):
    if hours >= 48:
        return f"{int(hours // 24)}d"
    return f"{int(hours)}h"


def _compact_minutes(minutes):
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


def summarise_coverage(rows_with_value, total_rows, unit="mi"):
    """
    Render an aggregate honestly: "1,842 mi across 26 of 31 days".

    Any fleet total MUST carry its coverage. NULL means unknown and is excluded
    from the sum; presenting the sum without saying how many days are missing
    makes a partial month look like a full one.

    When nothing is known, say it in words. "across 0 of 1 days" sitting beside
    an em-dash reads like a broken number rather than an honest one — but a day
    genuinely cannot produce a figure until the PREVIOUS day has a closing
    odometer, so a vehicle's first polled day is legitimately blank.
    """
    if not total_rows:
        return ""
    if rows_with_value == 0:
        return "no full day yet — needs a prior day's closing odometer"
    if rows_with_value == total_rows:
        return f"across all {total_rows} days"
    return f"across {rows_with_value} of {total_rows} days"


def timedelta_days(a, b):
    """Whole days between two dates, None-safe."""
    if a is None or b is None:
        return None
    return (a - b).days if isinstance(a - b, timedelta) else None
