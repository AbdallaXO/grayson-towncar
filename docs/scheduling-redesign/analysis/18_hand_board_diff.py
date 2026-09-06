#!/usr/bin/env python
"""18 — Where the Day-Builder loses to the hand board (Build 4, step 2).

THE QUESTION
------------
Run the Day-Builder COLD on the most recent real built dates (plus any extra
date named on the command line — the parked "Aug 3 question"), and for every
trip the ENGINE farmed that the HUMANS kept in-house, say what the humans did
differently, in the engine's own terms.

v2 (2026-09-02) — every cause below was re-derived independently against the
shipped code and the tap record before it was trusted; the buckets marked
[v2] exist because the first cut mislabelled trips. See "WHAT V1 GOT WRONG".

  ROSTER_INACTIVE      the human driver is inactive / not in-house
  ROSTER_NO_CAR        no car row (DVA) that day — the engine's roster is
                       DVA-only, so he does not exist to it
  ROSTER_OFF           he has a car row, but saved availability says off
  WINDOW_WRAP_BUG [v2] his saved window WRAPS MIDNIGHT (start > end, e.g.
                       17:00–05:00). Every engine hour gate assumes
                       start <= end, so the predicate rejects EVERY pickup of
                       the day and the driver is unusable. An engine defect,
                       not a rule the team bent.
  CLASS                his car is a lower class than the booking
  WINDOW_PICKUP        the pickup itself falls outside his saved window
  WINDOW_CLEAR_BY [v2] the pickup is INSIDE the window; only the static clear
                       ESTIMATE lands past the window's end. The engine reads
                       the same saved number three ways (greedy pre-filter =
                       last pickup; Guard C = clear-by; the dispatcher-facing
                       label = neither), so this is an interpretation gap.
  SHARE_DATA_INVALID [v2]  the car row itself is impossible: >2 holders on one
                       unit, or the share gate fails even at pad 0 (a RAW
                       overlap = one car in two places at once)
  SHARE_PARTNER_OFF [v2]   the co-holder is not on the engine's roster, so the
                       engine never modelled the split at all
  SHARE_CONFLICT       a GENUINE pad verdict: clean at pad 0, partner on the
                       roster, rejected only by engine_share_pad_min
  HUMAN_TIGHT_TURN     negative slack on the engine's own static turnaround
                       clock, against the jobs the engine KEPT (mutual pairs
                       between two lost trips are resolved greedily, so one
                       conflict produces one row, never two)
  HUMAN_LONG_DAY       his real day's span exceeds the engine's cap for him
                       AND this trip is what takes him over
  HUMAN_POLICY_TIGHT   declined by the run's minimum turn buffer — asked of
                       the shipped check_feasibility, which exempts a
                       same-terminal airport turn, not of the raw number
  SPLIT_HALF_IGNORED   he shares a car, the team used him in one half only,
                       and stripping the engine's outside-half jobs MAKES THE
                       TRIP FIT (the causal test; without it the flag fires on
                       trips lost to something else entirely)
  ENGINE_MISSED_FREE   the trip fits his engine-built day as-is
  ENGINE_FILLED_DIFF   the engine spent his time on other trips

SECONDARY FLAGS (never decide the cause):
  REST    the human placement breaches the 510-min rest floor vs adjacent days
  RETIME  the pickup moved >= 30 min after the day-before-20:00 cutoff. Base
          rate on these dates: 8.6% of all legs, 14.6% of arrivals — so this
          is a small effect, not the explanation.
  CLOCK_DEFAULTS [v2]  the tight turn's deficit rests on model DEFAULTS: the
          flat 45-min dwell, DEFAULT_DRIVE_TIME (35) standing in for an
          address pair the category table cannot price, or the cross-property
          Disney->Disney average applied to an adjacent-resort hop.

WHAT V1 GOT WRONG (all verified against the shipped code, all fixed here)
------------------------------------------------------------------------
1. Mutual pairs were double-counted: two adjacent human legs that conflict
   with EACH OTHER both got HUMAN_TIGHT_TURN, though the engine could have
   seated one. v2 tests each trip against the day minus ALL of that driver's
   lost trips, then re-adds them greedily in pickup order.
2. HUMAN_POLICY_TIGHT compared the raw buffer to min_turn_buffer; the shipped
   engine routes that floor through effective_min_buffer, which returns 0 for
   a same-terminal airport arrival. 2 of 5 rows were not declined at all.
3. SHARE_CONFLICT never tested pad 0, so a physically impossible car row
   (three holders on one unit) read as "the team ran a tight handoff".
4. SPLIT_HALF_IGNORED was not causal — it fired whenever the engine put a job
   outside the human's half, even when the blocker was elsewhere.
5. The reality check used the picked-up tap. For an ARRIVAL the booked time is
   the flight's landing slot, so that number is deplaning dwell, not lateness;
   and a "batch tap" (picked-up and completed seconds apart, entered at
   drop-off) turns a clean job into a 2-hour delay. v2 measures the
   ON-LOCATION tap, and discards batch taps.
6. HUMAN_LONG_DAY did not check whether the trip itself caused the overage.

Every verdict is the SHIPPED code's own: scheduler.check_feasibility with the
pipeline's windows and buffers, car_share.sharers_conflict, feasibility_guards
window_check / get_effective_window / effective_min_buffer,
scheduler.effective_span_hours. The raw side supplies only the real board, the
tap record and the pickup history.

METHOD (17's technique, extended with a per-date RESTORE so consecutive dates
never see each other cleared): raw side = the frozen read-only snapshot;
Django side = a migrated throwaway copy. Per date: capture the real
assignments, clear the day cold, run the shipped pipeline at the real roster
(baseline), ask the Day-Builder for its plan (epsilon=0), diagnose every
lost/gained trip, then RESTORE the day's assignments exactly.

USAGE
  GRAYSON_SNAPSHOT_DB=<frozen copy> python docs/scheduling-redesign/analysis/18_hand_board_diff.py \
      [--recent 10] [--extra 2026-08-03 ...] [--no-plan]

Outputs: out/18_lost_trips.csv, out/18_per_date.csv, out/18_cause_ranking.csv.
"""
import argparse
import datetime as dt
import importlib.util
import os
import sys
import time
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common as C  # noqa: E402

sys.path.insert(0, C.REPO_ROOT)

FARM_PREMIUM = 70.99          # standby_mints.FARMOUT_PREMIUM_PER_LEG (re-read below)
RETIME_FLAG_MIN = 30
BATCH_TAP_MAX_SEC = 120       # picked-up within 2 min of completed = one batch entry
CAUSE_ORDER = [
    "ROSTER_INACTIVE", "ROSTER_NO_CAR", "ROSTER_OFF", "WINDOW_WRAP_BUG",
    "SHARE_DATA_INVALID", "SHARE_PARTNER_OFF", "CLASS", "WINDOW_PICKUP",
    "WINDOW_CLEAR_BY", "SHARE_CONFLICT", "HUMAN_TIGHT_TURN", "HUMAN_LONG_DAY",
    "HUMAN_POLICY_TIGHT", "SPLIT_HALF_IGNORED", "ENGINE_MISSED_FREE",
    "ENGINE_FILLED_DIFF",
]
FIX_TOUCHES = {
    "ROSTER_NO_CAR": "Day Setup roster (who gets a car). D16 'catch the rest' already names bench "
                     "drivers; a split / second-shift proposal is the lever (standby_mints, "
                     "handoff_chain, day_setup)",
    "ROSTER_OFF": "the driver's saved weekly availability (data) — the engine's roster is "
                  "DVA-eligible AND available; the team rosters people on their nominal day off",
    "ROSTER_INACTIVE": "drivers admin (is_active) — data, not code",
    "WINDOW_WRAP_BUG": "ENGINE DEFECT: every hour gate assumes start <= end. scheduler.py "
                       "suggest_assignments (~:1998) and its clones, feasibility_guards."
                       "window_check (clear-by anchored on target_date), drivers/availability.py "
                       "is_pickup_within_window. An overnight driver is invisible to the engine.",
    "CLASS": "vehicle pairing in Day Setup (day_setup.suggest_day_setup unit affinity) and the "
             "booked class (Leg.effective_vehicle_type) — a bigger car for that driver that day",
    "WINDOW_PICKUP": "the driver's saved weekly window (stale vs practice for these drivers) and "
                     "the engine's hard hour gate in scheduler.suggest_assignments",
    "WINDOW_CLEAR_BY": "feasibility_guards.END_HOUR_MODE = 'CLEAR_BY' vs the greedy pass's "
                       "last-pickup reading of the SAME saved number — the engine is internally "
                       "inconsistent and the UI never says End means 'done by'",
    "SHARE_DATA_INVALID": "Day Setup data: >2 drivers on one unit (car_share."
                          "MAX_DRIVERS_PER_VEHICLE_DATE is enforced only in apply_day_setup) or a "
                          "car row that puts one car in two places. It also IDLES the co-holders.",
    "SHARE_PARTNER_OFF": "same as ROSTER_OFF, but on the co-holder — the engine cannot model a "
                         "split whose other half it does not have",
    "SHARE_CONFLICT": "car_share.sharers_conflict (convention A) + SchedulerSettings."
                      "engine_share_pad_min; handoff placement (handoff_chain, share_split_hour)",
    "HUMAN_TIGHT_TURN": "the engine's STATIC PLANNING CLOCK: scheduler.chain_clear_dt "
                        "(flat STATIC_FLOOR_DWELL_MIN = 45 on every arrival), chain_repo_minutes "
                        "/ DRIVE_TIME_ESTIMATES (DEFAULT_DRIVE_TIME = 35 whenever the category "
                        "table cannot price the pair; Disney->Disney = 12 cross-property average "
                        "on adjacent-resort hops), feasibility_guards.required_turnaround",
    "HUMAN_LONG_DAY": "span caps: SPAN_HARD_HOURS_DEFAULT 15 / stub max_hours / saved max_hours "
                      "(feasibility_guards._capped_max_hours). D4 allows 15h only as a priced, "
                      "visible exception",
    "HUMAN_POLICY_TIGHT": "SchedulerSettings.min_turn_buffer and per-driver "
                          "default_min_turn_buffer (the engine's Guard B' planning floor)",
    "SPLIT_HALF_IGNORED": "shared-car split modelling: planned_start_hour/planned_end_hour are "
                          "NULL on every car row, and only the auto-assign MODAL prefill "
                          "(views.py ~:12618) ever reads them — the cold Day-Builder path does "
                          "not, so it spreads a half-day driver across the whole day",
    "ENGINE_MISSED_FREE": "assignment_pipeline pass 8 free-insertion sweep, the class-match guard, "
                          "the reserved-mismatch skip in suggest_assignments",
    "ENGINE_FILLED_DIFF": "greedy order and scoring in scheduler.suggest_assignments, leg_value / "
                          "evict_to_farm_for_value, recover_residuals_via_swaps — WHICH trips the "
                          "engine chooses to fill a driver's day with",
}
ASSUMPTIONS = (
    "Lost trip = in-house on the real board (A6 universe, in-house driver) but farmed "
    "by the Day-Builder's fixed-headcount plan at the dispatcher's real roster.",
    "Every gate verdict is the shipped code's own, evaluated on the REAL board (what the "
    "humans accepted) and on the ENGINE board (why the engine did not). The first failing "
    "gate in CAUSE_ORDER is the primary cause; every flag is kept in the CSV.",
    "Tight turns are tested against the day minus ALL of that driver's lost trips, then "
    "re-added greedily in pickup order — so two legs that conflict with EACH OTHER produce "
    "one row, not two.",
    "Reality is read from the ON-LOCATION tap (the driver reached the pickup point), never "
    "the picked-up tap: for an arrival the booked time is the flight's landing slot, so "
    "picked-up minus booked is deplaning dwell, not lateness. Taps entered in a batch "
    "(picked-up within 2 min of completed) are discarded as unusable.",
    "Dollars: one lost trip = the flat farm-out premium (standby_mints."
    "FARMOUT_PREMIUM_PER_LEG) — the real leg ran in-house, so no affiliate rate was "
    "captured. Per-28-day figures scale the per-day rate over the sampled dates.",
    "Consecutive dates are safe: each date is cleared, diagnosed and RESTORED before the next.",
)


def load_module(name, fname):
    here = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location(name, os.path.join(here, fname))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# --------------------------------------------------------------------------
# dates
# --------------------------------------------------------------------------

def pick_dates(con, horizon, n_recent, extra):
    rows = C.q(con, C.live_legs_sql(
        "l.pickup_date d, COUNT(*) n",
        "AND l.driver_id IN (SELECT id FROM drivers_driver WHERE LOWER(driver_type)='inhouse') "
        "AND l.pickup_date <= ?", order="GROUP BY l.pickup_date ORDER BY l.pickup_date DESC"),
        (str(horizon.last_actuals_day),))
    recent = [dt.date.fromisoformat(r["d"]) for r in rows[:n_recent]]
    picks = set(recent)
    for e in extra:
        picks.add(dt.date.fromisoformat(e))
    return sorted(picks), sorted(recent)


# --------------------------------------------------------------------------
# raw-side helpers: history + the tap record
# --------------------------------------------------------------------------

def retime_minutes(con, leg_id, day, final_time):
    """(delta_min, tag): final pickup minus the pickup as of the day-before-20:00
    cutoff (D 00:00 UTC == D-1 20:00 EDT). None when no history exists."""
    hist = C.q(con, "SELECT pickup_time, history_date FROM reservations_historicalleg "
                    "WHERE id=? ORDER BY history_date", (leg_id,))
    if not hist:
        return None, ""
    cutoff = f"{day.isoformat()} 00:00:00"
    before = [h for h in hist if h["history_date"] and str(h["history_date"])[:19] <= cutoff]
    ref = (before[-1] if before else hist[0])["pickup_time"]
    if not ref or final_time is None:
        return None, ""
    try:
        t_ref = dt.time.fromisoformat(str(ref)[:8])
    except ValueError:
        return None, ""
    delta = (dt.datetime.combine(day, final_time)
             - dt.datetime.combine(day, t_ref)).total_seconds() / 60.0
    return int(round(delta)), ("as of the night before" if before else "at booking")


def arrival_minutes(con, leg_id, day, booked_time):
    """(minutes the driver reached the pickup point vs the booked time, quality):
    from the ON-LOCATION tap. quality in {'ok', 'batch', 'none'}.

    A 'batch' record (picked-up within BATCH_TAP_MAX_SEC of completed, with no
    on-location tap) means the driver entered his taps at drop-off — the instants
    are unusable and are NOT reported as lateness."""
    rows = C.q(con, "SELECT status, timestamp FROM reservations_legstatus WHERE leg_id=? "
                    "AND status IN ('on-location','picked-up','completed') ORDER BY timestamp",
               (leg_id,))
    if not rows or booked_time is None:
        return None, "none"
    ol = [r for r in rows if r["status"] == "on-location"]
    pu = [r for r in rows if r["status"] == "picked-up"]
    cm = [r for r in rows if r["status"] == "completed"]
    if not ol:
        if pu and cm:
            gap = (C.to_local(cm[-1]["timestamp"])
                   - C.to_local(pu[-1]["timestamp"])).total_seconds()
            if abs(gap) <= BATCH_TAP_MAX_SEC:
                return None, "batch"
        return None, "none"
    at = C.to_local(ol[-1]["timestamp"])
    return int(round((at - dt.datetime.combine(day, booked_time)).total_seconds() / 60.0)), "ok"


# --------------------------------------------------------------------------
# Django-side helpers
# --------------------------------------------------------------------------

def capture_day(day):
    from reservations.models import Leg
    return dict(Leg.objects.filter(pickup_date=day, driver__isnull=False)
                .values_list("id", "driver_id"))


def restore_day(day, saved):
    from reservations.models import Leg
    by_driver = defaultdict(list)
    for lid, did in saved.items():
        by_driver[did].append(lid)
    n = 0
    for did, ids in by_driver.items():
        n += Leg.objects.filter(id__in=ids, pickup_date=day).update(driver_id=did)
    return n


def board_for(assign, legs, drivers, day, dva_rows):
    from dispatching.scheduler import build_driver_schedules
    legs_by_id = {l.id: l for l in legs}
    drivers_by_id = {d.id: d for d in drivers}
    stamped = []
    for lid, did in assign.items():
        lg = legs_by_id.get(lid)
        if lg is not None and did in drivers_by_id:
            lg.driver = drivers_by_id[did]
            lg.driver_id = did
            stamped.append(lg)
    try:
        return build_driver_schedules(legs, drivers, day, dva_rows=dva_rows)
    finally:
        for lg in stamped:
            lg.driver = None
            lg.driver_id = None


def sched_without(sched, leg_ids):
    from dispatching.scheduler import DriverDaySchedule
    drop = set(leg_ids)
    return DriverDaySchedule(driver_id=sched.driver_id, driver_name=sched.driver_name,
                             driver_type=sched.driver_type,
                             slots=[s for s in sched.slots if s.leg_id not in drop],
                             vehicle_cap=sched.vehicle_cap)


def sched_with(sched, extra_slots):
    from dispatching.scheduler import DriverDaySchedule
    return DriverDaySchedule(driver_id=sched.driver_id, driver_name=sched.driver_name,
                             driver_type=sched.driver_type,
                             slots=sorted(list(sched.slots) + list(extra_slots),
                                          key=lambda s: s.pickup_time),
                             vehicle_cap=sched.vehicle_cap)


def fmt_t(t):
    return t.strftime("%I:%M %p").lstrip("0") if t else "?"


def short(s, n=28):
    s = (s or "").strip()
    return s if len(s) <= n else s[:n - 1] + "…"


def clock_defaults(prev_slot, leg, day):
    """Does this turn's deficit rest on the static clock's DEFAULTS? Returns a
    short human string naming each default used, or ''. Reads the shipped tables."""
    from dispatching.analytics import categorize_location
    from dispatching.scheduler import (
        DRIVE_TIME_ESTIMATES, DEFAULT_DRIVE_TIME, STATIC_FLOOR_DWELL_MIN)
    if prev_slot is None:
        return ""
    notes = []
    p_from, p_to = prev_slot.pickup_category, prev_slot.dropoff_category
    if (p_from, p_to) not in DRIVE_TIME_ESTIMATES:
        notes.append(f"the previous job's own drive priced at the {DEFAULT_DRIVE_TIME}-min "
                     f"default ({p_from or 'unknown'} to {p_to or 'unknown'} is not in the table)")
    if prev_slot.trip_type == "arrival":
        notes.append(f"a flat {STATIC_FLOOR_DWELL_MIN}-min dwell on the previous arrival")
    n_from = p_to
    n_to = categorize_location(leg.pickup_location)
    if (n_from, n_to) not in DRIVE_TIME_ESTIMATES:
        notes.append(f"the reposition priced at the {DEFAULT_DRIVE_TIME}-min default "
                     f"({n_from or 'unknown'} to {n_to or 'unknown'} is not in the table)")
    elif n_from == n_to and str(n_from).startswith("Disney"):
        notes.append(f"the {DRIVE_TIME_ESTIMATES[(n_from, n_to)]}-min cross-property average "
                     f"for a hop between two adjacent Disney resorts")
    return "; ".join(notes)


# --------------------------------------------------------------------------
# per-driver tight-turn resolution (mutual pairs)
# --------------------------------------------------------------------------

def resolve_tight_turns(H, lost_ids, ctx):
    """Which of driver H's lost trips are genuinely lost to a turn the engine
    calls impossible, and which merely conflict with ANOTHER lost trip.

    Method (the engine's own greedy semantics): start from his real day with ALL
    his lost trips removed — that is the part of his board the engine kept — then
    re-add the lost trips in pickup order, testing each with the shipped
    check_feasibility. A trip that fails against that growing board is a real
    tight turn. A trip that fails only because an earlier lost trip was re-added
    first is the second half of ONE conflict and falls through to another cause.
    """
    from dispatching.scheduler import (
        check_feasibility, driver_min_buffer, _make_sim_slot, _slot_chain_end)
    day = ctx["day"]
    real = ctx["real_board"].get(H)
    out = {}
    if real is None:
        return out
    base = sched_without(real, lost_ids)
    mb = driver_min_buffer(H, ctx["run_min_buffer"], ctx["driver_min_buffers"])
    growing = base
    order = sorted(lost_ids, key=lambda lid: (ctx["legs_by_id"][lid].pickup_time, lid))
    for lid in order:
        leg = ctx["legs_by_id"][lid]
        f0 = check_feasibility(growing, leg, day, driver_window=None, min_buffer=0)
        fmb = check_feasibility(growing, leg, day, driver_window=None, min_buffer=mb)
        prev = None
        if growing.slots:
            pdt = dt.datetime.combine(day, leg.pickup_time)
            earlier = [s for s in growing.slots
                       if dt.datetime.combine(day, s.pickup_time) <= pdt]
            if earlier:
                prev = max(earlier, key=lambda s: _slot_chain_end(s, day))
        out[lid] = {"tight": not f0.feasible,
                    "policy": bool(f0.feasible and not fmb.feasible),
                    "buffer": f0.buffer_minutes, "reason": f0.reason,
                    "min_buffer": mb, "prev_slot": prev,
                    "defaults": clock_defaults(prev, leg, day) if not f0.feasible else ""}
        if f0.feasible:
            growing = sched_with(growing, [_make_sim_slot(leg, day)])
    return out


# --------------------------------------------------------------------------
# the diagnosis of one lost trip
# --------------------------------------------------------------------------

def diagnose(leg, H, ctx, turn_info):
    from dispatching import feasibility_guards as fg
    from dispatching.scheduler import (
        check_feasibility, effective_span_hours, estimate_job_end_time,
        get_compatible_vehicle_types, driver_min_buffer)
    from dispatching.car_share import sharers_conflict, MAX_DRIVERS_PER_VEHICLE_DATE
    from drivers.availability import _weekly_or_defaults

    day = ctx["day"]
    flags, det = [], {}
    d = ctx["drivers_all_by_id"].get(H)
    det["driver"] = str(d) if d else f"driver {H}"
    dva = ctx["dva_by_driver"].get(H)
    det["unit"] = (f"#{dva.vehicle.vehicle_number} {ctx['vt'](dva.vehicle)}"
                   if dva and dva.vehicle else "no car row")
    det["planned_hours"] = (f"{dva.planned_start_hour}-{dva.planned_end_hour}"
                            if dva and (dva.planned_start_hour is not None
                                        or dva.planned_end_hour is not None) else "")
    pickup_dt = dt.datetime.combine(day, leg.pickup_time)
    try:
        clear_dt = estimate_job_end_time(leg, day)
    except Exception:
        clear_dt = pickup_dt + dt.timedelta(minutes=75)
    det["clear"] = clear_dt.strftime("%H:%M")

    # ── roster level ──
    if H not in ctx["roster_ids"]:
        if d is None or not d.is_active or d.driver_type != "inhouse":
            flags.append("ROSTER_INACTIVE")
            det["why"] = "inactive or not in-house in the snapshot"
        elif dva is None:
            flags.append("ROSTER_NO_CAR")
            det["why"] = "no car row that day — the engine's roster is DVA-only"
        else:
            eff = d.get_effective_availability(day)
            base = _weekly_or_defaults(d, day)
            if eff.get("has_exception") and eff.get("exception_type") == "off":
                det["why"] = f"time-off exception ({eff.get('exception_reason') or 'no reason'})"
            elif not base["is_available"]:
                det["why"] = f"saved weekly schedule says off on {day.strftime('%A')}s"
            else:
                det["why"] = "saved availability resolves to unavailable"
            flags.append("ROSTER_OFF")
        return flags, det

    sh, eh = ctx["driver_hours"][H]
    flex = H in ctx["flexible"]
    det["saved_window"] = "flexible" if flex else f"{sh}:00–{eh}:59"
    stub = fg.STUB_DRIVER_WINDOWS.get(H) if fg.USE_STUB_WINDOWS else None
    det["stub_window"] = (f"{stub['start']}–{stub['end']} max {stub['max_hours']}h"
                          if stub else "")
    w_eng = ctx["capped_windows"].get(H)
    mh_conf = ctx["driver_max_hours"].get(H)
    w_conf = {"start": sh, "end": eh, "flexible": flex,
              "max_hours": fg._capped_max_hours(configured_mh=mh_conf)}

    # ── the wrap-around window defect: start > end means an overnight shift ──
    if not flex and sh is not None and eh is not None and sh > eh:
        flags.append("WINDOW_WRAP_BUG")
        det["why_window"] = (f"his saved window {sh}:00–{eh}:59 crosses midnight (an evening "
                             f"shift). Every engine hour gate assumes start <= end, so the test "
                             f"rejects EVERY pickup of the day and he is unusable")

    # ── the car row and its co-holders ──
    partners = ctx["partners_real"].get(H)
    holders = ctx["holders_by_unit"].get(dva.vehicle_id) if dva else None
    if holders and len(holders) > MAX_DRIVERS_PER_VEHICLE_DATE:
        flags.append("SHARE_DATA_INVALID")
        names = ", ".join(str(ctx["drivers_all_by_id"].get(x, x)) for x in sorted(holders))
        det["why_share"] = (f"{len(holders)} drivers hold {det['unit']} that day ({names}) — one "
                            f"car cannot be in three places, and the bad row also idles the "
                            f"co-holders")
    elif partners:
        det["share_partner"] = ", ".join(str(ctx["drivers_all_by_id"].get(p, p)) for p in partners)
        sim = dict(ctx["real_board"])
        rs = ctx["real_board"].get(H)
        if rs is not None:
            sim[H] = sched_without(rs, [leg.id])
        if sharers_conflict(leg, H, ctx["partners_real"], sim, day):
            raw = sharers_conflict(leg, H, ctx["partners_real"], sim, day, pad_min=0)
            off_roster = sorted(p for p in partners if p not in ctx["roster_ids"])
            if raw:
                flags.append("SHARE_DATA_INVALID")
                det["why_share"] = (f"his trip RAW-overlaps a co-holder's job on {det['unit']} "
                                    f"(shared with {det['share_partner']}) — even with no pad at "
                                    f"all, so the car row puts one car in two places")
            elif off_roster:
                flags.append("SHARE_PARTNER_OFF")
                names = ", ".join(str(ctx["drivers_all_by_id"].get(p, p)) for p in off_roster)
                det["why_share"] = (f"he shares {det['unit']} with {names}, who is not on the "
                                    f"engine's roster that day — so the engine never modelled the "
                                    f"split and gave him the whole car")
            else:
                flags.append("SHARE_CONFLICT")
                det["why_share"] = (f"shares {det['unit']} with {det['share_partner']}; clean with "
                                    f"no pad, rejected only by the engine's "
                                    f"{ctx['cfg'].engine_share_pad_min}-min clear-to-pickup pad")

    # ── class ──
    dvt = ctx["vtypes"].get(H)
    lvt = str(leg.effective_vehicle_type or "")
    if dvt and lvt and lvt not in get_compatible_vehicle_types(dvt):
        flags.append("CLASS")
        det["why_class"] = f"his car is a {dvt}; the booking is a {lvt}"

    # ── windows: pickup-outside vs clear-by-only ──
    if "WINDOW_WRAP_BUG" not in flags:
        single_span = (clear_dt - pickup_dt).total_seconds() / 3600.0
        pickup_outside = (not flex and (leg.pickup_time < dt.time(sh, 0)
                                        or leg.pickup_time > dt.time(eh, 59)))
        ok_conf, r_conf = fg.window_check(w_conf, leg.pickup_time, clear_dt, single_span,
                                          target_date=day)
        ok_eng, r_eng = (fg.window_check(w_eng, leg.pickup_time, clear_dt, single_span,
                                         target_date=day) if w_eng else (True, ""))
        if pickup_outside:
            flags.append("WINDOW_PICKUP")
            det["why_window"] = (f"pickup {fmt_t(leg.pickup_time)} is outside his saved window "
                                 f"{sh}:00–{eh}:59")
        elif not ok_conf:
            flags.append("WINDOW_CLEAR_BY")
            over = int((clear_dt - dt.datetime.combine(day, dt.time(min(eh, 23), 0)))
                       .total_seconds() / 60.0)
            det["why_window"] = (f"the pickup is inside his window; the engine's ESTIMATE has him "
                                 f"clearing {clear_dt.strftime('%H:%M')}, {over} min past the "
                                 f"{eh}:00 end it reads as 'done by'")
        elif not ok_eng:
            flags.append("WINDOW_CLEAR_BY")
            det["why_window"] = f"hardcoded stub window ({det['stub_window']}): {r_eng}"

    # ── the turn, resolved across all of this driver's lost trips ──
    ti = turn_info.get(leg.id) or {}
    det["real_turn_buffer"] = ti.get("buffer", 999)
    det["real_turn_reason"] = ti.get("reason", "")
    det["clock_defaults"] = ti.get("defaults", "")
    if ti.get("tight"):
        flags.append("HUMAN_TIGHT_TURN")
    elif ti.get("policy"):
        flags.append("HUMAN_POLICY_TIGHT")
        det["why_policy"] = (f"{ti.get('buffer')} min spare; the engine's minimum for him on this "
                             f"turn is {ti.get('min_buffer')} min")

    # ── span of the human's real day vs the engine's cap ──
    real_sched = ctx["real_board"].get(H)
    real_slots = sorted(real_sched.slots, key=lambda s: s.pickup_time) if real_sched else []
    if real_slots:
        raw, eff_h = effective_span_hours(real_slots, day)
        det["real_span_raw"] = round(raw, 1)
        det["real_span_eff"] = round(eff_h, 1)
        cap = (w_eng or {}).get("max_hours") or fg.SPAN_HARD_HOURS_DEFAULT
        det["cap"] = cap
        if raw > cap + 1e-9:
            others = [s for s in real_slots if s.leg_id != leg.id]
            raw_wo = effective_span_hours(others, day)[0] if others else 0.0
            if raw_wo <= cap + 1e-9:
                flags.append("HUMAN_LONG_DAY")
                stub_mh = (stub or {}).get("max_hours")
                src = ("the stub table" if stub_mh is not None and float(stub_mh) < 15.0
                       else ("his saved max hours" if mh_conf else "the 15h default"))
                det["why_span"] = (f"his real day spans {raw:.1f}h against the engine's {cap:g}h "
                                   f"cap ({src}); this trip is what takes him over")

    det["rest"] = "".join(s[0] for (did, s) in sorted(ctx["pre_rest"]) if did == H)

    # ── the engine's own board ──
    eng_sched = ctx["eng_board"].get(H)
    if eng_sched is not None:
        mb = driver_min_buffer(H, ctx["run_min_buffer"], ctx["driver_min_buffers"])
        f_eng = check_feasibility(eng_sched, leg, day, driver_window=w_eng, min_buffer=mb)
        share_eng = (sharers_conflict(leg, H, ctx["partners_eng"], ctx["eng_board"], day)
                     if ctx["partners_eng"].get(H) else False)
        hour_ok = flex or (dt.time(sh, 0) <= leg.pickup_time <= dt.time(eh, 59))
        det["eng_fit"] = bool(f_eng.feasible and not share_eng and hour_ok)
        det["eng_reason"] = (f_eng.reason if not f_eng.feasible else
                             ("share conflict" if share_eng else
                              ("outside hour window" if not hour_ok else "fits")))
        near = [s for s in sorted(eng_sched.slots, key=lambda s: s.pickup_time)
                if abs((dt.datetime.combine(day, s.pickup_time) - pickup_dt)
                       .total_seconds()) <= 3 * 3600]
        det["eng_nearby"] = "; ".join(
            f"{fmt_t(s.pickup_time)} {s.trip_type} L{s.leg_id}"
            + ("*" if s.leg_id in ctx["human_farmed"] else "") for s in near)
        det["eng_legs"] = len(eng_sched.slots)

        split_idx = CAUSE_ORDER.index("SPLIT_HALF_IGNORED")
        if (partners and real_slots and eng_sched.slots
                and not any(f in flags for f in CAUSE_ORDER[:split_idx])):
            r_first = dt.datetime.combine(day, min(s.pickup_time for s in real_slots))
            r_last = dt.datetime.combine(day, max(s.pickup_time for s in real_slots))
            outside = [s for s in eng_sched.slots
                       if dt.datetime.combine(day, s.pickup_time) < r_first - dt.timedelta(hours=2)
                       or dt.datetime.combine(day, s.pickup_time) > r_last + dt.timedelta(hours=2)]
            if outside:
                stripped = sched_without(eng_sched, [s.leg_id for s in outside])
                f_str = check_feasibility(stripped, leg, day, driver_window=w_eng, min_buffer=mb)
                if f_str.feasible and not det["eng_fit"]:
                    flags.append("SPLIT_HALF_IGNORED")
                    first_out = sorted(outside, key=lambda s: s.pickup_time)[0]
                    det["why_split"] = (
                        f"the team used him only {fmt_t(r_first.time())}–{fmt_t(r_last.time())} on "
                        f"shared {det['unit']} (planned hours on the car row: "
                        f"{det['planned_hours'] or 'never set'}); the engine gave him "
                        f"{len(outside)} job(s) outside that half, first at "
                        f"{fmt_t(first_out.pickup_time)}, and stripping them makes this trip fit")
        miss_idx = CAUSE_ORDER.index("ENGINE_MISSED_FREE")
        if not any(f in flags for f in CAUSE_ORDER[:miss_idx]):
            flags.append("ENGINE_MISSED_FREE" if det["eng_fit"] else "ENGINE_FILLED_DIFF")
    else:
        det["eng_fit"] = None
        if not flags:
            flags.append("ENGINE_FILLED_DIFF")
    det["real_legs"] = len(real_slots)
    return flags, det


def primary(flags):
    for c in CAUSE_ORDER:
        if c in flags:
            return c
    return flags[0] if flags else "UNKNOWN"


def explain(row):
    c = row["cause"]
    base = (f"{row['date']} {row['pickup']} {row['trip_type']} {row['route']} — the team gave it "
            f"to {row['driver']}")
    why = {
        "ROSTER_NO_CAR": "who had NO car row in Day Setup that day, so the engine could not use "
                         "him at all",
        "ROSTER_OFF": f"whose saved availability said off ({row.get('why', '')}), so the engine "
                      f"dropped him from the roster",
        "ROSTER_INACTIVE": "who is inactive in the snapshot",
        "WINDOW_WRAP_BUG": f"— {row.get('why_window', '')}. This is an engine bug, not a rule the "
                           f"team bent",
        "SHARE_DATA_INVALID": f"— {row.get('why_share', '')}",
        "SHARE_PARTNER_OFF": f"— {row.get('why_share', '')}",
        "SHARE_CONFLICT": f"— {row.get('why_share', '')}",
        "CLASS": f"in {row['unit']} — {row.get('why_class', '')}; the engine never seats a bigger "
                 f"booking in a smaller car",
        "WINDOW_PICKUP": f"— {row.get('why_window', '')}; the engine obeys the saved window, the "
                         f"team did not",
        "WINDOW_CLEAR_BY": f"— {row.get('why_window', '')}",
        "HUMAN_TIGHT_TURN": (f"with {row['real_turn_buffer']} min of slack on the engine's static "
                             f"clock ({row.get('real_turn_reason', '')})"
                             + (f" — and that shortfall is built on {row.get('clock_defaults')}"
                                if row.get("clock_defaults")
                                else " — the team ran a turn the engine calls impossible")),
        "HUMAN_LONG_DAY": f"— {row.get('why_span', '')}",
        "HUMAN_POLICY_TIGHT": f"— {row.get('why_policy', '')}; it fits physically, the engine "
                              f"declines it on policy",
        "SPLIT_HALF_IGNORED": f"— {row.get('why_split', '')}",
        "ENGINE_MISSED_FREE": f"and it FITS his engine-built day as-is ({row['eng_legs']} jobs) — "
                              f"the engine simply did not seat it",
        "ENGINE_FILLED_DIFF": (f"but the engine filled his day differently ({row['eng_legs']} "
                               f"jobs; near this time: {row.get('eng_nearby') or 'nothing'}) so it "
                               f"no longer fits ({row.get('eng_reason', '')})"),
    }.get(c, "")
    extra = []
    if row.get("arrive_quality") == "ok" and row.get("arrive_min") not in (None, ""):
        m = int(row["arrive_min"])
        extra.append(f"in reality he reached the pickup {abs(m)} min "
                     f"{'after' if m > 0 else 'before'} the booked time")
    if row.get("next_arrive_quality") == "ok" and row.get("next_arrive_min") not in (None, ""):
        m = int(row["next_arrive_min"])
        extra.append(f"and reached the next job ({row.get('next_booked', '')}) {abs(m)} min "
                     f"{'late' if m > 0 else 'early'}")
    if row.get("rest"):
        extra.append("his real day also breaches overnight rest")
    if row.get("retime_min") not in (None, "") and abs(int(row["retime_min"])) >= RETIME_FLAG_MIN:
        extra.append(f"the pickup moved {int(row['retime_min']):+d} min after the night-before "
                     f"cutoff")
    s = f"{base} {why}"
    if extra:
        s += " (" + "; ".join(extra) + ")"
    return s


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--recent", type=int, default=10)
    ap.add_argument("--extra", nargs="*", default=[])
    ap.add_argument("--no-plan", action="store_true")
    args = ap.parse_args()
    t0 = time.time()
    try:                       # Windows consoles default to cp1252; the report uses arrows
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    con = C.connect()
    h = C.Horizon(con)
    C.preamble("18_hand_board_diff.py",
               "Build 4 step 2: where the Day-Builder loses to the hand board, trip by trip",
               h, ASSUMPTIONS)
    picks, recent = pick_dates(con, h, args.recent, args.extra)
    print(f"\nrecent built dates ({len(recent)}): {', '.join(str(d) for d in recent)}")
    if args.extra:
        print(f"extra: {', '.join(args.extra)}")

    g17 = load_module("gate17", "17_build3_gate.py")
    dtype, raw_rows, _dva_map = g17.load_raw(con, min(picks), max(picks))

    g17.django_on_copy()
    m09 = g17.load_09()
    import django.db.models as _dj_models
    _saved = {a: getattr(_dj_models, a) for a in ("Avg", "Count", "Q", "Sum", "F")}
    try:
        fg09, pp09, DRIVE, DEFAULT_DRIVE, catloc = m09.load_shipped()
    finally:
        for _a, _v in _saved.items():
            setattr(_dj_models, _a, _v)
    by_date = g17.raw_rows_by_date(raw_rows, m09, fg09, catloc)
    adjacent = defaultdict(list)
    for iso, rows_ in by_date.items():
        for r in rows_:
            if r["did"] is not None and dtype.get(r["did"]) == "inhouse":
                adjacent[(iso, r["did"])].append(r)

    from dispatching.models import SchedulerSettings
    from dispatching.scheduler import (
        preload_timing_cache, resolve_run_min_buffer, load_driver_min_buffers,
        load_all_driver_vtypes, effective_span_hours)
    from dispatching.car_share import build_sharer_partners, holders_by_unit
    from dispatching import feasibility_guards as fg
    from dispatching.standby_mints import FARMOUT_PREMIUM_PER_LEG
    from drivers.models import Driver, DriverVehicleAssignment, FleetVehicle
    global FARM_PREMIUM
    FARM_PREMIUM = float(FARMOUT_PREMIUM_PER_LEG)
    preload_timing_cache()
    cfg = SchedulerSettings.get_settings()
    g17.REST_MIN = float(cfg.rest_min_gap_minutes or 510)
    hard_cap = float(cfg.span_exception_max_hours)
    print(f"\nlive settings: rest {g17.REST_MIN:.0f} min, hard cap {hard_cap}h, engine share pad "
          f"{cfg.engine_share_pad_min} min, min turn buffer {cfg.min_turn_buffer}, "
          f"USE_STUB_WINDOWS={fg.USE_STUB_WINDOWS}")
    build_day_plan = None if args.no_plan else g17.load_optimizer()

    def vt(vehicle):
        try:
            return str(vehicle.vehicle_type.vehicle_type) if vehicle and vehicle.vehicle_type else ""
        except Exception:
            return ""

    trip_rows, per_date = [], []
    for day in picks:
        iso = day.isoformat()
        rows_ = by_date.get(iso, [])
        a6_ids = {r["id"] for r in rows_}
        real_assign = {r["id"]: r["did"] for r in rows_
                       if r["did"] is not None and dtype.get(r["did"]) == "inhouse"}
        human_farmed = {r["id"] for r in rows_
                        if r["did"] is not None and dtype.get(r["did"]) != "inhouse"}
        real_boards_raw = defaultdict(list)
        for r in rows_:
            if r["id"] in real_assign:
                real_boards_raw[r["did"]].append(r)
        pre_rest = g17.rest_breaches(iso, real_boards_raw, adjacent, m09.OCC)
        tapped = len(C.q(con, "SELECT DISTINCT s.leg_id FROM reservations_legstatus s "
                              "JOIN reservations_leg l ON l.id = s.leg_id "
                              "WHERE l.pickup_date = ? AND s.status IN "
                              "('on-location','picked-up')", (iso,)))

        saved = capture_day(day)
        n_cleared = g17.clear_day(day)
        print(f"\n{'=' * 78}\n{iso}: {len(a6_ids)} live legs; real board {len(real_assign)} "
              f"in-house on {len(set(real_assign.values()))} drivers; cleared {n_cleared} (cold)")
        if tapped == 0:
            print("  NOTE: zero driver taps on this date — the board is a PLAN, not an outcome; "
                  "no reality check is possible here")
        try:
            base_assign, legs, drivers, base_s = g17.run_baseline(day)
            legs_by_id = {l.id: l for l in legs}
            base = g17.board_metrics(base_assign, legs, drivers, day, a6_ids, hard_cap)
            print(f"  baseline : {base['coverage_pct']:.1f}%  {base['driver_days']} driver-days  "
                  f"{base['farm_a6']} farmed  ({base_s:.1f}s)")
            plan_assign = dict(base_assign)
            plan_pairs, additions, with_add, plan_s = None, [], None, 0.0
            plan_refused = ""
            plan = None
            if build_day_plan is not None:
                from dispatching.day_planner import PlanRefused
                t1 = time.time()
                try:
                    plan = build_day_plan(day, epsilon=0)
                except PlanRefused as ref:
                    plan_refused = ref.reason
                    print(f"  plan     : REFUSED — {ref.reason}  (diagnosing the baseline instead)")
                plan_s = time.time() - t1
            if plan is not None:
                plan_assign = dict(g17.plan_get(plan, "assignments") or {})
                plan_pairs = [tuple(p) for p in (g17.plan_get(plan, "dva_rows") or [])]
                additions = list(g17.plan_get(plan, "additions") or [])
                with_add = g17.plan_get(plan, "with_additions") or None
                pm = g17.board_metrics(plan_assign, legs, drivers, day, a6_ids, hard_cap)
                print(f"  plan     : {pm['coverage_pct']:.1f}%  {pm['driver_days']} driver-days  "
                      f"{pm['farm_a6']} farmed  ({plan_s:.1f}s, "
                      f"evals={g17.plan_get(plan, 'evaluations')}; "
                      f"{len(additions)} addition(s))")

            eng_in = {lid for lid in plan_assign if lid in a6_ids}
            hum_in = set(real_assign.keys())
            lost = sorted(hum_in - eng_in)
            gained = sorted(eng_in - hum_in)
            add_captured = set()
            for a in additions:
                add_captured.update(a.get("captured_leg_ids") or [])

            roster_ids = {d.id for d in drivers}
            _drs, driver_hours, flexible, driver_max_hours = g17.day_roster(day)
            capped_windows = {}
            for d in drivers:
                se = driver_hours.get(d.id)
                capped_windows[d.id] = fg.get_effective_window(d.id, configured={
                    "start": se[0] if se else None, "end": se[1] if se else None,
                    "max_hours": driver_max_hours.get(d.id), "flexible": d.id in flexible})
            all_ids = roster_ids | set(real_assign.values())
            drivers_all = list(Driver.objects.filter(id__in=all_ids).select_related("profile")
                               .prefetch_related("weekly_schedule", "date_overrides"))
            drivers_all_by_id = {d.id: d for d in drivers_all}
            dva_rows = list(DriverVehicleAssignment.objects.filter(date=day)
                            .select_related("vehicle", "vehicle__vehicle_type"))
            dva_by_driver = {r.driver_id: r for r in dva_rows}
            if plan_pairs and set(plan_pairs) != {(r.driver_id, r.vehicle_id)
                                                  for r in dva_rows if r.vehicle_id}:
                vehicles = {v.id: v for v in FleetVehicle.objects
                            .filter(id__in=[v for _d, v in plan_pairs])
                            .select_related("vehicle_type")}
                eng_rows = [DriverVehicleAssignment(date=day, driver_id=d_, vehicle=vehicles[v_])
                            for d_, v_ in plan_pairs if v_ in vehicles]
            else:
                eng_rows = dva_rows
            real_board = board_for(real_assign, legs, drivers_all, day, dva_rows)
            eng_board = board_for(plan_assign, legs, drivers, day, eng_rows)
            ctx = {
                "day": day, "cfg": cfg, "roster_ids": roster_ids, "legs_by_id": legs_by_id,
                "driver_hours": driver_hours, "flexible": flexible,
                "driver_max_hours": driver_max_hours, "capped_windows": capped_windows,
                "drivers_all_by_id": drivers_all_by_id, "dva_by_driver": dva_by_driver,
                "holders_by_unit": holders_by_unit(
                    (r.driver_id, r.vehicle_id) for r in dva_rows if r.vehicle_id),
                "vtypes": load_all_driver_vtypes(day, rows=dva_rows), "vt": vt,
                "real_board": real_board, "eng_board": eng_board,
                "partners_real": build_sharer_partners(all_ids, day, rows=dva_rows),
                "partners_eng": build_sharer_partners(roster_ids, day, rows=eng_rows),
                "run_min_buffer": resolve_run_min_buffer(None),
                "driver_min_buffers": load_driver_min_buffers(list(all_ids)),
                "pre_rest": pre_rest, "human_farmed": human_farmed,
            }

            hum_over_soft = hum_over_hard = 0
            for did, sched in real_board.items():
                if sched.slots:
                    _r, e = effective_span_hours(sched.slots, day)
                    hum_over_soft += e > fg.SPAN_SOFT_EFFECTIVE_HOURS
                    hum_over_hard += e > hard_cap
            eng_over_soft = eng_over_hard = 0
            for did, sched in eng_board.items():
                if sched.slots:
                    _r, e = effective_span_hours(sched.slots, day)
                    eng_over_soft += e > fg.SPAN_SOFT_EFFECTIVE_HOURS
                    eng_over_hard += e > hard_cap

            missing = sorted(set(real_assign.values()) - roster_ids)
            if missing:
                hum_counts = Counter(real_assign.values())
                parts = []
                for did in missing:
                    d = drivers_all_by_id.get(did)
                    if d is None or not d.is_active or d.driver_type != "inhouse":
                        why = "inactive / not in-house"
                    elif did not in dva_by_driver:
                        why = "no car row in Day Setup"
                    else:
                        eff = d.get_effective_availability(day)
                        if eff.get("exception_type") == "off":
                            why = "time-off exception"
                        else:
                            why = f"saved schedule says off on {day.strftime('%A')}s"
                    parts.append(f"{d or did} ({hum_counts.get(did, 0)} legs; {why})")
                print("  team drivers the engine could not use: " + "; ".join(parts))
            idle = [d for d in drivers if not eng_board[d.id].slots]
            if idle:
                hum_counts = Counter(real_assign.values())
                print("  engine left idle: " + "; ".join(
                    f"{d} (team gave him {hum_counts.get(d.id, 0)})" for d in idle))
            print(f"  human    : {100.0 * len(hum_in) / len(a6_ids):.1f}%  "
                  f"{len(set(real_assign.values()))} driver-days  {hum_over_soft} >13.5h  "
                  f"{hum_over_hard} >15h   | engine {eng_over_soft} >13.5h {eng_over_hard} >15h")
            print(f"  lost {len(lost)} (human kept, engine farmed)   gained {len(gained)} "
                  f"(engine kept, human farmed)   net {len(eng_in) - len(hum_in):+d}"
                  + (f"   D16 additions would recapture {len(set(lost) & add_captured)} of the lost"
                     if additions else ""))

            lost_by_driver = defaultdict(list)
            for lid in lost:
                lost_by_driver[real_assign[lid]].append(lid)
            turn_info = {}
            for H, ids in lost_by_driver.items():
                turn_info.update(resolve_tight_turns(H, ids, ctx))

            for lid in lost:
                leg = legs_by_id.get(lid)
                H = real_assign[lid]
                if leg is None:
                    continue
                flags, det = diagnose(leg, H, ctx, turn_info)
                delta, tag = retime_minutes(con, lid, day, leg.pickup_time)
                a_min, a_q = arrival_minutes(con, lid, day, leg.pickup_time)
                det["arrive_min"] = "" if a_min is None else a_min
                det["arrive_quality"] = a_q
                rs = ctx["real_board"].get(H)
                nxt = None
                if rs is not None:
                    later = sorted((s for s in rs.slots if s.pickup_time > leg.pickup_time),
                                   key=lambda s: s.pickup_time)
                    nxt = later[0] if later else None
                if nxt is not None:
                    n_min, n_q = arrival_minutes(con, nxt.leg_id, day, nxt.pickup_time)
                    det["next_leg_id"] = nxt.leg_id
                    det["next_booked"] = fmt_t(nxt.pickup_time)
                    det["next_arrive_min"] = "" if n_min is None else n_min
                    det["next_arrive_quality"] = n_q
                row = {"date": iso, "leg_id": lid, "pickup": fmt_t(leg.pickup_time),
                       "trip_type": leg.get_trip_type(),
                       "vclass": str(leg.effective_vehicle_type or ""),
                       "route": f"{short(leg.pickup_location)} → {short(leg.dropoff_location)}",
                       "human_driver_id": H, "cause": primary(flags), "flags": "|".join(flags),
                       "retime_min": "" if delta is None else delta, "retime_ref": tag,
                       "in_addition": lid in add_captured, "tapped_date": tapped > 0}
                row.update(det)
                trip_rows.append(row)
                print("   • " + explain(row))
            if gained:
                gl = ", ".join(f"{fmt_t(legs_by_id[g].pickup_time)} {legs_by_id[g].get_trip_type()}"
                               for g in gained if g in legs_by_id)
                print(f"   ○ engine kept, team farmed: {gl}")
            eng_in_with_add = ""
            if with_add and with_add.get("farm_outs") is not None:
                eng_in_with_add = len(a6_ids) - int(with_add.get("farm_outs"))
            per_date.append({"date": iso, "legs": len(a6_ids), "human_in": len(hum_in),
                             "human_dd": len(set(real_assign.values())),
                             "engine_in": len(eng_in),
                             "engine_dd": len(set(plan_assign.values())),
                             "engine_in_with_additions": eng_in_with_add,
                             "lost": len(lost), "gained": len(gained),
                             "net": len(eng_in) - len(hum_in),
                             "human_over_13_5": hum_over_soft, "human_over_15": hum_over_hard,
                             "engine_over_13_5": eng_over_soft, "engine_over_15": eng_over_hard,
                             "engine_idle_roster": len(idle),
                             "team_drivers_not_on_engine_roster": len(missing),
                             "legs_tapped": tapped, "plan_refused": plan_refused,
                             "baseline_s": round(base_s, 1), "plan_s": round(plan_s, 1)})
        finally:
            n_restored = restore_day(day, saved)
            print(f"  restored {n_restored}/{len(saved)} assignments on the copy")

    # ── ranking ──
    n_days = len(picks)
    by_cause = Counter(r["cause"] for r in trip_rows)
    C.hdr("CAUSE RANKING — trips the engine farmed that the team kept in-house  [measured]")
    print(f"{'cause':20s}{'trips':>6s}{'/day':>7s}{'$/28d':>9s}  what a fix would touch")
    rank_rows = []
    for cause, n in by_cause.most_common():
        per_day = n / n_days
        usd28 = per_day * 28 * FARM_PREMIUM
        print(f"{cause:20s}{n:6d}{per_day:7.2f}{usd28:9,.0f}  {FIX_TOUCHES.get(cause, '')}")
        rank_rows.append({"cause": cause, "trips": n, "trips_per_day": round(per_day, 2),
                          "usd_per_28d": round(usd28, 0),
                          "fix_touches": FIX_TOUCHES.get(cause, "")})

    choice = sum(n for c, n in by_cause.items()
                 if c in ("ENGINE_FILLED_DIFF", "ENGINE_MISSED_FREE"))
    defect = sum(n for c, n in by_cause.items()
                 if c in ("WINDOW_WRAP_BUG", "SHARE_DATA_INVALID", "SHARE_PARTNER_OFF",
                          "SPLIT_HALF_IGNORED", "WINDOW_CLEAR_BY"))
    bent = len(trip_rows) - choice - defect
    gained_total = sum(r["gained"] for r in per_date)
    print(f"\nnet accounting over {n_days} dates: {len(trip_rows)} lost, {gained_total} gained, "
          f"net {gained_total - len(trip_rows):+d}.")
    print(f"  {bent:3d} lost to a rule the team bent (hours, turns, windows, class)")
    print(f"  {defect:3d} lost to something the engine cannot see or reads wrong "
          f"(bad car rows, overnight windows, unmodelled splits, the clear-by reading)")
    print(f"  {choice:3d} lost to the engine's own placement choices — against {gained_total} "
          f"trips it kept that the team farmed")

    tt = [r for r in trip_rows if r["cause"] == "HUMAN_TIGHT_TURN"]
    with_def = [r for r in tt if r.get("clock_defaults")]
    print(f"\ntight turns: {len(tt)}; {len(with_def)} rest on the static clock's own DEFAULTS "
          f"(the flat 45-min dwell, the 35-min stand-in for an address pair the table cannot "
          f"price, or the cross-property Disney average)")
    real = [r for r in tt if r.get("next_arrive_quality") == "ok"]
    if real:
        vals = sorted(int(r["next_arrive_min"]) for r in real)
        print(f"  of the {len(real)} whose NEXT job has a usable on-location tap: the driver "
              f"reached it a median {vals[len(vals) // 2]:+d} min vs booked, worst {vals[-1]:+d}; "
              f"{sum(1 for v in vals if v > 15)} arrived more than 15 min late")
    nq = Counter(r.get("next_arrive_quality", "") for r in tt)
    print(f"  tap quality on those next jobs: {dict(nq)} "
          f"('batch' = taps entered together at drop-off, unusable)")

    sec_rest = sum(1 for r in trip_rows if r.get("rest"))
    sec_ret = sum(1 for r in trip_rows if r.get("retime_min") not in ("", None)
                  and abs(int(r["retime_min"])) >= RETIME_FLAG_MIN)
    print(f"\nsecondary flags: REST on {sec_rest} of {len(trip_rows)} lost trips; "
          f"RETIME >= {RETIME_FLAG_MIN} min on {sec_ret}")
    print(f"lost {len(trip_rows)} over {n_days} dates = {len(trip_rows) / n_days:.2f}/day "
          f"≈ ${len(trip_rows) / n_days * 28 * FARM_PREMIUM:,.0f} per 28 days at the "
          f"${FARM_PREMIUM} premium")

    C.hdr("PER DATE  [measured]")
    print(f"{'date':11s}{'legs':>5s}{'hum%':>7s}{'eng%':>7s}{'hum dd':>7s}{'eng dd':>7s}"
          f"{'lost':>6s}{'gain':>6s}{'net':>5s}{'h>13.5':>7s}{'h>15':>5s}{'e>13.5':>7s}{'e>15':>5s}")
    for r in per_date:
        print(f"{r['date']:11s}{r['legs']:5d}{100.0 * r['human_in'] / r['legs']:7.1f}"
              f"{100.0 * r['engine_in'] / r['legs']:7.1f}{r['human_dd']:7d}{r['engine_dd']:7d}"
              f"{r['lost']:6d}{r['gained']:6d}{r['net']:5d}{r['human_over_13_5']:7d}"
              f"{r['human_over_15']:5d}{r['engine_over_13_5']:7d}{r['engine_over_15']:5d}")

    if trip_rows:
        cols = ["date", "leg_id", "pickup", "trip_type", "vclass", "route", "human_driver_id",
                "driver", "unit", "cause", "flags", "saved_window", "stub_window", "cap",
                "real_span_raw", "real_span_eff", "real_turn_buffer", "real_turn_reason",
                "clock_defaults", "share_partner", "rest", "retime_min", "retime_ref",
                "eng_fit", "eng_reason", "eng_nearby", "eng_legs", "real_legs", "in_addition",
                "planned_hours", "arrive_min", "arrive_quality", "next_leg_id", "next_booked",
                "next_arrive_min", "next_arrive_quality", "tapped_date",
                "why", "why_window", "why_class", "why_share", "why_span", "why_policy",
                "why_split", "clear"]
        p = C.write_csv("18_lost_trips.csv", cols,
                        [[r.get(c, "") for c in cols] for r in trip_rows])
        print(f"\nWrote: {os.path.relpath(p, C.REPO_ROOT)}")
    if per_date:
        cols = list(per_date[0].keys())
        p = C.write_csv("18_per_date.csv", cols, [[r.get(c, "") for c in cols] for r in per_date])
        print(f"Wrote: {os.path.relpath(p, C.REPO_ROOT)}")
    if rank_rows:
        cols = list(rank_rows[0].keys())
        p = C.write_csv("18_cause_ranking.csv", cols,
                        [[r.get(c, "") for c in cols] for r in rank_rows])
        print(f"Wrote: {os.path.relpath(p, C.REPO_ROOT)}")
    print(f"\nruntime: {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
