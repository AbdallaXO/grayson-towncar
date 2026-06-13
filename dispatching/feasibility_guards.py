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
# 2026-06-11 FOUNDER PICK: 15.0 (was 17.0). 18-day sandbox sweep 06-13..06-30
# (docs/scheduler-audit/0613-sandbox-results.md): 15h costs ~1 in-house job/day on
# medium days, ~2.5/day on busy, ~0 on slow — and removes every 15h+ day, including
# the 16-17h days the 17h default still built on SLOW boards. A tighter stub/
# configured/modal value still wins via min(). An INTENTIONAL longer day is the
# dispatcher's call: a typed per-driver Max hrs RAISES the cap past this default
# ("a typed number means it"), bounded by SPAN_ABS_CEILING_HOURS below.
SPAN_HARD_HOURS_DEFAULT = 15.0
# ABSOLUTE inhumane bound (founder 2026-06-10: "no driver ever gets an inhumane
# day"; his own hand-built max was 16.5h). The pre-2026-06-11 17h default lives on
# here as the ceiling a dispatcher-typed Max hrs can reach — NOTHING exceeds it.
SPAN_ABS_CEILING_HOURS = 17.0
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

# Ceiling for the AUTOMATIC span-cap coverage rescue. The rescue pass may lift a
# driver's personal/default cap to keep a leg in-house (red badge), but NEVER past
# this — before this rule the lift was unbounded and built 17.8h (shelley) and 18.3h
# (Aftab) days around the 00:0x airport arrivals on 2026-05-16. Tracks the policy
# default (15h): the engine never AUTOMATICALLY builds past policy; only an explicit
# dispatcher-typed Max hrs goes higher (up to SPAN_ABS_CEILING_HOURS). A leg that
# fits nobody under the ceiling farms with a named reason, exactly like the founder
# does by hand.
SPAN_RESCUE_CEILING_HOURS = SPAN_HARD_HOURS_DEFAULT

# Night-duty boundary (founder rule 2026-06-10): a pickup BEFORE this hour is night work
# and must never glue onto a Flexible day driver — windows-as-policy proved whack-a-mole
# (blocking shelley/sereen just moved the 00:02 leg to the next flexible driver). A
# NON-flexible window whose explicit start covers the hour still qualifies (a deliberate
# night shift). Set to 3 from the founder's own boards: his in-house days start as early
# as 03:00 (Michael Olmo) / 03:45 (runer), but every 00:00-02:59 arrival was farmed.
NIGHT_LEG_FLEX_BLOCK = True
NIGHT_LEG_BOUNDARY_HOUR = 3

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
def _capped_max_hours(stub_mh=None, configured_mh=None):
    """Effective duty cap for one driver.

    The dispatcher's per-driver value (modal-typed Max hrs or saved availability
    max_hours) is INTENT — "a typed number means it" — so it may RAISE the cap past
    SPAN_HARD_HOURS_DEFAULT, bounded by SPAN_ABS_CEILING_HOURS (typed 16 binds at 16;
    typed 18 binds at 17). Without one, the global default holds. The observed-history
    stub is OPTIMISTIC data, not intent: it only ever tightens, never raises."""
    if configured_mh is not None:
        cap = min(float(configured_mh), float(SPAN_ABS_CEILING_HOURS))
    else:
        cap = float(SPAN_HARD_HOURS_DEFAULT)
    if stub_mh is not None:
        cap = min(cap, float(stub_mh))
    return cap


def get_effective_window(driver_id, configured=None, enforce_cap=True):
    """Return the window dict to enforce for this driver.

    {"start": h, "end": h, "max_hours": float|None, "flexible": bool}
    When USE_STUB_WINDOWS: use the observed-history stub (flexible=False so it actually
    binds during testing). Otherwise use the caller-supplied `configured` window (the real
    DriverWeeklySchedule-derived dict). Returns None if nothing is known (=> no window guard).

    enforce_cap (with ENFORCE_SPAN_CAPS): clamp max_hours via _capped_max_hours —
    min(stub, default) without a per-driver value; a dispatcher-typed/saved value may
    raise past the default up to SPAN_ABS_CEILING_HOURS. The stub values are OPTIMISTIC
    observed history (David 24h, roberto 23h, ...), which is exactly what let auto-assign
    build 15-18h days. Unknown drivers get a cap-only synthetic window whose start/end stay None (a
    non-None end would NEWLY enforce a clear-by on drivers who today have no window at all).
    Pass enforce_cap=False on analytics / manual-sovereign paths (fleet_intel, the manual
    swap-validation endpoint) so the cap never silently changes shipped numbers or blocks a
    dispatcher's intentional long day.
    """
    cap_on = ENFORCE_SPAN_CAPS and enforce_cap
    # enforce_cap=False marks the MANUAL-SOVEREIGN callers (manual swap revalidation,
    # analytics): their windows carry night_exempt so the night rule never hard-blocks
    # an intentional dispatcher move (founder: "flag but do it").
    night_exempt = not enforce_cap
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
                    "max_hours": _capped_max_hours(
                        configured_mh=configured.get("max_hours") if configured else None),
                    "flexible": flexible, "night_exempt": night_exempt}
        max_h = w["max_hours"]
        if cap_on:
            max_h = _capped_max_hours(
                stub_mh=max_h,
                configured_mh=configured.get("max_hours") if configured else None)
        # The dispatcher's typed window is INTENT ("the modal is authoritative") and the
        # stub is OPTIMISTIC observed history — so a TIGHTER configured start/end wins
        # over the stub (founder-brain 2026-06: Yovanny typed 6-18 in the modal but the
        # stub's end=20 let auto-assign seat a job clearing 19:15 past his clear-by).
        # The stub still tightens a LOOSER configured window, exactly like max_hours.
        start_h, end_h = w["start"], w["end"]
        if configured is not None:
            c_start, c_end = configured.get("start"), configured.get("end")
            if c_start is not None and start_h is not None:
                start_h = max(start_h, int(c_start))
            if c_end is not None and end_h is not None:
                end_h = min(end_h, int(c_end))
        return {"start": start_h, "end": end_h, "max_hours": max_h,
                "flexible": flexible, "night_exempt": night_exempt}
    if not cap_on:
        if configured is not None and night_exempt:
            return dict(configured, night_exempt=True)
        return configured
    if configured is None:
        return {"start": None, "end": None, "max_hours": _capped_max_hours(),
                "flexible": False, "night_exempt": night_exempt}
    capped = dict(configured)
    capped["max_hours"] = _capped_max_hours(configured_mh=configured.get("max_hours"))
    capped["night_exempt"] = night_exempt
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

    # NIGHT bound — a 00:00-(boundary-1):59 pickup is night duty: Flexible means "any time
    # within a normal day", not "also works the middle of the night". Two escapes:
    # (1) an EXPLICIT start that covers the hour — explicitness beats the flexible flag,
    #     so a dispatcher typing From=00:00 in the builder, an accepted advisor night
    #     card (planned_start_hour=0), or a stub observed working nights still seats;
    # (2) night_exempt windows (get_effective_window(enforce_cap=False) — the manual-
    #     sovereign paths): an intentional dispatcher move is never hard-blocked, and a
    #     pre-existing night leg must not poison execute_swap's all-legs revalidation.
    if (flexible and NIGHT_LEG_FLEX_BLOCK
            and pickup_time.hour < NIGHT_LEG_BOUNDARY_HOUR
            and not window.get("night_exempt")
            and not (start is not None and start <= pickup_time.hour)):
        return False, (f"night pickup {pickup_time.strftime('%H:%M')} needs an explicit "
                       f"night-shift window (Flexible does not cover "
                       f"00:00-0{NIGHT_LEG_BOUNDARY_HOUR}:00)")

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
