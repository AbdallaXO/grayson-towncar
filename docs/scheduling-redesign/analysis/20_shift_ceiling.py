#!/usr/bin/env python
"""20 — What do driver shift limits actually cost? (Build 4, the founder's method)

THE QUESTION (founder, 2026-09-02)
----------------------------------
His own way of building a day:

  1. run the assignment engine with every available driver treated as if he
     could work the whole day, to see the most trips the FLEET could keep
     in-house if hours were not in the way;
  2. then read each VEHICLE's day, find the gaps, and split the cars that have
     a usable one into a morning driver and an evening driver, instead of
     asking eighteen people to work a full long day.

Step 1 is a measurement, and it decides whether step 2 is worth building. This
script runs it.

WHAT IT COMPARES, per date, from a cold start
---------------------------------------------
  A  REAL      the shipped pipeline at the dispatcher's own roster, with every
               hour limit in force (saved windows, the hardcoded per-driver
               stub table, the 15h cap). This is today's build.
  C  CEILING   the same roster and the same engine, with the hour limits taken
               off: no start/end window, no max-hours cap. Everything else is
               untouched — the same turnaround maths, the same shared-car
               gate, the same vehicle-class rules, the same cars.

C - A is what shift limits cost in trips. If that number is small, hours are
not the binding constraint and splitting cars cannot repay the effort.

THEN, ON THE CEILING BOARD, THE PRICE OF REACHING IT
----------------------------------------------------
The ceiling is not a schedule: it hands drivers days no one may legally work.
So for every VEHICLE-day in run C the script reads the car's own timeline and
asks the questions step 2 asks:

  * how long is that car's day, and would one driver be over 13.5h / 15h?
  * where is the biggest gap in it?
  * is that gap a REAL handoff window? Not idle time: the shipped chain
    (drop the guest, wash, fuel, back to base, next driver waiting) via
    handoff_chain.handoff_band — GREEN clears it outright, AMBER needs an
    explicit plan, RED cannot be done.
  * if the car is split at that gap, are BOTH halves inside 13.5h?

That gives the honest arithmetic: the ceiling, minus the part of it no legal
split can hold, equals the reachable prize — and the number of extra bodies
the evening halves would need.

METHOD: raw side = the frozen read-only snapshot; Django side = a migrated
throwaway copy. Each date is cleared cold, measured twice, and RESTORED. No
writes to anything real, no billable lookups (probe_mode wraps both runs).

USAGE
  GRAYSON_SNAPSHOT_DB=<frozen copy> python docs/scheduling-redesign/analysis/20_shift_ceiling.py \
      [--recent 10] [--extra 2026-08-03 ...]

Outputs: out/20_ceiling_per_date.csv, out/20_vehicle_days.csv.
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

SOFT_H = 13.5
ASSUMPTIONS = (
    "Both runs use the SAME roster (the dispatcher's own DVA-eligible, available "
    "drivers) and the SAME cars. The only difference is the hour limits: run C "
    "passes no start/end window and no max-hours cap, and switches off the hardcoded "
    "per-driver stub window table and the span-cap clamp for the duration of that run.",
    "Everything else in run C is the shipped engine untouched: the same turnaround "
    "clock, the same shared-car gate, the same vehicle-class rules, the same eight "
    "build passes.",
    "A vehicle-day's timeline is the trips the engine put on the driver holding that "
    "car. A gap is measured from the outgoing driver's CLEAR time (last pickup before "
    "the gap + the P50 occupancy tail) to the next pickup — the same arithmetic the "
    "handoff calibration used.",
    "A gap counts as splittable only when handoff_chain.handoff_band returns GREEN or "
    "AMBER for the two zones involved. RED gaps are idle time, not handoff windows.",
    "'Both halves legal' means each half's span is within 13.5h. The trips are not "
    "re-solved after the split, so this is the optimistic reading of the split: it "
    "assumes the same trips stay on the same car.",
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


def fmt_t(t):
    return t.strftime("%I:%M %p").lstrip("0") if t else "?"


def run_pipeline(day, drivers, legs, windows_kwargs, dva_rows):
    """One pipeline run under the given window settings. Returns the result."""
    from dispatching.assignment_pipeline import (
        PipelineLocks, PipelineWindows, run_assignment_pipeline)
    from dispatching.scheduler import resolve_run_min_buffer, load_driver_min_buffers
    from dispatching.route_distance import probe_mode
    with probe_mode():
        return run_assignment_pipeline(
            legs, drivers, day,
            PipelineWindows(run_min_buffer=resolve_run_min_buffer(None),
                            driver_min_buffers=load_driver_min_buffers(
                                [d.id for d in drivers]),
                            **windows_kwargs),
            PipelineLocks(), dva_rows=dva_rows)


def vehicle_timelines(assignments, legs, drivers, day, dva_rows):
    """{vehicle_id: [slots sorted]} for the board this assignment map describes."""
    from dispatching.scheduler import build_driver_schedules
    legs_by_id = {l.id: l for l in legs}
    drivers_by_id = {d.id: d for d in drivers}
    stamped = []
    for lid, did in assignments.items():
        lg = legs_by_id.get(lid)
        if lg is not None and did in drivers_by_id:
            lg.driver = drivers_by_id[did]
            lg.driver_id = did
            stamped.append(lg)
    try:
        board = build_driver_schedules(legs, drivers, day, dva_rows=dva_rows)
    finally:
        for lg in stamped:
            lg.driver = None
            lg.driver_id = None
    veh_of = {r.driver_id: r.vehicle for r in dva_rows if r.vehicle_id}
    out = defaultdict(list)
    for did, sched in board.items():
        v = veh_of.get(did)
        if v is None or not sched.slots:
            continue
        out[v].extend(sched.slots)
    for v in out:
        out[v].sort(key=lambda s: s.pickup_time)
    return out


def analyse_vehicle_day(vehicle, slots, day, cfg):
    """Span, biggest usable handoff gap, and whether a split makes both halves legal."""
    from dispatching.handoff_chain import (
        handoff_band, occupancy_kind, OCCUPANCY_LEAD_TAIL_P50)
    from dispatching import feasibility_guards as fg
    from dispatching.scheduler import effective_span_hours

    raw, eff = effective_span_hours(slots, day)
    row = {"vehicle": f"#{vehicle.vehicle_number}", "legs": len(slots),
           "first": fmt_t(slots[0].pickup_time),
           "last": fmt_t(slots[-1].pickup_time),
           "span_h": round(raw, 1), "eff_h": round(eff, 1),
           "over_13_5": raw > SOFT_H, "over_15": raw > 15.0,
           "best_gap_min": 0, "best_gap_band": "", "cut_at": "",
           "am_h": "", "pm_h": "", "split_legal": False, "split_reason": ""}
    if len(slots) < 2:
        row["split_reason"] = "only one trip on this car"
        return row

    best = None
    for i in range(len(slots) - 1):
        a, b = slots[i], slots[i + 1]
        kind = occupancy_kind(a.pickup_category, a.dropoff_category)
        tail = OCCUPANCY_LEAD_TAIL_P50[kind][1]
        clear = dt.datetime.combine(day, a.pickup_time) + dt.timedelta(minutes=tail)
        gap = (dt.datetime.combine(day, b.pickup_time) - clear).total_seconds() / 60.0
        bd = handoff_band(a.dropoff_category, b.pickup_category, gap,
                          incoming_is_arrival=(b.pickup_category in fg.AIRPORT_TERMINALS),
                          green_pct=cfg.handoff_gap_green_pct,
                          amber_floor_pct=cfg.handoff_gap_amber_floor_pct)
        if bd["band"] == "red":
            continue
        am = slots[:i + 1]
        pm = slots[i + 1:]
        am_h = effective_span_hours(am, day)[0]
        pm_h = effective_span_hours(pm, day)[0]
        legal = am_h <= SOFT_H and pm_h <= SOFT_H
        key = (legal, gap)
        if best is None or key > best[0]:
            best = (key, gap, bd, b, am_h, pm_h, legal)
    if best is None:
        row["split_reason"] = "no gap on this car clears the wash/fuel/base chain"
        return row
    _k, gap, bd, b, am_h, pm_h, legal = best
    row.update({"best_gap_min": int(gap), "best_gap_band": bd["band"],
                "cut_at": fmt_t(b.pickup_time), "am_h": round(am_h, 1),
                "pm_h": round(pm_h, 1), "split_legal": legal})
    if not legal:
        row["split_reason"] = (f"the best usable gap still leaves a half over {SOFT_H}h "
                               f"(AM {am_h:.1f}h / PM {pm_h:.1f}h)")
    return row


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
    C.preamble("20_shift_ceiling.py",
               "What do driver shift limits cost, and can splitting cars buy it back?",
               h, ASSUMPTIONS)
    picks = pick_dates(con, h, args.recent, args.extra)
    print(f"\ndates ({len(picks)}): {', '.join(str(d) for d in picks)}")

    g17 = load_module("gate17", "17_build3_gate.py")
    dtype, raw_rows, _dva = g17.load_raw(con, min(picks), max(picks))
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

    from dispatching.models import SchedulerSettings
    from dispatching.scheduler import preload_timing_cache, effective_span_hours
    from dispatching import feasibility_guards as fg
    from drivers.models import DriverVehicleAssignment
    preload_timing_cache()
    cfg = SchedulerSettings.get_settings()
    hard_cap = float(cfg.span_exception_max_hours)

    per_date, veh_rows = [], []
    for day in picks:
        iso = day.isoformat()
        rows_ = by_date.get(iso, [])
        a6 = {r["id"] for r in rows_}
        real_in = {r["id"] for r in rows_
                   if r["did"] is not None and dtype.get(r["did"]) == "inhouse"}
        saved = g17.capture_day(day) if hasattr(g17, "capture_day") else dict(
            __import__("reservations.models", fromlist=["Leg"]).Leg.objects
            .filter(pickup_date=day, driver__isnull=False)
            .values_list("id", "driver_id"))
        g17.clear_day(day)
        try:
            drivers, driver_hours, flexible, driver_max_hours = g17.day_roster(day)
            legs = g17.load_day_legs(day)
            dva_rows = list(DriverVehicleAssignment.objects.filter(date=day)
                            .select_related("vehicle", "vehicle__vehicle_type"))

            # ── A: today's build, every hour limit in force ──
            resA = run_pipeline(day, drivers, legs, {
                "driver_hours": driver_hours, "flexible_drivers": flexible,
                "driver_max_hours": driver_max_hours}, dva_rows)
            A = g17.board_metrics(dict(resA.assignments), legs, drivers, day, a6, hard_cap)

            # ── C: the ceiling — no window, no cap ──
            _stub, _caps = fg.USE_STUB_WINDOWS, fg.ENFORCE_SPAN_CAPS
            fg.USE_STUB_WINDOWS, fg.ENFORCE_SPAN_CAPS = False, False
            try:
                resC = run_pipeline(day, drivers, legs, {
                    "driver_hours": {}, "flexible_drivers": set(),
                    "driver_max_hours": {}}, dva_rows)
                Cm = g17.board_metrics(dict(resC.assignments), legs, drivers, day,
                                       a6, hard_cap)
                tl = vehicle_timelines(dict(resC.assignments), legs, drivers, day, dva_rows)
            finally:
                fg.USE_STUB_WINDOWS, fg.ENFORCE_SPAN_CAPS = _stub, _caps

            # ── the price of the ceiling, car by car ──
            day_rows = []
            for vehicle, slots in sorted(tl.items(), key=lambda kv: kv[0].vehicle_number):
                r = analyse_vehicle_day(vehicle, slots, day, cfg)
                r["date"] = iso
                day_rows.append(r)
                veh_rows.append(r)
            need_split = [r for r in day_rows if r["over_13_5"]]
            can_split = [r for r in need_split if r["split_legal"]]
            stuck = [r for r in need_split if not r["split_legal"]]

            spansC = []
            from dispatching.scheduler import build_driver_schedules
            legs_by_id = {l.id: l for l in legs}
            dby = {d.id: d for d in drivers}
            stamped = []
            for lid, did in resC.assignments.items():
                lg = legs_by_id.get(lid)
                if lg is not None and did in dby:
                    lg.driver = dby[did]; lg.driver_id = did; stamped.append(lg)
            boardC = build_driver_schedules(legs, drivers, day, dva_rows=dva_rows)
            for lg in stamped:
                lg.driver = None; lg.driver_id = None
            for did, s in boardC.items():
                if s.slots:
                    spansC.append(effective_span_hours(s.slots, day)[0])
            c_over_soft = sum(1 for x in spansC if x > SOFT_H)
            c_over_hard = sum(1 for x in spansC if x > 15.0)

            gain = Cm["assigned_a6"] - A["assigned_a6"]
            print(f"\n{iso}  {len(a6)} trips")
            print(f"   A  hours ON  : {A['coverage_pct']:.1f}% in-house  "
                  f"({A['assigned_a6']} kept, {A['farm_a6']} farmed)  "
                  f"{A['driver_days']} drivers  {len(A['over_soft'])} over 13.5h")
            print(f"   C  hours OFF : {Cm['coverage_pct']:.1f}% in-house  "
                  f"({Cm['assigned_a6']} kept, {Cm['farm_a6']} farmed)  "
                  f"{Cm['driver_days']} drivers  {c_over_soft} over 13.5h, "
                  f"{c_over_hard} over 15h  (longest {max(spansC or [0]):.1f}h)")
            print(f"   ceiling gain : {gain:+d} trips     "
                  f"cars needing a split {len(need_split)}, "
                  f"splittable {len(can_split)}, stuck {len(stuck)}")
            for r in stuck[:4]:
                print(f"      {r['vehicle']} {r['span_h']}h {r['legs']} trips — "
                      f"{r['split_reason']}")

            per_date.append({
                "date": iso, "trips": len(a6),
                "real_board_in_house": len(real_in),
                "A_kept": A["assigned_a6"], "A_cov": round(A["coverage_pct"], 1),
                "A_drivers": A["driver_days"], "A_over_13_5": len(A["over_soft"]),
                "C_kept": Cm["assigned_a6"], "C_cov": round(Cm["coverage_pct"], 1),
                "C_drivers": Cm["driver_days"], "C_over_13_5": c_over_soft,
                "C_over_15": c_over_hard,
                "C_longest_h": round(max(spansC or [0]), 1),
                "ceiling_gain": gain,
                "cars_over_13_5": len(need_split),
                "cars_splittable": len(can_split), "cars_stuck": len(stuck),
                "extra_bodies_needed": len(can_split),
            })
        finally:
            from reservations.models import Leg
            by_driver = defaultdict(list)
            for lid, did in saved.items():
                by_driver[did].append(lid)
            for did, ids in by_driver.items():
                Leg.objects.filter(id__in=ids, pickup_date=day).update(driver_id=did)

    # ── the answer ──
    n = len(per_date)
    tot_A = sum(r["A_kept"] for r in per_date)
    tot_C = sum(r["C_kept"] for r in per_date)
    tot_trips = sum(r["trips"] for r in per_date)
    gain = tot_C - tot_A
    C.hdr("WHAT SHIFT LIMITS COST  [measured]")
    print(f"{'date':12s}{'trips':>6s}{'A kept':>8s}{'C kept':>8s}{'gain':>6s}"
          f"{'A cov':>7s}{'C cov':>7s}{'C >13.5h':>9s}{'C >15h':>7s}{'longest':>8s}")
    for r in per_date:
        print(f"{r['date']:12s}{r['trips']:6d}{r['A_kept']:8d}{r['C_kept']:8d}"
              f"{r['ceiling_gain']:+6d}{r['A_cov']:7.1f}{r['C_cov']:7.1f}"
              f"{r['C_over_13_5']:9d}{r['C_over_15']:7d}{r['C_longest_h']:8.1f}")
    print(f"\nover {n} dates: hours ON kept {tot_A} of {tot_trips} "
          f"({100.0 * tot_A / tot_trips:.1f}%), hours OFF kept {tot_C} "
          f"({100.0 * tot_C / tot_trips:.1f}%)")
    print(f"THE CEILING GAIN: {gain:+d} trips over {n} dates = "
          f"{gain / n:+.2f} trips/day")
    try:
        from dispatching.standby_mints import FARMOUT_PREMIUM_PER_LEG as P
        print(f"   at the ${float(P):.2f} farm-out premium that is "
              f"${gain / n * 28 * float(P):,.0f} per 28 days")
    except Exception:
        pass

    C.hdr("THE PRICE OF REACHING IT — splitting the cars  [measured]")
    tot_need = sum(r["cars_over_13_5"] for r in per_date)
    tot_can = sum(r["cars_splittable"] for r in per_date)
    tot_stuck = sum(r["cars_stuck"] for r in per_date)
    print(f"car-days whose ceiling day runs past 13.5h : {tot_need} "
          f"({tot_need / n:.1f}/day)")
    print(f"   of those, splittable at a real handoff gap: {tot_can} "
          f"({tot_can / n:.1f}/day) — each needs one extra body")
    print(f"   no usable gap, so a trip must come off     : {tot_stuck} "
          f"({tot_stuck / n:.1f}/day)")
    bands = Counter(r["best_gap_band"] for r in veh_rows if r["split_legal"])
    print(f"   handoff quality of those splits: {dict(bands)}")
    gaps = sorted(r["best_gap_min"] for r in veh_rows if r["split_legal"])
    if gaps:
        print(f"   gap at the cut: median {gaps[len(gaps) // 2]} min, "
              f"P10 {gaps[max(0, len(gaps) // 10 - 1)]}, P90 {gaps[int(0.9 * len(gaps))]}")
    cuts = Counter(r["cut_at"].split(":")[0] + (" PM" if "PM" in r["cut_at"] else " AM")
                   for r in veh_rows if r["split_legal"])
    print(f"   where the cut falls: {dict(sorted(cuts.items(), key=lambda kv: -kv[1])[:6])}")

    cols = list(per_date[0].keys())
    p = C.write_csv("20_ceiling_per_date.csv", cols,
                    [[r[c] for c in cols] for r in per_date])
    print(f"\nWrote: {os.path.relpath(p, C.REPO_ROOT)}")
    cols = ["date"] + [c for c in veh_rows[0].keys() if c != "date"]
    p = C.write_csv("20_vehicle_days.csv", cols,
                    [[r.get(c, "") for c in cols] for r in veh_rows])
    print(f"Wrote: {os.path.relpath(p, C.REPO_ROOT)}")
    print(f"\nruntime: {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
