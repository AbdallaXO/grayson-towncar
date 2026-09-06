#!/usr/bin/env python
"""24 — The board and the engine still disagree about when a driver is free.

THE SPLIT (06_DAY_MANAGER §0.1, §3.1, Phase 1.1)
------------------------------------------------
analysis/22 measured the PLANNING side of this and it was fixed:
``scheduler.CHAIN_CLEAR_TAKES_LATER`` makes chain feasibility read
max(static clear, board's measured clear) whenever the job being seated after it
is a FIXED-TIME pickup — a return, a departure, a cruise. An airport arrival is
exempt: the guest is still deplaning, so arriving on the static clock is fine.

The LIVE side never got the same rule. ``board_validation.turn_slack_minutes``
— the one formula behind the board's turn chips, the manual-assign warnings, the
Recovery Advisor's overlap/cascade/reach math, ``validate_post_move_board`` and
every swap revalidation — still calls ``_slot_chain_end(prev_slot, date)`` with
``take_later`` left at False. Its own docstring claims it is "the SAME arithmetic
scheduler.check_feasibility uses"; since 2026-09-02 that is no longer true.

So a turn can be clean on the dispatcher's screen and impossible to the engine
at the same minute — and the advisor, reading the same formula, cannot see the
break it exists to catch.

WHAT THIS MEASURES
------------------
Every adjacent slot pair on the REAL hand-built boards of the replay window,
priced twice:

    without   turn_slack_minutes with CHAIN_CLEAR_TAKES_LATER forced OFF
    with      turn_slack_minutes exactly as shipped

The shipped formula runs BOTH times, with only the module-level rule flag patched
between them, so neither side is a re-implementation and the script keeps working
whichever side of the fix it is run on. Before 2026-09-05 "with" and "without"
were identical, because the live path did not carry the rule; after it, the gap
between them is what the fix bought.

Reported per day: fixed-time turns, band flips ('' / tight / critical via
``pickup_policy.turn_band``), and the case that matters —

    the live path says CLEAN or TIGHT and the corrected clock says NEGATIVE

which is a turn the board tells a dispatcher is fine and the engine would
refuse to build.

USAGE
  python docs/scheduling-redesign/analysis/24_live_clock_split.py [--days 28]

Outputs: out/24_flips.csv          per-date counts
         out/24_clean_negative.csv every turn the board calls fine and the
                                   corrected clock calls impossible
"""
import argparse
import datetime as dt
import importlib.util
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common as C  # noqa: E402

sys.path.insert(0, C.REPO_ROOT)

ASSUMPTIONS = (
    "Boards are the REAL hand-built ones for each date — nothing is rebuilt, "
    "reassigned or re-timed. This measures what the two clocks say about the "
    "board a dispatcher actually worked with.",
    "The correction is exactly CHAIN_CLEAR_TAKES_LATER: prev clear becomes "
    "max(chain_clear_dt, estimated_end_time), and ONLY when the next pickup is "
    "not an airport arrival (feasibility_guards.is_airport_arrival). It can only "
    "ever make a turn tighter, never looser.",
    "Slack is turn_slack_minutes as shipped, called with no recorded pickup — the "
    "PLANNING clock, which is what the board chips and validate_post_move_board "
    "use. The advisor's detection clock re-anchors on recorded pickups on top of "
    "this; that re-anchor is unaffected by the rule measured here.",
    "Pairs the shipped formula cannot judge (it returns None) are counted and "
    "excluded, never treated as clean.",
)


def load_module(name, fname):
    here = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location(name, os.path.join(here, fname))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=28)
    ap.add_argument("--dates", nargs="*", default=[])
    args = ap.parse_args()
    t0 = time.time()

    con = C.connect()
    hz = C.Horizon(con)
    C.preamble("24_live_clock_split.py",
               "how often does the board call a turn fine that the engine calls impossible?",
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

    from unittest.mock import patch
    from dispatching import scheduler as sched_mod
    from dispatching.board_validation import turn_slack_minutes
    from dispatching.scheduler import (build_driver_schedules, preload_timing_cache,
                                       CHAIN_CLEAR_TAKES_LATER)
    from dispatching import feasibility_guards as fg
    from dispatching import pickup_policy
    from reservations.models import Leg
    preload_timing_cache()
    live_has_rule = "take_later" in __import__("inspect").getsource(turn_slack_minutes)
    print(f"scheduler.CHAIN_CLEAR_TAKES_LATER = {CHAIN_CLEAR_TAKES_LATER} "
          f"(planning side, 2026-09-02)")
    print(f"board_validation.turn_slack_minutes carries the rule: "
          f"{'YES (fixed 2026-09-05)' if live_has_rule else 'NO — this is the bug'}")

    per_date, offenders = [], []
    for day in picks:
        legs = list(Leg.objects.filter(pickup_date=day, driver__isnull=False,
                                       driver__driver_type="inhouse")
                    .exclude(status="cancelled")
                    .exclude(reservation__status__in=("cancelled", "canceled"))
                    .select_related("driver", "driver__profile", "reservation",
                                    "reservation__vehicle", "vehicle",
                                    "flight_information")
                    .prefetch_related("legstop_set", "legflight_set"))
        if not legs:
            continue
        drivers = list({l.driver_id: l.driver for l in legs}.values())
        board = build_driver_schedules(legs, drivers, day)
        n_pairs = n_fixed = n_unjudgeable = n_flip = n_clean_neg = n_worse = 0
        worst = 0
        for did, sched in board.items():
            slots = sorted(sched.slots, key=lambda s: (s.pickup_time, s.leg_id))
            for a, b in zip(slots, slots[1:]):
                n_pairs += 1
                with patch.object(sched_mod, "CHAIN_CLEAR_TAKES_LATER", False):
                    live = turn_slack_minutes(a, b, day)
                corrected = turn_slack_minutes(a, b, day)
                if live is None or corrected is None:
                    n_unjudgeable += 1
                    continue
                if fg.is_airport_arrival(b.trip_type, b.pickup_category):
                    continue                      # arrival: the rule exempts it
                n_fixed += 1
                delta = live - corrected
                if delta <= 0:
                    continue
                n_worse += 1
                worst = max(worst, delta)
                lb = pickup_policy.turn_band(live)
                cb = pickup_policy.turn_band(corrected)
                if lb != cb:
                    n_flip += 1
                if corrected < 0 and lb in ("", "tight"):
                    n_clean_neg += 1
                    offenders.append([
                        str(day), sched.driver_name, a.leg_id, b.leg_id,
                        str(a.pickup_time), str(b.pickup_time),
                        str(b.trip_type), live, corrected, delta, lb or "clean",
                    ])
        per_date.append({
            "date": str(day), "legs": len(legs), "pairs": n_pairs,
            "unjudgeable": n_unjudgeable, "fixed_time_pairs": n_fixed,
            "pairs_clock_differs": n_worse, "band_flips": n_flip,
            "clean_or_tight_but_negative": n_clean_neg, "worst_delta_min": worst,
        })
        print(f"  {day}  legs {len(legs):>4}  pairs {n_pairs:>4}  fixed-time {n_fixed:>4}"
              f"  clock differs {n_worse:>4}  flips {n_flip:>3}"
              f"  clean-but-negative {n_clean_neg:>3}", flush=True)

    n = len(per_date)
    C.sub(f"OVER {n} REAL BOARDS")
    tot = lambda k: sum(r[k] for r in per_date)
    print(f"  adjacent turn pairs            {tot('pairs'):>6}   "
          f"{tot('pairs') / n:>6.1f}/day")
    print(f"  fixed-time turns (rule applies){tot('fixed_time_pairs'):>6}   "
          f"{tot('fixed_time_pairs') / n:>6.1f}/day")
    print(f"  where the two clocks differ    {tot('pairs_clock_differs'):>6}   "
          f"{tot('pairs_clock_differs') / n:>6.1f}/day")
    print(f"  band flips                     {tot('band_flips'):>6}   "
          f"{tot('band_flips') / n:>6.1f}/day")
    print(f"  CLEAN/TIGHT WITHOUT THE RULE,  {tot('clean_or_tight_but_negative'):>6}   "
          f"{tot('clean_or_tight_but_negative') / n:>6.1f}/day")
    print(f"  NEGATIVE WITH IT")
    print(f"  pairs the formula can't judge  {tot('unjudgeable'):>6}   "
          f"{tot('unjudgeable') / n:>6.1f}/day")
    if live_has_rule:
        print("\n  The live formula now carries the rule, so those turns are the ones "
              "the fix\n  NEWLY BANDS RED. Before 2026-09-05 a dispatcher was shown a "
              "clean or tight\n  chip on every one of them.")
    else:
        print("\n  The live formula does NOT carry the rule, so every turn above is a "
              "chip a\n  dispatcher is shown as fine on a turn the engine would refuse "
              "to build.")

    if offenders:
        C.sub("THE TEN WORST — a dispatcher was shown a clean chip on these turns")
        print(f"{'date':12s}{'driver':<22}{'from':>9}{'to':>9}{'next':<12}"
              f"{'w/out':>6}{'with':>6}{'delta':>7}{'chip':>8}")
        for o in sorted(offenders, key=lambda r: r[8])[:10]:
            print(f"{o[0]:12s}{str(o[1])[:21]:<22}{o[4][:5]:>9}{o[5][:5]:>9}"
                  f"{str(o[6])[:11]:<12}{o[7]:>6}{o[8]:>6}{o[9]:>7}{o[10]:>8}")

    cols = list(per_date[0].keys())
    p1 = C.write_csv("24_flips.csv", cols, [[r[c] for c in cols] for r in per_date])
    p2 = C.write_csv("24_clean_negative.csv",
                     ["date", "driver", "prev_leg", "next_leg", "prev_pickup",
                      "next_pickup", "next_trip_type", "live_slack", "corrected_slack",
                      "delta_min", "live_band"], offenders)
    for p in (p1, p2):
        print(f"\nWrote: {os.path.relpath(p, C.REPO_ROOT)}")
    print(f"runtime: {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
