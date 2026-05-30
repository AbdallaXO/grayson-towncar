"""
Phase-3 feasibility guards (capacity / context-turnaround / per-driver window).

Design goals:
  * Pure, side-effect-free functions so they are trivially unit-testable and cheap to
    call inside the scheduler's hot loops. Callers fetch DB data and pass it in.
  * All policy lives behind clearly-named, trivially-flippable config flags.
  * The per-driver windows are a STUB derived from observed history (see
    docs/driver_reality_report.md). Swapping in real, production-configured windows is a
    one-line change: set USE_STUB_WINDOWS = False (then get_effective_window reads the
    live DriverWeeklySchedule the caller passes in). NEVER writes to driver records.
"""
from datetime import datetime, time as dt_time

# ── Airport terminals (kept consistent with dispatching.analytics.categorize_location) ──
AIRPORT_TERMINALS = ("MCO Terminal", "SFB Terminal")

# ════════════════════════════════════════════════════════════════════════════
# CONFIG FLAGS  (defaults chosen now; flip later without touching logic)
# ════════════════════════════════════════════════════════════════════════════

# Guard C — how end_hour is interpreted. CLEAR_BY = driver must FINISH by end_hour
# (applied consistently in check_feasibility + both assignment paths). LAST_PICKUP =
# legacy behavior (pickup must be <= end_hour).
END_HOUR_MODE = "CLEAR_BY"           # {"CLEAR_BY", "LAST_PICKUP"}

# Guard C — "flexible" means flexible on START time, but a driver with a clear-by bound
# is STILL bound by it (not all-or-nothing). Set False to make flexible bypass the end too.
FLEXIBLE_RESPECTS_CLEAR_BY = True

# Guard B — turnaround tuning (minutes).
DEPLANING_GRACE_MIN = 20             # pax deplane/collect bags => slack on airport arrival pickups
SAFETY_PAD_MIN = 10                  # global pad added on top of every turnaround

# Guard C — use the observed-history STUB windows below until real windows are configured.
USE_STUB_WINDOWS = True

# ════════════════════════════════════════════════════════════════════════════
# STUB DRIVER WINDOWS  — PROVISIONAL, from docs/driver_reality_report.md (observed
# history Feb–May 2026). OPTIMISTIC (captures what a driver did, not hard limits).
# Replace with real configured windows in prod, then set USE_STUB_WINDOWS = False.
# {driver_id: {"start": hour, "end": hour, "max_hours": float|None}}
# ════════════════════════════════════════════════════════════════════════════
STUB_DRIVER_WINDOWS = {
    1:  {"start": 0,  "end": 23, "max_hours": 22},   # Rayyan Vorajee
    6:  {"start": 5,  "end": 23, "max_hours": 6},    # placeholder (likely should be deactivated)
    9:  {"start": 0,  "end": 23, "max_hours": 9},    # Abdalla
    20: {"start": 3,  "end": 22, "max_hours": 14},   # Carlos Medina
    26: {"start": 2,  "end": 21, "max_hours": 23},   # neuma
    31: {"start": 4,  "end": 23, "max_hours": 16},   # Julio Bonilla
    32: {"start": 0,  "end": 23, "max_hours": 23},   # roberto
    33: {"start": 3,  "end": 23, "max_hours": 18},   # runer
    34: {"start": 7,  "end": 23, "max_hours": 16},   # shipo
    35: {"start": 0,  "end": 23, "max_hours": 16},   # Hasan
    38: {"start": 2,  "end": 23, "max_hours": 16},   # Angel Almanzar
    46: {"start": 6,  "end": 20, "max_hours": 14},   # Yovanny Suarez
    48: {"start": 2,  "end": 20, "max_hours": 17},   # Michael Olmo
    49: {"start": 4,  "end": 23, "max_hours": 17},   # alex
    51: {"start": 0,  "end": 23, "max_hours": 24},   # David Encarancion
    52: {"start": 3,  "end": 22, "max_hours": 16},   # Junaid Baidr
    53: {"start": 3,  "end": 22, "max_hours": 17},   # sereen
    54: {"start": 0,  "end": 23, "max_hours": 16},   # Steven Kleisath
    55: {"start": 4,  "end": 23, "max_hours": 17},   # rizwan
    56: {"start": 3,  "end": 23, "max_hours": 18},   # george
    57: {"start": 3,  "end": 23, "max_hours": 17},   # Seline
    58: {"start": 0,  "end": 23, "max_hours": 20},   # ken
    59: {"start": 1,  "end": 23, "max_hours": 20},   # Aftab
    61: {"start": 5,  "end": 23, "max_hours": 15},   # Idrees
    62: {"start": 1,  "end": 23, "max_hours": 21},   # shelley
    63: {"start": 4,  "end": 19, "max_hours": 13},   # mesfin
    64: {"start": 7,  "end": 23, "max_hours": 15},   # Raymond
    65: {"start": 8,  "end": 23, "max_hours": 15},   # HassanA
}


# NOTE: Guard A (physical-capacity fit) was intentionally REMOVED. Booking-time
# validation already enforces party/luggage/car-seat limits against the booked vehicle
# type, and an assignment-time capacity check fired false positives off stale per-vehicle
# seat-count data (e.g. a real 14-pax-van booking on a FleetVehicle whose rates.Vehicle
# capacity read 5). Guards here are B (turnaround) + C (window) only.


# ════════════════════════════════════════════════════════════════════════════
# GUARD B — context-dependent turnaround
# ════════════════════════════════════════════════════════════════════════════
def required_turnaround(reposition_drive_min, next_is_airport_arrival, same_terminal,
                        deplaning_grace=None, safety_pad=None):
    """Minutes required between the previous job's clear time and the next pickup.

    Rules:
      * airport-drop -> airport ARRIVAL at the SAME terminal: ~0 reposition (deplaning is slack)
      * resort/other -> airport ARRIVAL pickup: real drive time minus deplaning grace
      * anything -> non-arrival (incl. Port Canaveral): full real drive time each way
      * + a global safety pad on top of every turnaround.
    `reposition_drive_min` is the category drive time the caller computed.
    """
    dg = DEPLANING_GRACE_MIN if deplaning_grace is None else deplaning_grace
    pad = SAFETY_PAD_MIN if safety_pad is None else safety_pad
    if next_is_airport_arrival:
        base = 0 if same_terminal else max(0, reposition_drive_min - dg)
    else:
        base = reposition_drive_min
    return base + pad


def is_airport_arrival(trip_type, pickup_category):
    return trip_type == "arrival" and pickup_category in AIRPORT_TERMINALS


# ════════════════════════════════════════════════════════════════════════════
# GUARD C — per-driver window (stub-backed)
# ════════════════════════════════════════════════════════════════════════════
def get_effective_window(driver_id, configured=None):
    """Return the window dict to enforce for this driver.

    {"start": h, "end": h, "max_hours": float|None, "flexible": bool}
    When USE_STUB_WINDOWS: use the observed-history stub (flexible=False so it actually
    binds during testing). Otherwise use the caller-supplied `configured` window (the real
    DriverWeeklySchedule-derived dict). Returns None if nothing is known (=> no window guard).
    """
    if USE_STUB_WINDOWS:
        w = STUB_DRIVER_WINDOWS.get(driver_id)
        if not w:
            return None
        return {"start": w["start"], "end": w["end"], "max_hours": w["max_hours"], "flexible": False}
    return configured


def window_check(window, pickup_time, clear_dt, span_hours_after,
                 target_date=None, mode=None, flexible_respects_clear_by=None):
    """(ok, reason) for whether adding a leg respects the driver's window + max_hours.

    window: {"start", "end", "max_hours", "flexible"}; None => skip.
    pickup_time: datetime.time of the new leg's pickup.
    clear_dt: datetime when the new leg clears (finishes).
    span_hours_after: driver's day span (first pickup -> last clear) IF this leg is added.
    target_date: the schedule date; used to build an ABSOLUTE clear-by datetime so a leg
        that clears AFTER MIDNIGHT (e.g. a 22:30 pickup clearing 00:30 next day) is correctly
        judged a clear-by violation rather than evading it via a bare hour comparison.
    """
    if not window:
        return True, ""
    mode = END_HOUR_MODE if mode is None else mode
    frcb = FLEXIBLE_RESPECTS_CLEAR_BY if flexible_respects_clear_by is None else flexible_respects_clear_by
    flexible = bool(window.get("flexible", False))
    start = window.get("start")
    end = window.get("end")
    max_h = window.get("max_hours")

    # START bound — bypassed for flexible drivers (flexible on start time).
    if not flexible and start is not None and pickup_time.hour < start:
        return False, f"pickup {pickup_time.strftime('%H:%M')} before start {start}:00"

    # END bound.
    if end is not None:
        if mode == "CLEAR_BY":
            enforce_end = (not flexible) or frcb
            if enforce_end and clear_dt is not None:
                # must FINISH by end:00 (a clear exactly at end:00 is OK).
                if target_date is not None:
                    clear_by_dt = datetime.combine(target_date, dt_time(min(int(end), 23), 0))
                    over = clear_dt > clear_by_dt   # correctly catches next-day (after-midnight) clears
                else:  # same-day fallback only (no date supplied)
                    over = clear_dt.hour > end or (clear_dt.hour == end and clear_dt.minute > 0)
                if over:
                    return False, f"clears {clear_dt.strftime('%H:%M')} after clear-by {end}:00"
        else:  # LAST_PICKUP
            if not flexible and pickup_time.hour > end:
                return False, f"pickup {pickup_time.strftime('%H:%M')} after last-pickup {end}:00"

    # MAX HOURS — hard cap on day span (wall-clock first pickup -> last clear).
    if max_h is not None and span_hours_after is not None and span_hours_after > float(max_h):
        return False, f"day span {span_hours_after:.1f}h > max_hours {max_h}"

    return True, ""
