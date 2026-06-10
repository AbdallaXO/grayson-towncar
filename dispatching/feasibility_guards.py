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

# Guard C — a "flexible" driver works AND finishes anytime (founder rule: flexible = no
# fixed start and no clear-by). So flexible bypasses BOTH the start and the end/clear-by
# bound. Set True to revert to the old behavior (flexible respects a clear-by end).
FLEXIBLE_RESPECTS_CLEAR_BY = False

# Guard B — turnaround tuning (minutes).
# Slack on airport-ARRIVAL pickups. The pickup time is the flight's GATE arrival; the driver only
# needs to be at baggage claim ~this many minutes after it (they clear the cell-lot/security
# checkpoint while pax deplane + collect bags, so they meet curbside on time). Founder rule:
# driver there 10–15 min max after gate; pax curbside 10–20. Set to 15 (top of range) so the
# engine stops farming legs over phantom 1–15 min "lateness" that never happens in reality.
DEPLANING_GRACE_MIN = 15
# Global pad added on top of every turnaround. Set to 0 by founder decision: dispatch
# monitors/adjusts jobs live, so the engine should allow tight back-to-back turnarounds
# (a turnaround needs only the real drive time). check_feasibility still WARNS on tight
# (<15 min) turns and still hard-rejects true overlaps. Raise this to re-introduce slack.
SAFETY_PAD_MIN = 0

# Guard C — use the observed-history STUB windows below until real windows are configured.
USE_STUB_WINDOWS = True

# ── Duty-span caps (Span Governor, 2026-06: docs/scheduler-automation/auto-assign-hour-
# balancing-design.md). The Guard C max_hours gate has always existed; these flags fix the
# VALUES it enforces. Calibrated to the founder's own hand-built boards (39 driver-days:
# raw span median 12.3h / p90 15.2h / max 16.5h; max CONTINUOUS duty ~13.5h).
# ENFORCE_SPAN_CAPS=False restores byte-identical pre-cap behavior everywhere.
ENFORCE_SPAN_CAPS = True
# HARD ceiling on RAW span (first pickup -> last clear), enforced via window_check.
# 17h never blocks anything the founder built himself (his max: 16.5h, a split-day); it
# trims only the optimistic stub values (David 24h, roberto/neuma 23h, ...). A tighter
# stub/configured/modal value still wins via min().
SPAN_HARD_HOURS_DEFAULT = 17.0
# SOFT steering on EFFECTIVE span (raw span minus one PRE-EXISTING internal break of
# >= SPAN_GAP_CREDIT_MIN_MIN minutes, credit capped at SPAN_GAP_CREDIT_MAX_MIN). Marginal
# progressive pricing in the builder scoring: free under FREE_HOURS, SPAN_SOFT_RATE pts/hr
# from FREE_HOURS to SPAN_SOFT_EFFECTIVE_HOURS, SPAN_STEEP_RATE pts/hr beyond. The target
# is STRICTLY-GREATER (a founder-built 13.5h-effective day pays no steep rate).
SPAN_SOFT_PRICING = True
SPAN_SOFT_FREE_HOURS = 12.0
SPAN_SOFT_EFFECTIVE_HOURS = 13.5
SPAN_SOFT_RATE = 25           # tiebreak-scale: loses to chain/coherence/scarcity bonuses
SPAN_STEEP_RATE = 120         # an hour past target outweighs any single bonus; never gates
SPAN_SEEDER_RATE_SCALE = 0.5  # build_smart_schedule seats only on score>0 — half rate so the
                              # span term steers Build-1st days without silently dropping legs
SPAN_GAP_CREDIT_MIN_MIN = 120  # a >=2h hole is a real off-duty break (founder split-days: 3-5h)
SPAN_GAP_CREDIT_MAX_MIN = 300  # cap the credit so one 6h hole can't excuse a 20h day

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
      * airport ARRIVAL pickup: the driver only needs to reach the curb by gate-arrival +
        deplaning grace (pax are deplaning + collecting bags), so the FULL grace is credited —
        even for a short same-terminal hop. The result may go NEGATIVE, meaning the pickup can be
        slightly before the driver clears the previous job (he's already at the airport and the
        pax aren't out yet). This is what stops the engine farming same-airport turns it wrongly
        called "impossible" (e.g. drop a return at MCO 1:35, grab a 1:34 MCO arrival — fine).
      * anything -> non-arrival (incl. Port Canaveral / returns / departures): full real drive
        time each way, NO grace (the passenger is waiting, the driver must be on time).
      * + a global safety pad on top of every turnaround.
    `reposition_drive_min` is the category/live drive time the caller computed.
    """
    dg = DEPLANING_GRACE_MIN if deplaning_grace is None else deplaning_grace
    pad = SAFETY_PAD_MIN if safety_pad is None else safety_pad
    if next_is_airport_arrival:
        base = (0 if same_terminal else reposition_drive_min) - dg   # full deplaning credit; may be < 0
    else:
        base = reposition_drive_min
    return base + pad


def is_airport_arrival(trip_type, pickup_category):
    return trip_type == "arrival" and pickup_category in AIRPORT_TERMINALS


# ════════════════════════════════════════════════════════════════════════════
# GUARD C — per-driver window (stub-backed)
# ════════════════════════════════════════════════════════════════════════════
def _capped_max_hours(*values):
    """min() over the non-None candidate max_hours values plus the global hard default."""
    candidates = [float(v) for v in values if v is not None]
    candidates.append(float(SPAN_HARD_HOURS_DEFAULT))
    return min(candidates)


def get_effective_window(driver_id, configured=None, enforce_cap=True):
    """Return the window dict to enforce for this driver.

    {"start": h, "end": h, "max_hours": float|None, "flexible": bool}
    When USE_STUB_WINDOWS: use the observed-history stub (flexible=False so it actually
    binds during testing). Otherwise use the caller-supplied `configured` window (the real
    DriverWeeklySchedule-derived dict). Returns None if nothing is known (=> no window guard).

    enforce_cap (with ENFORCE_SPAN_CAPS): clamp max_hours to
    min(stub, configured, SPAN_HARD_HOURS_DEFAULT) — the stub values are OPTIMISTIC observed
    history (David 24h, roberto 23h, ...), which is exactly what let auto-assign build 15-18h
    days. Unknown drivers get a cap-only synthetic window whose start/end stay None (a
    non-None end would NEWLY enforce a clear-by on drivers who today have no window at all).
    Pass enforce_cap=False on analytics / manual-sovereign paths (fleet_intel, the manual
    swap-validation endpoint) so the cap never silently changes shipped numbers or blocks a
    dispatcher's intentional long day.
    """
    cap_on = ENFORCE_SPAN_CAPS and enforce_cap
    if USE_STUB_WINDOWS:
        w = STUB_DRIVER_WINDOWS.get(driver_id)
        # Use the observed-history stub for start/end/max, but HONOR the driver's REAL
        # flexible flag (from the caller's `configured` window) — never hardcode False.
        # Otherwise flexible drivers were treated as rigid in auto-assign/swaps and late
        # jobs they could cover (e.g. a 10:24 PM van clearing ~11:46) got farmed.
        flexible = bool(configured and configured.get("flexible"))
        if not w:
            if not cap_on:
                return None
            return {"start": None, "end": None,
                    "max_hours": _capped_max_hours(configured.get("max_hours") if configured else None),
                    "flexible": flexible}
        max_h = w["max_hours"]
        if cap_on:
            max_h = _capped_max_hours(max_h, configured.get("max_hours") if configured else None)
        return {"start": w["start"], "end": w["end"], "max_hours": max_h, "flexible": flexible}
    if not cap_on:
        return configured
    if configured is None:
        return {"start": None, "end": None, "max_hours": _capped_max_hours(), "flexible": False}
    capped = dict(configured)
    capped["max_hours"] = _capped_max_hours(configured.get("max_hours"))
    return capped


def window_check(window, pickup_time, clear_dt, span_hours_after,
                 target_date=None, mode=None, flexible_respects_clear_by=None,
                 span_hours_before=None):
    """(ok, reason) for whether adding a leg respects the driver's window + max_hours.

    window: {"start", "end", "max_hours", "flexible"}; None => skip.
    pickup_time: datetime.time of the new leg's pickup.
    clear_dt: datetime when the new leg clears (finishes).
    span_hours_after: driver's day span (first pickup -> last clear) IF this leg is added.
    target_date: the schedule date; used to build an ABSOLUTE clear-by datetime so a leg
        that clears AFTER MIDNIGHT (e.g. a 22:30 pickup clearing 00:30 next day) is correctly
        judged a clear-by violation rather than evading it via a bare hour comparison.
    span_hours_before: the day span WITHOUT the leg. When the day ALREADY exceeds max_hours
        (a pre-existing/manual board, or a modal cap set below the saved day), only inserts
        that GROW the span are blocked — hole-fills that leave the span unchanged stay legal,
        so an over-cap driver isn't frozen out of every insert. None => legacy total-span gate.
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
        already_over = (span_hours_before is not None
                        and span_hours_before > float(max_h))
        grows = (span_hours_before is None
                 or span_hours_after > span_hours_before + 1e-9)
        if not already_over or grows:
            return False, f"day span {span_hours_after:.1f}h > max_hours {max_h}"

    return True, ""
