#!/usr/bin/env python
"""05 — Flight anchors, the occupancy interval, and the required-driver reconciliation.

THE QUESTION THIS SCRIPT EXISTS TO ANSWER
-----------------------------------------
When does a leg start and stop consuming driver/vehicle capacity?

A previous pass concluded that because booked `pickup_time` precedes the `picked-up` tap by
~35 min on airport-origin legs, `pickup_time` was unreliable. That was WRONG. For airport
arrivals `pickup_time` IS the flight arrival time — set mechanically, not approximately — and
the gap to the pickup is DWELL (deplaning, bags, walk to the meet point). This script proves
the convention, walks the full ladder, derives the true occupancy interval, and then re-runs
peak concurrency under it against the incumbent.

NO HARDCODED DATES. Every window comes from _common at run time.

Two modes:
  * plain            : pure-SQL analysis, no Django. Always runs.
  * with Django      : additionally drives the PRODUCTION estimator
                       dispatching.day_setup.peak_concurrency for the incumbent comparison.
                       Enable by running under the analysis settings module:

    cd <repo root> && PYTHONPATH="<scratch>;." DJANGO_SETTINGS_MODULE=analysis_settings \\
        ENABLE_DEBUG_TOOLBAR=0 python docs/scheduling-redesign/analysis/05_flights_and_occupancy.py

    That settings module points Django at a COPY of the snapshot, because
    peak_concurrency -> estimate_job_end_time -> resolve_drive_minutes can write a
    RouteDistanceCache row. NEVER point Django at content/db.sqlite3.
"""

import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common as C  # noqa: E402

ASSUMPTIONS = (
    "Occupancy starts when the driver sets off (`on-the-way`), not at the booked pickup time "
    "and not at `picked-up`. Validated in section 4 by splitting on first-leg-of-day.",
    "The flight table's *_local columns are stored in UTC despite the name "
    "(docs/operational-data-audit.md sec 1.3). Section 2 re-proves this against the taps.",
    "Lead and tail must be fitted on the SAME legs. Fitting them on different subsets inflates "
    "the interval; section 5 reports the consistency check that catches it.",
    "Affiliate driver rows are companies, not people, so affiliate legs cannot be counted "
    "against a headcount. Section 7 measures this directly.",
)


def trip_kinds(con, start, end):
    """Legs in [start, end] with their kind, booked instant and taps."""
    rows = C.q(con, f"""SELECT l.id, l.pickup_date d, l.pickup_time pt,
                               l.pickup_location pl, l.dropoff_location dl,
                               l.driver_id did, dr.driver_type dtp
                        {C.LEG_JOIN}
                        LEFT JOIN drivers_driver dr ON dr.id = l.driver_id
                        WHERE {C.LIVE_LEG} AND {C.SANE_DATES}
                          AND l.pickup_date BETWEEN ? AND ?""", (str(start), str(end)))
    return rows


def sweep(intervals):
    """Max overlap and when it occurs. Starts before ends at a tie (conservative)."""
    ev = sorted([(a, 1) for a, _ in intervals] + [(b, -1) for _, b in intervals],
                key=lambda x: (x[0], -x[1]))
    cur = peak = 0
    at = None
    for t, delta in ev:
        cur += delta
        if cur > peak:
            peak, at = cur, t
    return peak, at


def main():
    con = C.connect()
    h = C.Horizon(con)
    C.preamble("05_flights_and_occupancy.py",
               "flight anchors, the occupancy interval, and the required-driver reconciliation",
               h, ASSUMPTIONS)

    byday = C.legs_per_day(con)
    scan_from = dt.date.fromisoformat(min(byday))   # derived: first day carrying a leg
    segs = C.changepoints(byday, scan_from, h.today, min_seg=28, min_effect=0.08)
    current = segs[-1]
    prior = segs[-2] if len(segs) > 1 else segs[-1]
    tap_start = h.first_tap_day
    tap_end = h.last_actuals_day

    print(f"\nderived regimes  : prior {prior[0]}..{prior[1]} ({prior[3]:.1f} legs/day), "
          f"current {current[0]}..{current[1]} ({current[3]:.1f} legs/day)")
    print(f"actuals window   : {tap_start} .. {tap_end}")

    taps = C.first_taps(con)
    rows = trip_kinds(con, tap_start, tap_end)
    print(f"legs in actuals window: {len(rows)}")

    # ---------------------------------------------------------------- 1. ladder
    C.hdr("1. THE LADDER — minutes from booked pickup_time to each tap [measured]")
    ladder = {}
    for r in rows:
        s = C.booked_dtm(r["d"], r["pt"])
        if not s:
            continue
        k = C.trip_kind(r["pl"], r["dl"])
        t = taps.get(r["id"], {})
        for stat in ("on-the-way", "on-location", "picked-up", "completed"):
            if stat in t:
                v = (t[stat] - s).total_seconds() / 60.0
                if -600 < v < 900:
                    ladder.setdefault((k, stat), []).append(v)
    print(f"{'kind':10s} {'tap':12s} {'n':>6s} {'P10':>8s} {'P25':>8s} {'P50':>8s} "
          f"{'P75':>8s} {'P90':>8s}")
    for k in ("ARRIVAL", "DEPARTURE", "OTHER"):
        for stat in ("on-the-way", "on-location", "picked-up", "completed"):
            v = ladder.get((k, stat), [])
            if len(v) < 40:
                continue
            print(f"{k:10s} {stat:12s} {len(v):6d} {C.pct(v,10):8.1f} {C.pct(v,25):8.1f} "
                  f"{C.pct(v,50):8.1f} {C.pct(v,75):8.1f} {C.pct(v,90):8.1f}")
    print("""
READ:
  * DEPARTURE `picked-up` sits at ~P50 +3 min: on that half of the board the booked time IS
    the passenger event, accurate to the minute.
  * ARRIVAL `picked-up` sits at ~P50 +40 min: the booked time is the FLIGHT time and the guest
    boards ~40 min later. That is dwell, NOT lateness.
  * On BOTH, `on-the-way` PRECEDES the booked time — so using pickup_time as the start of
    occupancy starts the interval TOO LATE, the opposite of the earlier reading.""")

    # ------------------------------------------------- 2. prove the flight convention
    C.hdr("2. IS ARRIVAL pickup_time THE FLIGHT TIME? [measured]")
    frows = C.q(con, f"""SELECT l.id, l.pickup_date d, l.pickup_time pt,
                                l.pickup_location pl, l.dropoff_location dl,
                                f.scheduled_gate_arrival_local sga,
                                f.actual_gate_arrival_local aga,
                                f.estimated_gate_arrival_local ega,
                                f.scheduled_arrival_local sa
                         {C.LEG_JOIN}
                         JOIN reservations_flight f ON f.id = l.flight_information_id
                         WHERE {C.LIVE_LEG} AND {C.SANE_DATES}
                           AND l.pickup_date BETWEEN ? AND ?""", (str(tap_start), str(tap_end)))
    arr = [r for r in frows if C.trip_kind(r["pl"], r["dl"]) == "ARRIVAL"]
    print(f"arrival legs carrying a flight row: {len(arr)}")

    def flight_dt(raw):
        if not raw:
            return None
        try:
            return dt.datetime.fromisoformat(str(raw).replace("T", " ")[:19])
        except ValueError:
            return None

    print("\nBooked pickup_time MINUS the flight column, the column treated as UTC->local:")
    print(f"{'column':6s} {'n':>6s} {'P25':>8s} {'P50':>8s} {'P75':>8s} {'within +-1min':>14s}")
    for col in ("aga", "ega", "sga", "sa"):
        vals = []
        for r in arr:
            p = C.booked_dtm(r["d"], r["pt"])
            g = flight_dt(r[col])
            if not (p and g):
                continue
            v = (p - C.to_local(g)).total_seconds() / 60.0
            if -240 < v < 240:
                vals.append(v)
        if not vals:
            continue
        exact = sum(1 for x in vals if abs(x) <= 1)
        print(f"{col:6s} {len(vals):6d} {C.pct(vals,25):8.1f} {C.pct(vals,50):8.1f} "
              f"{C.pct(vals,75):8.1f} {100*exact/len(vals):13.1f}%")
    print("""
`actual_gate_arrival_local` matching on ~three quarters of legs to within a minute is a
MECHANISM, not a correlation: pickup_time is re-synced to the flight as it updates. The
audit log carries the same edits under the literal reason string "Flight match".

TIMEZONE PROOF: if the *_local columns were really local, the dwell below would read about
-200 minutes instead of a physically sensible +30 to +40.""")

    dwell_utc, dwell_local, onloc = [], [], []
    for r in arr:
        t = taps.get(r["id"], {})
        g = flight_dt(r["aga"])
        if not g:
            continue
        if t.get("picked-up"):
            a = (t["picked-up"] - C.to_local(g)).total_seconds() / 60.0
            b = (t["picked-up"] - g).total_seconds() / 60.0
            if -600 < a < 600:
                dwell_utc.append(a)
            if -600 < b < 600:
                dwell_local.append(b)
        if t.get("on-location"):
            v = (t["on-location"] - C.to_local(g)).total_seconds() / 60.0
            if -600 < v < 600:
                onloc.append(v)
    print(C.fmt_describe("  true dwell, column as UTC", dwell_utc))
    print(C.fmt_describe("  true dwell, column as local", dwell_local))
    print(C.fmt_describe("  driver on-location vs gate", onloc))
    if onloc:
        early = 100.0 * sum(1 for x in onloc if x < 0) / len(onloc)
        print(f"  driver on location BEFORE the plane docked: {early:.1f}%")
    print("  (prior audit published: dwell P50 +37 / P75 +47; on-location P50 +11; early 28%)")

    # ------------------------------------------- 3. is on-the-way a real event?
    C.hdr("3. IS `on-the-way` A PHYSICAL EVENT OR BOOKKEEPING? [measured]")
    excl = {r["id"] for r in C.q(con, "SELECT id FROM drivers_driver WHERE exclude_from_timing=1")}
    per_day = {}
    for r in rows:
        t = taps.get(r["id"], {})
        if "on-the-way" not in t or r["did"] is None:
            continue
        per_day.setdefault((r["did"], r["d"]), []).append((t["on-the-way"], r))
    pos = {}
    for (did, _d), items in per_day.items():
        items.sort(key=lambda x: x[0])
        for i, (a, r) in enumerate(items):
            s = C.booked_dtm(r["d"], r["pt"])
            if not s:
                continue
            v = (a - s).total_seconds() / 60.0
            if not -300 < v < 300:
                continue
            clean = r["dtp"] != "affiliate" and did not in excl
            if not clean:
                continue
            pos.setdefault((C.trip_kind(r["pl"], r["dl"]),
                            "first" if i == 0 else "later"), []).append(v)
    print("Minutes from booked pickup_time to `on-the-way`, clean in-house cohort only.")
    print("A driver's FIRST leg of the day cannot be contaminated by the previous job's close.")
    print(f"{'kind':10s} {'position':9s} {'n':>6s} {'P25':>8s} {'P50':>8s} {'P75':>8s}")
    for k in ("ARRIVAL", "DEPARTURE", "OTHER"):
        for p in ("first", "later"):
            v = pos.get((k, p), [])
            if len(v) < 40:
                continue
            print(f"{k:10s} {p:9s} {len(v):6d} {C.pct(v,25):8.1f} {C.pct(v,50):8.1f} "
                  f"{C.pct(v,75):8.1f}")
    for k in ("ARRIVAL", "DEPARTURE"):
        f, l = pos.get((k, "first"), []), pos.get((k, "later"), [])
        if f and l:
            print(f"  {k:10s} first-of-day P50 {C.pct(f,50):+.1f} vs later P50 "
                  f"{C.pct(l,50):+.1f}  -> difference {C.pct(l,50)-C.pct(f,50):+.1f} min")
    print("""
A small difference means the tap behaves the same when it CANNOT be contaminated as when it
can, so as a PERCENTILE OVER MANY LEGS it is a valid occupancy anchor.

It is NOT safe per leg: `completed(N) -> on-the-way(N+1)` is bimodal, with a large mass inside
one minute (the driver closes A and opens B in the same tap) and a negative tail. See 02.""")

    # ------------------------------------------- 4. fit the interval (paired legs)
    C.hdr("4. THE OCCUPANCY INTERVAL, fitted on PAIRED legs [measured]")
    paired = {}
    for r in rows:
        t = taps.get(r["id"], {})
        s = C.booked_dtm(r["d"], r["pt"])
        otw, comp = t.get("on-the-way"), t.get("completed")
        if not (s and otw and comp):
            continue
        lead = (s - otw).total_seconds() / 60.0
        tail = (comp - s).total_seconds() / 60.0
        dur = (comp - otw).total_seconds() / 60.0
        if -60 < lead < 300 and -60 < tail < 400 and 0 < dur < 500:
            paired.setdefault(C.trip_kind(r["pl"], r["dl"]), []).append((lead, tail, dur))
    print("lead = minutes the driver sets off BEFORE the booked time")
    print("tail = minutes after the booked time until `completed`")
    print(f"{'kind':10s} {'n':>6s} {'leadP50':>8s} {'leadP75':>8s} {'tailP50':>8s} "
          f"{'tailP75':>8s} {'durP50':>8s} {'check':>7s}")
    MODEL = {}
    for k in ("ARRIVAL", "DEPARTURE", "OTHER"):
        v = paired.get(k, [])
        if len(v) < 100:
            continue
        L = [x[0] for x in v]
        T = [x[1] for x in v]
        D = [x[2] for x in v]
        l50, l75 = C.pct(L, 50), C.pct(L, 75)
        t50, t75 = C.pct(T, 50), C.pct(T, 75)
        d50 = C.pct(D, 50)
        MODEL[k] = {"l50": l50, "l75": l75, "t50": t50, "t75": t75}
        print(f"{k:10s} {len(v):6d} {l50:8.1f} {l75:8.1f} {t50:8.1f} {t75:8.1f} "
              f"{d50:8.1f} {l50+t50-d50:+7.1f}")
    print("""
`check` = (leadP50 + tailP50) - durationP50. Near zero means the construction is sound.
Fitting lead and tail on DIFFERENT leg subsets inflates the interval — a trap this script
avoids by requiring both taps on the same leg.

RECOMMENDED: occupancy = [pickup_time - lead(kind), pickup_time + tail(kind)] at P50 for
staffing. P75 is for single-leg feasibility, NOT for an aggregate: a P75 interval applied to
every leg at once compounds into a peak nobody has ever had to cover.""")
    if not MODEL:
        print("insufficient paired data — stopping before the concurrency comparison")
        return

    C.write_csv("05_occupancy_ladder.csv",
                ["kind", "lead_p50", "lead_p75", "tail_p50", "tail_p75"],
                [[k, m["l50"], m["l75"], m["t50"], m["t75"]] for k, m in MODEL.items()])

    # ------------------------------------------- 5. concurrency comparison
    C.hdr("5. CONCURRENCY: incumbent vs corrected vs realised")
    try:
        import django  # noqa: F401
        django.setup()
        from dispatching import day_setup
        from drivers.models import FleetVehicle
        from reservations.models import Leg
        HAVE_DJANGO = True
        units = list(FleetVehicle.objects.filter(is_active=True))
        BUF = day_setup.DAY_SETUP_PEAK_BUFFER
        print(f"Django available. Active fleet = {len(units)} cars. "
              f"DAY_SETUP_PEAK_BUFFER = {BUF}")
    except Exception as exc:                                    # noqa: BLE001
        HAVE_DJANGO = False
        units, BUF = [], 1
        print(f"Django NOT available ({type(exc).__name__}) — incumbent column skipped.")
        print("Re-run under analysis_settings to include it (see the module docstring).")

    dtp = {r["id"]: r["driver_type"] for r in
           C.q(con, "SELECT id, driver_type FROM drivers_driver")}
    by_date = {}
    for r in rows:
        by_date.setdefault(r["d"], []).append(r)

    out = []
    start = max(tap_start, prior[0])
    d = start
    while d <= tap_end:
        rs = by_date.get(d.isoformat(), [])
        if not rs:
            d += dt.timedelta(days=1)
            continue
        corr, corr_in, real = [], [], []
        for r in rs:
            s = C.booked_dtm(r["d"], r["pt"])
            if not s:
                continue
            m = MODEL.get(C.trip_kind(r["pl"], r["dl"]))
            if not m:
                continue
            iv = (s - dt.timedelta(minutes=m["l50"]), s + dt.timedelta(minutes=m["t50"]))
            corr.append(iv)
            if r["did"] and dtp.get(r["did"]) != "affiliate":
                corr_in.append(iv)
            t = taps.get(r["id"], {})
            a = t.get("on-the-way") or t.get("on-location")
            b = t.get("completed")
            if a and b and b > a:
                real.append((a, b))
        if not corr:
            d += dt.timedelta(days=1)
            continue
        p_corr, _ = sweep(corr)
        p_in, _ = sweep(corr_in) if corr_in else (0, None)
        p_real, at_real = sweep(real) if real else (0, None)
        worked = len({r["did"] for r in rs
                      if r["did"] and dtp.get(r["did"]) != "affiliate"})
        inc, inc_at, must = None, None, None
        if HAVE_DJANGO:
            legs = list(Leg.objects.filter(pickup_date=d)
                        .exclude(reservation__status="cancelled").exclude(status="cancelled")
                        .select_related("reservation__vehicle", "vehicle", "reservation",
                                        "flight_information"))
            pc = day_setup.peak_concurrency(d, legs=legs)
            inc = pc["overall"][0]
            inc_at = pc["overall"][1].strftime("%H:%M") if pc["overall"][1] else ""
            _parked, staffed = day_setup.parkable_units(units, pc["cumulative"],
                                                        pc["overall"][0])
            must = len(staffed)
        out.append([d, len(rs), inc, inc_at, must, p_corr, p_in, p_real,
                    at_real.strftime("%H:%M") if at_real else "", worked])
        d += dt.timedelta(days=1)

    n = len(out)
    print(f"\n{n} dates, {out[0][0]} .. {out[-1][0]}")
    print(f"\n{'date':11s} {'dow':4s} {'legs':>5s} {'INC':>4s} {'must':>4s} {'CORR':>5s} "
          f"{'inCORR':>6s} {'REAL':>5s} {'worked':>6s}")
    for r in out[-21:]:
        print(f"{r[0]} {r[0].strftime('%a'):4s} {r[1]:5d} "
              f"{(r[2] if r[2] is not None else 0):4d} "
              f"{(r[4] if r[4] is not None else 0):4d} {r[5]:5d} {r[6]:6d} {r[7]:5d} {r[9]:6d}")

    def mean(f):
        v = [f(r) for r in out if f(r) is not None]
        return sum(v) / len(v) if v else float("nan")

    print(f"\nmean incumbent peak           {mean(lambda r: r[2]):6.1f}")
    print(f"mean corrected peak (all legs){mean(lambda r: r[5]):6.1f}")
    print(f"mean realised peak (taps)     {mean(lambda r: r[7]):6.1f}")
    print(f"mean corrected, IN-HOUSE legs {mean(lambda r: r[6]):6.1f}")
    print(f"mean in-house drivers worked  {mean(lambda r: r[9]):6.1f}")

    # ------------------------------------------- 6. the reconciliation
    C.hdr("6. THE RECONCILIATION — how often each signal fires a shortage [measured]")

    def share(f):
        k = sum(1 for r in out if f(r))
        return f"{k:3d} of {n}  ({100.0*k/n:3.0f}%)"

    if HAVE_DJANGO:
        print("vs IN-HOUSE DRIVERS WHO ACTUALLY WORKED")
        print("  raw peak            > worked :", share(lambda r: r[2] > r[9]))
        print(f"  peak + {BUF} (shipped target)> worked :",
              share(lambda r: r[2] + BUF > r[9]))
        print("  must_run (shipped warning) > worked :", share(lambda r: r[4] > r[9]))
        print("  must_run == full fleet (saturated)  :",
              share(lambda r: r[4] == len(units)))
        print("  raw peak >= fleet size              :",
              share(lambda r: r[2] >= len(units)))
        print(f"\n  mean (peak + {BUF}) - drivers worked : "
              f"{mean(lambda r: r[2] + BUF - r[9]):+.1f} drivers")
    print("\nCORRECTED interval, same comparison")
    print("  corrected peak, ALL legs  > worked :", share(lambda r: r[5] > r[9]))
    print("  corrected peak, IN-HOUSE  > worked :", share(lambda r: r[6] > r[9]))
    print(f"\n  mean (all-leg corrected) - (in-house corrected): "
          f"{mean(lambda r: r[5]-r[6]):+.1f} legs")
    print("""
THE FINDING: the incumbent's error is the DENOMINATOR, not the interval. peak_concurrency
counts every leg INCLUDING the ~19% that are farmed out, then that total is compared against
IN-HOUSE headcount — a total against a part. Restrict the same computation to legs in-house
actually ran and the peak stops exceeding the bodies available.

The gap `peak + 1` reports is not a shortage. It is the farm-out volume, restated.""")

    C.write_csv("05_concurrency_compare.csv",
                ["date", "dow", "legs", "incumbent_peak", "incumbent_at", "must_run",
                 "corrected_all", "corrected_inhouse", "realised", "realised_at",
                 "inhouse_drivers_worked"],
                [[r[0], r[0].strftime("%a"), r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8],
                  r[9]] for r in out])

    # ------------------------------------------- 7. affiliates are companies
    C.hdr("7. AFFILIATE ROWS ARE COMPANIES, NOT PEOPLE [measured]")
    per = {}
    for r in rows:
        t = taps.get(r["id"], {})
        a = t.get("on-the-way") or t.get("on-location")
        b = t.get("completed")
        if r["did"] and a and b and b > a:
            per.setdefault((r["did"], r["d"]), []).append((a, b))
    worst = {}
    for (did, _d), iv in per.items():
        pk, _ = sweep(iv)
        if pk > worst.get(did, 0):
            worst[did] = pk
    inh = [v for k, v in worst.items() if dtp.get(k) != "affiliate"]
    aff = [v for k, v in worst.items() if dtp.get(k) == "affiliate"]
    if inh and aff:
        print(f"  max simultaneous in-flight legs on ONE driver row:")
        print(f"    in-house  n={len(inh):3d}  median {C.pct(inh,50):.0f}  max {max(inh)}")
        print(f"    affiliate n={len(aff):3d}  median {C.pct(aff,50):.0f}  max {max(aff)}")
    print("""
A person cannot drive several cars at once, so an affiliate row reaching a high simultaneous
count is a VENDOR dispatching its own fleet behind one login. Affiliate capacity is elastic
behind a single id and must never be counted as one body in a headcount comparison.""")

    print("\nWrote: out/05_occupancy_ladder.csv, out/05_concurrency_compare.csv")


if __name__ == "__main__":
    main()
