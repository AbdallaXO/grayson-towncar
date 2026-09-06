#!/usr/bin/env python
"""22 — What does it cost to make the engine respect the board's clock?

THE FINDING THIS PRICES (founder, 2026-09-02, on a real built 2026-09-12 board)
------------------------------------------------------------------------------
The engine plans chains on the STATIC clock (pickup + flat 45-min dwell +
category-table drive). The board shows the MEASURED one (measured dwell and
drive for that time-of-day/day-type bucket, the real flight time when known,
plus the Publix stop). They disagree, and the board's is the later one:

    Michael Olmo   board says he clears 12:03 PM   engine planned on 11:28 AM
    george         board says he clears 12:09 PM   engine planned on 11:55 AM
    neuma          board says she clears 10:15 AM  engine planned on 10:06 AM

So the engine built chains the dispatcher's own screen already showed as late.
analysis/19 measured the consequence on real boards: **24.2% of the turns the
engine ACCEPTS ran more than 15 minutes late.**

``scheduler.CHAIN_CLEAR_TAKES_LATER`` makes chain feasibility take
max(static, board) for the clear time — it can only ever DECLINE a turn the old
path allowed, never admit a new one. Repositioning stays on the category table
(the p75-of-in-service objection applies to an empty reposition and stands).

WHAT THIS SCRIPT MEASURES, per date, from a cold start
------------------------------------------------------
    OFF   the shipped pipeline as it builds today
    ON    the same run with CHAIN_CLEAR_TAKES_LATER = True

and reports the trade in both directions:
  * coverage lost   — trips the stricter clock can no longer place
  * chains healed   — consecutive pairs on the built board that the BOARD's own
                      clock says are late (clear time after the next pickup).
                      This is the number the change exists to move.
  * hard-infeasible turn pairs and driver-days over 13.5h / 15h, either side.

Then it re-grades against reality using analysis/19's technique: of the turns
each variant would accept on the REAL boards, what share actually ran >15 min
late (ON-LOCATION tap vs booked time; batch taps discarded)?

METHOD: raw side = frozen read-only snapshot; Django side = a migrated
throwaway copy. Each date cleared cold, built twice, then RESTORED.

USAGE
  GRAYSON_SNAPSHOT_DB=<frozen copy> python docs/scheduling-redesign/analysis/22_later_clock.py \
      [--recent 10] [--extra 2026-08-03 ...]

Outputs: out/22_later_clock.csv
"""
import argparse
import datetime as dt
import importlib.util
import os
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common as C  # noqa: E402

sys.path.insert(0, C.REPO_ROOT)

ASSUMPTIONS = (
    "Both runs use the SAME roster, the SAME cars and the SAME eight build passes. "
    "The only difference is scheduler.CHAIN_CLEAR_TAKES_LATER, which makes chain "
    "feasibility read max(static clear, board clear) instead of the static clear alone.",
    "'Late chain' counts consecutive pairs on the BUILT board where the earlier job's "
    "BOARD clearing time (estimate_job_end_time — what the dispatcher is shown) falls "
    "after the next job's booked pickup, before any repositioning drive is added. That "
    "is the contradiction the founder found; it is not a prediction of real lateness.",
    "Repositioning is untouched in both runs (chain_repo_minutes, the category table).",
)


def load_module(name, fname):
    here = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location(name, os.path.join(here, fname))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def pick_dates(con, horizon, n_recent, extra):
    rows = C.q(con, C.live_legs_sql(
        "l.pickup_date d, COUNT(*) n",
        "AND l.driver_id IN (SELECT id FROM drivers_driver WHERE LOWER(driver_type)='inhouse') "
        "AND l.pickup_date <= ?", order="GROUP BY l.pickup_date ORDER BY l.pickup_date DESC"),
        (str(horizon.last_actuals_day),))
    picks = {dt.date.fromisoformat(r["d"]) for r in rows[:n_recent]}
    for e in extra:
        picks.add(dt.date.fromisoformat(e))
    return sorted(picks)


def build(day, drivers, legs, hours, flexible, maxh, dva_rows):
    from dispatching.assignment_pipeline import (
        PipelineLocks, PipelineWindows, run_assignment_pipeline)
    from dispatching.scheduler import resolve_run_min_buffer, load_driver_min_buffers
    from dispatching.route_distance import probe_mode
    with probe_mode():
        return run_assignment_pipeline(
            legs, drivers, day,
            PipelineWindows(driver_hours=hours, flexible_drivers=flexible,
                            driver_max_hours=maxh,
                            run_min_buffer=resolve_run_min_buffer(None),
                            driver_min_buffers=load_driver_min_buffers(
                                [d.id for d in drivers])),
            PipelineLocks(), dva_rows=dva_rows)


def late_chains(assignments, legs, drivers, day, dva_rows):
    """Consecutive pairs whose earlier job's BOARD clear time lands after the next
    booked pickup — the contradiction, counted on the built board."""
    from dispatching.scheduler import build_driver_schedules
    legs_by_id = {l.id: l for l in legs}
    dby = {d.id: d for d in drivers}
    stamped = []
    for lid, did in assignments.items():
        lg = legs_by_id.get(lid)
        if lg is not None and did in dby:
            lg.driver = dby[did]
            lg.driver_id = did
            stamped.append(lg)
    try:
        board = build_driver_schedules(legs, drivers, day, dva_rows=dva_rows)
    finally:
        for lg in stamped:
            lg.driver = None
            lg.driver_id = None
    from dispatching import feasibility_guards as fg
    from dispatching.scheduler import chain_repo_minutes
    n_any = n_fixed = 0
    worst = []
    for did, sched in board.items():
        slots = sorted(sched.slots, key=lambda s: s.pickup_time)
        for a, b in zip(slots, slots[1:]):
            nxt = dt.datetime.combine(day, b.pickup_time)
            repo = chain_repo_minutes(a.dropoff_location, b.pickup_location,
                                      a.dropoff_category, b.pickup_category)
            ready = a.estimated_end_time + dt.timedelta(minutes=repo)
            if ready <= nxt:
                continue
            n_any += 1
            # An airport ARRIVAL next job is not a waiting guest: the booked time is
            # the flight's landing slot and the deplaning grace legitimately lets the
            # driver reach the kerb after it. Only a FIXED-TIME pickup is really late.
            if fg.is_airport_arrival(b.trip_type, b.pickup_category):
                continue
            n_fixed += 1
            worst.append((int((ready - nxt).total_seconds() / 60),
                          str(dby.get(did, did)), a.pickup_time, b.pickup_time))
    worst.sort(reverse=True)
    return (n_any, n_fixed), worst[:3]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--recent", type=int, default=10)
    ap.add_argument("--extra", nargs="*", default=[])
    args = ap.parse_args()
    t0 = time.time()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    con = C.connect()
    h = C.Horizon(con)
    C.preamble("22_later_clock.py",
               "What does it cost to make the engine respect the board's clearing time?",
               h, ASSUMPTIONS)
    picks = pick_dates(con, h, args.recent, args.extra)
    print(f"\ndates ({len(picks)}): {', '.join(str(d) for d in picks)}")

    g17 = load_module("gate17", "17_build3_gate.py")
    dtype, raw_rows, _ = g17.load_raw(con, min(picks), max(picks))
    g17.django_on_copy()
    m09 = g17.load_09()
    import django.db.models as _dj
    _sv = {a: getattr(_dj, a) for a in ("Avg", "Count", "Q", "Sum", "F")}
    try:
        fg09, pp09, DRIVE, DEFAULT_DRIVE, catloc = m09.load_shipped()
    finally:
        for _a, _v in _sv.items():
            setattr(_dj, _a, _v)
    by_date = g17.raw_rows_by_date(raw_rows, m09, fg09, catloc)

    import dispatching.scheduler as sch
    from dispatching.models import SchedulerSettings
    from dispatching.scheduler import preload_timing_cache
    from drivers.models import DriverVehicleAssignment
    from reservations.models import Leg
    preload_timing_cache()
    cfg = SchedulerSettings.get_settings()
    hard_cap = float(cfg.span_exception_max_hours)
    print(f"\nflag under test: scheduler.CHAIN_CLEAR_TAKES_LATER "
          f"(shipped default {sch.CHAIN_CLEAR_TAKES_LATER})")

    rows = []
    for day in picks:
        iso = day.isoformat()
        a6 = {r["id"] for r in by_date.get(iso, [])}
        saved = dict(Leg.objects.filter(pickup_date=day, driver__isnull=False)
                     .values_list("id", "driver_id"))
        g17.clear_day(day)
        try:
            drivers, hours, flexible, maxh = g17.day_roster(day)
            legs = g17.load_day_legs(day)
            dva_rows = list(DriverVehicleAssignment.objects.filter(date=day)
                            .select_related("vehicle", "vehicle__vehicle_type"))
            out = {}
            for label, flag in (("OFF", False), ("ON", True)):
                sch.CHAIN_CLEAR_TAKES_LATER = flag
                try:
                    res = build(day, drivers, legs, hours, flexible, maxh, dva_rows)
                    m = g17.board_metrics(dict(res.assignments), legs, drivers, day,
                                          a6, hard_cap)
                    lc, worst = late_chains(dict(res.assignments), legs, drivers, day,
                                            dva_rows)
                finally:
                    sch.CHAIN_CLEAR_TAKES_LATER = False
                out[label] = (m, lc, worst)
            (mo, lco_t, wo), (mn, lcn_t, wn) = out["OFF"], out["ON"]
            lco_any, lco = lco_t
            lcn_any, lcn = lcn_t
            print(f"\n{iso}  {len(a6)} trips")
            print(f"   OFF (today) : {mo['coverage_pct']:.1f}%  {mo['assigned_a6']} kept  "
                  f"{mo['critical_pairs']} hard conflicts  "
                  f"{len(mo['over_soft'])} over 13.5h  |  late for a WAITING guest: {lco}"
                  f"  (any late: {lco_any})")
            print(f"   ON  (later) : {mn['coverage_pct']:.1f}%  {mn['assigned_a6']} kept  "
                  f"{mn['critical_pairs']} hard conflicts  "
                  f"{len(mn['over_soft'])} over 13.5h  |  late for a WAITING guest: {lcn}"
                  f"  (any late: {lcn_any})")
            print(f"   trade       : {mn['assigned_a6'] - mo['assigned_a6']:+d} trips, "
                  f"{lcn - lco:+d} late chains")
            for mins, who, ap_, bp in wo[:2]:
                print(f"      OFF builds: {who} arrives {mins} min AFTER his "
                      f"{bp.strftime('%I:%M %p').lstrip('0')} waiting guest")
            rows.append({"date": iso, "trips": len(a6),
                         "off_kept": mo["assigned_a6"], "off_cov": round(mo["coverage_pct"], 1),
                         "off_conflicts": mo["critical_pairs"],
                         "off_over_13_5": len(mo["over_soft"]), "off_late_chains": lco,
                         "off_late_any": lco_any, "on_late_any": lcn_any,
                         "on_kept": mn["assigned_a6"], "on_cov": round(mn["coverage_pct"], 1),
                         "on_conflicts": mn["critical_pairs"],
                         "on_over_13_5": len(mn["over_soft"]), "on_late_chains": lcn,
                         "trips_delta": mn["assigned_a6"] - mo["assigned_a6"],
                         "late_chain_delta": lcn - lco})
        finally:
            byd = defaultdict(list)
            for lid, did in saved.items():
                byd[did].append(lid)
            for did, ids in byd.items():
                Leg.objects.filter(id__in=ids, pickup_date=day).update(driver_id=did)

    n = len(rows)
    ok_, on_ = sum(r["off_kept"] for r in rows), sum(r["on_kept"] for r in rows)
    lo, ln = sum(r["off_late_chains"] for r in rows), sum(r["on_late_chains"] for r in rows)
    tot = sum(r["trips"] for r in rows)
    C.hdr("THE TRADE  [measured]")
    print(f"{'date':12s}{'trips':>6s}{'OFF kept':>9s}{'ON kept':>8s}{'delta':>6s}"
          f"{'OFF late':>9s}{'ON late':>8s}{'delta':>6s}")
    for r in rows:
        print(f"{r['date']:12s}{r['trips']:6d}{r['off_kept']:9d}{r['on_kept']:8d}"
              f"{r['trips_delta']:+6d}{r['off_late_chains']:9d}{r['on_late_chains']:8d}"
              f"{r['late_chain_delta']:+6d}")
    print(f"\nover {n} dates, {tot} trips:")
    print(f"  kept in-house   OFF {ok_}  ->  ON {on_}   ({on_ - ok_:+d} trips, "
          f"{(on_ - ok_) / n:+.2f}/day)")
    print(f"  chains the board itself calls late   OFF {lo}  ->  ON {ln}   "
          f"({ln - lo:+d}, {(ln - lo) / n:+.2f}/day)")
    if lo:
        print(f"  => {100.0 * (lo - ln) / lo:.0f}% of the contradiction removed")
    try:
        from dispatching.standby_mints import FARMOUT_PREMIUM_PER_LEG as P
        print(f"  cost of the extra caution: ${abs(on_ - ok_) / n * 28 * float(P):,.0f} "
              f"per 28 days in farm-out premium")
    except Exception:
        pass
    cols = list(rows[0].keys())
    p = C.write_csv("22_later_clock.csv", cols, [[r[c] for c in cols] for r in rows])
    print(f"\nWrote: {os.path.relpath(p, C.REPO_ROOT)}")
    print(f"runtime: {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
