#!/usr/bin/env python
"""19 — Is the engine's planning clock too tight? (Build 4, step 2 evidence)

THE QUESTION
------------
The Day-Builder farms trips the humans kept in-house, and the single largest
"the team bent a rule" bucket in analysis/18 is HUMAN_TIGHT_TURN: a turn the
engine's own static clock calls impossible. Two readings of that fact:

    (a) the humans gambled and the engine is right to refuse, or
    (b) the clock is pessimistic and the engine is farming work it could do.

This script settles it on the whole board, not on the lost trips alone. For
every CONSECUTIVE PAIR of jobs on a real in-house driver-day it computes the
engine's own verdict (scheduler.check_feasibility on the earlier job's chain
clear time, with the shipped turnaround math) and then asks the tap record
what actually happened: did the driver reach the second pickup, and when?

    "impossible" pair  =  the engine would refuse to build this turn
    reality            =  the ON-LOCATION tap on the second job vs its booked
                          time. Never the picked-up tap: on an arrival the
                          booked time is the flight's landing slot, so
                          picked-up minus booked is deplaning dwell. Taps
                          entered in a batch (picked-up within 2 min of
                          completed, no on-location) are discarded.

It also decomposes each refusal into the clock's INPUTS, so a fix has a
target: the flat 45-min dwell on arrivals (STATIC_FLOOR_DWELL_MIN), the
35-min DEFAULT_DRIVE_TIME standing in for an address pair the category table
cannot price, and the cross-property Disney->Disney average applied to a hop
between two adjacent resorts.

Finally it prices the pessimism: how much of each refusal would survive if the
dwell were the measured median instead of a flat floor.

METHOD: raw side = the frozen read-only snapshot (real boards + taps); Django
side = a migrated throwaway copy of it, for the shipped feasibility code. No
writes anywhere, no pipeline runs, no billable lookups.

USAGE
  GRAYSON_SNAPSHOT_DB=<frozen copy> python docs/scheduling-redesign/analysis/19_clock_calibration.py \
      [--recent 10] [--extra 2026-08-03 ...]

Outputs: out/19_turn_pairs.csv (one row per consecutive pair),
out/19_clock_summary.csv.
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

BATCH_TAP_MAX_SEC = 120
LATE_BAR_MIN = 15          # "late" for the second pickup, on the on-location tap

ASSUMPTIONS = (
    "The universe is every CONSECUTIVE pair of jobs on a real in-house driver-day, "
    "A6-filtered, on the sampled dates — the turns the team actually ran.",
    "The engine verdict is scheduler.check_feasibility with driver_window=None and "
    "min_buffer=0: the pure physical turnaround test, no window and no policy floor, "
    "so a refusal here is the clock alone.",
    "Reality is the ON-LOCATION tap on the SECOND job vs its booked pickup time. On an "
    "arrival the booked time is the flight's landing slot, so the picked-up tap measures "
    "deplaning, not punctuality. Pairs whose second job has no usable tap are counted "
    "separately and never scored.",
    "The dwell counterfactual re-prices the first job's clear time with a different "
    "arrival dwell and re-asks the same shipped turnaround question; nothing else moves.",
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


def on_location_minutes(con, leg_id, day, booked_time):
    """(min the driver reached the pickup vs booked, quality in ok/batch/none)."""
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


def fmt_t(t):
    return t.strftime("%I:%M %p").lstrip("0") if t else "?"


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
    C.preamble("19_clock_calibration.py",
               "Is the engine's static planning clock too tight? Every real turn, judged and "
               "then checked against the taps", h, ASSUMPTIONS)
    picks = pick_dates(con, h, args.recent, args.extra)
    print(f"\ndates ({len(picks)}): {', '.join(str(d) for d in picks)}")

    g17 = load_module("gate17", "17_build3_gate.py")
    g17.django_on_copy()

    from dispatching.scheduler import (
        DRIVE_TIME_ESTIMATES, DEFAULT_DRIVE_TIME, STATIC_FLOOR_DWELL_MIN,
        build_driver_schedules, check_feasibility, chain_clear_dt, chain_repo_minutes,
        preload_timing_cache, _slot_chain_end)
    from dispatching.analytics import categorize_location
    from dispatching import feasibility_guards as fg
    from drivers.models import Driver
    from reservations.models import Leg
    preload_timing_cache()
    print(f"\nshipped clock: arrival dwell {STATIC_FLOOR_DWELL_MIN} min, unpriced pair "
          f"{DEFAULT_DRIVE_TIME} min, deplaning grace {fg.DEPLANING_GRACE_MIN} min, "
          f"safety pad {fg.SAFETY_PAD_MIN} min")

    rows_out = []
    for day in picks:
        legs = list(Leg.objects.filter(pickup_date=day, driver__isnull=False,
                                       driver__driver_type="inhouse")
                    .exclude(status="cancelled")
                    .exclude(reservation__status__in=("cancelled", "canceled"))
                    .select_related("driver", "driver__profile", "reservation",
                                    "reservation__vehicle", "vehicle", "flight_information")
                    .prefetch_related("legstop_set", "legflight_set"))
        if not legs:
            continue
        drivers = list({l.driver_id: l.driver for l in legs}.values())
        board = build_driver_schedules(legs, drivers, day)
        legs_by_id = {l.id: l for l in legs}
        for did, sched in board.items():
            slots = sorted(sched.slots, key=lambda s: s.pickup_time)
            for a, b in zip(slots, slots[1:]):
                nb = legs_by_id.get(b.leg_id)
                if nb is None:
                    continue
                from dispatching.scheduler import DriverDaySchedule
                only_a = DriverDaySchedule(driver_id=did, driver_name=sched.driver_name,
                                           driver_type=sched.driver_type, slots=[a],
                                           vehicle_cap=sched.vehicle_cap)
                f = check_feasibility(only_a, nb, day, driver_window=None, min_buffer=0)
                clear = _slot_chain_end(a, day)
                repo = chain_repo_minutes(a.dropoff_location, nb.pickup_location,
                                          a.dropoff_category, b.pickup_category)
                is_arr = fg.is_airport_arrival(b.trip_type, b.pickup_category)
                same_term = (a.dropoff_category == b.pickup_category)
                req = fg.required_turnaround(repo, is_arr, same_terminal=same_term)
                # which inputs are model DEFAULTS?
                defaults = []
                if (a.pickup_category, a.dropoff_category) not in DRIVE_TIME_ESTIMATES:
                    defaults.append("first job's drive unpriced")
                if a.trip_type == "arrival":
                    defaults.append("flat arrival dwell")
                if (a.dropoff_category, b.pickup_category) not in DRIVE_TIME_ESTIMATES:
                    defaults.append("reposition unpriced")
                elif (a.dropoff_category == b.pickup_category
                      and str(a.dropoff_category).startswith("Disney")):
                    defaults.append("cross-property Disney average")
                # dwell counterfactual: what if an arrival's dwell were 25 min?
                relieved_25 = relieved_35 = None
                if a.trip_type == "arrival":
                    for alt, name in ((25, "relieved_25"), (35, "relieved_35")):
                        shift = STATIC_FLOOR_DWELL_MIN - alt
                        newbuf = f.buffer_minutes + shift
                        if name == "relieved_25":
                            relieved_25 = newbuf >= 0
                        else:
                            relieved_35 = newbuf >= 0
                late, q = on_location_minutes(con, b.leg_id, day, b.pickup_time)
                rows_out.append({
                    "date": day.isoformat(), "driver_id": did, "driver": sched.driver_name,
                    "first_leg": a.leg_id, "first_pickup": fmt_t(a.pickup_time),
                    "first_type": a.trip_type,
                    "second_leg": b.leg_id, "second_pickup": fmt_t(b.pickup_time),
                    "second_type": b.trip_type,
                    "from_cat": a.dropoff_category, "to_cat": b.pickup_category,
                    "engine_ok": f.feasible, "buffer_min": f.buffer_minutes,
                    "reason": f.reason, "clear": clear.strftime("%H:%M"),
                    "repo_min": repo, "required_turn_min": req,
                    "defaults": "|".join(defaults),
                    "relieved_at_dwell25": relieved_25, "relieved_at_dwell35": relieved_35,
                    "second_on_location_min": "" if late is None else late,
                    "tap_quality": q,
                })
        print(f"  {day}: {sum(1 for r in rows_out if r['date'] == day.isoformat())} pairs")

    n = len(rows_out)
    bad = [r for r in rows_out if not r["engine_ok"]]
    C.hdr("THE CLOCK vs THE BOARD THE TEAM ACTUALLY RAN  [measured]")
    print(f"consecutive pairs on real in-house driver-days : {n}")
    print(f"pairs the engine's clock calls IMPOSSIBLE      : {len(bad)}  "
          f"({100.0 * len(bad) / n:.1f}%)")
    graded = [r for r in bad if r["tap_quality"] == "ok"]
    if graded:
        vals = sorted(int(r["second_on_location_min"]) for r in graded)
        late = [v for v in vals if v > LATE_BAR_MIN]
        print(f"\nof those, {len(graded)} have a usable on-location tap on the SECOND job:")
        print(f"  the driver reached it       : median {vals[len(vals) // 2]:+d} min vs booked, "
              f"P10 {vals[max(0, int(0.1 * len(vals)) - 1)]:+d}, "
              f"P90 {vals[min(len(vals) - 1, int(0.9 * len(vals)))]:+d}, worst {vals[-1]:+d}")
        print(f"  arrived more than {LATE_BAR_MIN} min late : {len(late)} of {len(graded)} "
              f"({100.0 * len(late) / len(graded):.0f}%)")
        print(f"  arrived on or before booked : {sum(1 for v in vals if v <= 0)} of {len(graded)}")
    tq = Counter(r["tap_quality"] for r in bad)
    print(f"  tap coverage on the refused pairs: {dict(tq)}")

    ok_pairs = [r for r in rows_out if r["engine_ok"] and r["tap_quality"] == "ok"]
    if ok_pairs:
        v2 = sorted(int(r["second_on_location_min"]) for r in ok_pairs)
        l2 = [v for v in v2 if v > LATE_BAR_MIN]
        print(f"\nCONTROL — pairs the engine ACCEPTS ({len(ok_pairs)} with a usable tap): "
              f"median {v2[len(v2) // 2]:+d} min, more than {LATE_BAR_MIN} late "
              f"{len(l2)} ({100.0 * len(l2) / len(ok_pairs):.0f}%)")
        if graded:
            print(f"  => the refused turns ran {100.0 * len(late) / len(graded):.0f}% late vs "
                  f"{100.0 * len(l2) / len(ok_pairs):.0f}% on the accepted ones. A clock that is "
                  f"right would separate these two groups.")

    C.hdr("WHAT THE REFUSALS REST ON  [measured]")
    cnt = Counter()
    for r in bad:
        for d_ in (r["defaults"].split("|") if r["defaults"] else ["no default — real numbers"]):
            cnt[d_] += 1
    for k, v in cnt.most_common():
        print(f"  {v:4d}  {k}")
    arr = [r for r in bad if r["first_type"] == "arrival"]
    if arr:
        r25 = sum(1 for r in arr if r["relieved_at_dwell25"])
        r35 = sum(1 for r in arr if r["relieved_at_dwell35"])
        print(f"\n{len(arr)} of the {len(bad)} refusals follow an ARRIVAL, so the flat "
              f"{STATIC_FLOOR_DWELL_MIN}-min dwell is in the arithmetic:")
        print(f"  would become feasible at a 35-min dwell: {r35}")
        print(f"  would become feasible at a 25-min dwell: {r25}")
    deficits = sorted(r["buffer_min"] for r in bad)
    if deficits:
        print(f"\nhow far short the refusals are: median {deficits[len(deficits) // 2]} min, "
              f"P25 {deficits[int(0.25 * len(deficits))]}, "
              f"P75 {deficits[int(0.75 * len(deficits))]}; "
              f"{sum(1 for d_ in deficits if d_ >= -15)} are within 15 min of feasible")

    cols = list(rows_out[0].keys())
    p = C.write_csv("19_turn_pairs.csv", cols, [[r.get(c, "") for c in cols] for r in rows_out])
    print(f"\nWrote: {os.path.relpath(p, C.REPO_ROOT)}")
    summary = [{
        "pairs": n, "refused": len(bad), "refused_pct": round(100.0 * len(bad) / n, 1),
        "refused_graded": len(graded),
        "refused_late_gt15": len(late) if graded else "",
        "refused_late_pct": round(100.0 * len(late) / len(graded), 1) if graded else "",
        "accepted_graded": len(ok_pairs),
        "accepted_late_pct": round(100.0 * len(l2) / len(ok_pairs), 1) if ok_pairs else "",
        "after_arrival": len(arr), "relieved_at_dwell35": r35 if arr else "",
        "relieved_at_dwell25": r25 if arr else "",
    }]
    p = C.write_csv("19_clock_summary.csv", list(summary[0].keys()),
                    [[r[c] for c in summary[0]] for r in summary])
    print(f"Wrote: {os.path.relpath(p, C.REPO_ROOT)}")
    print(f"\nruntime: {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
