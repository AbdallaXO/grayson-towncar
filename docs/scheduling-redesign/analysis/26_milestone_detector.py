#!/usr/bin/env python
"""26 — Watch the assumption, not the trip. Does the founder's rule actually work?

THE RULE BEING TESTED (06_DAY_MANAGER §3.4, founder spec 2026-09-05)
--------------------------------------------------------------------
Stop asking "is this turn tight?" and start asking "what had to be true, by
when, for the next trip to work — and is it still true?"

Every leg with a following job has a LATEST SAFE PICKUP: the last minute the
current guest can be collected and still leave enough drive, unload and
turnaround time for the next job. It is not a new model — it is the shipped
chain math read backwards:

    service            = _slot_chain_end(A, day) − A.booked_pickup      (forward)
    latest_safe_clear  = B.booked_pickup − required_turnaround(repo, ...)
    LATEST SAFE PICKUP = latest_safe_clear − service

When that minute passes with no recorded pickup, the assumption the schedule was
built on has failed. That is a FACT about the plan, not a forecast about the
driver — which is the whole point: every detector scored in §3.3 forecasts, and
is right 17.5% of the time about returns.

WHAT THIS MEASURES — four numbers, and the second one is the one that decides
---------------------------------------------------------------------------
  PRECISION    when the milestone is missed, does the next trip actually run late?
  RECALL       of the next trips that DID run late, how many had their milestone
               missed first? **Nothing in this project has ever measured this.**
               A detector that is precise and catches a third of the trouble is
               not what was asked for, and no precision number would reveal it.
  WARNING TIME minutes between the milestone being missed and the next pickup.
               5 minutes is useless; 40 is a rescue. Reported as a distribution,
               because the mean would hide exactly the cases that matter.
  VOLUME       missed milestones per day, against the ≤5-a-glance budget and the
               70.9 scanner tasks/day of §0.2.

Swept over a GRACE parameter (how long after the milestone before it speaks) and
an EARLY parameter (start watching before it), so the founder picks the trade-off
from real numbers instead of being asked to guess a tolerance.

METHOD
------
Read-only. Real hand-built boards, exactly as 24 does — nothing is rebuilt or
reassigned. The milestone comes from production's own formulas; the observation
is the recorded picked-up tap; the outcome is production's ``pickup_deadline``
against the on-location tap with 19's batch-tap rule. Pairs whose driver changed
after the milestone are reported separately, since the chain being judged was not
the chain that existed at the time.

USAGE
  python docs/scheduling-redesign/analysis/26_milestone_detector.py [--days 28]

Outputs: out/26_milestone_sweep.csv    precision/recall/volume per (grace, early)
         out/26_warning_time.csv       the warning-time distribution
         out/26_missed_milestones.csv  every fire, with names and times
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

GRACES = (0, 3, 5, 10)          # minutes after the milestone before it speaks
EARLIES = (0, 10)               # minutes before the milestone to start watching
LATE_BAR = 15

ASSUMPTIONS = (
    "The milestone is derived from the SHIPPED forward math read backwards — "
    "_slot_chain_end for this leg's service time, chain_repo_minutes for the "
    "reposition, feasibility_guards.required_turnaround for the pad. No new "
    "constant is introduced anywhere in this script.",
    "The observation is the FIRST recorded picked-up tap. 93.7% of in-house legs "
    "in this window carry one and only 4 are bulk-entered, so the signal is real; "
    "the ~6% with no tap at all are reported as their own row, because for those "
    "a missed milestone is ambiguous (he may have picked up and not tapped) and "
    "that ambiguity is what GPS is for.",
    "The outcome is the NEXT leg running >15 min past pickup_policy.pickup_deadline, "
    "measured on its on-location tap with 19's batch rule — the same bar §3.3 used, "
    "so the two are directly comparable.",
    "Boards are the real hand-built ones. A pair whose driver changed after the "
    "milestone is counted separately: the chain judged was not the chain that "
    "existed at the time, so including it would flatter the rule.",
)


def load_module(name, fname):
    here = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location(name, os.path.join(here, fname))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def first_tap(con, leg_id, status):
    rows = C.q(con, "SELECT timestamp FROM reservations_legstatus WHERE leg_id=? "
                    "AND status=? ORDER BY timestamp LIMIT 1", (leg_id, status))
    return C.to_local(rows[0]["timestamp"]) if rows else None


def driver_changed_after(con, leg_id, when):
    """True if this leg's driver was reassigned after `when` — the chain we are
    judging was not the chain that stood at the milestone."""
    rows = C.q(con, "SELECT history_date, driver_id FROM reservations_historicalleg "
                    "WHERE id=? ORDER BY history_date", (leg_id,))
    seen, changed = None, False
    for r in rows:
        if seen is not None and r["driver_id"] != seen:
            if C.to_local(r["history_date"]) > when:
                changed = True
        seen = r["driver_id"]
    return changed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=28)
    ap.add_argument("--dates", nargs="*", default=[])
    args = ap.parse_args()
    t0 = time.time()

    con = C.connect()
    hz = C.Horizon(con)
    C.preamble("26_milestone_detector.py",
               "does 'watch the assumption' catch trouble in time to fix it?",
               hz, ASSUMPTIONS)

    if args.dates:
        picks = sorted(dt.date.fromisoformat(d) for d in args.dates)
    else:
        rows = C.q(con, C.live_legs_sql(
            "l.pickup_date d, COUNT(*) n",
            "AND l.driver_id IN (SELECT id FROM drivers_driver "
            "WHERE LOWER(driver_type)='inhouse') AND l.pickup_date <= ?",
            order="GROUP BY l.pickup_date ORDER BY l.pickup_date DESC"),
            (str(hz.last_actuals_day),))
        picks = sorted(dt.date.fromisoformat(r["d"]) for r in rows[:args.days])
    print(f"\ndates ({len(picks)}): {picks[0]} .. {picks[-1]}")

    load_module("gate17", "17_build3_gate.py").django_on_copy()
    g19 = load_module("clock19", "19_clock_calibration.py")

    from dispatching.board_validation import turn_slack_minutes
    from dispatching.scheduler import (build_driver_schedules, preload_timing_cache,
                                       _slot_chain_end, chain_repo_minutes)
    from dispatching import feasibility_guards as fg
    from dispatching.pickup_policy import pickup_deadline
    from reservations.models import Leg
    preload_timing_cache()

    pairs = []
    for day in picks:
        legs = list(Leg.objects.filter(pickup_date=day, driver__isnull=False,
                                       driver__driver_type="inhouse")
                    .exclude(status="cancelled")
                    .exclude(reservation__status__in=("cancelled", "canceled"))
                    .select_related("driver", "driver__profile", "reservation",
                                    "reservation__vehicle", "vehicle",
                                    "flight_information")
                    .prefetch_related("legstop_set", "legflight_set__flight"))
        if not legs:
            continue
        by_id = {l.id: l for l in legs}
        drivers = list({l.driver_id: l.driver for l in legs}.values())
        board = build_driver_schedules(legs, drivers, day)
        for did, sched in board.items():
            slots = sorted(sched.slots, key=lambda s: (s.pickup_time, s.leg_id))
            for a, b in zip(slots, slots[1:]):
                la, lb = by_id.get(a.leg_id), by_id.get(b.leg_id)
                if la is None or lb is None or not a.pickup_time or not b.pickup_time:
                    continue
                try:
                    a_booked = dt.datetime.combine(day, a.pickup_time)
                    b_booked = dt.datetime.combine(day, b.pickup_time)
                    service = _slot_chain_end(a, day) - a_booked
                    repo = chain_repo_minutes(a.dropoff_location, b.pickup_location,
                                              a.dropoff_category, b.pickup_category)
                    req = fg.required_turnaround(
                        repo, fg.is_airport_arrival(b.trip_type, b.pickup_category),
                        same_terminal=(a.dropoff_category == b.pickup_category))
                    milestone = b_booked - dt.timedelta(minutes=req) - service
                except Exception:
                    continue
                tap = first_tap(con, a.leg_id, "picked-up")
                # outcome on the NEXT leg, same bar as 23
                _, quality = g19.on_location_minutes(con, lb.id, day, lb.pickup_time)
                late_min = None
                if quality == "ok":
                    ol = first_tap(con, lb.id, "on-location")
                    try:
                        dl, _ = pickup_deadline(lb, aware=False)
                    except Exception:
                        dl = None
                    if ol and dl:
                        late_min = (ol - dl).total_seconds() / 60.0
                pairs.append({
                    "date": day, "driver": sched.driver_name,
                    "leg_a": a.leg_id, "leg_b": b.leg_id,
                    "a_booked": a_booked, "b_booked": b_booked,
                    "b_trip": str(b.trip_type), "milestone": milestone,
                    "tap": tap, "slack": turn_slack_minutes(a, b, day),
                    "late_min": late_min,
                    "moved": driver_changed_after(con, b.leg_id, milestone),
                })
        print(f"  {day}  pairs {sum(1 for p in pairs if p['date'] == day):>4}", flush=True)

    n_days = len(picks)
    scorable = [p for p in pairs if p["late_min"] is not None and not p["moved"]]
    truly_late = [p for p in scorable if p["late_min"] > LATE_BAR]
    C.sub("POPULATION")
    print(f"  chained pairs on real boards           {len(pairs):>6}  "
          f"{len(pairs) / n_days:>6.1f}/day")
    print(f"  ... judgeable (next leg has a clean tap, driver unchanged)"
          f"{len(scorable):>6}")
    print(f"  ... of those, the next leg ran >15 late{len(truly_late):>6}  "
          f"{len(truly_late) / n_days:>6.1f}/day  <-- what a warning system must catch")
    no_tap = [p for p in pairs if p["tap"] is None]
    print(f"  pairs where the FIRST leg has no pickup tap at all "
          f"{len(no_tap):>6}  ({100.0 * len(no_tap) / max(1, len(pairs)):.1f}% — "
          f"the GPS-disambiguation case)")

    # ── sweep ───────────────────────────────────────────────────────────────
    C.sub("THE SWEEP — fire when the milestone passes, plus a grace")
    print(f"{'early':>6}{'grace':>7}{'fires/day':>11}{'precision':>11}{'recall':>9}"
          f"{'warn P50':>10}{'warn P25':>10}{'caught in time':>16}")
    sweep, warn_rows, fires_out = [], [], []
    for early in EARLIES:
        for grace in GRACES:
            fired, hits, warns = [], 0, []
            for p in scorable:
                fire_at = p["milestone"] - dt.timedelta(minutes=early) \
                          + dt.timedelta(minutes=grace)
                missed = (p["tap"] is None) or (p["tap"] > fire_at)
                if not missed:
                    continue
                fired.append(p)
                w = (p["b_booked"] - fire_at).total_seconds() / 60.0
                warns.append(w)
                if p["late_min"] > LATE_BAR:
                    hits += 1
            prec = 100.0 * hits / len(fired) if fired else None
            rec = 100.0 * hits / len(truly_late) if truly_late else None
            in_time = (100.0 * sum(1 for p, w in zip(fired, warns)
                                   if p["late_min"] > LATE_BAR and w >= 20)
                       / len(truly_late)) if truly_late else None
            row = {"early_min": early, "grace_min": grace,
                   "fires": len(fired), "fires_per_day": round(len(fired) / n_days, 1),
                   "precision_pct": round(prec, 1) if prec is not None else None,
                   "recall_pct": round(rec, 1) if rec is not None else None,
                   "warn_p50": round(C.pct(warns, 50), 1) if warns else None,
                   "warn_p25": round(C.pct(warns, 25), 1) if warns else None,
                   "recall_with_20min_warning_pct": round(in_time, 1)
                   if in_time is not None else None}
            sweep.append(row)
            print(f"{early:>6}{grace:>7}{row['fires_per_day']:>11.1f}"
                  f"{(row['precision_pct'] or 0):>10.1f}%{(row['recall_pct'] or 0):>8.1f}%"
                  f"{(row['warn_p50'] or 0):>10.0f}{(row['warn_p25'] or 0):>10.0f}"
                  f"{(row['recall_with_20min_warning_pct'] or 0):>15.1f}%")
            if early == 0 and grace == 5:
                warn_rows = [[round(w, 1), 1 if p["late_min"] > LATE_BAR else 0,
                              p["b_trip"]] for p, w in zip(fired, warns)]
                fires_out = [[str(p["date"]), str(p["driver"])[:30], p["leg_a"], p["leg_b"],
                              p["a_booked"].strftime("%H:%M"),
                              p["b_booked"].strftime("%H:%M"),
                              p["milestone"].strftime("%H:%M"),
                              p["tap"].strftime("%H:%M") if p["tap"] else "",
                              p["b_trip"], p["slack"], round(p["late_min"], 1),
                              round(w, 1)] for p, w in zip(fired, warns)]

    C.sub("BY TRIP TYPE OF THE JOB AT RISK  (early=0, grace=5)")
    byk = defaultdict(lambda: [0, 0, 0])
    for p in scorable:
        fire_at = p["milestone"] + dt.timedelta(minutes=5)
        missed = (p["tap"] is None) or (p["tap"] > fire_at)
        k = p["b_trip"]
        byk[k][2] += 1
        if p["late_min"] > LATE_BAR:
            byk[k][1] += 1
        if missed:
            byk[k][0] += 1
    print(f"{'next trip':<14}{'pairs':>8}{'ran late':>10}{'fires':>8}")
    for k, (f, l, tot) in sorted(byk.items(), key=lambda kv: -kv[1][2]):
        print(f"{k[:13]:<14}{tot:>8}{l:>10}{f:>8}")

    cols = list(sweep[0].keys())
    p1 = C.write_csv("26_milestone_sweep.csv", cols,
                     [[r[c] for c in cols] for r in sweep])
    p2 = C.write_csv("26_warning_time.csv",
                     ["warning_minutes", "next_leg_ran_late", "next_trip_type"], warn_rows)
    p3 = C.write_csv("26_missed_milestones.csv",
                     ["date", "driver", "leg_a", "leg_b", "a_booked", "b_booked",
                      "milestone", "actual_pickup_tap", "b_trip", "board_slack",
                      "b_late_min", "warning_min"], fires_out)
    for p in (p1, p2, p3):
        print(f"\nWrote: {os.path.relpath(p, C.REPO_ROOT)}")
    print(f"runtime: {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
