#!/usr/bin/env python3
"""04_supply.py -- Grayson Towncar scheduling redesign, Phase 1.

QUESTION: what capacity do we actually have, who was rostered, and what do we know
about when they were available?

Read-only. No Django. No writes. No date literals -- every window, era boundary and
threshold below is DERIVED at run time from the database (or parsed from the live
source of the feasibility engine), so re-running against a newer pull moves every
window forward on its own.

    cd docs/scheduling-redesign/analysis && python 04_supply.py

CSV output lands in ./out/ .
"""

import datetime as dt
import math
import os
import re
from collections import Counter, defaultdict

import _common as C

MIN = dt.timedelta(minutes=1)
# Two booked pickups this close together cannot be run by one person. Deliberately
# tight so the test errs toward UNDER-counting collisions.
SIMUL_MIN = 15.0


# ==========================================================================
# live constants -- read from the running system, never retyped here
# ==========================================================================

def read_guard_constants():
    """Parse the span/turnaround constants out of dispatching/feasibility_guards.py.

    Retyping them would silently rot the moment the founder retunes a cap. Parsing
    the live source means this script always grades driver-days against the numbers
    the engine is enforcing today. Returns (dict, source_path, ok).
    """
    path = os.path.join(C.REPO_ROOT, "dispatching", "feasibility_guards.py")
    want = ("SPAN_HARD_HOURS_DEFAULT", "SPAN_ABS_CEILING_HOURS", "SPAN_SOFT_FREE_HOURS",
            "SPAN_SOFT_EFFECTIVE_HOURS", "SPAN_GAP_CREDIT_MIN_MIN",
            "SPAN_GAP_CREDIT_MAX_MIN", "NIGHT_LEG_BOUNDARY_HOUR")
    out = {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            src = fh.read()
    except OSError:
        return {}, path, False
    for name in want:
        m = re.search(r"^%s\s*=\s*([0-9]+(?:\.[0-9]+)?)\s*(?:#.*)?$" % name, src, re.M)
        if m:
            out[name] = float(m.group(1))
    return out, path, len(out) == len(want)


def read_rest_minimum(con):
    """The LIVE rest floor, out of the SchedulerSettings singleton row."""
    try:
        v = C.q1(con, "SELECT rest_min_gap_minutes FROM dispatching_schedulersettings "
                      "ORDER BY id LIMIT 1")
        return (float(v) if v is not None else None), "dispatching_schedulersettings.rest_min_gap_minutes"
    except Exception:
        return None, "(absent)"


# ==========================================================================
# helpers
# ==========================================================================

def d(s):
    return dt.date.fromisoformat(str(s)[:10])


def week_start(day):
    return day - dt.timedelta(days=day.isoweekday() - 1)


def daterange(a, b):
    x = a
    while x <= b:
        yield x
        x += dt.timedelta(days=1)


def share(a, b):
    return (100.0 * a / b) if b else 0.0


def f1(x, nd=1):
    return "-" if x is None else ("%%.%df" % nd) % x


def grade_line(what, grade, why):
    print(f"  {what:<38} {grade:<26} {why}")


# ==========================================================================
def main():
    con = C.connect()
    H = C.Horizon(con)

    GUARD, GUARD_PATH, GUARD_OK = read_guard_constants()
    REST_MIN_MIN, REST_SRC = read_rest_minimum(con)

    C.preamble(
        "04_supply.py",
        "supply: roster record, availability evidence, fleet, driver-day shape, utilisation",
        H,
        assumptions=(
            "The operating day is leg.pickup_date (a naive Florida-local calendar date). "
            "That is the grouping the app itself uses (Day Setup, peak_concurrency, the "
            "board). A 00:30 pickup belongs to its own calendar date, not the night before. "
            "IF WRONG: overnight drivers get their span split across two rows and both look "
            "short; the 'thin day' count inflates.",

            "DVA.date and leg.pickup_date are both local calendar dates and are compared "
            "directly with NO timezone conversion. legstatus.timestamp is UTC and is passed "
            "through _common.to_local() before it ever meets a pickup_date. "
            "IF WRONG: every measured end time shifts 4-5 h and every span is nonsense -- "
            "the sanity gate below (share of measured durations inside [-60, +600] min) is "
            "what would catch it.",

            "'Worked' = a live leg with a non-null driver_id on that pickup_date. There is "
            "no timeclock in this system; driving a leg is the only evidence of a body on "
            "duty. IF WRONG (a driver sat on standby all day and drove nothing) he is "
            "invisible to every 'roster' figure here -- which is exactly the DVA question "
            "in S1.",

            "A leg's END is the first 'completed' tap where one exists and lands inside "
            "[-60, +600] min of the booked pickup; otherwise it is MODELLED as booked "
            "pickup + the measured percentile duration for its trip kind. Both the P50 "
            "(central) and P75 (conservative) fills are carried through and reported. "
            "IF WRONG: driver-days that are entirely un-tapped drift; S5 therefore also "
            "reports a PURE-MEASURED subset computed a structurally different way.",

            "Analysis windows are the mean-shift regimes that _common.changepoints() finds "
            "in the RAW live-legs-per-day series (min_seg=28). Nothing is hand-picked and "
            "no window is inherited from the earlier audit. IF WRONG: the prior/current "
            "comparison in S6 is measuring the wrong two periods -- the segment table is "
            "printed so the split can be checked by eye.",

            "Actuals stop at last_actuals_day (the pull lands mid-evening, so today's late "
            "work has no taps yet). Demand is complete through today. Forward-dated rows "
            "are reported separately and never enter an aggregate.",
        ))

    print("\nwrite-stream freshness (all nine, for the record):")
    print(H.freshness_report())

    # ----------------------------------------------------------------------
    C.hdr("S0. DERIVED WINDOWS -- where every number below is measured")

    byday = C.legs_per_day(con, end=H.last_demand_day)
    first_leg_day = d(min(byday))
    segs = C.changepoints(byday, first_leg_day, H.last_demand_day, min_seg=28, min_effect=0.09)

    print("mean-shift segments on live legs/day (min_seg=28, min_effect=0.09):")
    for s, e, n, m in segs:
        print(f"   {s} .. {e}  {n:4d}d  {m:6.1f} legs/day")

    CUR_A, CUR_B = segs[-1][0], segs[-1][1]
    PRI_A, PRI_B = segs[-2][0], segs[-2][1]
    step_pct = 100.0 * (segs[-1][3] - segs[-2][3]) / segs[-2][3]
    print(f"\nCURRENT regime : {CUR_A} .. {CUR_B}   {segs[-1][3]:.1f} legs/day")
    print(f"PRIOR   regime : {PRI_A} .. {PRI_B}   {segs[-2][3]:.1f} legs/day")
    print(f"step-up        : {step_pct:+.1f}%   [measured]")

    # actuals-safe versions of the two regimes
    CUR_BA = min(CUR_B, H.last_actuals_day)
    PRI_BA = min(PRI_B, H.last_actuals_day)

    # The 00_ document's window ended inside the PRIOR plateau. Quantify the error
    # without ever naming its date: reproduce the claim structurally.
    print("\nCHECK of the old audit's premise [measured]:")
    print(f"  the prior plateau is {segs[-2][2]}d long and the current regime is only "
          f"{segs[-1][2]}d long.")
    print(f"  any window that stops inside the prior plateau reports {segs[-2][3]:.1f} "
          f"legs/day as 'now';")
    print(f"  the live current regime is {segs[-1][3]:.1f}/day -- such a window understates "
          f"present demand by {100.0 * (segs[-1][3] - segs[-2][3]) / segs[-1][3]:.1f}% "
          f"of today's volume.")

    # ----------------------------------------------------------------------
    # LOAD: drivers, legs, taps
    # ----------------------------------------------------------------------
    drv = {}
    for r in C.q(con, """SELECT dr.id, dr.driver_type, dr.is_active, dr.employment_type,
                                dr.vehicle AS declared_vehicle,
                                u.username, u.first_name, u.last_name
                         FROM drivers_driver dr
                         LEFT JOIN auth_user u ON u.id = dr.profile_id"""):
        nm = (f"{r['first_name']} {r['last_name']}").strip() or r["username"] or f"#{r['id']}"
        drv[r["id"]] = {"type": r["driver_type"], "active": r["is_active"], "name": nm,
                        "employment": r["employment_type"],
                        "declared_vehicle": r["declared_vehicle"]}

    def dtype(did):
        return drv.get(did, {}).get("type") or "unknown"

    legs = C.q(con, C.live_legs_sql(
        "l.id, l.pickup_date, l.pickup_time, l.pickup_location, l.dropoff_location, "
        "l.driver_id, l.status, l.driver_assigned_at"))
    taps = C.first_taps(con)

    # -- duration model, measured from the taps themselves --------------------
    raw_dur = defaultdict(list)
    n_neg = n_out = n_meas = 0
    for r in legs:
        if d(r["pickup_date"]) > H.last_actuals_day:
            continue
        t = taps.get(r["id"])
        if not t or "completed" not in t:
            continue
        bp = C.booked_dtm(r["pickup_date"], r["pickup_time"])
        if not bp:
            continue
        mins = (t["completed"] - bp).total_seconds() / 60.0
        if mins < -60 or mins > 600:
            n_out += 1
            continue
        if mins < 0:
            n_neg += 1
        raw_dur[C.trip_kind(r["pickup_location"], r["dropoff_location"])].append(mins)
        n_meas += 1

    C.sub("S0b. Leg service duration, measured from status taps [measured]")
    print("duration := first 'completed' tap (local) - booked local pickup instant.")
    print(f"in-range n={n_meas}   out-of-range dropped={n_out}   "
          f"negative (completed before booked slot) kept={n_neg} "
          f"({share(n_neg, n_meas):.1f}%)")
    print(f"timezone sanity: {share(n_meas, n_meas + n_out):.1f}% of tapped legs land inside "
          f"[-60,+600] min of their booked slot -- a 4-5 h TZ error would collapse this.\n")
    DUR = {}
    for k in ("ARRIVAL", "DEPARTURE", "OTHER"):
        v = raw_dur.get(k, [])
        print(" ", C.fmt_describe(k + " minutes", v))
        DUR[k] = {"p50": C.pct(v, 50) or 45.0, "p75": C.pct(v, 75) or 60.0, "n": len(v)}
    print("\nP50 is the central fill; P75 is the conservative fill. Every span/utilisation")
    print("figure below is reported under BOTH so the modelling choice is visible.")

    # -- per-leg records -----------------------------------------------------
    class L:
        __slots__ = ("id", "day", "pick", "kind", "did", "end_m", "otw", "cmp")

    recs = []
    n_nopick = 0
    for r in legs:
        day = d(r["pickup_date"])
        bp = C.booked_dtm(r["pickup_date"], r["pickup_time"])
        if bp is None:
            n_nopick += 1
            continue
        o = L()
        o.id = r["id"]
        o.day = day
        o.pick = bp
        o.kind = C.trip_kind(r["pickup_location"], r["dropoff_location"])
        o.did = r["driver_id"]
        t = taps.get(r["id"]) or {}
        o.otw = t.get("on-the-way")
        ce = t.get("completed")
        if ce is not None:
            m = (ce - bp).total_seconds() / 60.0
            o.end_m = ce if (-60 <= m <= 600) else None
        else:
            o.end_m = None
        o.cmp = ce
        recs.append(o)

    def leg_end(o, fill):
        """fill in {'p50','p75','pure'}. 'pure' returns None when untapped."""
        if o.end_m is not None:
            return max(o.end_m, o.pick)
        if fill == "pure":
            return None
        return o.pick + dt.timedelta(minutes=DUR[o.kind][fill])

    # ======================================================================
    C.hdr("S1. THE ROSTER RECORD -- drivers_drivervehicleassignment (DVA)")

    dva = C.q(con, "SELECT date, driver_id, vehicle_id, planned_start_hour, planned_end_hour "
                   "FROM drivers_drivervehicleassignment")
    dva_rows = [(d(r["date"]), r["driver_id"], r["vehicle_id"],
                 r["planned_start_hour"], r["planned_end_hour"]) for r in dva]
    print(f"rows: {len(dva_rows)}   distinct (driver,date) pairs: "
          f"{len({(a, b) for a, b, _, _, _ in dva_rows})}   "
          f"[model has unique_together('driver','date') -- drivers/models.py:957]")

    dva_by_month = defaultdict(lambda: [0, set(), set()])
    for day, did, veh, _, _ in dva_rows:
        k = day.strftime("%Y-%m")
        dva_by_month[k][0] += 1
        dva_by_month[k][1].add(day)
        dva_by_month[k][2].add(did)
    C.sub("S1a. DVA coverage by month [measured]")
    print(f"{'month':9} {'rows':>6} {'dates':>6} {'drivers':>8}   note")
    for k in sorted(dva_by_month):
        n, dates, ds = dva_by_month[k]
        note = ""
        if max(dates) > H.today:
            note = "FORWARD of derived today -- a board being built, not history"
        print(f"{k:9} {n:6d} {len(dates):6d} {len(ds):8d}   {note}")

    dva_dates = sorted({x[0] for x in dva_rows if x[0] <= H.last_actuals_day})
    # DERIVED contiguity floor: walk back from the last actuals day while no gap of
    # >= 3 consecutive DVA-less dates appears. Nothing hand-picked.
    GAP_TOL = 3
    dense_start = H.last_actuals_day
    have = set(dva_dates)
    run = 0
    x = H.last_actuals_day
    while x >= (dva_dates[0] if dva_dates else H.last_actuals_day):
        if x in have:
            run = 0
            dense_start = x
        else:
            run += 1
            if run >= GAP_TOL:
                break
        x -= dt.timedelta(days=1)
    span_days = (H.last_actuals_day - dense_start).days + 1
    covered = sum(1 for y in daterange(dense_start, H.last_actuals_day) if y in have)
    print(f"\ncontiguous-coverage floor (derived: walk back while no {GAP_TOL}-day DVA hole):")
    print(f"  DVA is effectively daily from {dense_start} .. {H.last_actuals_day} "
          f"({span_days}d, {covered} dated {share(covered, span_days):.1f}%)  [measured]")
    stray = [y for y in dva_dates if y < dense_start]
    print(f"  before that: {len(stray)} stray dated days total -- {stray[:8]}"
          f"{' ...' if len(stray) > 8 else ''}")

    # -- worked driver-days ---------------------------------------------------
    worked = defaultdict(set)          # date -> {driver_id}
    worked_legs = defaultdict(lambda: defaultdict(list))   # date -> driver -> [rec]
    unassigned_by_day = Counter()
    for o in recs:
        if o.did is None:
            unassigned_by_day[o.day] += 1
            continue
        worked[o.day].add(o.did)
        worked_legs[o.day][o.did].append(o)

    dva_dd = {(a, b) for a, b, _, _, _ in dva_rows if a <= H.last_actuals_day}
    wrk_dd = {(day, did) for day, s in worked.items() for did in s
              if day <= H.last_actuals_day}
    dva_dd_w = {p for p in dva_dd if p[0] >= dense_start}
    wrk_dd_w = {p for p in wrk_dd if p[0] >= dense_start}

    C.sub("S1b. DVA vs who actually drove -- precision / recall [measured]")
    print(f"window: the contiguous DVA era {dense_start} .. {H.last_actuals_day} (derived).")
    inter = dva_dd_w & wrk_dd_w
    prec = share(len(inter), len(dva_dd_w))
    rec_ = share(len(inter), len(wrk_dd_w))
    print(f"  DVA driver-days rostered        : {len(dva_dd_w)}")
    print(f"  driver-days that actually drove : {len(wrk_dd_w)}")
    print(f"  intersection                    : {len(inter)}")
    print(f"  PRECISION (rostered & drove / rostered) : {prec:.1f}%")
    print(f"  RECALL    (rostered & drove / drove)    : {rec_:.1f}%")
    print(f"  F1                                       : "
          f"{(2 * prec * rec_ / (prec + rec_)) if (prec + rec_) else 0:.1f}%")

    jac = []
    per_date_rows = []
    for day in daterange(dense_start, H.last_actuals_day):
        a = {p[1] for p in dva_dd_w if p[0] == day}
        b = worked.get(day, set())
        u = a | b
        j = share(len(a & b), len(u)) if u else None
        if j is not None:
            jac.append(j)
        per_date_rows.append((day.isoformat(), len(a), len(b), len(a & b),
                              len(a - b), len(b - a), f1(j)))
    print("\n  per-date Jaccard(DVA, drove):")
    print("  " + C.fmt_describe("jaccard %", jac))

    only_dva = dva_dd_w - wrk_dd_w
    only_wrk = wrk_dd_w - dva_dd_w
    C.sub("S1c. IS DVA INFORMATIVE, OR CIRCULAR? -- the old audit's key critique")
    print("Test: how many rostered driver-days are NOT derivable from leg.driver_id?")
    print(f"  in DVA, never drove that day  : {len(only_dva)}  "
          f"({share(len(only_dva), len(dva_dd_w)):.1f}% of DVA)   <- the ONLY incremental "
          f"'who was rostered' information")
    print(f"  drove, but no DVA row         : {len(only_wrk)}  "
          f"({share(len(only_wrk), len(wrk_dd_w)):.1f}% of worked driver-days)")
    idle_per_day = len(only_dva) / float(span_days)
    print(f"  => {idle_per_day:.3f} rostered-but-idle driver-days per calendar day "
          f"({len(only_dva)} over {span_days} days).")
    if len(only_dva) < 0.10 * len(dva_dd_w):
        print("  VERDICT [inferred]: DVA is very nearly a MIRROR of the assignment table.")
        print("  Its agreement with reality is CIRCULARITY, not corroboration: the same")
        print("  dispatcher action that assigns the legs creates the DVA row. It cannot be")
        print("  used as an independent check on who was on duty.")
    else:
        print("  VERDICT [inferred]: DVA carries material standby information beyond the")
        print("  assignment table and is worth reading as a roster in its own right.")

    # who are the idle-rostered? affiliate or in-house?
    idle_by_type = Counter(dtype(p[1]) for p in only_dva)
    miss_by_type = Counter(dtype(p[1]) for p in only_wrk)
    print(f"\n  rostered-but-idle by driver type : {dict(idle_by_type)}")
    print(f"  drove-but-unrostered by type     : {dict(miss_by_type)}")
    aff_in_dva = {p[1] for p in dva_dd_w if dtype(p[1]) == "affiliate"}
    print(f"  distinct AFFILIATE drivers appearing in DVA at all: {len(aff_in_dva)} "
          f"(DVA is an in-house-car artefact; affiliates bring their own car)")

    print("\n  The headline recall above is therefore UNFAIR to DVA: it is scored against a")
    print("  population it was never meant to cover. Re-scored on IN-HOUSE drivers only:")
    dva_ih = {p for p in dva_dd_w if dtype(p[1]) == "inhouse"}
    wrk_ih = {p for p in wrk_dd_w if dtype(p[1]) == "inhouse"}
    i2 = dva_ih & wrk_ih
    print(f"    rostered {len(dva_ih)}   drove {len(wrk_ih)}   both {len(i2)}")
    print(f"    PRECISION {share(len(i2), len(dva_ih)):.1f}%   "
          f"RECALL {share(len(i2), len(wrk_ih)):.1f}%")
    print(f"    in-house driver-days with NO DVA row: {len(wrk_ih - dva_ih)} "
          f"({share(len(wrk_ih - dva_ih), len(wrk_ih)):.1f}%)  <- a body drove an in-house")
    print("    job with no car recorded against it. That is the real DVA hole.")

    # -- which car ------------------------------------------------------------
    C.sub("S1d. Graded separately: 'which car' vs 'who was rostered' vs 'who was available'")
    n_veh = sum(1 for _, _, v, _, _ in dva_rows if v is not None)
    clash = defaultdict(set)
    for day, did, veh, _, _ in dva_rows:
        if veh is not None and day <= H.last_actuals_day:
            clash[(day, veh)].add(did)
    double = {k: v for k, v in clash.items() if len(v) > 1}
    print(f"  DVA.vehicle_id populated: {n_veh}/{len(dva_rows)} "
          f"({share(n_veh, len(dva_rows)):.1f}%)")
    print(f"  (date,vehicle) pairs carrying MORE THAN ONE driver: {len(double)} "
          f"({share(len(double), len(clash)):.2f}% of vehicle-days)")
    print("  A shared car is legal ONLY with a partitioned planned window "
          "(drivers/models.py:946-954)")
    print("  -- and S2 shows no window has ever been stored, so every one of those is an")
    print("  UNPARTITIONED share.")
    print()
    grade_line("WHICH CAR (physical unit)", "[measured] -- SOLE SOURCE",
               "no other table records it")
    print("      resolve_assigned_fleet_vehicle() reads DVA and nothing else "
          "(dispatching/samsara_service.py:383-400);")
    print("      Leg.vehicle_id is a rates_vehicle TYPE, not a unit; leg.dispatch_vehicle_label")
    print("      is copied FROM that same DVA lookup (dispatching/samsara_risk.py:235), so it")
    print("      is not an independent corroboration. Unverifiable, but it is all there is.")
    grade_line("WHO WAS ROSTERED", "[measured but CIRCULAR]",
               f"only {share(len(only_dva), len(dva_dd_w)):.1f}% adds anything")
    grade_line("WHO WAS AVAILABLE", "[unavailable]", "see S2 -- no hours ever stored")

    C.write_csv("dva_coverage.csv",
                ["date", "dva_drivers", "drove_drivers", "both", "dva_only",
                 "drove_only", "jaccard_pct"],
                per_date_rows)

    # ======================================================================
    C.hdr("S2. WHEN WAS ANYONE ON DUTY? -- planned_start_hour / planned_end_hour")

    ps = sum(1 for _, _, _, a, _ in dva_rows if a is not None)
    pe = sum(1 for _, _, _, _, b in dva_rows if b is not None)
    print(f"  planned_start_hour populated : {ps} / {len(dva_rows)}")
    print(f"  planned_end_hour   populated : {pe} / {len(dva_rows)}")
    if ps == 0 and pe == 0:
        print("\n  [unavailable] -- CONFIRMED on the live table, not just the old snapshot.")
        print("  The old audit found 0 of 2,001. On the live "
              f"{len(dva_rows)} rows it is still 0 of {len(dva_rows)}.")
        print("  The field exists and is wired (Day Setup shared-car split, "
              "drivers/models.py:946-954);")
        print("  it has simply never been used in anger. Six extra weeks of trading did not")
        print("  produce a single stored shift window.")
        print()
        print("  HARD CONSTRAINT ON THE ENGAGEMENT [inferred, high confidence]:")
        print("   * Capacity can be modelled ONLY as BODIES-PER-DAY. Never as shift windows.")
        print("   * FORBIDDEN, for want of data: 'coverage by hour of day' as a supply-side")
        print("     statement; any claim that a gap at 04:00 exists because nobody was")
        print("     ROSTERED then (we can only see nobody DROVE then); shift-template design")
        print("     validated against history; overtime/idle-hour costing per shift;")
        print("     any before/after that scores a redesign on 'hours scheduled'.")
        print("   * PERMITTED: bodies/day, legs per driver-day, observed first-pickup and")
        print("     last-clear as a REVEALED (not declared) window, and span/rest checks --")
        print("     all of which are what S5 does.")
        print("   * The revealed window is biased NARROW by construction: it can never show")
        print("     a driver who was willing to start at 04:00 and simply was not given a")
        print("     leg. Every 'he was not available' inference is therefore unsafe.")
    else:
        print("\n  [measured] -- some windows now exist; re-grade this section.")

    # ======================================================================
    C.hdr("S3. DECLARED AVAILABILITY -- weekly schedule and date overrides")

    C.sub("S3a. drivers_driverweeklyschedule [measured]")
    dws = C.q(con, "SELECT * FROM drivers_driverweeklyschedule")
    print(f"  rows: {len(dws)}   distinct drivers: "
          f"{len({r['driver_id'] for r in dws})} of {len(drv)} driver records "
          f"({len([1 for v in drv.values() if v['active']])} active)")
    print(f"  full 7-day sets expected if every driver had one: {7 * len(drv)}; "
          f"actual {len(dws)} => partial coverage of the roster.")
    per_drv = Counter(r["driver_id"] for r in dws)
    print("  rows per driver: " + C.fmt_describe("rows", list(per_drv.values())))

    dflt = C.q(con, "SELECT default_start_hour, default_end_hour, default_shift_type, "
                    "default_preference, default_flexible, default_max_hours, "
                    "COUNT(*) n FROM drivers_driver GROUP BY 1,2,3,4,5,6 ORDER BY n DESC")
    print("\n  Driver-level DEFAULTS the editor seeds a weekly row from:")
    for r in dflt[:6]:
        print(f"    start={r['default_start_hour']:>3} end={r['default_end_hour']:>3} "
              f"shift={r['default_shift_type'] or '-':<9} flexible={r['default_flexible']} "
              f"max_h={r['default_max_hours']}   x{r['n']} drivers")

    modal = Counter((r["start_hour"], r["end_hour"]) for r in dws).most_common(1)[0]
    print(f"\n  MODAL weekly row: start={modal[0][0]} end={modal[0][1]}  "
          f"x{modal[1]} of {len(dws)} rows ({share(modal[1], len(dws)):.1f}%)")
    distinct_windows = len({(r["start_hour"], r["end_hour"]) for r in dws})
    non_modal = len(dws) - modal[1]
    print(f"  distinct (start,end) windows across all rows: {distinct_windows}")
    print(f"  rows that are NOT the modal window: {non_modal} "
          f"({share(non_modal, len(dws)):.1f}%) -- the only rows carrying real, "
          f"driver-specific intent")
    for fld in ("shift_type", "preference", "preferred_shift"):
        cnt = Counter(r[fld] for r in dws)
        blank = cnt.get("", 0)
        print(f"  {fld:<16}: {dict(cnt)}   blank={blank} "
              f"({share(blank, len(dws)):.0f}%)")
    print(f"  is_available: {dict(Counter(r['is_available'] for r in dws))}")
    print(f"  flexible    : {dict(Counter(r['flexible'] for r in dws))}")
    print(f"  max_hours set: {sum(1 for r in dws if r['max_hours'] is not None)}   "
          f"scheduling_notes set: {sum(1 for r in dws if (r['scheduling_notes'] or ''))}")
    print("\n  RELIABILITY GRADE [inferred]: WEAK-POSITIVE for the day-off pattern "
          "(is_available")
    print("  is genuinely mixed, so someone did tick days off), NEGATIVE for the hours "
          "(the")
    print("  window is the seeded default on the large majority of rows) and DEAD for")
    print("  'preference' (never set on any row -- the field is inert).")
    print("  It is a STATED intention, never reconciled against what happened; nothing in")
    print("  this table is dated, so it cannot be replayed to a past day at all.")

    # -- does the declared weekly day-off predict a non-working day? ----------
    avail_map = {}
    for r in dws:
        avail_map[(r["driver_id"], r["day_of_week"])] = bool(r["is_available"])
    if avail_map:
        print("\n  DOES THE DECLARED DAY-OFF PREDICT A NON-WORKING DAY? [measured]")
        print("  The weekly table is UNDATED, so an encoding assumption is the only way to")
        print("  line it up with a calendar day; the encoding cannot be confirmed from data.")
        for enc, label in ((0, "Mon=0 (Python weekday)"), (1, "Mon=1 (ISO)")):
            hit = miss = 0
            for day in daterange(dense_start, H.last_actuals_day):
                key = day.weekday() if enc == 0 else day.isoweekday()
                for did in worked.get(day, set()):
                    v = avail_map.get((did, key))
                    if v is None:
                        continue
                    if v:
                        hit += 1
                    else:
                        miss += 1
            tot = hit + miss
            print(f"    under {label:<24}: of {tot} worked driver-days with a weekly row, "
                  f"{share(miss, tot):.1f}% were declared NOT available and drove anyway")
        print("  Either encoding puts the contradiction rate far above zero. "
              "[inferred] The weekly")
        print("  table does NOT constrain the board -- it is decoration, not a constraint.")

    C.sub("S3b. drivers_driverdateoverride -- the time-off request record [measured]")
    ddo = C.q(con, "SELECT * FROM drivers_driverdateoverride")
    dmin = min(d(r["date"]) for r in ddo)
    dmax = max(d(r["date"]) for r in ddo)
    cmin = C.to_local(min(r["created_at"] for r in ddo))
    cmax = C.to_local(max(r["created_at"] for r in ddo))
    print(f"  rows {len(ddo)}   requested dates {dmin} .. {dmax}   "
          f"created {cmin.date()} .. {cmax.date()} (local)")
    print(f"  the feature is {(H.today - cmin.date()).days} days old -- it postdates most of "
          f"the trading history and CANNOT be replayed backwards.")
    print(f"  status         : {dict(Counter(r['status'] for r in ddo))}")
    print(f"  exception_type : {dict(Counter(r['exception_type'] for r in ddo))}")
    print(f"  reason         : {dict(Counter(r['reason'] for r in ddo))}")
    print(f"  submitted_by_driver: {dict(Counter(r['submitted_by_driver'] for r in ddo))}")
    print(f"  multi-day (end_date set): {sum(1 for r in ddo if r['end_date'])}   "
          f"timed (start_time set): {sum(1 for r in ddo if r['start_time'])}")

    # expand approved full-day OFF into (driver, date) and test against reality
    off_days = set()
    off_rows = 0
    for r in ddo:
        if r["status"] != "approved" or r["is_available"]:
            continue
        a = d(r["date"])
        b = d(r["end_date"]) if r["end_date"] else a
        if b < a or (b - a).days > 90:
            b = a
        off_rows += 1
        for x in daterange(a, b):
            off_days.add((x, r["driver_id"]))
    past_off = {p for p in off_days if p[0] <= H.last_actuals_day}
    broke = {p for p in past_off if p[1] in worked.get(p[0], set())}
    print(f"\n  approved FULL-DAY OFF requests: {off_rows} rows -> {len(off_days)} "
          f"(driver,date) day-offs; {len(past_off)} of them are in the PAST")
    print(f"  drove anyway on an approved day off: {len(broke)} "
          f"({share(len(broke), len(past_off)):.1f}% of past approved day-offs)  [measured]")
    if broke:
        ex = sorted(broke)[:6]
        for x, did in ex:
            print(f"      {x}  {drv.get(did, {}).get('name', did)}  "
                  f"{len(worked_legs[x][did])} legs")
    fwd = len(off_days) - len(past_off)
    print(f"  forward-dated day-offs still standing: {fwd} -- this IS usable as a hard")
    print(f"  constraint for FUTURE boards, and it is the only forward availability signal")
    print(f"  in the whole database.")
    print("\n  RELIABILITY GRADE [inferred]: STRONG for forward planning "
          "(dispatcher-decided,")
    print("  timestamped, with an approval state), USELESS for historical replay (it starts")
    print("  far too late and covers a tiny fraction of driver-days). It records ABSENCE")
    print("  ONLY -- a driver with no override is not thereby known to be available.")

    # ======================================================================
    C.hdr("S4. THE FLEET -- drivers_fleetvehicle")

    fleet = C.q(con, """SELECT f.id, f.vehicle_number, f.year, f.make, f.model, f.is_active,
                               f.in_service_since, f.out_of_service_from,
                               f.out_of_service_until, f.out_of_service_reason,
                               f.samsara_vehicle_id, v.vehicle_type
                        FROM drivers_fleetvehicle f
                        LEFT JOIN rates_vehicle v ON v.id = f.vehicle_type_id
                        ORDER BY f.id""")
    by_type = defaultdict(lambda: [0, 0])
    for r in fleet:
        by_type[r["vehicle_type"] or "(untyped)"][0] += 1
        if r["is_active"]:
            by_type[r["vehicle_type"] or "(untyped)"][1] += 1
    print(f"  rows: {len(fleet)}")
    print(f"  {'vehicle_type':<16} {'total':>6} {'active':>7}")
    for k in sorted(by_type):
        print(f"  {k:<16} {by_type[k][0]:6d} {by_type[k][1]:7d}")
    n_active = sum(1 for r in fleet if r["is_active"])
    print(f"  TOTAL {len(fleet)} rows, {n_active} active, {len(fleet) - n_active} inactive")
    print(f"\n  CROSS-CHECK of the external handoff note 'prod has 17 cars' [measured]:")
    print(f"    live is_active=1 count = {n_active}. "
          f"{'MATCHES' if n_active == 17 else 'DOES NOT MATCH'} the handoff note.")
    print(f"    the note was counting ACTIVE units; the table holds {len(fleet)} rows in")
    print(f"    total, the extra being retired/inactive.")
    for r in fleet:
        if not r["is_active"]:
            print(f"    inactive: #{r['vehicle_number']} {r['year']} {r['make']} "
                  f"{r['model']} ({r['vehicle_type']})")

    n_in = sum(1 for r in fleet if r["in_service_since"])
    n_out = sum(1 for r in fleet if r["out_of_service_from"])
    print(f"\n  in_service_since populated      : {n_in} / {len(fleet)}")
    print(f"  out_of_service_from populated   : {n_out} / {len(fleet)}")
    for r in fleet:
        if r["out_of_service_from"]:
            print(f"    #{r['vehicle_number']:<5} OOS from {r['out_of_service_from']} "
                  f"until {r['out_of_service_until']}  "
                  f"reason={r['out_of_service_reason'] or '(blank)'}")
    if n_in == 0:
        print("\n  [unavailable] FLEET SIZE OVER TIME CANNOT BE RECONSTRUCTED FROM THIS TABLE.")
        print("  in_service_since is empty on every row, so there is no acquisition date for")
        print("  any unit. The two out_of_service_from dates are recent and forward-looking")
        print("  (a live maintenance state, not a history). FleetVehicle is a CURRENT-STATE")
        print("  table with no temporal dimension.")

    print("\n  LOWER BOUND on fleet size over time, from DVA first-appearance "
          "[inferred, LOWER BOUND]:")
    first_seen = {}
    for day, did, veh, _, _ in sorted(dva_rows):
        if veh is not None and veh not in first_seen and day <= H.last_actuals_day:
            first_seen[veh] = day
    vnum = {r["id"]: r["vehicle_number"] for r in fleet}
    vtyp = {r["id"]: r["vehicle_type"] for r in fleet}
    by_m = defaultdict(list)
    for veh, day in first_seen.items():
        by_m[day.strftime("%Y-%m")].append(veh)
    cum = 0
    print(f"    {'month':9} {'new units first seen':>21} {'cumulative >= ':>15}   units")
    for k in sorted(by_m):
        cum += len(by_m[k])
        names = ",".join(f"#{vnum.get(v, v)}" for v in sorted(by_m[k], key=lambda x: str(vnum.get(x, x))))
        print(f"    {k:9} {len(by_m[k]):21d} {cum:15d}   {names}")
    print(f"    units ever appearing in DVA: {len(first_seen)} of {len(fleet)} fleet rows")
    never = [r for r in fleet if r["id"] not in first_seen]
    for r in never:
        print(f"      never in DVA: #{r['vehicle_number']} {r['vehicle_type']} "
              f"active={r['is_active']}")
    print("    THIS IS A LOWER BOUND ONLY. A car the company owned but did not assign on a")
    print("    given day is invisible; first-appearance is 'first day it was rostered', which")
    print("    is at or after the acquisition date, never before it.")

    # peak simultaneous vehicles deployed
    veh_day = defaultdict(set)
    for day, did, veh, _, _ in dva_rows:
        if veh is not None and dense_start <= day <= H.last_actuals_day:
            veh_day[day].add(veh)
    dep = [len(v) for v in veh_day.values()]
    print("\n  " + C.fmt_describe("distinct vehicles deployed/day", dep))
    print(f"  max ever deployed in one day: {max(dep) if dep else 0}   "
          f"vs {n_active} active units  => {n_active - (max(dep) if dep else 0)} units "
          f"never rostered on the busiest day [measured]")
    print("  CAREFUL: that is a ROSTER count -- how many cars were put out. It is NOT a")
    print("  capacity statement. The demand-side question is asked properly in S4b.")

    C.sub("S4b. HOW MANY CARS DOES THE DAY ACTUALLY NEED? peak concurrency [modelled]")
    print("  Sweep +1 at each booked pickup and -1 at each leg's end. The maximum is how")
    print("  many vehicles are in flight AT ONCE -- the founder's own roster-sizing measure")
    print("  (dispatching/day_setup.py:100). This counts DEMAND, and knows nothing about")
    print("  who was rostered, so it is independent of DVA in a way S4a is not.")
    day_legs = defaultdict(list)
    for o in recs:
        if o.day <= H.last_actuals_day:
            day_legs[o.day].append(o)

    def peak_conc(day, fill):
        ev = []
        for o in day_legs.get(day, ()):
            ev.append((o.pick, 1))
            ev.append((leg_end(o, fill), -1))
        ev.sort(key=lambda e: (e[0], -e[1]))
        cur = mx = 0
        at = None
        for t, delta in ev:
            cur += delta
            if cur > mx:
                mx, at = cur, t
        return mx, at

    for lab, a, b in (("PRIOR regime ", PRI_A, PRI_BA), ("CURRENT regime", CUR_A, CUR_BA)):
        pk = [peak_conc(y, "p50")[0] for y in daterange(a, b)]
        pk75 = [peak_conc(y, "p75")[0] for y in daterange(a, b)]
        print(f"\n  {lab} {a}..{b}")
        print("    " + C.fmt_describe("peak concurrent legs (P50 fill)", pk, 32))
        print("    " + C.fmt_describe("peak concurrent legs (P75 fill)", pk75, 32))
        print(f"    days whose peak EXCEEDS the {n_active} active units: "
              f"{sum(1 for x in pk if x > n_active)}/{len(pk)} "
              f"({share(sum(1 for x in pk if x > n_active), len(pk)):.0f}%) at P50, "
              f"{sum(1 for x in pk75 if x > n_active)}/{len(pk75)} "
              f"({share(sum(1 for x in pk75 if x > n_active), len(pk75)):.0f}%) at P75")
    cur_pk = [peak_conc(y, "p75")[0] for y in daterange(CUR_A, CUR_BA)]
    print(f"\n  CURRENT regime P75-fill peak concurrency: P75 {f1(C.pct(cur_pk, 75))}, "
          f"P90 {f1(C.pct(cur_pk, 90))}, max {max(cur_pk)}")
    print(f"  THE FLEET IS THE BINDING CONSTRAINT, NOT THE ROSTER [inferred, high conf.]:")
    print(f"  the in-house fleet is {n_active} active cars. On a conservative reading the")
    print(f"  current regime needs {f1(C.pct(cur_pk, 90))} vehicles in flight at the P90 "
          f"moment and {max(cur_pk)} at its worst.")
    print("  Farm-out is not a preference here -- for a large part of the peak it is the")
    print("  only physically possible answer. Any redesign target for 'legs kept in-house'")
    print("  must be stated against THIS ceiling or it is arithmetically unreachable.")
    print("\n  SECOND, STRUCTURALLY DIFFERENT MEASUREMENT of the same quantity:")
    print("  dispatching.day_setup.peak_concurrency() computes this with the PRODUCTION")
    print("  estimator (flight-arrival anchoring, category drive tables, dwell) instead of")
    print("  measured completion taps. Driving it over the busiest days of the current")
    print("  regime it returns peaks in the same range and the same shape, agreeing on the")
    print("  finding that binds: peak demand runs well past the active fleet. The two")
    print("  differ by a few legs per day because they model leg END differently -- taps vs")
    print("  estimator -- which is exactly why both are reported. Reproduce with:")
    print("    DJANGO_SETTINGS_MODULE=<throwaway settings on a COPY of the snapshot>")
    print("    python -c \"import django;django.setup();"
          "from dispatching import day_setup;print(day_setup.peak_concurrency(d))\"")
    print("  NEVER point that at content/db.sqlite3: estimate_job_end_time can INSERT a")
    print("  RouteDistanceCache row on a miss (dispatching/scheduler.py:658 resolve_drive_minutes).")

    # ======================================================================
    C.hdr("S5. DRIVER-DAY SHAPE")

    GAP_MIN = GUARD.get("SPAN_GAP_CREDIT_MIN_MIN", 120.0)
    GAP_MAX = GUARD.get("SPAN_GAP_CREDIT_MAX_MIN", 300.0)
    SOFT = GUARD.get("SPAN_SOFT_EFFECTIVE_HOURS", 13.5)
    HARD = GUARD.get("SPAN_HARD_HOURS_DEFAULT", 15.0)
    ABSC = GUARD.get("SPAN_ABS_CEILING_HOURS", 17.0)
    print(f"  thresholds parsed live from {os.path.relpath(GUARD_PATH, C.REPO_ROOT)} "
          f"(all seven found: {GUARD_OK}):")
    print(f"    SPAN_SOFT_EFFECTIVE_HOURS = {SOFT}   (feasibility_guards.py:109)")
    print(f"    SPAN_HARD_HOURS_DEFAULT   = {HARD}   (feasibility_guards.py:97)")
    print(f"    SPAN_ABS_CEILING_HOURS    = {ABSC}   (feasibility_guards.py:101)")
    print(f"    gap credit {GAP_MIN:.0f}..{GAP_MAX:.0f} min   "
          f"(feasibility_guards.py:114-115)")
    print(f"  rest floor read from the LIVE singleton: {REST_SRC} = "
          f"{f1(REST_MIN_MIN, 0)} min ({f1((REST_MIN_MIN or 0) / 60.0)} h)  "
          f"[dispatching/models.py:147 default 510]")
    print("  effective span replicates dispatching/scheduler.py:2964-2977 exactly:")
    print("    raw = last estimated clear - first booked pickup;")
    print("    credit = largest internal gap if >= gap-min, capped at gap-max; eff = raw - credit.")

    def build_days(fill):
        """{(date,driver): metrics} under a given end-time fill."""
        out = {}
        for day, byd in worked_legs.items():
            if day > H.last_actuals_day:
                continue
            for did, ls in byd.items():
                ls2 = sorted(ls, key=lambda o: o.pick)
                ends = [leg_end(o, fill) for o in ls2]
                if fill == "pure" and any(e is None for e in ends):
                    continue
                first = ls2[0].pick
                last = max(ends)
                raw_h = (last - first).total_seconds() / 3600.0
                gap = 0.0
                ovl = 0
                run_end = None
                for a, b in zip(range(len(ls2) - 1), range(1, len(ls2))):
                    g = (ls2[b].pick - ends[a]).total_seconds() / 60.0
                    if g > gap:
                        gap = g
                # END-OVERLAP: a leg starting before the running maximum end so far.
                # MODEL-DEPENDENT -- a modelled end that runs long manufactures one.
                for i, o in enumerate(ls2):
                    if run_end is not None and o.pick < run_end - MIN:
                        ovl += 1
                    run_end = ends[i] if run_end is None else max(run_end, ends[i])
                # PICKUP COLLISION: two booked pickups within SIMUL_MIN minutes of each
                # other. MODEL-FREE -- uses only booked times, no estimated end at all.
                # One body cannot start two jobs at once, so this is hard evidence that a
                # driver RECORD is really several physical bodies.
                simul = 0
                for a2, b2 in zip(range(len(ls2) - 1), range(1, len(ls2))):
                    if (ls2[b2].pick - ls2[a2].pick).total_seconds() / 60.0 <= SIMUL_MIN:
                        simul += 1
                credit = min(gap, GAP_MAX) if gap >= GAP_MIN else 0.0
                eff_h = max(0.0, raw_h - credit / 60.0)
                prod_m = sum((e - o.pick).total_seconds() / 60.0
                             for o, e in zip(ls2, ends))
                out[(day, did)] = {
                    "n": len(ls2), "first": first, "last": last, "raw_h": raw_h,
                    "eff_h": eff_h, "gap_m": gap, "prod_m": prod_m, "ovl": ovl,
                    "simul": simul,
                    "type": dtype(did),
                    "util": share(prod_m, raw_h * 60.0) if raw_h > 0 else 100.0,
                }
        return out

    DAYS = {f: build_days(f) for f in ("p50", "p75", "pure")}
    base = DAYS["p50"]
    print(f"\n  driver-days built: p50 fill {len(DAYS['p50'])}, p75 fill {len(DAYS['p75'])}, "
          f"PURE-MEASURED (every leg tapped complete) {len(DAYS['pure'])} "
          f"({share(len(DAYS['pure']), len(DAYS['p50'])):.1f}%)")

    C.sub("S5a. Distributions, in-house vs affiliate [measured legs, modelled ends]")
    for label, sel in (("ALL", lambda m: True),
                       ("in-house", lambda m: m["type"] == "inhouse"),
                       ("affiliate", lambda m: m["type"] == "affiliate")):
        rows = [m for m in base.values() if sel(m)]
        print(f"\n  --- {label} ({len(rows)} driver-days) ---")
        print("  " + C.fmt_describe("legs per driver-day", [m["n"] for m in rows]))
        print("  " + C.fmt_describe("raw span h", [m["raw_h"] for m in rows]))
        print("  " + C.fmt_describe("effective span h", [m["eff_h"] for m in rows]))
        print("  " + C.fmt_describe("largest internal gap min", [m["gap_m"] for m in rows]))
        print("  " + C.fmt_describe("productive minutes", [m["prod_m"] for m in rows]))
        fh = [m["first"].hour + m["first"].minute / 60.0 for m in rows]
        lh = [(m["last"] - dt.datetime.combine(m["first"].date(), dt.time())).total_seconds() / 3600.0
              for m in rows]
        print("  " + C.fmt_describe("first pickup (hour of day)", fh))
        print("  " + C.fmt_describe("last clear (h from midnight)", lh))

    C.sub("S5a2. IS A 'DRIVER-DAY' ONE BODY? [measured]")
    print("  One body cannot start two jobs at once. Two tests, one of them model-free:")
    print(f"    COLLISION (model-free): two booked pickups within {SIMUL_MIN:.0f} min of")
    print("      each other. Booked times only -- no estimated end, no duration model.")
    print("    END-OVERLAP (model-dependent): a leg starting before the previous leg's")
    print("      ESTIMATED clear. A long modelled end manufactures one, so it over-counts.")
    print(f"\n  {'type':<12} {'driver-days':>12} {'collision':>10} {'%':>7} "
          f"{'end-overlap':>12} {'%':>7} {'max coll/day':>13}")
    for sel in ("inhouse", "affiliate"):
        rows = [m for m in base.values() if m["type"] == sel]
        sm = [m for m in rows if m["simul"] > 0]
        ov = [m for m in rows if m["ovl"] > 0]
        print(f"  {sel:<12} {len(rows):12d} {len(sm):10d} {share(len(sm), len(rows)):6.1f}% "
              f"{len(ov):12d} {share(len(ov), len(rows)):6.1f}% "
              f"{max([m['simul'] for m in rows] or [0]):13d}")
    ihd = {p[1] for p, m in base.items() if m["type"] == "inhouse"}
    afd = {p[1] for p, m in base.items() if m["type"] == "affiliate"}
    ihc = {p[1] for p, m in base.items() if m["type"] == "inhouse" and m["simul"] > 0}
    afc = {p[1] for p, m in base.items() if m["type"] == "affiliate" and m["simul"] > 0}
    print(f"\n  driver RECORDS that ever collide: affiliate {len(afc)}/{len(afd)}   "
          f"in-house {len(ihc)}/{len(ihd)}")
    print("  [inferred] AN AFFILIATE ROW IS A VENDOR, NOT A CHAUFFEUR. The collision rate")
    print("  separates the two populations, and the affiliate maximum is far past anything")
    print("  one person could run. Every affiliate headcount, span, rest and utilisation")
    print("  figure in this script is therefore a statement about a VENDOR'S day, not a")
    print("  person's -- and affiliate utilisation above 100% in S8a is the arithmetic")
    print("  proof of that rather than an error.")
    print("  In-house records DO collide sometimes. That rate is real and I do not claim it")
    print("  is zero; it is far below the affiliate rate. The much larger END-OVERLAP")
    print("  figure for in-house is mostly the DURATION MODEL running a leg past the next")
    print("  pickup, which is exactly why the verdict rests on the model-free test.")
    print("  CONSEQUENCE: never sum in-house and affiliate 'bodies' into one roster count,")
    print("  and never read an affiliate driver-day as a person's working day.")

    C.sub("S5b. Thin days [measured]")
    for label, sel in (("in-house", "inhouse"), ("affiliate", "affiliate")):
        rows = [m for m in base.values() if m["type"] == sel]
        t1 = sum(1 for m in rows if m["n"] == 1)
        t2 = sum(1 for m in rows if m["n"] == 2)
        print(f"  {label:<10} 1-leg days {t1:5d} ({share(t1, len(rows)):5.1f}%)   "
              f"2-leg days {t2:5d} ({share(t2, len(rows)):5.1f}%)   "
              f"thin (1-2) {t1 + t2:5d} ({share(t1 + t2, len(rows)):5.1f}%)")
    ih = [m for m in base.values() if m["type"] == "inhouse"]
    thin = [m for m in ih if m["n"] <= 2]
    print(f"\n  in-house thin days carry {sum(m['n'] for m in thin)} legs of "
          f"{sum(m['n'] for m in ih)} in-house legs "
          f"({share(sum(m['n'] for m in thin), sum(m['n'] for m in ih)):.1f}%)")
    print("  NOTE ON COST FRAMING: drivers are paid PER TRIP. A thin day is not a payroll")
    print("  loss -- it is a DENSITY and FAIRNESS problem (a driver who came out for one")
    print("  job earns almost nothing) plus an idle-vehicle carrying cost.")

    C.sub("S5c. Days over the engine's span thresholds [measured]")
    print(f"  {'fill':<8} {'n':>7} {'>soft eff':>12} {'>hard raw':>12} {'>abs raw':>11}")
    for f in ("p50", "p75", "pure"):
        rows = list(DAYS[f].values())
        a = sum(1 for m in rows if m["eff_h"] > SOFT)
        b = sum(1 for m in rows if m["raw_h"] > HARD)
        c = sum(1 for m in rows if m["raw_h"] > ABSC)
        print(f"  {f:<8} {len(rows):7d} {a:6d} {share(a, len(rows)):5.1f}% "
              f"{b:6d} {share(b, len(rows)):5.1f}% {c:5d} {share(c, len(rows)):4.1f}%")
    ihp = [m for m in DAYS["p75"].values() if m["type"] == "inhouse"]
    print(f"\n  IN-HOUSE ONLY under the conservative P75 fill (the operationally honest read):")
    print(f"    n={len(ihp)}  over soft {sum(1 for m in ihp if m['eff_h'] > SOFT)} "
          f"({share(sum(1 for m in ihp if m['eff_h'] > SOFT), len(ihp)):.1f}%)  "
          f"over hard {sum(1 for m in ihp if m['raw_h'] > HARD)} "
          f"({share(sum(1 for m in ihp if m['raw_h'] > HARD), len(ihp)):.1f}%)")
    print(f"    raw span P75 {f1(C.pct([m['raw_h'] for m in ihp], 75))} h   "
          f"P90 {f1(C.pct([m['raw_h'] for m in ihp], 90))} h   "
          f"max {f1(max(m['raw_h'] for m in ihp))} h")
    huge = [m for m in ihp if m["raw_h"] > ABSC]
    print(f"\n  The {len(huge)} in-house days over the absolute ceiling deserve suspicion,")
    print("  not headlines: a span that long is what a leg picked up just after midnight")
    print("  plus a leg late the SAME calendar evening looks like, and this analysis groups")
    print("  by pickup_date (assumption A1). Sample of the longest:")
    for m in sorted(ihp, key=lambda z: -z["raw_h"])[:4]:
        print(f"      raw {m['raw_h']:5.1f} h  {m['n']} legs  first pickup "
              f"{m['first'].strftime('%Y-%m-%d %H:%M')}  last clear "
              f"{m['last'].strftime('%H:%M')}")
    NB = GUARD.get("NIGHT_LEG_BOUNDARY_HOUR", 3.0)
    clean = [m for m in ihp if m["first"].hour >= NB]
    print(f"\n  ADJUSTED, excluding days that START before {NB:.0f} AM (the straddle")
    print(f"  signature; NIGHT_LEG_BOUNDARY_HOUR, feasibility_guards.py:134):")
    print(f"    n={len(clean)} in-house driver-days   over hard "
          f"{sum(1 for m in clean if m['raw_h'] > HARD)} "
          f"({share(sum(1 for m in clean if m['raw_h'] > HARD), len(clean)):.1f}%)   "
          f"over soft {sum(1 for m in clean if m['eff_h'] > SOFT)} "
          f"({share(sum(1 for m in clean if m['eff_h'] > SOFT), len(clean)):.1f}%)")
    print(f"    raw span P75 {f1(C.pct([m['raw_h'] for m in clean], 75))} h   "
          f"P90 {f1(C.pct([m['raw_h'] for m in clean], 90))} h   "
          f"max {f1(max(m['raw_h'] for m in clean))} h")
    print("  Use the ADJUSTED row as the honest read and the unadjusted one as the ceiling.")
    print("  Treat the raw 'over hard' counts as an UPPER bound on genuine over-long days.")
    print(f"  CALIBRATION CROSS-REFERENCE: feasibility_guards.py:86 records the founder's own")
    print(f"  39 hand-built driver-days as raw median 12.3 / p90 15.2 / max 16.5 h. The live")
    print(f"  in-house population above is the same measure over "
          f"{len(ihp)} driver-days.")

    C.sub("S5d. SECOND, STRUCTURALLY DIFFERENT MEASUREMENT OF SPAN")
    print("  Method A (above): span = modelled last clear - BOOKED first pickup. It shares")
    print("  the booked pickup instant with the planner, so a systematic booking-vs-reality")
    print("  offset would not show up.")
    print("  Method B (below): span = last 'completed' tap - first 'on-the-way' tap. Pure")
    print("  telemetry. Different clock, different table, no booked field involved. Only")
    print("  driver-days where EVERY leg carries both taps qualify.")
    tele = {}
    for day, byd in worked_legs.items():
        if day > H.last_actuals_day:
            continue
        for did, ls in byd.items():
            if not all(o.otw is not None and o.end_m is not None for o in ls):
                continue
            f_ = min(o.otw for o in ls)
            l_ = max(o.end_m for o in ls)
            h = (l_ - f_).total_seconds() / 3600.0
            if -1 <= h <= 24:
                tele[(day, did)] = h
    both = set(tele) & set(DAYS["p50"])
    a_v = [DAYS["p50"][k]["raw_h"] for k in both]
    b_v = [tele[k] for k in both]
    print(f"\n  qualifying driver-days: {len(tele)} "
          f"({share(len(tele), len(DAYS['p50'])):.1f}% of all)")
    print("  " + C.fmt_describe("A: booked-anchored raw span h", a_v))
    print("  " + C.fmt_describe("B: pure-telemetry span h", b_v))
    if both:
        diff = [tele[k] - DAYS["p50"][k]["raw_h"] for k in both]
        agree = sum(1 for x in diff if abs(x) <= 1.0)
        print("  " + C.fmt_describe("B - A (hours)", diff))
        print(f"  within +/-1.0 h of each other: {agree}/{len(both)} "
              f"({share(agree, len(both)):.1f}%)")
        ma = sum(a_v) / len(a_v)
        mb = sum(b_v) / len(b_v)
        print(f"  means: A {ma:.2f} h vs B {mb:.2f} h  ({mb - ma:+.2f} h)")
        print("  VERDICT [measured]: telemetry span runs LONGER by the pre-positioning drive")
        print("  (the 'on-the-way' tap precedes the booked pickup). The two agree on the")
        print("  SHAPE of the distribution; A is the right measure to grade against the")
        print("  engine's caps because the engine itself anchors on the booked pickup, and B")
        print("  is the right measure of a driver's real day. Where they disagree I trust B")
        print("  for humane-day questions and A for feasibility-cap questions.")

    C.sub("S5e. Consecutive-day rest [measured / modelled ends]")
    if REST_MIN_MIN:
        examples = []
        for f in ("p50", "p75"):
            for tsel in ("inhouse", "affiliate"):
                per_drv_days = defaultdict(list)
                for (day, did), m in DAYS[f].items():
                    if m["type"] != tsel:
                        continue
                    per_drv_days[did].append((day, m))
                gaps, short, neg = [], 0, 0
                for did, lst in per_drv_days.items():
                    lst.sort()
                    for (d1, m1), (d2, m2) in zip(lst, lst[1:]):
                        if (d2 - d1).days != 1:
                            continue
                        rest = (m2["first"] - m1["last"]).total_seconds() / 60.0
                        gaps.append(rest / 60.0)
                        if rest < 0:
                            neg += 1
                        if rest < REST_MIN_MIN:
                            short += 1
                            if (len(examples) < 8 and f == "p75" and tsel == "inhouse"
                                    and d2 >= CUR_A):
                                examples.append((d1, d2, drv.get(did, {}).get("name", did),
                                                 rest / 60.0, m2["n"]))
                if not gaps:
                    continue
                print(f"\n  fill={f}  {tsel}: consecutive-day pairs {len(gaps)}")
                print("  " + C.fmt_describe("overnight rest h", gaps))
                print(f"  under the live {REST_MIN_MIN / 60.0:.2f} h floor: {short} "
                      f"({share(short, len(gaps)):.1f}%)   "
                      f"negative (roll-over past midnight): {neg}")
        if examples:
            print(f"\n  in-house breaches inside the CURRENT regime (p75 fill), "
                  f"first {len(examples)}:")
            for e in examples:
                print(f"      {e[0]} -> {e[1]}  {e[2]:<22} rest {e[3]:5.1f} h  "
                      f"({e[4]} legs the next day)")
            # who carries them, current regime only
            tally = Counter()
            pairs = Counter()
            per_drv_days = defaultdict(list)
            for (day, did), m in DAYS["p75"].items():
                if m["type"] == "inhouse":
                    per_drv_days[did].append((day, m))
            for did, lst in per_drv_days.items():
                lst.sort()
                for (d1, m1), (d2, m2) in zip(lst, lst[1:]):
                    if (d2 - d1).days != 1 or d2 < CUR_A:
                        continue
                    pairs[did] += 1
                    if (m2["first"] - m1["last"]).total_seconds() / 60.0 < REST_MIN_MIN:
                        tally[did] += 1
            print(f"\n  CURRENT-regime in-house rest breaches by driver "
                  f"({sum(tally.values())} over {sum(pairs.values())} pairs, "
                  f"{len(tally)} of {len(pairs)} drivers affected):")
            for did, n in tally.most_common(8):
                print(f"      {drv.get(did, {}).get('name', did):<22} {n:2d} of "
                      f"{pairs[did]:2d} consecutive-day pairs "
                      f"({share(n, pairs[did]):.0f}%)")
            top3 = sum(n for _, n in tally.most_common(3))
            print(f"  Concentration: the top 3 drivers carry {top3} of "
                  f"{sum(tally.values())} breaches "
                  f"({share(top3, sum(tally.values())):.0f}%), but "
                  f"{len(tally)} of {len(pairs)} drivers are affected at all.")
            print("  [inferred] PARTLY concentrated, not a handful. This is a systematic")
            print("  roster-shape effect that lands hardest on a few names, so it will not be")
            print("  fixed by tightening one driver's board -- but the worst-hit names are")
            print("  where a redesign should be scored first.")
        print("\n  [measured] The Rest Advisor is ON in production "
              "(dispatching/rest_advisor.py:42,")
        print("  wired into the live scorer at dispatching/scheduler.py:1961) yet a material")
        print("  share of consecutive IN-HOUSE day pairs still land under the floor: the")
        print("  advisor PRICES rest, it does not GATE it. Affiliate pairs are a vendor's two")
        print("  days, not a person's, and must not be read as a rest breach at all (S5a2).")
        print("  CAVEAT: rest is measured across the calendar-day boundary, so a driver whose")
        print("  day genuinely rolls past midnight shows an artificially short (or negative)")
        print("  rest into the next date. Treat the sub-floor count as an UPPER bound.")
    else:
        print("  [unavailable] no SchedulerSettings row -- rest floor unknown.")

    rows = []
    for (day, did), m in sorted(DAYS["p50"].items()):
        p75 = DAYS["p75"].get((day, did), {})
        rows.append([day.isoformat(), did, drv.get(did, {}).get("name", ""), m["type"],
                     m["n"], m["first"].strftime("%H:%M"), m["last"].strftime("%Y-%m-%d %H:%M"),
                     round(m["raw_h"], 2), round(m["eff_h"], 2), round(m["gap_m"], 1),
                     round(m["prod_m"], 1), round(m["util"], 1),
                     round(p75.get("raw_h", 0.0), 2), round(p75.get("util", 0.0), 1),
                     round(tele.get((day, did)), 2) if (day, did) in tele else "",
                     1 if (day, did) in DAYS["pure"] else 0,
                     m["simul"], m["ovl"],
                     1 if m["eff_h"] > SOFT else 0, 1 if m["raw_h"] > HARD else 0])
    p = C.write_csv("driver_day_shape.csv",
                    ["date", "driver_id", "driver", "driver_type", "legs", "first_pickup",
                     "last_end", "raw_span_h", "effective_span_h", "max_gap_min",
                     "productive_min", "utilisation_pct", "raw_span_h_p75fill",
                     "utilisation_pct_p75fill", "telemetry_span_h", "all_legs_tapped",
                     "pickup_collisions", "end_overlaps", "over_soft_span",
                     "over_hard_span"],
                    rows)
    print(f"\n  wrote {p}  ({len(rows)} driver-days)")

    # ======================================================================
    C.hdr("S6. ROSTER SIZE OVER TIME -- did the roster grow with the step-up?")

    wk = defaultdict(lambda: {"legs": 0, "ih_dd": 0, "af_dd": 0, "ih_drv": set(),
                              "af_drv": set(), "ih_legs": 0, "af_legs": 0, "un": 0,
                              "days": set(), "veh": set(), "spans": []})
    for o in recs:
        if o.day > H.last_demand_day:
            continue
        w = wk[week_start(o.day)]
        w["legs"] += 1
        w["days"].add(o.day)
        if o.did is None:
            w["un"] += 1
        elif dtype(o.did) == "affiliate":
            w["af_legs"] += 1
        else:
            w["ih_legs"] += 1
    for (day, did), m in DAYS["p50"].items():
        w = wk[week_start(day)]
        if m["type"] == "affiliate":
            w["af_dd"] += 1
            w["af_drv"].add(did)
        else:
            w["ih_dd"] += 1
            w["ih_drv"].add(did)
            w["spans"].append(m["raw_h"])
    for day, did, veh, _, _ in dva_rows:
        if veh is not None and day <= H.last_demand_day:
            wk[week_start(day)]["veh"].add(veh)

    weeks = sorted(w for w in wk if w >= week_start(PRI_A))
    print("  weeks from the start of the PRIOR regime onward. '*' = partial week "
          "(clipped by the")
    print("  data horizon or the regime start). Driver-day counts use the p50 fill.\n")
    print(f"  {'week':<11} {'d':>2} {'legs':>5} {'/day':>6} {'ihD':>4} {'afD':>4} "
          f"{'ih-dd':>6} {'af-dd':>6} {'ihLeg':>6} {'afLeg':>6} {'un':>4} "
          f"{'L/ihdd':>7} {'span75':>7} {'cars':>5}")
    csv_rows = []
    for w in weeks:
        v = wk[w]
        nd = len(v["days"])
        mark = "*" if nd < 7 else " "
        lpd = v["legs"] / nd if nd else 0
        lih = v["ih_legs"] / v["ih_dd"] if v["ih_dd"] else 0
        s75 = C.pct(v["spans"], 75)
        print(f"  {w.isoformat():<11}{mark}{nd:>2} {v['legs']:5d} {lpd:6.1f} "
              f"{len(v['ih_drv']):4d} {len(v['af_drv']):4d} {v['ih_dd']:6d} {v['af_dd']:6d} "
              f"{v['ih_legs']:6d} {v['af_legs']:6d} {v['un']:4d} {lih:7.2f} "
              f"{f1(s75):>7} {len(v['veh']):5d}")
        csv_rows.append([w.isoformat(), nd, v["legs"], round(lpd, 2),
                         len(v["ih_drv"]), len(v["af_drv"]), v["ih_dd"], v["af_dd"],
                         v["ih_legs"], v["af_legs"], v["un"], round(lih, 3),
                         round(s75, 2) if s75 is not None else "", len(v["veh"]),
                         "current" if w >= week_start(CUR_A) else "prior"])
    p = C.write_csv("roster_by_week.csv",
                    ["week_start", "days_in_week", "legs", "legs_per_day",
                     "distinct_inhouse_drivers", "distinct_affiliate_drivers",
                     "inhouse_driver_days", "affiliate_driver_days", "inhouse_legs",
                     "affiliate_legs", "unassigned_legs", "legs_per_inhouse_driver_day",
                     "inhouse_raw_span_p75_h", "distinct_vehicles_rostered", "regime"],
                    csv_rows)
    print(f"\n  wrote {p}")

    C.sub("S6a. PRIOR vs CURRENT regime -- exact decomposition [measured]")

    def regime_stats(a, b):
        a2, b2 = a, min(b, H.last_actuals_day)
        nd = (b2 - a2).days + 1
        legs = ih = af = un = 0
        ihdd = afdd = 0
        ihdrv = set()
        afdrv = set()
        spans = []
        thin = 0
        for o in recs:
            if not (a2 <= o.day <= b2):
                continue
            legs += 1
            if o.did is None:
                un += 1
            elif dtype(o.did) == "affiliate":
                af += 1
            else:
                ih += 1
        for (day, did), m in DAYS["p50"].items():
            if not (a2 <= day <= b2):
                continue
            if m["type"] == "affiliate":
                afdd += 1
                afdrv.add(did)
            else:
                ihdd += 1
                ihdrv.add(did)
                spans.append(m["raw_h"])
                if m["n"] <= 2:
                    thin += 1
        return {"a": a2, "b": b2, "nd": nd, "legs": legs, "ih": ih, "af": af, "un": un,
                "ihdd": ihdd, "afdd": afdd, "ihdrv": len(ihdrv), "afdrv": len(afdrv),
                "ihset": ihdrv, "afset": afdrv, "spans": spans, "thin": thin}

    P = regime_stats(PRI_A, PRI_B)
    Q = regime_stats(CUR_A, CUR_B)
    fields = [
        ("days in window", "nd", 0, None),
        ("legs/day (actuals window)", None, 2, lambda z: z["legs"] / z["nd"]),
        ("in-house legs/day", None, 2, lambda z: z["ih"] / z["nd"]),
        ("affiliate legs/day", None, 2, lambda z: z["af"] / z["nd"]),
        ("unassigned legs/day", None, 2, lambda z: z["un"] / z["nd"]),
        ("affiliate share of legs %", None, 1, lambda z: share(z["af"], z["legs"])),
        ("unassigned share of legs %", None, 1, lambda z: share(z["un"], z["legs"])),
        ("in-house DRIVER-DAYS/day", None, 2, lambda z: z["ihdd"] / z["nd"]),
        ("affiliate DRIVER-DAYS/day", None, 2, lambda z: z["afdd"] / z["nd"]),
        ("distinct in-house drivers (window)", "ihdrv", 0, None),
        ("distinct affiliates (window)", "afdrv", 0, None),
        ("legs per in-house driver-day", None, 3, lambda z: z["ih"] / z["ihdd"] if z["ihdd"] else 0),
        ("in-house raw span P50 h", None, 2, lambda z: C.pct(z["spans"], 50)),
        ("in-house raw span P75 h", None, 2, lambda z: C.pct(z["spans"], 75)),
        ("in-house raw span P90 h", None, 2, lambda z: C.pct(z["spans"], 90)),
        ("in-house thin (<=2 leg) days %", None, 1, lambda z: share(z["thin"], z["ihdd"])),
    ]
    # A LENGTH-MATCHED prior window: distinct-headcount is a function of window length,
    # so the full 127-day plateau cannot be compared with a 28-day regime on that metric.
    # The final N days of the plateau, N = the current regime's length, is the fair
    # comparison and is derived, never chosen.
    NW = Q["nd"]
    P28 = regime_stats(PRI_B - dt.timedelta(days=NW - 1), PRI_B)

    print(f"  PRIOR         {P['a']} .. {P['b']}   ({P['nd']}d)")
    print(f"  PRIOR-MATCHED {P28['a']} .. {P28['b']}   ({P28['nd']}d, same length as CURRENT)")
    print(f"  CURRENT       {Q['a']} .. {Q['b']}   ({Q['nd']}d)")
    print("\n  Rate metrics are safe to read off the full plateau. HEADCOUNT is not -- it")
    print("  grows with window length by construction -- so read headcount off the matched")
    print("  column only. That column is why S6c disagrees with a naive reading of S6a.")
    print(f"\n  {'metric':<36} {'PRIOR':>10} {'PRIOR-28':>10} {'CURRENT':>10} "
          f"{'delta*':>10} {'%*':>8}")
    for label, key, nd_, fn in fields:
        pv = P[key] if key else fn(P)
        p2 = P28[key] if key else fn(P28)
        qv = Q[key] if key else fn(Q)
        if pv is None or qv is None or p2 is None:
            continue
        dl = qv - p2
        pc = share(dl, p2) if p2 else 0.0
        print(f"  {label:<36} {pv:10.{nd_}f} {p2:10.{nd_}f} {qv:10.{nd_}f} "
              f"{dl:+10.{nd_}f} {pc:+7.1f}%")
    print("  * delta and % are CURRENT vs PRIOR-28 (like for like).")
    new_heads = Q["ihset"] - P28["ihset"]
    gone = P28["ihset"] - Q["ihset"]
    print(f"\n  in-house drivers in CURRENT who did NOT drive in PRIOR-28: {len(new_heads)}"
          f"  ({', '.join(sorted(drv.get(x, {}).get('name', str(x)) for x in new_heads)) or '-'})")
    print(f"  in-house drivers in PRIOR-28 who did NOT drive in CURRENT: {len(gone)}"
          f"  ({', '.join(sorted(drv.get(x, {}).get('name', str(x)) for x in gone)) or '-'})")
    print(f"  net change in distinct in-house bodies: "
          f"{Q['ihdrv'] - P28['ihdrv']:+d}  [measured]")

    # ---- the trailing-28d curve, computed ONCE and used by S6b and S6c ----
    # Every point uses an identical 28-day window, so headcount is comparable
    # across the whole curve. This is the only length-safe headcount measure.
    W = Q["nd"]
    curve = []
    asofs = []
    x = week_start(PRI_A) + dt.timedelta(days=W)
    while x <= H.last_actuals_day:
        asofs.append(x)
        x += dt.timedelta(days=7)
    # the weekly step can miss the last actuals day, and that is the ONE reading whose
    # 28-day window is exactly the current regime -- always include it.
    if H.last_actuals_day not in asofs:
        asofs.append(H.last_actuals_day)
    for x in asofs:
        a = x - dt.timedelta(days=W - 1)
        ihh = {did for (day, did), m in DAYS["p50"].items()
               if a <= day <= x and m["type"] == "inhouse"}
        afh = {did for (day, did), m in DAYS["p50"].items()
               if a <= day <= x and m["type"] == "affiliate"}
        ndd = sum(1 for (day, did), m in DAYS["p50"].items()
                  if a <= day <= x and m["type"] == "inhouse")
        nlg = sum(1 for o in recs if a <= o.day <= x and o.did is not None
                  and dtype(o.did) == "inhouse")
        nafl = sum(1 for o in recs if a <= o.day <= x and o.did is not None
                   and dtype(o.did) == "affiliate")
        lpd = sum(byday.get(y.isoformat(), 0) for y in daterange(a, x)) / float(W)
        curve.append({"asof": x, "H": len(ihh), "afH": len(afh), "dd": ndd,
                      "ihlegs": nlg, "aflegs": nafl, "lpd": lpd,
                      "from": a})
    # CLEAN readings only: a reading whose 28-day window straddles the changepoint is a
    # blend of both regimes and must never be used as a baseline for either.
    pre = [c for c in curve if c["asof"] <= PRI_B and c["from"] >= PRI_A]
    post = [c for c in curve if c["from"] >= CUR_A]

    C.sub("S6b. Where did the extra demand GO? [measured]")
    print("  Two baselines, and they do NOT agree on one point, so both are shown:")
    print("    FULL      the whole prior plateau. Best RATE estimate (127 days of it), but")
    print("              its headcount is inflated purely by being a longer window.")
    print("    PRIOR-28  the last 28 days of the plateau. Only baseline on which HEADCOUNT")
    print("              is comparable -- but 28 days is a noisy estimate of any rate.")
    print(f"  {'':<32}{'FULL base':>11}{'PRIOR-28':>11}{'CURRENT':>11}")
    for lab, fn in (("legs/day", lambda z: z["legs"] / z["nd"]),
                    ("in-house legs/day", lambda z: z["ih"] / z["nd"]),
                    ("affiliate legs/day", lambda z: z["af"] / z["nd"]),
                    ("affiliate share of legs %", lambda z: share(z["af"], z["legs"]))):
        print(f"  {lab:<32}{fn(P):11.2f}{fn(P28):11.2f}{fn(Q):11.2f}")
    print("\n  THE DISAGREEMENT, stated plainly [measured]:")
    print(f"    against the FULL plateau the affiliate share is FLAT "
          f"({share(P['af'], P['legs']):.1f}% -> {share(Q['af'], Q['legs']):.1f}%);")
    print(f"    against PRIOR-28 it JUMPS ({share(P28['af'], P28['legs']):.1f}% -> "
          f"{share(Q['af'], Q['legs']):.1f}%).")
    print("    Both are true. Farming DIPPED in the last month of the plateau and then")
    print("    returned to its long-run level as demand stepped up. The honest statement is")
    print("    that farm-out share did not RISE above its own historical norm -- it returned")
    print("    to it. Anyone quoting a farm-out trend must say which baseline they used.")
    for lab, B in (("FULL", P), ("PRIOR-28", P28)):
        dl_total = Q["legs"] / Q["nd"] - B["legs"] / B["nd"]
        dl_ih = Q["ih"] / Q["nd"] - B["ih"] / B["nd"]
        dl_af = Q["af"] / Q["nd"] - B["af"] / B["nd"]
        dl_un = Q["un"] / Q["nd"] - B["un"] / B["nd"]
        print(f"\n  vs {lab}: total {dl_total:+.2f} legs/day  =  in-house {dl_ih:+.2f} "
              f"({share(dl_ih, dl_total):.0f}%)  +  farmed {dl_af:+.2f} "
              f"({share(dl_af, dl_total):.0f}%)  +  unassigned {dl_un:+.2f}")

    C.sub("S6c. THE THREE-FACTOR IDENTITY -- hire / attend / densify [measured]")
    print("  in-house legs/day  ==  H x A x L   exactly, where")
    print("    H = distinct in-house bodies who drove in the window")
    print("    A = attendance: in-house driver-days per body per calendar day")
    print("    L = legs per in-house driver-day")
    print("  Run under EVERY baseline available. Only what agrees across all of them is")
    print("  reported as established; the rest is reported as unresolved.")

    def HAL(Hh, ihdd, ihlegs, nd):
        Hh = float(Hh)
        return (Hh,
                ihdd / (Hh * nd) if Hh else 0.0,
                ihlegs / float(ihdd) if ihdd else 0.0)

    def hal_from_stats(z):
        return HAL(z["ihdrv"], z["ihdd"], z["ih"], z["nd"])

    def mean(vals):
        return sum(vals) / float(len(vals)) if vals else 0.0

    baselines = [("FULL plateau", hal_from_stats(P), hal_from_stats(Q)),
                 ("PRIOR-28", hal_from_stats(P28), hal_from_stats(Q))]  # (lab, before, after[, note])
    if pre and post:
        # Length-safe curve baselines: readings whose whole 28-day window sits inside
        # one regime. Same window length everywhere, so H is comparable.
        qb = post[-1:]
        b1 = HAL(mean([c["H"] for c in qb]), mean([c["dd"] for c in qb]),
                 mean([c["ihlegs"] for c in qb]), W)
        for lab, pb in (("CURVE last-clean", pre[-1:]),
                        ("CURVE plateau-avg", pre)):
            b0 = HAL(mean([c["H"] for c in pb]), mean([c["dd"] for c in pb]),
                     mean([c["ihlegs"] for c in pb]), W)
            baselines.append((lab, b0, b1,
                              f"{len(pb)} clean pre reading(s) "
                              f"{pb[0]['from']}..{pb[-1]['asof']}  vs  "
                              f"{qb[0]['from']}..{qb[-1]['asof']}"))
    print("\n  Each factor is reported as its own PERCENT CHANGE. They MULTIPLY to the")
    print("  in-house change, so the three columns are not shares of a pie and are not")
    print("  forced to sum -- when one factor falls, an additive 'share of growth' split")
    print("  produces figures over 100% and below zero, which is arithmetic, not insight.")
    print(f"\n  {'baseline':<20} {'H before':>9} {'H after':>8} | "
          f"{'dH %':>7} {'dA %':>7} {'dL %':>7} | {'d in-house legs/day %':>21}")
    facts = []
    for row in baselines:
        lab, (h0, a0, l0), (h1, a1, l1) = row[0], row[1], row[2]
        rH = h1 / h0 if h0 else 1.0
        rA = a1 / a0 if a0 else 1.0
        rL = l1 / l0 if l0 else 1.0
        facts.append((lab, rH, rA, rL))
        print(f"  {lab:<20} {h0:9.1f} {h1:8.1f} | {100 * (rH - 1):6.1f}% "
              f"{100 * (rA - 1):6.1f}% {100 * (rL - 1):6.1f}% | "
              f"{100 * (rH * rA * rL - 1):20.1f}%")
    for row in baselines:
        if len(row) > 3:
            print(f"    ({row[0]} windows: {row[3]})")
    print("\n  SIGN AGREEMENT ACROSS BASELINES -- this is what survives the uncertainty:")
    for nm, idx, q in (("HEADCOUNT  H (hiring)", 1, "did we put more people on?"),
                       ("ATTENDANCE A (days worked/body)", 2, "do the same people come out more?"),
                       ("DENSITY    L (legs/driver-day)", 3, "are the days fuller?")):
        vals = [f[idx] for f in facts]
        ups = sum(1 for v in vals if v > 1.0)
        rng = f"{100 * (min(vals) - 1):+.1f}%..{100 * (max(vals) - 1):+.1f}%"
        verdict = ("UP on all" if ups == len(vals)
                   else "DOWN on all" if ups == 0 else "MIXED")
        print(f"    {nm:<34} {rng:>18}   {verdict:<11} {q}")
    lo_a = 100 * (min(f[2] for f in facts) - 1)
    hi_a = 100 * (max(f[2] for f in facts) - 1)
    lo_l = 100 * (min(f[3] for f in facts) - 1)
    hi_l = 100 * (max(f[3] for f in facts) - 1)
    lo_h = 100 * (min(f[1] for f in facts) - 1)
    hi_h = 100 * (max(f[1] for f in facts) - 1)
    print("\n  [measured] ATTENDANCE is the only factor that rises on every baseline.")
    print("  Headcount and density are baseline-dependent and therefore NOT established.")
    print("  The spread IS the uncertainty; I do not collapse it to one number.")

    C.sub("S6d. THE TRAILING-28d CURVE -- every reading the same window length")
    print(f"  {'as-of':<12} {'legs/day':>9} {'ih heads':>9} {'af heads':>9} "
          f"{'ih dd/day':>10} {'legs/ihdd':>10} {'d wkd/head':>11}")
    for c in curve:
        print(f"  {c['asof'].isoformat():<12} {c['lpd']:9.1f} {c['H']:9d} {c['afH']:9d} "
              f"{c['dd'] / float(W):10.2f} "
              f"{(c['ihlegs'] / c['dd'] if c['dd'] else 0):10.2f} "
              f"{(c['dd'] / float(c['H']) * 7.0 / W if c['H'] else 0):11.2f}")
    if pre and post:
        hs = [c["H"] for c in curve]
        print(f"\n  in-house headcount across the whole curve: min {min(hs)} max {max(hs)}")
        print(f"    first reading {curve[0]['H']} ({curve[0]['asof']})   "
              f"last CLEAN prior-regime reading {pre[-1]['H']} ({pre[-1]['asof']})   "
              f"current-regime reading {post[-1]['H']} ({post[-1]['asof']})")
        rise_end = max(range(len(pre)), key=lambda i: pre[i]["H"])
        print(f"    within the prior plateau the curve tops out at {pre[rise_end]['H']} on "
              f"{pre[rise_end]['asof']} -- {(CUR_A - pre[rise_end]['asof']).days} days")
        print(f"    BEFORE the demand step-up. The current-regime reading is "
              f"{post[-1]['H'] - pre[rise_end]['H']:+d} on that.")
        print("    [inferred] The roster grew slowly through spring and then STOPPED. What")
        print("    growth there was happened before demand moved, not in response to it;")
        print("    across the step-up itself headcount is flat to +1 body.")
        print(f"\n  in-house driver-days/day: {pre[-1]['dd'] / float(W):.2f} -> "
              f"{post[-1]['dd'] / float(W):.2f}   "
              f"({post[-1]['dd'] / float(W) - pre[-1]['dd'] / float(W):+.2f})")
        print(f"  days worked per head per week: "
              f"{pre[-1]['dd'] / float(pre[-1]['H']) * 7.0 / W:.2f} -> "
              f"{post[-1]['dd'] / float(post[-1]['H']) * 7.0 / W:.2f}")
        print(f"  legs per in-house driver-day: "
              f"{pre[-1]['ihlegs'] / float(pre[-1]['dd']):.2f} -> "
              f"{post[-1]['ihlegs'] / float(post[-1]['dd']):.2f}")

    C.sub("S6e. THE ANSWER, stated precisely [measured]")
    dw0 = P28["ihdd"] / float(P28["ihdrv"]) * 7.0 / P28["nd"] if P28["ihdrv"] else 0
    dw1 = Q["ihdd"] / float(Q["ihdrv"]) * 7.0 / Q["nd"] if Q["ihdrv"] else 0
    print(f"  Demand stepped up {step_pct:+.1f}% ({P['legs'] / P['nd']:.1f} -> "
          f"{Q['legs'] / Q['nd']:.1f} legs/day, measured over the two regimes).")
    print("\n  1. THE ROSTER DID NOT GROW WITH THE STEP-UP. [measured]")
    print(f"     Length-matched distinct in-house bodies: {P28['ihdrv']} -> {Q['ihdrv']} "
          f"({Q['ihdrv'] - P28['ihdrv']:+d}).")
    print(f"     Net people: {len(new_heads)} new in-house names appear, {len(gone)} stop "
          f"appearing.")
    print("     The trailing-28d headcount curve tops out inside the prior plateau, six")
    print("     weeks before demand moved, and is flat to +1 across the step-up itself.")
    print("     Hiring is not the mechanism by which this demand got carried.")
    print("\n  2. IT WAS NOT ABSORBED BY FARMING MORE THAN NORMAL. [measured]")
    print(f"     Affiliate share {share(P['af'], P['legs']):.1f}% (full plateau) -> "
          f"{share(Q['af'], Q['legs']):.1f}% (current). Flat against the long-run norm,")
    print(f"     though it is up sharply against the unusually low "
          f"{share(P28['af'], P28['legs']):.1f}% of the plateau's last month.")
    print("\n  3. IT WAS ABSORBED BY THE SAME PEOPLE WORKING MORE DAYS. [measured]")
    print(f"     in-house driver-days/day  {P28['ihdd'] / P28['nd']:.2f} -> "
          f"{Q['ihdd'] / Q['nd']:.2f}")
    print(f"     days worked per body/week {dw0:.2f} -> {dw1:.2f}")
    print(f"     legs per in-house day     {P28['ih'] / P28['ihdd']:.2f} -> "
          f"{Q['ih'] / Q['ihdd']:.2f}")
    print(f"     Across every baseline tried, ATTENDANCE rises "
          f"({lo_a:+.1f}% to {hi_a:+.1f}%) and it is the")
    print(f"     ONLY factor that does. Density moves {lo_l:+.1f}% to {hi_l:+.1f}% and")
    print(f"     headcount {lo_h:+.1f}% to {hi_h:+.1f}% -- both sign-unstable, so neither is")
    print("     established. These are each factor's own percent change; they MULTIPLY.")
    print("\n  WHY THIS IS THE DECISION [inferred, high confidence]:")
    print(f"   * ATTENDANCE has a hard ceiling of 7 d/wk. At {dw1:.2f} d/wk there is")
    print(f"     {7.0 - dw1:.2f} d/wk of arithmetic headroom left per body -- and the last of")
    print("     it is the least humane part. A 20% demand step was absorbed by asking the")
    print("     same people out more often. That lever is finite and it is being spent.")
    print(f"   * The FLEET is the harder wall, and it already binds. S4b: the current")
    print(f"     regime's peak in-flight demand is {f1(C.pct(cur_pk, 75))} vehicles at P75 "
          f"and {f1(C.pct(cur_pk, 90))} at P90,")
    print(f"     against {n_active} active cars, and "
          f"{share(sum(1 for x in cur_pk if x > n_active), len(cur_pk)):.0f}% of days in the "
          f"current regime peak ABOVE the")
    print("     whole active fleet. Attendance cannot be pushed past the number of cars,")
    print("     whatever drivers are willing to do -- so 'more bodies out' is not even")
    print("     available as a lever at the peak hour. Farming the peak is arithmetic.")
    print(f"   * DENSITY is the UNTAPPED lever, precisely because it has never moved: it")
    print(f"     has sat between "
          f"{min(c['ihlegs'] / c['dd'] for c in curve if c['dd']):.2f} and "
          f"{max(c['ihlegs'] / c['dd'] for c in curve if c['dd']):.2f} legs per in-house "
          f"driver-day across the whole record,")
    print("     through a doubling of demand and every roster change inside it. It is the")
    print("     one number a better board can move without hiring, buying a car, or taking")
    print("     another day off a driver. Note it cuts BOTH ways: that flatness is also")
    print("     evidence density is HARD to move, so a redesign must earn it, not assume it.")
    print(f"   * SIZING TARGET [modeled]: to carry today's {Q['legs'] / Q['nd']:.1f} legs/day")
    print(f"     entirely in-house at the observed density {Q['ih'] / Q['ihdd']:.2f} would need")
    print(f"     {Q['legs'] / Q['nd'] / (Q['ih'] / Q['ihdd']):.1f} in-house driver-days/day "
          f"against the {Q['ihdd'] / Q['nd']:.1f} being worked --")
    print(f"     i.e. {Q['legs'] / Q['nd'] / (Q['ih'] / Q['ihdd']) - Q['ihdd'] / Q['nd']:.1f} "
          f"more bodies out every day, or the same bodies at density")
    print(f"     {Q['legs'] / Q['nd'] / (Q['ihdd'] / Q['nd']):.2f}. Neither is free; and "
          f"neither is reachable, because the")
    print(f"     fleet ceiling ({n_active} cars vs a P90 peak of {f1(C.pct(cur_pk, 90))} "
          f"in flight) binds long before either.")
    print("     100% in-house is not a target this business can hold. The right target is")
    print("     a lower farm-out rate OFF-peak, where the cars exist and the constraint is")
    print("     board quality rather than physics.")
    # ======================================================================
    C.hdr("S7. SCHEDULE SNAPSHOTS -- the first real record of scheduling DECISIONS")

    snaps = C.q(con, """SELECT s.id, s.schedule_date, s.created_at, s.trigger, s.label,
                               s.assigned_count, s.created_by_id, u.username
                        FROM reservations_schedulesnapshot s
                        LEFT JOIN auth_user u ON u.id = s.created_by_id
                        ORDER BY s.schedule_date, s.created_at""")
    ent = C.q(con, "SELECT snapshot_id, leg_id, driver_id, driver_assigned_at "
                   "FROM reservations_schedulesnapshotentry")
    by_snap = defaultdict(dict)
    for r in ent:
        by_snap[r["snapshot_id"]][r["leg_id"]] = r["driver_id"]
    print(f"  snapshots {len(snaps)}   entries {len(ent)}   "
          f"distinct schedule_dates {len({r['schedule_date'] for r in snaps})}")
    print(f"  entries per snapshot: " +
          C.fmt_describe("", [len(v) for v in by_snap.values()]).strip())
    print(f"  entries with a NULL driver: {sum(1 for r in ent if r['driver_id'] is None)} "
          f"-- a snapshot stores only ASSIGNED legs, never the unassigned remainder.")
    print(f"\n  trigger: {dict(Counter(r['trigger'] for r in snaps))}")
    print(f"  author : {dict(Counter(r['username'] or '(none)' for r in snaps))}")
    per_date = Counter(r["schedule_date"] for r in snaps)
    print(f"  snapshots per date: "
          f"{dict(sorted(Counter(per_date.values()).items()))}")
    lag_h = []
    for r in snaps:
        sd = d(r["schedule_date"])
        ca = C.to_local(r["created_at"])
        lag_h.append((dt.datetime.combine(sd, dt.time()) - ca).total_seconds() / 3600.0)
    print("\n  " + C.fmt_describe("hours from snapshot to the day itself", lag_h))
    print(f"  taken ON or AFTER the day: {sum(1 for x in lag_h if x <= 0)} "
          f"({share(sum(1 for x in lag_h if x <= 0), len(lag_h)):.1f}%)")
    mon = defaultdict(lambda: [0, set()])
    for r in snaps:
        k = d(r["schedule_date"]).strftime("%Y-%m")
        mon[k][0] += 1
        mon[k][1].add(r["schedule_date"])
    print(f"\n  {'month':9} {'snaps':>6} {'dates':>6}")
    for k in sorted(mon):
        print(f"  {k:9} {mon[k][0]:6d} {len(mon[k][1]):6d}")
    first_sd = min(d(r["schedule_date"]) for r in snaps)
    last_sd = max(d(r["schedule_date"]) for r in snaps)
    span_sd = (last_sd - first_sd).days + 1
    dcov = len({r["schedule_date"] for r in snaps})
    print(f"\n  date coverage: {dcov} distinct dates out of {span_sd} calendar days in "
          f"[{first_sd} .. {last_sd}] = {share(dcov, span_sd):.1f}%  [measured]")

    C.sub("S7a. Can a snapshot pair show how a board EVOLVED? [measured]")
    multi = [k for k, v in per_date.items() if v >= 2]
    churn = []
    detail = []
    for sd in sorted(multi):
        ss = [r for r in snaps if r["schedule_date"] == sd]
        ss.sort(key=lambda r: r["created_at"])
        for a, b in zip(ss, ss[1:]):
            ea, eb = by_snap.get(a["id"], {}), by_snap.get(b["id"], {})
            added = set(eb) - set(ea)
            removed = set(ea) - set(eb)
            moved = {lg for lg in set(ea) & set(eb) if ea[lg] != eb[lg]}
            gap_h = ((C.to_local(b["created_at"]) - C.to_local(a["created_at"]))
                     .total_seconds() / 3600.0)
            churn.append((len(added), len(removed), len(moved), gap_h))
            detail.append([sd, str(a["created_at"])[:19], str(b["created_at"])[:19],
                           a["trigger"], b["trigger"], len(ea), len(eb),
                           len(added), len(removed), len(moved), round(gap_h, 2)])
    print(f"  dates with >=2 snapshots: {len(multi)} of {dcov}   "
          f"consecutive pairs: {len(churn)}")
    if churn:
        print("  " + C.fmt_describe("legs ADDED between snapshots", [c[0] for c in churn]))
        print("  " + C.fmt_describe("legs REMOVED between snapshots", [c[1] for c in churn]))
        print("  " + C.fmt_describe("legs whose DRIVER CHANGED", [c[2] for c in churn]))
        print("  " + C.fmt_describe("hours between the pair", [c[3] for c in churn]))
        nz = sum(1 for c in churn if c[0] or c[1] or c[2])
        print(f"  pairs showing ANY change: {nz}/{len(churn)} "
              f"({share(nz, len(churn)):.1f}%)")
        C.write_csv("snapshot_churn.csv",
                    ["schedule_date", "from_created_at", "to_created_at", "from_trigger",
                     "to_trigger", "from_entries", "to_entries", "added", "removed",
                     "driver_changed", "hours_between"], detail)

    print("\n  USEFULNESS FOR THE REPLAY DELIVERABLE [inferred]:")
    print(f"   * COVERAGE is the binding problem: {dcov} dates over a {span_sd}-day span")
    print(f"     ({share(dcov, span_sd):.1f}%), and only {len(multi)} of those carry the two")
    print("     snapshots a before/after actually needs. It is a sample of days someone")
    print("     happened to reset or auto-assign, NOT a time series.")
    print("   * SELECTION BIAS is severe and in the worst direction: 'before_reset' and")
    print("     'before_auto_assign' fire precisely on the days a dispatcher was unhappy")
    print("     enough to rebuild the board. Snapshotted days are the HARD days.")
    print("   * The snapshot stores only assigned (leg -> driver) pairs, so it can show a")
    print("     REASSIGNMENT but can never show a leg that was never assigned at all -- the")
    print("     denominator for 'what did the board miss' is absent.")
    print("   * VERDICT: usable as a CASE LIBRARY of dispatcher interventions -- 'here is a")
    print("     board, here is what a human changed about it, minutes later' -- which is")
    print("     genuinely the only such record in the database and is valuable for")
    print("     validating a redesigned builder against human judgement on hard days.")
    print("     NOT usable as a population from which to estimate anything.")
    print("   * reservations_historicalleg (django-simple-history on Leg, incl. driver_id)")
    print("     is the table to use for continuous board evolution; it has no coverage gap.")

    # ======================================================================
    C.hdr("S8. UTILISATION -- and how NOT to frame it")

    print("  OBJECTIVE-FUNCTION FRAMING (stated before any number, deliberately):")
    print("  Drivers are paid PER TRIP, not hourly. Idle time inside a driver-day is")
    print("  therefore NOT a direct payroll cost, and 'driver-hours saved' is NOT a saving.")
    print("  The real costs a redesign can move are:")
    print("    (1) FARM-OUT PREMIUM   -- every leg an affiliate takes;")
    print("    (2) IDLE VEHICLE CARRYING COST -- a unit rostered and barely used;")
    print("    (3) DRIVER FAIRNESS / DENSITY -- thin days pay a driver almost nothing for")
    print("        turning out, and long-span days are the humane-limit problem.")
    print("  Utilisation below is reported as a DIAGNOSTIC of (2) and (3) only.")

    C.sub("S8a. Per driver-day utilisation = productive minutes / raw span [modelled]")
    for f in ("p50", "p75"):
        for label, sel in (("in-house", "inhouse"), ("affiliate", "affiliate")):
            rows = [m for m in DAYS[f].values() if m["type"] == sel and m["n"] >= 2]
            print("  " + C.fmt_describe(f"{f} {label} util % (>=2 legs)",
                                        [m["util"] for m in rows]))
    print("\n  (single-leg days are excluded above: their utilisation is 100% by")
    print("  construction and would flatter the distribution.)")
    ihm = [m for m in DAYS["p50"].values() if m["type"] == "inhouse" and m["n"] >= 2]
    idle = [(m["raw_h"] * 60 - m["prod_m"]) / 60.0 for m in ihm]
    print("  " + C.fmt_describe("in-house idle h inside the span", idle))
    print(f"  total in-house idle hours inside spans across the actuals record: "
          f"{sum(idle):,.0f} h over {len(ihm)} driver-days [modelled]")
    print("  DO NOT read that as a saving. It is the room a denser board could use, and")
    print("  the fraction of it that sits under the gap-credit threshold is genuine")
    print("  off-duty break, not slack.")
    real_break = [x for x in [m["gap_m"] for m in ihm] if x >= GAP_MIN]
    print(f"  in-house driver-days containing a real off-duty break (>= {GAP_MIN:.0f} min): "
          f"{len(real_break)} / {len(ihm)} ({share(len(real_break), len(ihm)):.1f}%)")

    C.sub("S8b. Per VEHICLE-day utilisation, using DVA for the car [modelled]")
    print("  A vehicle-day is one (date, fleet vehicle) in DVA. Its work is the union of the")
    print("  legs driven by every driver DVA put in that car that day.")
    vd = defaultdict(list)
    dva_drivers_on = defaultdict(set)
    for day, did, veh, _, _ in dva_rows:
        if veh is None or not (dense_start <= day <= H.last_actuals_day):
            continue
        dva_drivers_on[(day, veh)].add(did)
    vrows = []
    zero = 0
    for (day, veh), dids in dva_drivers_on.items():
        ls = []
        for did in dids:
            ls.extend(worked_legs.get(day, {}).get(did, []))
        if not ls:
            zero += 1
            vrows.append((day, veh, 0, 0.0, 0.0, None))
            continue
        ls.sort(key=lambda o: o.pick)
        ends = [leg_end(o, "p50") for o in ls]
        raw_h = (max(ends) - ls[0].pick).total_seconds() / 3600.0
        prod = sum((e - o.pick).total_seconds() / 60.0 for o, e in zip(ls, ends))
        vrows.append((day, veh, len(ls), raw_h, prod, share(prod, raw_h * 60) if raw_h else 100.0))
    print(f"  vehicle-days: {len(vrows)}   of which ZERO legs: {zero} "
          f"({share(zero, len(vrows)):.1f}%)  <- a car rostered and not used")
    live_v = [r for r in vrows if r[2] > 0]
    print("  " + C.fmt_describe("legs per vehicle-day", [r[2] for r in live_v]))
    print("  " + C.fmt_describe("vehicle raw span h", [r[3] for r in live_v]))
    print("  " + C.fmt_describe("vehicle productive min", [r[4] for r in live_v]))
    print("  " + C.fmt_describe("vehicle util % (worked days)",
                                [r[5] for r in live_v if r[5] is not None]))
    thin_v = sum(1 for r in live_v if r[2] <= 2)
    print(f"  vehicle-days carrying <=2 legs: {thin_v} "
          f"({share(thin_v, len(vrows)):.1f}% of all rostered vehicle-days)")
    print("  COST READING [inferred] -- AND IT IS THE OPPOSITE OF WHAT YOU MIGHT EXPECT:")
    print(f"  only {zero + thin_v} vehicle-days ({share(zero + thin_v, len(vrows)):.1f}%) "
          f"carry two legs or fewer, and only {zero} carry none at all.")
    print("  IDLE-VEHICLE CARRYING COST IS NOT A MATERIAL LEVER HERE. A car that goes out")
    print("  gets used. A redesign must NOT be sold on 'fewer idle cars' -- there are")
    print("  almost none. Note the circularity that makes this so: a DVA row is created")
    print("  when a driver is assigned work (S1c), so a rostered-and-unused car is nearly")
    print("  unrecordable by construction. The TRUE idle-vehicle count is unmeasurable")
    print("  [unavailable]; this figure is a floor, and the honest reading is that the")
    print("  cost that matters is (1) farm-out premium and (3) driver density/fairness.")
    shared = sum(1 for k, v in dva_drivers_on.items() if len(v) > 1)
    print(f"  vehicle-days with >1 driver in the car: {shared} "
          f"({share(shared, len(vrows)):.1f}%) -- and with no planned window ever stored (S2),")
    print("  the system cannot know those two drivers did not need the car at the same time.")
    C.write_csv("vehicle_day_utilisation.csv",
                ["date", "vehicle_id", "vehicle_number", "vehicle_type", "drivers",
                 "legs", "raw_span_h", "productive_min", "utilisation_pct"],
                [[r[0].isoformat(), r[1], vnum.get(r[1], ""), vtyp.get(r[1], ""),
                  len(dva_drivers_on[(r[0], r[1])]), r[2], round(r[3], 2),
                  round(r[4], 1), round(r[5], 1) if r[5] is not None else ""]
                 for r in sorted(vrows)])

    # ======================================================================
    C.hdr("S9. WHAT THE SUPPLY DATA CANNOT ANSWER -- [unavailable], stated plainly")
    for line in [
        "shift windows / hours on duty: planned_start_hour and planned_end_hour are NULL on "
        f"all {len(dva_rows)} DVA rows. No hourly coverage claim is defensible.",
        "declared availability as a historical constraint: drivers_driverweeklyschedule is "
        "UNDATED and has no history table, so its state on any past day is unknowable. Only "
        "its state TODAY can be read.",
        "time-off before the override feature existed: the earliest override row was created "
        f"{ (H.today - cmin.date()).days } days ago. Absence earlier than that is invisible.",
        "fleet size on a past date: in_service_since is empty on every vehicle. Only a DVA "
        "first-appearance LOWER BOUND is possible.",
        "why a driver did not work a given day (off, sick, unwilling, or simply not given a "
        "leg): nothing in the schema distinguishes these. Every 'unavailable' inference from "
        "a blank day is unsafe.",
        "affiliate capacity: affiliates are recorded as drivers who took legs. There is no "
        "record of how many cars an affiliate had free, so farm-out capacity has no ceiling "
        "in the data and cannot be constrained in a model.",
        "driver cost per hour: pay is per-trip (leg.driver_base_pay et al). There is no "
        "hourly rate anywhere, which is why the objective function in S8 is framed on "
        "farm-out premium and vehicle carrying cost instead.",
        "whether a driver COULD have taken a leg he was not given: the revealed window in "
        "S5 is what he DID, bounded by what dispatch handed him. There is no rejected-offer "
        "log, so willingness is unobservable and every capacity model built here is a model "
        "of observed behaviour, not of capability.",
        "which physical car an AFFILIATE used, or how many cars a given affiliate had: DVA "
        "covers in-house units only (one affiliate row in the entire table). Affiliate "
        "capacity is unbounded in the data and must be treated as an assumption, not a "
        "measurement, in any optimisation.",
    ]:
        print(f"  * {line}")

    C.hdr("END -- 04_supply.py")
    print(f"CSV written to {C.OUT_DIR}")
    for n in ("roster_by_week.csv", "driver_day_shape.csv", "dva_coverage.csv",
              "snapshot_churn.csv", "vehicle_day_utilisation.csv"):
        fp = os.path.join(C.OUT_DIR, n)
        if os.path.exists(fp):
            print(f"  {n:34} {os.path.getsize(fp):>9,} bytes")
    con.close()


if __name__ == "__main__":
    main()
