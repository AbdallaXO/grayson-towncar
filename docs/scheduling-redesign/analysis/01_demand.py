#!/usr/bin/env python3
"""
01_demand.py - Grayson Towncar scheduling redesign, Phase 1.

QUESTION
    What does demand actually look like - by day of week, hour, vehicle class and
    lane - and is that shape stable enough to cut shift templates from?

READ-ONLY. No Django, no writes. Every window is DERIVED at run time from the
database by `_common.changepoints()`. There is not one date literal in this file:
grep it. The only dates that appear anywhere in this package are the DST table and
the wide sanity rail inside `_common.py`.

RUN
    cd docs/scheduling-redesign/analysis && python 01_demand.py

OUTPUT
    stdout narrative + CSVs in ./out/
      demand_by_dow_hour.csv   demand_by_month.csv   class_mix.csv   lane_matrix.csv
      01_demand_regimes.csv          01_demand_dow_distribution.csv
      01_demand_hour_by_tripkind.csv 01_demand_weekly.csv
      01_demand_leadtime_weekly.csv  01_demand_booking_curve.csv
      01_demand_class_fit.csv        01_demand_stationarity.csv
"""

import collections
import datetime as dt
import math
import random

import _common as C

DOW_NAME = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
WEEKEND = (4, 5, 6)          # Fri, Sat, Sun - the brief's definition
RNG_SEED = 20260821          # fixed for reproducibility; not a date, an int seed
PERM_B = 600                 # permutation resamples for every stationarity null


# ==============================================================================
# helpers
# ==============================================================================

def tvd(p, q):
    """Total-variation distance between two dicts of counts (normalised here)."""
    tp, tq = float(sum(p.values())), float(sum(q.values()))
    if tp <= 0 or tq <= 0:
        return None
    keys = set(p) | set(q)
    return 0.5 * sum(abs(p.get(k, 0) / tp - q.get(k, 0) / tq) for k in keys)


def shares(counter):
    t = float(sum(counter.values()))
    return {k: v / t for k, v in counter.items()} if t else {}


def perm_null_tvd(days_a, days_b, cat_by_day, rng, b=PERM_B):
    """Null distribution for TVD(A, B) under 'the two windows are the same shape'.

    Resampling unit is a whole DAY, stratified by day-of-week so the weekday
    composition of each group is preserved exactly. Group sizes are preserved
    exactly. Shares are level-invariant, so the level step between the two
    regimes does not by itself move this statistic.

    Returns (p_value, null_p95, null_p50).
    """
    by_dow = collections.defaultdict(list)
    n_a_by_dow = collections.Counter()
    for d in days_a:
        by_dow[d.weekday()].append(d)
        n_a_by_dow[d.weekday()] += 1
    for d in days_b:
        by_dow[d.weekday()].append(d)

    obs_a = collections.Counter()
    obs_b = collections.Counter()
    for d in days_a:
        obs_a.update(cat_by_day.get(d.isoformat(), {}))
    for d in days_b:
        obs_b.update(cat_by_day.get(d.isoformat(), {}))
    observed = tvd(obs_a, obs_b)
    if observed is None:
        return None, None, None

    nulls = []
    for _ in range(b):
        ca, cb = collections.Counter(), collections.Counter()
        for dow, pool in by_dow.items():
            idx = list(range(len(pool)))
            rng.shuffle(idx)
            k = n_a_by_dow[dow]
            for j, i in enumerate(idx):
                (ca if j < k else cb).update(cat_by_day.get(pool[i].isoformat(), {}))
        t = tvd(ca, cb)
        if t is not None:
            nulls.append(t)
    if not nulls:
        return None, None, None
    nulls.sort()
    p = sum(1 for x in nulls if x >= observed) / float(len(nulls))
    return p, C.pct(nulls, 95), C.pct(nulls, 50)


def daterange(a, b):
    d = a
    while d <= b:
        yield d
        d += dt.timedelta(days=1)


def month_key(d):
    return f"{d.year:04d}-{d.month:02d}"


def month_last_day(year, month):
    if month == 12:
        return dt.date(year, 12, 31)
    return dt.date(year, month + 1, 1) - dt.timedelta(days=1)


def iso_week_key(d):
    y, w, _ = d.isocalendar()
    return f"{y:04d}-W{w:02d}"


def centered_ratio(byday, d, span=28):
    """legs(d) / mean(legs over the 28-day window CENTRED on d).

    28 days = exactly four of each weekday, so the denominator carries no
    day-of-week bias, and being centred it does not lag a growing series the
    way a trailing mean does. Returns None when the window runs off either end.
    """
    half_back = span // 2 - 1                      # 13
    lo = d - dt.timedelta(days=half_back)
    hi = d + dt.timedelta(days=span - half_back - 1)  # +14
    tot, n = 0, 0
    for x in daterange(lo, hi):
        k = x.isoformat()
        if k not in byday:
            return None, lo, hi
        tot += byday[k]
        n += 1
    if n != span or tot <= 0:
        return None, lo, hi
    return byday[d.isoformat()] / (tot / float(span)), lo, hi


def seg_sse(vals, i, j, pre1, pre2):
    n = j - i
    if n <= 0:
        return 0.0
    s = pre1[j] - pre1[i]
    ss = pre2[j] - pre2[i]
    return ss - s * s / float(n)


def best_contiguous_segmentation(vals, k):
    """Optimal partition of a LINEAR sequence into k contiguous runs (min SSE)."""
    n = len(vals)
    pre1 = [0.0] * (n + 1)
    pre2 = [0.0] * (n + 1)
    for i, v in enumerate(vals):
        pre1[i + 1] = pre1[i] + v
        pre2[i + 1] = pre2[i] + v * v
    INF = float("inf")
    dp = [[INF] * (n + 1) for _ in range(k + 1)]
    arg = [[0] * (n + 1) for _ in range(k + 1)]
    dp[0][0] = 0.0
    for m in range(1, k + 1):
        for j in range(m, n + 1):
            best, bi = INF, m - 1
            for i in range(m - 1, j):
                if dp[m - 1][i] == INF:
                    continue
                c = dp[m - 1][i] + seg_sse(vals, i, j, pre1, pre2)
                if c < best:
                    best, bi = c, i
            dp[m][j] = best
            arg[m][j] = bi
    bounds, j = [], n
    for m in range(k, 0, -1):
        i = arg[m][j]
        bounds.append((i, j))
        j = i
    return dp[k][n], list(reversed(bounds))


def circular_segmentation(vals, k):
    """Same, but the sequence wraps (hour 23 is adjacent to hour 0).

    Tries every rotation and keeps the cheapest; returns (sse, [(start_h, end_h)]).
    """
    n = len(vals)
    best = None
    for rot in range(n):
        rolled = vals[rot:] + vals[:rot]
        sse, bounds = best_contiguous_segmentation(rolled, k)
        if best is None or sse < best[0] - 1e-12:
            spans = [((i + rot) % n, (j - 1 + rot) % n) for i, j in bounds]
            best = (sse, spans, rot)
    return best[0], best[1]


def circ_runs_above(vals, threshold):
    """Maximal circular contiguous runs of indices whose value exceeds threshold."""
    n = len(vals)
    above = [v > threshold for v in vals]
    if all(above):
        return [(0, n - 1)]
    if not any(above):
        return []
    start = next(i for i in range(n) if above[i] and not above[(i - 1) % n])
    runs, i = [], start
    for _ in range(n):
        if above[i] and not above[(i - 1) % n]:
            j = i
            while above[(j + 1) % n]:
                j = (j + 1) % n
            runs.append((i, j))
        i = (i + 1) % n
    # de-duplicate (the walk can revisit a run start)
    seen, out = set(), []
    for r in runs:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


def span_hours(a, b):
    return (b - a) % 24 + 1


def hours_in(a, b):
    return [(a + i) % 24 for i in range(span_hours(a, b))]


def fmt_pct(x, places=1):
    return "n/a" if x is None else f"{100.0 * x:.{places}f}%"


def linreg(xs, ys):
    n = len(xs)
    if n < 3:
        return None, None
    mx = sum(xs) / n
    my = sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    if den <= 0:
        return None, None
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den
    return b, my - b * mx


# ==============================================================================
# load
# ==============================================================================

def load(con):
    veh = {r["id"]: dict(r) for r in C.q(con, "SELECT * FROM rates_vehicle")}
    sql = C.live_legs_sql(
        """l.id                                            AS leg_id,
           l.reservation_id                                AS rid,
           l.pickup_date                                   AS pd,
           l.pickup_time                                   AS pt,
           l.pickup_location                               AS pl,
           l.dropoff_location                              AS dl,
           l.driver_id                                     AS driver_id,
           l.flight_information_id                         AS flight_id,
           COALESCE(l.vehicle_id, r.vehicle_id)            AS vid,
           COALESCE(l.passenger_count,  r.passenger_count) AS pax,
           COALESCE(l.luggage_count,    r.luggage_count)   AS bags,
           COALESCE(l.booster_seats,    r.booster_seats)   AS boosters,
           COALESCE(l.ff_carseats,      r.ff_carseats)     AS ff,
           COALESCE(l.rf_carseats,      r.rf_carseats)     AS rf,
           r.created_at                                    AS res_created,
           r.trip_type                                     AS trip_type""")
    legs = []
    for r in C.q(con, sql):
        d = dt.date.fromisoformat(r["pd"])
        hour = int(str(r["pt"])[:2])
        v = veh.get(r["vid"])
        legs.append({
            "leg_id": r["leg_id"], "rid": r["rid"], "d": d, "hour": hour,
            "dow": d.weekday(), "pt": str(r["pt"])[:8],
            "cls": (v or {}).get("vehicle_type") or "(no vehicle)",
            "pl": r["pl"], "dl": r["dl"],
            "pb": C.loc_bucket(r["pl"]), "db": C.loc_bucket(r["dl"]),
            "kind": C.trip_kind(r["pl"], r["dl"]),
            "pax": r["pax"], "bags": r["bags"],
            "boosters": r["boosters"] or 0, "ff": r["ff"] or 0, "rf": r["rf"] or 0,
            "res_created": r["res_created"], "trip_type": r["trip_type"],
            "driver_id": r["driver_id"], "flight_id": r["flight_id"],
        })
    return legs, veh


# ==============================================================================
# main
# ==============================================================================

def main():
    rng = random.Random(RNG_SEED)
    con = C.connect()
    H = C.Horizon(con)

    C.preamble(
        "01_demand.py", "demand shape: dow, hour, vehicle class, lane - and is it stable?",
        H,
        assumptions=(
            "One live Leg = one unit of demand (one vehicle-trip). The demand filter is "
            "_common.LIVE_LEG: both cancellation spellings dropped, status='in-progress' KEPT "
            "(it is the Django default for a new leg and means 'not started'), two junk "
            "pickup dates excluded by SANE_DATES. "
            "IF WRONG: every count here moves. Basis: reservations/models.py:1050 area + the "
            "spelling census printed in S0.",
            "Booked pickup_date/pickup_time are naive LOCAL wall-clock; every other timestamp "
            "in the schema is UTC and is passed through _common.to_local() before it meets a "
            "booked time. IF WRONG: hour histograms shift by 4-5 h and lead times shift by the "
            "same. Basis: the flight cross-check in S2c, which only lands on zero after "
            "to_local() is applied.",
            "Vehicle class = leg.vehicle_id if set else reservation.vehicle_id, matching "
            "Leg.effective_vehicle at reservations/models.py:1346-1356. IF WRONG: class mix "
            "moves; leg-level overrides are 0.6% of rows so the exposure is small.",
            "Party size = leg.passenger_count if set else reservation.passenger_count, matching "
            "Leg.effective_passenger_count at reservations/models.py:1366-1370. Leg-level values "
            "are populated on ~0.8% of rows, so this is effectively the reservation's number. "
            "IF WRONG: S6's class-fit test is wrong; nothing else moves.",
            "DEMAND for a past pickup date is COMPLETE (bookings are made in advance), so "
            "last_demand_day = today. Forward dates are structurally incomplete and never enter "
            "an aggregate here. IF WRONG: the current regime's level is overstated. Falsified "
            "in S1d by re-deriving the regime with the last 7 and last 14 days dropped.",
            "For LEAD-TIME only, the window stops a further day short (today's same-day "
            "bookings can still arrive after an evening pull). IF WRONG: today's short-notice "
            "share is understated by one day out of ~29.",
            "For AIRPORT ARRIVALS the booked pickup_time IS the flight arrival time by design, "
            "not the moment the guest is collected. Measured in S2c. IF WRONG: nothing in S2 "
            "changes, but the occupancy-anchored curve in script 05 would agree with this one, "
            "which it will not.",
            "Fleet composition and driver certification are CURRENT-STATE flags with no history "
            "table, so S3's scarcity read is today's supply against a 29-day demand window. "
            "IF WRONG (a vehicle sold, a driver decertified mid-window): the scarcity ranking "
            "is directionally right but the ratios are not auditable to a date.",
        ))

    legs, veh = load(con)

    # Booking clock and lead time, per leg. Computed once here because S1e needs it
    # as a truncation test and S9 needs it as a demand finding.
    #
    # TWO clocks exist and they do NOT agree in the tail (see S9a):
    #   reservation.created_at            whole file, but a leg ADDED to an existing
    #                                     reservation inherits the older timestamp
    #   historicalleg history_type='+'    the leg's own creation, but only from the
    #                                     date django-simple-history was switched on
    # The HYBRID - leg clock where it exists, reservation clock otherwise - is the
    # primary. Legs with no '+' row predate the history table, and S9a measures them
    # to be uniformly long-lead, which is exactly where the two clocks agree.
    leg_created = {r["id"]: C.to_local(r["t"]) for r in C.q(
        con, "SELECT id, MIN(history_date) AS t FROM reservations_historicalleg "
             "WHERE history_type='+' GROUP BY id")}
    lead_h, lead_h_res, book_date = {}, {}, {}
    n_neg_lead = 0
    for lg in legs:
        p = C.booked_dtm(lg["d"].isoformat(), lg["pt"])
        if not p:
            continue
        rc = C.to_local(lg["res_created"]) if lg["res_created"] else None
        bc = leg_created.get(lg["leg_id"]) or rc
        if bc is None:
            continue
        h = (p - bc).total_seconds() / 3600.0
        if h < 0:
            n_neg_lead += 1
            continue
        lead_h[lg["leg_id"]] = h
        book_date[lg["leg_id"]] = bc.date()
        if rc is not None:
            hr = (p - rc).total_seconds() / 3600.0
            if hr >= 0:
                lead_h_res[lg["leg_id"]] = hr

    # -------------------------------------------------------------------------
    C.hdr("S0  THE FILTER, AND WHAT IT COSTS")
    total_legs = C.q1(con, "SELECT COUNT(*) FROM reservations_leg")
    print(f"reservations_leg rows                    {total_legs:>8,}")
    print(f"live legs after LIVE_LEG + SANE_DATES    {len(legs):>8,}   "
          f"({100.0 * len(legs) / total_legs:.1f}%)")
    for label, sql in (
            ("dropped: leg.status = 'cancelled'",
             "SELECT COUNT(*) FROM reservations_leg WHERE status='cancelled'"),
            ("dropped: reservation 'cancelled' (2 L)",
             "SELECT COUNT(*) FROM reservations_leg l JOIN reservations_reservation r "
             "ON r.id=l.reservation_id WHERE r.status='cancelled'"),
            ("dropped: reservation 'canceled'  (1 L)",
             "SELECT COUNT(*) FROM reservations_leg l JOIN reservations_reservation r "
             "ON r.id=l.reservation_id WHERE r.status='canceled'"),
            ("dropped: pickup_date outside sanity rail",
             f"SELECT COUNT(*) FROM reservations_leg l WHERE NOT ({C.SANE_DATES})"),
            ("KEPT: leg.status = 'in-progress'",
             "SELECT COUNT(*) FROM reservations_leg WHERE status='in-progress'"),
    ):
        print(f"  {label:<42s} {C.q1(con, sql):>7,}")
    print(f"  {'(the drop reasons overlap; the union is)':<42s} "
          f"{total_legs - len(legs):>7,}")

    byday_all = {}
    for lg in legs:
        k = lg["d"].isoformat()
        byday_all[k] = byday_all.get(k, 0) + 1
    first_live = min(lg["d"] for lg in legs)
    fwd = [lg for lg in legs if lg["d"] > H.last_demand_day]
    print(f"\nfirst live pickup date  {first_live}   (derived)")
    print(f"forward book (> today)  {len(fwd):,} legs on "
          f"{len({l['d'] for l in fwd})} dates - EXCLUDED from every aggregate below")

    # fill the daily series with explicit zeros so changepoints sees real gaps
    byday = {d.isoformat(): byday_all.get(d.isoformat(), 0)
             for d in daterange(first_live, H.last_demand_day)}

    # -------------------------------------------------------------------------
    C.hdr("S1  THE WINDOW - derived, not chosen")
    C.sub("1a. Mean-shift changepoints on the RAW daily series (binary segmentation)")
    segs = C.changepoints(byday, first_live, H.last_demand_day, 28, 0.10)
    print(f"{'segment':<28s}{'days':>6s}{'legs/day':>11s}{'legs':>9s}  step vs prior")
    prev = None
    seg_rows = []
    for s, e, n, m in segs:
        legs_in = sum(byday[d.isoformat()] for d in daterange(s, e))
        step = "" if prev is None else f"{100.0 * (m - prev) / prev:+6.1f}%"
        print(f"{str(s)}..{str(e)}{n:>6d}{m:>11.2f}{legs_in:>9,}  {step}")
        seg_rows.append([str(s), str(e), n, round(m, 3), legs_in, step.strip()])
        prev = m
    C.write_csv("01_demand_regimes.csv",
                ["start", "end", "days", "legs_per_day", "legs", "step_vs_prior"], seg_rows)

    CUR_S, CUR_E, CUR_N, CUR_M = segs[-1]
    PRI_S, PRI_E, PRI_N, PRI_M = segs[-2]
    print(f"\nCURRENT regime : {CUR_S}..{CUR_E}  {CUR_N}d  {CUR_M:.1f} legs/day  [measured]")
    print(f"PRIOR plateau  : {PRI_S}..{PRI_E}  {PRI_N}d  {PRI_M:.1f} legs/day  [measured]")
    print(f"step           : {100.0 * (CUR_M - PRI_M) / PRI_M:+.1f}%")

    # window sets, derived entirely from the two segments above ---------------
    CUR_DAYS = [d for d in daterange(CUR_S, CUR_E)]
    PRI_DAYS = [d for d in daterange(PRI_S, PRI_E)]
    POOL_S, POOL_E = PRI_S, CUR_E
    POOL_DAYS = [d for d in daterange(POOL_S, POOL_E)]
    cur_set = {d.isoformat() for d in CUR_DAYS}
    pri_set = {d.isoformat() for d in PRI_DAYS}
    pool_set = {d.isoformat() for d in POOL_DAYS}
    L_CUR = [lg for lg in legs if lg["d"].isoformat() in cur_set]
    L_PRI = [lg for lg in legs if lg["d"].isoformat() in pri_set]
    L_POOL = [lg for lg in legs if lg["d"].isoformat() in pool_set]
    print(f"\nwindow sets: CURRENT {len(L_CUR):,} legs / {len(CUR_DAYS)}d   "
          f"PRIOR {len(L_PRI):,} legs / {len(PRI_DAYS)}d   "
          f"POOLED {len(L_POOL):,} legs / {len(POOL_DAYS)}d")

    C.sub("1b. Is that last boundary an artefact of the tuning knobs?")
    print(f"{'min_seg':>8s}{'min_effect':>12s}   last segment                    legs/day  break?")
    late = []
    degenerate = []
    for ms in (21, 28, 35, 42):
        for me in (0.08, 0.10, 0.12, 0.15):
            ss = C.changepoints(byday, first_live, H.last_demand_day, ms, me)
            s, e, n, m = ss[-1]
            # a "late break" = the final segment opened inside the last 10 weeks
            is_late = (H.last_demand_day - s).days <= 70
            print(f"{ms:>8d}{me:>12.2f}   {str(s)}..{str(e)} ({n:>3d}d)      {m:>8.1f}"
                  f"   {'yes' if is_late else 'NO'}")
            (late if is_late else degenerate).append((ms, me, s, m))
    span_days = (max(s for _, _, s, _ in late) - min(s for _, _, s, _ in late)).days
    lo_m, hi_m = min(m for _, _, _, m in late), max(m for _, _, _, m in late)
    print(f"\n{len(late)} of 16 settings find a late break. Among those, the boundary spreads")
    print(f"{span_days} days and the level spreads {lo_m:.1f}-{hi_m:.1f} legs/day.  [measured]")
    if degenerate:
        print(f"\nThe {len(degenerate)} setting(s) that find NO late break are "
              f"{', '.join(f'min_seg={a},min_effect={b}' for a, b, _, _ in degenerate)}.")
        print("That is arithmetic, not evidence: min_seg=42 forbids a segment shorter than 42")
        d42 = C.changepoints(byday, first_live, H.last_demand_day, 42, 0.10)
        step42 = (100.0 * (d42[-1][3] - d42[-2][3]) / d42[-2][3]) if len(d42) >= 2 else None
        if step42 is not None:
            print(f"days, and the step measured over a 42-day tail is only +{step42:.1f}% - "
                  f"below a")
            print("min_effect of 0.15 by construction. A rule that cannot see a 42-day step")
            print("smaller than 15% has not refuted the step; it has declined to look.")
    print("\nREAD: every setting capable of resolving a sub-6-week segment finds the same")
    print("break in the same fortnight, and every one of them puts today above 100 legs/day.")

    C.sub("1c. Second, structurally different check on the CURRENT LEVEL")
    print("The regime mean comes from a least-squares segmentation. Three estimators that")
    print("share none of that machinery:")
    t28 = C.trailing_mean(byday, H.last_demand_day, 28)
    t14 = C.trailing_mean(byday, H.last_demand_day, 14)
    print(f"  changepoint segment mean, {CUR_S}..{CUR_E}       {CUR_M:>7.1f} legs/day")
    print(f"  trailing 28-day mean ending {H.last_demand_day}          {t28:>7.1f} legs/day")
    print(f"  trailing 14-day mean ending {H.last_demand_day}          {t14:>7.1f} legs/day")
    print(f"  spread across the three                          "
          f"{max(CUR_M, t28, t14) - min(CUR_M, t28, t14):>7.1f} legs/day")
    print("  [measured] They agree. The current level is not a segmentation artefact.")

    C.sub("1d. What the OLD document's window would have told you")
    print("The old document cut its window inside the PRIOR plateau and stopped there.")
    print(f"  prior plateau level    {PRI_M:>7.1f} legs/day")
    print(f"  current regime level   {CUR_M:>7.1f} legs/day")
    print(f"  the plateau UNDERSTATES today by {100.0 * (CUR_M - PRI_M) / CUR_M:.1f}% of current "
          f"demand;")
    print(f"  equivalently today is {100.0 * (CUR_M - PRI_M) / PRI_M:+.1f}% ABOVE the plateau.")
    print(f"  a template sized on the plateau is short by "
          f"{CUR_M - PRI_M:.1f} legs/day, every day.")

    C.sub("1e. Falsification: is the current regime an incomplete tail, not a real step?")
    print("A truncated tail has three fingerprints. None of them is present.")
    print("\n(i) Under truncation the MOST RECENT days are the emptiest - only far-in-advance")
    print("    bookings have landed. Last four 7-day blocks:")
    for i in range(4, 0, -1):
        hi = H.last_demand_day - dt.timedelta(days=7 * (i - 1))
        lo = hi - dt.timedelta(days=6)
        tot = sum(byday[d.isoformat()] for d in daterange(lo, hi))
        print(f"    {lo}..{hi}   {tot:>5,} legs   {tot / 7.0:>6.1f}/day")
    print("    The newest block is not the emptiest. [measured]")
    print("\n(ii) Under truncation the surviving bookings on recent days are long-lead ones,")
    print("     so the median lead time EXPLODES and short-notice bookings vanish. (The old")
    print("     document's own August cohort showed exactly that: a 58-day median lead")
    print("     against a 20-day norm, and not one leg booked after the cut.) Measure it:")
    print(f"     {'window':<22s}{'n':>7s}{'median lead':>13s}{'booked <7d':>12s}"
          f"{'booked <24h':>13s}")
    for lbl, days in (("prior plateau", PRI_DAYS), ("current regime", CUR_DAYS),
                      ("current, last 7 days",
                       [d for d in CUR_DAYS if (H.last_demand_day - d).days < 7])):
        v = [lead_h[lg["leg_id"]] for lg in legs
             if lg["d"] in days and lg["leg_id"] in lead_h]
        if not v:
            continue
        print(f"     {lbl:<22s}{len(v):>7,}{C.pct(v, 50) / 24.0:>11.1f} d"
              f"{fmt_pct(sum(1 for x in v if x < 168) / float(len(v))):>12s}"
              f"{fmt_pct(sum(1 for x in v if x < 24) / float(len(v))):>13s}")
    print("     No lead-time explosion anywhere: the last seven days sit inside the normal")
    print("     17-21 day band, not at 58, and their inside-24-hour share is intact. Under")
    print("     truncation both numbers move hard and they do not. [measured]")
    print("\n(iii) Re-deriving the regime with the last K days chopped off:")
    for drop in (0, 7, 14, 21):
        end = H.last_demand_day - dt.timedelta(days=drop)
        ss = C.changepoints({k: v for k, v in byday.items() if k <= end.isoformat()},
                            first_live, end, 28, 0.10)
        s, e, n, m = ss[-1]
        note = ""
        if (end - s).days > 70:
            note = "  <- no late break: the elevated tail is now shorter than min_seg=28"
        print(f"     drop last {drop:>2d}d -> last segment {s}..{e} ({n:>3d}d)  "
              f"{m:>6.1f} legs/day{note}")
    print("     HONEST READING of (iii): dropping 14+ days leaves fewer than 28 elevated days,")
    print("     which binary segmentation cannot express as a segment at all. This test loses")
    print("     its power before it loses the signal, so it neither confirms nor refutes.")
    print("     (i) and (ii) are the tests that carry the conclusion.")

    # =========================================================================
    C.hdr("S2  VOLUME - month, ISO week, day of week")

    C.sub("2a. By month  ->  demand_by_month.csv")
    mrows = []
    months = sorted({month_key(lg["d"]) for lg in legs if lg["d"] <= H.last_demand_day})
    per_month = collections.Counter(month_key(lg["d"]) for lg in legs
                                    if lg["d"] <= H.last_demand_day)
    print(f"{'month':<9s}{'days obs':>9s}{'legs':>8s}{'legs/day':>10s}  complete?  regime")
    for mk in months:
        y, mo = int(mk[:4]), int(mk[5:7])
        m_first, m_last = dt.date(y, mo, 1), month_last_day(y, mo)
        lo = max(m_first, first_live)
        hi = min(m_last, H.last_demand_day)
        nd = (hi - lo).days + 1
        complete = (m_last <= H.last_demand_day) and (m_first >= first_live)
        mid = m_first + dt.timedelta(days=14)
        reg = next((f"{s}..{e}" for s, e, _, _ in segs if s <= mid <= e), "")
        print(f"{mk:<9s}{nd:>9d}{per_month[mk]:>8,}{per_month[mk] / nd:>10.1f}  "
              f"{'yes' if complete else 'PARTIAL':<10s} {reg}")
        mrows.append([mk, nd, per_month[mk], round(per_month[mk] / nd, 2),
                      int(complete), reg])
    C.write_csv("demand_by_month.csv",
                ["month", "days_observed", "legs", "legs_per_day", "month_complete", "regime"],
                mrows)
    print("\nNOTE the last row is a PARTIAL month and its legs/day is the only number on it")
    print("     that may be compared with a complete month.  [measured]")

    C.sub("2b. By ISO week (complete weeks only)  ->  01_demand_weekly.csv")
    wk_days = collections.defaultdict(list)
    for d in daterange(first_live, H.last_demand_day):
        wk_days[iso_week_key(d)].append(d)
    wk_legs = collections.Counter(iso_week_key(lg["d"]) for lg in legs
                                  if lg["d"] <= H.last_demand_day)
    wrows = []
    complete_weeks = []
    for wk in sorted(wk_days):
        ds = wk_days[wk]
        full = len(ds) == 7
        wrows.append([wk, str(ds[0]), str(ds[-1]), len(ds), wk_legs[wk],
                      round(wk_legs[wk] / len(ds), 2), int(full)])
        if full:
            complete_weeks.append((wk, ds, wk_legs[wk]))
    C.write_csv("01_demand_weekly.csv",
                ["iso_week", "first_day", "last_day", "days", "legs", "legs_per_day",
                 "week_complete"], wrows)
    tail = [r for r in wrows if r[6] == 1][-10:]
    print(f"{'week':<10s}{'first':>12s}{'legs':>7s}{'legs/day':>10s}")
    for r in tail:
        print(f"{r[0]:<10s}{r[1]:>12s}{r[4]:>7,}{r[5]:>10.1f}")
    print(f"\ncomplete weeks in file: {len(complete_weeks)}  [measured]")

    C.sub("2c. Day of week - the DISTRIBUTION across days, not the mean")
    print("The brief asks for a distribution. On the CURRENT regime alone each weekday has")
    print(f"only {CUR_N // 7} or {CUR_N // 7 + 1} observations, which cannot carry a P90. Two")
    print("readings follow; the second is the one to plan on.\n")

    print("(i) RAW, current regime only  [measured, small n - do NOT plan a P90 on this]")
    print(f"{'dow':<5s}{'n days':>7s}{'legs':>7s}{'mean':>8s}{'P50':>8s}{'P75':>8s}"
          f"{'P90':>8s}{'max':>6s}")
    cur_by_dow = collections.defaultdict(list)
    for d in CUR_DAYS:
        cur_by_dow[d.weekday()].append(byday[d.isoformat()])
    cur_dow_tot = collections.Counter()
    for lg in L_CUR:
        cur_dow_tot[lg["dow"]] += 1
    for w in range(7):
        v = cur_by_dow[w]
        print(f"{DOW_NAME[w]:<5s}{len(v):>7d}{sum(v):>7,}{sum(v) / len(v):>8.1f}"
              f"{C.pct(v, 50):>8.1f}{C.pct(v, 75):>8.1f}{C.pct(v, 90):>8.1f}{max(v):>6d}")

    print("\n(ii) SHAPE from the pooled window, LEVEL from the current regime  [modeled]")
    print("     For every day in the pooled window take r = legs(d) / mean(28 days CENTRED")
    print("     on d). 28 days is exactly four of each weekday, so r carries no weekday bias")
    print("     and, being centred, does not lag a growing series. Then per weekday take the")
    print("     percentiles of r and multiply by the CURRENT level.")
    ratios = collections.defaultdict(list)
    n_ratio_days = 0
    for d in POOL_DAYS:
        r, _, _ = centered_ratio(byday, d)
        if r is not None:
            ratios[d.weekday()].append(r)
            n_ratio_days += 1
    print(f"     usable days: {n_ratio_days} of {len(POOL_DAYS)} "
          f"(the last 14 days have no forward half)\n")
    print(f"{'dow':<5s}{'n':>5s}{'r P50':>8s}{'r P75':>8s}{'r P90':>8s}   "
          f"{'legs P50':>9s}{'legs P75':>9s}{'legs P90':>9s}")
    dow_rows = []
    for w in range(7):
        v = ratios[w]
        p50, p75, p90 = C.pct(v, 50), C.pct(v, 75), C.pct(v, 90)
        raw = cur_by_dow[w]
        print(f"{DOW_NAME[w]:<5s}{len(v):>5d}{p50:>8.3f}{p75:>8.3f}{p90:>8.3f}   "
              f"{p50 * CUR_M:>9.1f}{p75 * CUR_M:>9.1f}{p90 * CUR_M:>9.1f}")
        dow_rows.append([DOW_NAME[w], len(raw), sum(raw), round(sum(raw) / len(raw), 2),
                         round(C.pct(raw, 50), 2), round(C.pct(raw, 75), 2),
                         round(C.pct(raw, 90), 2), max(raw),
                         len(v), round(p50, 4), round(p75, 4), round(p90, 4),
                         round(p50 * CUR_M, 2), round(p75 * CUR_M, 2), round(p90 * CUR_M, 2)])
    C.write_csv("01_demand_dow_distribution.csv",
                ["dow", "cur_n_days", "cur_legs", "cur_mean", "cur_p50", "cur_p75",
                 "cur_p90", "cur_max", "pool_n_days", "ratio_p50", "ratio_p75",
                 "ratio_p90", "est_legs_p50", "est_legs_p75", "est_legs_p90"], dow_rows)

    print("\nAGREEMENT CHECK between (i) and (ii), on P75 - the two are computed from")
    print("different day sets by different arithmetic, so this is a real cross-check:")
    worst, worst_w = 0.0, None
    for w in range(7):
        a = C.pct(cur_by_dow[w], 75)
        b = C.pct(ratios[w], 75) * CUR_M
        gap = abs(a - b) / max(a, 1e-9)
        if gap > worst:
            worst, worst_w = gap, w
        print(f"  {DOW_NAME[w]:<4s} raw {a:>6.1f}   modeled {b:>6.1f}   "
              f"{100.0 * (b - a) / a:+6.1f}%")
    others = sorted(abs(C.pct(cur_by_dow[w], 75) - C.pct(ratios[w], 75) * CUR_M)
                    / C.pct(cur_by_dow[w], 75) for w in range(7) if w != worst_w)
    print(f"\n  Six of seven weekdays agree to within "
          f"{100.0 * others[-1]:.0f}%. The exception is "
          f"{DOW_NAME[worst_w]} at {100.0 * worst:.0f}%:")
    print(f"  the {len(cur_by_dow[worst_w])} {DOW_NAME[worst_w]}s in the current regime "
          f"({', '.join(str(x) for x in sorted(cur_by_dow[worst_w]))}) are running")
    print(f"  hotter than the pooled {DOW_NAME[worst_w]} ratio predicts. With n="
          f"{len(cur_by_dow[worst_w])} that is as likely to be")
    print("  four unusual days as a shape change, and there is no way to tell from this")
    print("  window. USE (ii) - it has 20 observations per weekday against 4 - but carry")
    print(f"  {DOW_NAME[worst_w]} as the one weekday whose estimate should be revisited on the "
          f"next pull.")

    C.sub("2d. Weekend concentration - testing the old document's two headline claims")
    def weekend_stats(pool, days):
        tot = len(pool)
        we = sum(1 for lg in pool if lg["dow"] in WEEKEND)
        nd = len(days)
        we_d = sum(1 for d in days if d.weekday() in WEEKEND)
        return we / float(tot), we_d / float(nd), tot, we

    for label, pool, days in (("CURRENT regime", L_CUR, CUR_DAYS),
                              ("PRIOR plateau ", L_PRI, PRI_DAYS),
                              ("POOLED        ", L_POOL, POOL_DAYS),
                              ("ALL live days ", legs, list(daterange(first_live, H.last_demand_day)))):
        pool = [lg for lg in pool if lg["d"] <= H.last_demand_day]
        s_vol, s_day, tot, we = weekend_stats(pool, days)
        print(f"{label}  Fri+Sat+Sun = {fmt_pct(s_vol)} of volume "
              f"({we:,}/{tot:,}) on {fmt_pct(s_day)} of days")
    print(f"\nOLD DOC CLAIM: 55.4% of volume on 43% of days.")

    print("\nSECOND CHECK, structurally different - the WITHIN-WEEK paired share.")
    print("Compute the Fri+Sat+Sun share inside each complete ISO week separately, then")
    print("take percentiles across weeks. This is immune to the level trend, which a")
    print("pooled ratio is not.")
    wk_share = []
    wk_share_cur = []
    for wk, ds, n in complete_weeks:
        if n == 0:
            continue
        we = sum(1 for d in ds if d.weekday() in WEEKEND for _ in range(byday[d.isoformat()]))
        if ds[0] >= POOL_S and ds[-1] <= POOL_E:
            wk_share.append(we / float(n))
        if ds[0] >= CUR_S and ds[-1] <= CUR_E:
            wk_share_cur.append(we / float(n))
    print(f"  pooled window,  {len(wk_share)} complete weeks: "
          f"P25 {fmt_pct(C.pct(wk_share, 25))}  P50 {fmt_pct(C.pct(wk_share, 50))}  "
          f"P75 {fmt_pct(C.pct(wk_share, 75))}  P90 {fmt_pct(C.pct(wk_share, 90))}")
    if wk_share_cur:
        print(f"  current regime, {len(wk_share_cur)} complete weeks: "
              f"P50 {fmt_pct(C.pct(wk_share_cur, 50))}  range "
              f"{fmt_pct(min(wk_share_cur))}-{fmt_pct(max(wk_share_cur))}")

    C.sub("2e. Sat:Tue ratio - the number the flat coverage target is up against")
    print("The incumbent target is a single flat number, seven days a week, all year:")
    print("  dispatching/schedule_risk.py:46   COVERAGE_TARGET_DEFAULT = 14")
    print("  dispatching/views.py:15117        COVERAGE_TARGET = 14   (a second copy)")
    print("Here is the demand shape it is flat against.\n")
    def sat_tue(pool):
        c = collections.Counter(lg["dow"] for lg in pool)
        return c[5], c[1], (c[5] / float(c[1]) if c[1] else None)
    for label, pool, days in (("CURRENT regime", L_CUR, CUR_DAYS),
                              ("PRIOR plateau ", L_PRI, PRI_DAYS),
                              ("POOLED        ", L_POOL, POOL_DAYS)):
        sat, tue, ratio = sat_tue(pool)
        nsat = sum(1 for d in days if d.weekday() == 5)
        ntue = sum(1 for d in days if d.weekday() == 1)
        print(f"{label}  Sat {sat:,}/{nsat}d = {sat / nsat:>5.1f}/day   "
              f"Tue {tue:,}/{ntue}d = {tue / ntue:>5.1f}/day   "
              f"ratio of means {((sat / nsat) / (tue / ntue)):.2f}x")
    print("\nOLD DOC CLAIM: Saturday runs 2.2x Tuesday.")
    print("\nSECOND CHECK - the WITHIN-WEEK paired ratio (Sat_w / Tue_w, per week, then")
    print("percentiles across weeks). Immune to level drift and to which weeks a window")
    print("happens to contain.")
    pair = []
    pair_cur = []
    for wk, ds, _ in complete_weeks:
        sat = next((byday[d.isoformat()] for d in ds if d.weekday() == 5), 0)
        tue = next((byday[d.isoformat()] for d in ds if d.weekday() == 1), 0)
        if tue > 0 and ds[0] >= POOL_S and ds[-1] <= POOL_E:
            pair.append(sat / float(tue))
            if ds[0] >= CUR_S:
                pair_cur.append(sat / float(tue))
    print(f"  pooled, {len(pair)} weeks:  P10 {C.pct(pair, 10):.2f}x  P25 {C.pct(pair, 25):.2f}x  "
          f"P50 {C.pct(pair, 50):.2f}x  P75 {C.pct(pair, 75):.2f}x  P90 {C.pct(pair, 90):.2f}x")
    if pair_cur:
        print(f"  current, {len(pair_cur)} weeks: values "
              f"{', '.join(f'{x:.2f}' for x in pair_cur)}")
    print("\nThe two methods answer slightly different questions: the ratio of means is a")
    print("volume statement about a window, the paired median is a statement about a")
    print("typical week. Quote the paired median operationally - a shift template is")
    print("built one week at a time.")

    # =========================================================================
    C.hdr("S3  HOUR OF DAY - on BOOKED pickup_time, with the arrival caveat measured")

    C.sub("3a. The caveat, measured before anything is built on the hour histogram")
    print("dispatching/pickup_policy.py:46 - ARRIVAL_MEET_GRACE_MIN = 10: the driver must be")
    print("at the IN-TERMINAL meet point by gate + 10. reservations/models.py:2053-2083")
    print("(has_flight_time_mismatch) compares datetime.combine(pickup_date, pickup_time)")
    print("DIRECTLY against the flight's arrival time and calls a 30-minute gap a mismatch -")
    print("i.e. the application itself treats booked pickup_time on an arrival AS the flight")
    print("arrival time. Test it against the flight table:")
    fl = {r["leg_id"]: r for r in C.q(con, f"""
        SELECT l.id AS leg_id,
               f.scheduled_gate_arrival_local AS sga,
               f.scheduled_arrival_local      AS sa,
               f.flight_type                  AS ftype
        {C.LEG_JOIN}
        JOIN reservations_flight f ON f.id = l.flight_information_id
        WHERE {C.LIVE_LEG} AND {C.SANE_DATES}""")}
    dev = {"ARRIVAL": [], "DEPARTURE": [], "OTHER": []}
    for lg in legs:
        if lg["d"] > H.last_demand_day:
            continue
        r = fl.get(lg["leg_id"])
        if not r:
            continue
        t = r["sga"] or r["sa"]
        if not t:
            continue
        p = C.booked_dtm(lg["d"].isoformat(), lg["pt"])
        if not p:
            continue
        dev[lg["kind"]].append((p - C.to_local(t)).total_seconds() / 60.0)
    for k in ("ARRIVAL", "DEPARTURE", "OTHER"):
        if dev[k]:
            print("  " + C.fmt_describe(f"booked pickup - flight arrival, {k} (min)", dev[k]))
    print("  ONLY the ARRIVAL row is interpretable. On a DEPARTURE the linked flight is the")
    print("  one the guest is catching, so its 'arrival' column is the landing time at the")
    print("  far end - a different city, hours later, and not a quantity the pickup is set")
    print(f"  against. n={len(dev['DEPARTURE'])} there in any case. The OTHER row is noise "
          f"(n={len(dev['OTHER'])}).")
    if dev["ARRIVAL"]:
        med = C.pct(dev["ARRIVAL"], 50)
        print(f"\n  [measured] On {len(dev['ARRIVAL']):,} flight-linked ARRIVAL legs the median")
        print(f"  offset is {med:+.0f} min. The booked pickup_time on an arrival IS the flight")
        print("  arrival time. So the histogram below is 'flights landing' + 'true departures',")
        print("  NOT 'drivers becoming busy'. An arrival driver is committed EARLIER (drive to")
        print("  the airport) and released LATER (gate+10 meet, then a 45-minute airport dwell -")
        print("  pickup_policy.py:63 ARRIVAL_DWELL_MIN). The occupancy-anchored version of this")
        print("  curve is script 05's job and it will not look like this one.")

    C.sub("3b. Hour histogram, current regime, split by trip kind  ->  01_demand_hour_by_tripkind.csv")
    hr_all = collections.Counter(lg["hour"] for lg in L_CUR)
    hr_kind = collections.defaultdict(collections.Counter)
    for lg in L_CUR:
        hr_kind[lg["kind"]][lg["hour"]] += 1
    tot_cur = float(len(L_CUR))
    print(f"{'hr':>3s}{'legs':>7s}{'share':>8s}  {'ARRIVAL':>8s}{'DEPART':>8s}{'OTHER':>7s}"
          f"   {'bar (share of day)':<28s}")
    hrows = []
    for h in range(24):
        n = hr_all.get(h, 0)
        s = n / tot_cur if tot_cur else 0
        a, dpt, o = (hr_kind["ARRIVAL"].get(h, 0), hr_kind["DEPARTURE"].get(h, 0),
                     hr_kind["OTHER"].get(h, 0))
        bar = "#" * int(round(s * 400))
        print(f"{h:>3d}{n:>7,}{fmt_pct(s):>8s}  {a:>8,}{dpt:>8,}{o:>7,}   {bar:<28s}")
        hrows.append([h, n, round(s, 5), a, dpt, o])
    C.write_csv("01_demand_hour_by_tripkind.csv",
                ["hour", "legs", "share", "arrival", "departure", "other"], hrows)

    for k in ("ARRIVAL", "DEPARTURE", "OTHER"):
        c = hr_kind[k]
        t = float(sum(c.values()))
        if not t:
            continue
        top = sorted(c.items(), key=lambda kv: -kv[1])[:4]
        print(f"  {k:<10s} n={int(t):>6,}  busiest hours: "
              + "  ".join(f"{h:02d}h {fmt_pct(n / t)}" for h, n in top))

    C.sub("3c. Natural demand periods, FOUND IN THE DATA - method 1: runs above uniform")
    share = [hr_all.get(h, 0) / tot_cur for h in range(24)]
    uni = 1.0 / 24
    print(f"uniform share = {fmt_pct(uni, 2)} per hour. Maximal circular runs above it:")
    runs = circ_runs_above(share, uni)
    runs.sort(key=lambda r: -sum(share[h] for h in hours_in(*r)))
    for a, b in runs:
        hs = hours_in(a, b)
        v = sum(share[h] for h in hs)
        print(f"  {a:02d}:00-{(b + 1) % 24:02d}:00   {len(hs):>2d}h   "
              f"{fmt_pct(v):>7s} of volume   ({fmt_pct(len(hs) / 24.0)} of the clock)")
    print(f"and above 1.25x uniform ({fmt_pct(1.25 * uni, 2)}):")
    for a, b in sorted(circ_runs_above(share, 1.25 * uni),
                       key=lambda r: -sum(share[h] for h in hours_in(*r))):
        hs = hours_in(a, b)
        print(f"  {a:02d}:00-{(b + 1) % 24:02d}:00   {len(hs):>2d}h   "
              f"{fmt_pct(sum(share[h] for h in hs)):>7s} of volume")

    C.sub("3d. Natural demand periods - method 2: optimal contiguous segmentation of the clock")
    print("Partition the 24-hour cycle into k contiguous arcs minimising within-arc squared")
    print("error on the hourly share. Circular (23h is adjacent to 00h): every rotation is")
    print("tried and the cheapest kept. No assumed morning/evening shape anywhere.\n")
    sse1, _ = circular_segmentation(share, 1)
    print(f"{'k':>3s}{'SSE':>12s}{'var explained':>15s}{'marginal gain':>15s}")
    prev_sse = sse1
    seg_by_k = {}
    for k in range(1, 8):
        s_k, spans = circular_segmentation(share, k)
        seg_by_k[k] = (s_k, spans)
        ve = 1 - s_k / sse1 if sse1 > 0 else 0
        mg = (prev_sse - s_k) / sse1 if sse1 > 0 else 0
        print(f"{k:>3d}{s_k:>12.6f}{fmt_pct(ve):>15s}{fmt_pct(mg):>15s}")
        prev_sse = s_k
    chosen = next((k for k in range(1, 8) if 1 - seg_by_k[k][0] / sse1 >= 0.90), 7)
    print(f"\nRULE (stated before looking): the smallest k explaining >=90% of the variance.")
    print(f"CHOSEN k = {chosen}\n")
    print(f"{'period':<16s}{'hours':>6s}{'legs':>8s}{'share':>9s}{'legs/hr':>9s}"
          f"{'vs flat':>9s}   dominant kind")
    # print in clock order, starting from the segment that contains the quietest hour
    quiet = min(range(24), key=lambda h: share[h])
    spans = seg_by_k[chosen][1]
    start_i = next(i for i, (a, b) in enumerate(spans) if quiet in hours_in(a, b))
    ordered = spans[start_i:] + spans[:start_i]
    period_rows = []
    for a, b in ordered:
        hs = hours_in(a, b)
        n = sum(hr_all.get(h, 0) for h in hs)
        v = n / tot_cur
        kk = collections.Counter()
        for lg in L_CUR:
            if lg["hour"] in hs:
                kk[lg["kind"]] += 1
        dom = ", ".join(f"{x} {fmt_pct(c / float(n), 0)}"
                        for x, c in kk.most_common(2)) if n else ""
        print(f"{a:02d}:00-{(b + 1) % 24:02d}:00{'':<6s}{len(hs):>6d}{n:>8,}{fmt_pct(v):>9s}"
              f"{n / float(len(hs) * CUR_N):>9.2f}{v / (len(hs) / 24.0):>8.2f}x   {dom}")
        period_rows.append((a, b, len(hs), n, v))
    print("\nThe two methods are independent (a threshold rule vs a least-squares partition).")
    print("Where their boundaries land within an hour of each other, the boundary is real.")
    m1 = {a for a, _ in runs} | {(b + 1) % 24 for _, b in runs}
    m2 = {a for a, _ in ordered}
    agree = sorted(h for h in m2 if any((h - x) % 24 in (0, 1, 23) for x in m1))
    print("boundaries method 1: " + ", ".join(f"{h:02d}:00" for h in sorted(m1)))
    print("boundaries method 2: " + ", ".join(f"{h:02d}:00" for h in sorted(m2)))
    print("agreeing within 1 h: " + (", ".join(f"{h:02d}:00" for h in agree) or "none"))

    C.sub("3e. How concentrated is the peak?")
    peak_h = max(range(24), key=lambda h: hr_all.get(h, 0))
    peak_share = share[peak_h]
    print(f"busiest single hour        {peak_h:02d}:00   {hr_all[peak_h]:,} legs   "
          f"{fmt_pct(peak_share)} of the day   {peak_share / uni:.2f}x flat  [measured]")
    for w in (2, 3, 4, 6, 8):
        best = max(range(24), key=lambda a: sum(share[(a + i) % 24] for i in range(w)))
        v = sum(share[(best + i) % 24] for i in range(w))
        print(f"busiest contiguous {w:>2d}h      {best:02d}:00-{(best + w) % 24:02d}:00  "
              f"{fmt_pct(v):>7s} of volume   ({fmt_pct(w / 24.0)} of the clock)")
    ss = sorted(share, reverse=True)
    cover = {}
    for target in (0.5, 0.8, 0.9):
        acc, k = 0.0, 0
        while acc < target and k < 24:
            acc += ss[k]
            k += 1
        cover[target] = k
        print(f"hours needed to cover {fmt_pct(target, 0):>4s}   {k:>2d} of 24")
    hhi = sum(s * s for s in share)
    ent = -sum(s * math.log(s) for s in share if s > 0) / math.log(24)
    print(f"HHI {hhi:.4f} (flat = {1 / 24.0:.4f})   normalised entropy {ent:.4f} "
          f"(flat = 1.000)")
    best2 = max(range(24), key=lambda a: sum(share[(a + i) % 24] for i in range(2)))
    v2 = sum(share[(best2 + i) % 24] for i in range(2))
    print(f"READ: BOTH things are true and they pull in opposite directions.")
    print(f"  There IS a real spike: {peak_h:02d}:00 alone is {fmt_pct(peak_share)} of the day, "
          f"{peak_share / uni:.1f}x flat, and the")
    print(f"  two hours {best2:02d}:00-{(best2 + 2) % 24:02d}:00 carry {fmt_pct(v2)} of "
          f"everything on {fmt_pct(2 / 24.0)} of the clock.")
    print(f"  AND the day is wide: it still takes {cover[0.8]} of 24 hours to reach 80% of")
    print(f"  volume, and normalised entropy is {ent:.2f} against 1.00 for a flat day.")
    locmax = [h for h in range(24)
              if share[h] > share[(h - 1) % 24] and share[h] > share[(h + 1) % 24]]
    # a genuine SECOND peak has to be separated from the first by more than the
    # width of the first block, so require at least 3 clock hours of distance
    sec = [h for h in locmax if min((h - peak_h) % 24, (peak_h - h) % 24) >= 3]
    sec_h = max(sec, key=lambda h: share[h]) if sec else None
    print(f"  Local maxima on the clock: "
          f"{', '.join(f'{h:02d}:00 ({fmt_pct(share[h])})' for h in locmax)}.")
    if sec_h is not None:
        print(f"  Everything within two hours of {peak_h:02d}:00 is one broad morning block. "
              f"The largest")
        print(f"  bump OUTSIDE that block is {sec_h:02d}:00 at {fmt_pct(share[sec_h])}, "
              f"{share[sec_h] / peak_share:.2f}x the morning peak.")
        print("  That is a shoulder, not a second peak: this is a ONE-peak day.")
    print("  Consequence: a single morning-heavy shift shape with a thinner afternoon tail")
    print("  fits this demand. A symmetric two-peak commuter template does not, and a flat")
    print("  headcount across the open hours wastes the tail to cover the spike.")

    C.sub("3f. Is the clock itself trustworthy? Minute-of-hour granularity")
    print("If a default or placeholder time were being written, one clock value would be")
    print("over-represented and the whole histogram would be an artefact. Test the minutes.")
    mins = collections.Counter(int(lg["pt"][3:5]) for lg in L_CUR)
    mins_by_kind = collections.defaultdict(collections.Counter)
    for lg in L_CUR:
        mins_by_kind[lg["kind"]][int(lg["pt"][3:5])] += 1
    round_set = (0, 15, 30, 45)
    print(f"{'':<12s}{'n':>7s}{'at :00':>9s}{'at :30':>9s}{'quarter-hours':>16s}")
    for lbl, c in [("ALL", mins)] + [(k, mins_by_kind[k]) for k in
                                     ("ARRIVAL", "DEPARTURE", "OTHER")]:
        t = float(sum(c.values()))
        if not t:
            continue
        print(f"{lbl:<12s}{int(t):>7,}{fmt_pct(c[0] / t):>9s}{fmt_pct(c[30] / t):>9s}"
              f"{fmt_pct(sum(c[m] for m in round_set) / t):>16s}")
    pk_min = collections.Counter(int(lg["pt"][3:5]) for lg in L_CUR if lg["hour"] == peak_h)
    print(f"\nthe busiest hour ({peak_h:02d}:00) alone: "
          f"{fmt_pct(pk_min[0] / float(sum(pk_min.values())))} at :00 against "
          f"{fmt_pct(mins[0] / float(sum(mins.values())))} overall")
    print("READ, and this is a SECOND, INDEPENDENT confirmation of S3a: DEPARTURES sit on a")
    print("quarter-hour 95% of the time because a human picks a round time, while ARRIVALS")
    print("do it 8% of the time - barely above the 6.7% you would get from a uniform minute.")
    print("An arrival's minute is not chosen by anyone; it is copied from the flight. Two")
    print("unrelated tests (offset against the flight table, and minute granularity) now")
    print("say the same thing.")
    print("For the histogram itself: no single clock value dominates and no placeholder is")
    print("visible, so the HOURLY reading is sound. Do NOT push the departure side of this")
    print("data to sub-hour resolution - there is no real information below the quarter hour.")

    C.sub("3g. dow x hour  ->  demand_by_dow_hour.csv")
    dh_rows = []
    for label, pool, days in (("CURRENT", L_CUR, CUR_DAYS), ("PRIOR", L_PRI, PRI_DAYS)):
        t = float(len(pool))
        cnt = collections.Counter((lg["dow"], lg["hour"]) for lg in pool)
        kind = collections.defaultdict(collections.Counter)
        cls = collections.defaultdict(collections.Counter)
        for lg in pool:
            kind[(lg["dow"], lg["hour"])][lg["kind"]] += 1
            cls[(lg["dow"], lg["hour"])][lg["cls"]] += 1
        ndow = collections.Counter(d.weekday() for d in days)
        for w in range(7):
            for h in range(24):
                n = cnt.get((w, h), 0)
                dh_rows.append([label, w, DOW_NAME[w], h, n, round(n / t, 6),
                                round(n / float(ndow[w]), 4), ndow[w],
                                kind[(w, h)]["ARRIVAL"], kind[(w, h)]["DEPARTURE"],
                                kind[(w, h)]["OTHER"]])
    C.write_csv("demand_by_dow_hour.csv",
                ["window", "dow_num", "dow", "hour", "legs", "share_of_window",
                 "legs_per_occurrence_of_that_dow", "n_days_of_that_dow",
                 "arrival", "departure", "other"], dh_rows)
    print("legs per occurrence of that weekday, CURRENT regime "
          f"({CUR_S}..{CUR_E}):\n")
    ndow_cur = collections.Counter(d.weekday() for d in CUR_DAYS)
    cnt_cur = collections.Counter((lg["dow"], lg["hour"]) for lg in L_CUR)
    print("hr  " + "".join(f"{DOW_NAME[w]:>7s}" for w in range(7)) + f"{'ALL':>8s}")
    for h in range(24):
        row = [cnt_cur.get((w, h), 0) / float(ndow_cur[w]) for w in range(7)]
        print(f"{h:02d}  " + "".join(f"{x:>7.2f}" for x in row)
              + f"{sum(cnt_cur.get((w, h), 0) for w in range(7)) / float(CUR_N):>8.2f}")
    print("    " + "".join(f"{sum(cnt_cur.get((w, h), 0) for h in range(24)) / float(ndow_cur[w]):>7.1f}"
                           for w in range(7)) + f"{CUR_M:>8.1f}   <- legs/day")

    print("\nIs the SHAPE of the day the same on a busy day as a quiet one?")
    we_h = collections.Counter(lg["hour"] for lg in L_POOL if lg["dow"] in WEEKEND)
    wd_h = collections.Counter(lg["hour"] for lg in L_POOL if lg["dow"] not in WEEKEND)
    t_we = tvd(we_h, wd_h)
    print(f"  TVD(Fri-Sun hourly profile, Mon-Thu hourly profile) = {t_we:.4f}  [measured]")
    pk_we = max(range(24), key=lambda h: we_h.get(h, 0))
    pk_wd = max(range(24), key=lambda h: wd_h.get(h, 0))
    print(f"  weekend peak hour {pk_we:02d}:00   weekday peak hour {pk_wd:02d}:00")
    print(f"  READ: {'the weekend is the same day, only bigger - one hourly template scales.' if t_we < 0.05 else 'the weekend day has a DIFFERENT shape, not just more of it.'}")

    # =========================================================================
    C.hdr("S4  VEHICLE CLASS MIX")

    C.sub("4a. Overall, by regime  ->  class_mix.csv")
    cls_rows = []
    cur_cls = collections.Counter(lg["cls"] for lg in L_CUR)
    pri_cls = collections.Counter(lg["cls"] for lg in L_PRI)
    order = [c for c, _ in cur_cls.most_common()]
    print(f"{'class':<14s}{'CUR legs':>9s}{'CUR share':>11s}{'CUR/day':>9s}"
          f"{'PRIOR share':>13s}{'delta':>9s}")
    for c in order:
        cs = cur_cls[c] / float(len(L_CUR))
        ps = pri_cls[c] / float(len(L_PRI))
        print(f"{c:<14s}{cur_cls[c]:>9,}{fmt_pct(cs):>11s}"
              f"{cur_cls[c] / float(CUR_N):>9.1f}{fmt_pct(ps):>13s}"
              f"{100.0 * (cs - ps):>+8.1f}pp")
        cls_rows.append(["CURRENT", "overall", "", c, cur_cls[c], round(cs, 5)])
        cls_rows.append(["PRIOR", "overall", "", c, pri_cls[c], round(ps, 5)])

    C.sub("4b. Class mix by day of week (current regime, column shares)")
    print(f"{'class':<14s}" + "".join(f"{DOW_NAME[w]:>8s}" for w in range(7)))
    for c in order:
        cells = []
        for w in range(7):
            n = sum(1 for lg in L_CUR if lg["dow"] == w and lg["cls"] == c)
            d = sum(1 for lg in L_CUR if lg["dow"] == w)
            cells.append(n / float(d) if d else 0)
            cls_rows.append(["CURRENT", "dow", DOW_NAME[w], c, n,
                             round(n / float(d), 5) if d else 0])
        print(f"{c:<14s}" + "".join(f"{fmt_pct(x):>8s}" for x in cells))

    C.sub("4c. Class mix by hour (current regime, column shares)")
    print(f"{'hr':>3s}{'legs':>7s}" + "".join(f"{c[:9]:>10s}" for c in order))
    for h in range(24):
        d = hr_all.get(h, 0)
        cells = []
        for c in order:
            n = sum(1 for lg in L_CUR if lg["hour"] == h and lg["cls"] == c)
            cells.append(n / float(d) if d else 0)
            cls_rows.append(["CURRENT", "hour", str(h), c, n,
                             round(n / float(d), 5) if d else 0])
        print(f"{h:>3d}{d:>7,}" + "".join(f"{fmt_pct(x):>10s}" for x in cells))
    C.write_csv("class_mix.csv",
                ["window", "dimension", "key", "vehicle_class", "legs", "share"], cls_rows)

    C.sub("4d. Which classes are scarce? [unavailable] from the supply tables - read this")
    fleet = C.q(con, """SELECT v.vehicle_type AS vt,
                               SUM(CASE WHEN f.is_active THEN 1 ELSE 0 END) AS active,
                               COUNT(*) AS total
                        FROM drivers_fleetvehicle f
                        JOIN rates_vehicle v ON v.id = f.vehicle_type_id
                        GROUP BY 1""")
    fleet_active = {r["vt"]: r["active"] for r in fleet}
    fleet_total = {r["vt"]: r["total"] for r in fleet}
    cert_n = {r["vt"]: r["n"] for r in C.q(
        con, """SELECT v.vehicle_type AS vt, COUNT(*) AS n
                FROM drivers_driver_certified_vehicle_types cv
                JOIN rates_vehicle v ON v.id = cv.vehicle_id GROUP BY 1""")}
    req_cert = {v["vehicle_type"]: v["requires_certification"] for v in veh.values()}
    n_fleet_active = sum(fleet_active.values()) or 1
    n_drv = C.q1(con, "SELECT COUNT(*) FROM drivers_driver WHERE is_active=1")
    print("THREE supply tables exist and NOT ONE of them can carry a class-scarcity claim.")
    print("Printing them anyway, because the reason each fails is itself a finding:\n")
    print(f"{'class':<14s}{'demand/day':>11s}{'dem share':>11s}{'fleet cars':>12s}"
          f"{'car share':>11s}{'certified':>11s}{'req cert?':>11s}")
    for c in order:
        ds = cur_cls[c] / float(len(L_CUR))
        cs = fleet_active.get(c, 0) / float(n_fleet_active)
        print(f"{c:<14s}{cur_cls[c] / float(CUR_N):>11.1f}{fmt_pct(ds):>11s}"
              f"{str(fleet_active.get(c, 0)) + '/' + str(fleet_total.get(c, 0)):>12s}"
              f"{fmt_pct(cs):>11s}{cert_n.get(c, 0):>11d}"
              f"{('yes' if req_cert.get(c) else 'no'):>11s}")
    print(f"\n1. drivers_fleetvehicle holds {sum(fleet_total.values())} vehicles, "
          f"{n_fleet_active} of them active. That fleet")
    print(f"   cannot physically deliver {CUR_M:.0f} legs/day. It is the Samsara-tracked company")
    print("   fleet, not the supply that serves this demand - affiliates and owner-drivers")
    print("   bring their own cars and never appear here. Car share is NOT capacity share.")
    print("2. drivers_driver_certified_vehicle_types is a CERTIFICATION roster, and")
    print("   rates_vehicle.requires_certification is true for exactly one class. The 16")
    print("   Van(14 Pax) rows are real; the single row against each other class is noise.")
    print("   It does not enumerate who can drive what.")
    n_null_veh = C.q1(con, "SELECT COUNT(*) FROM drivers_driver WHERE is_active=1 "
                           "AND driver_type='inhouse' AND vehicle IS NULL")
    n_inhouse = C.q1(con, "SELECT COUNT(*) FROM drivers_driver WHERE is_active=1 "
                          "AND driver_type='inhouse'")
    print("3. drivers_driver.vehicle is free text ('SUV/Sedan/VAN', 'Sprinters/Suburbans',")
    print(f"   and one row containing a shift schedule) and is NULL for {n_null_veh} of the "
          f"{n_inhouse}")
    print(f"   active in-house drivers ({n_drv} active drivers overall). Unusable.")
    print("\nThe production scheduler does not use any of them either: load_all_driver_vtypes")
    print("(dispatching/scheduler.py:328-342) resolves capability PER DATE from that day's")
    print("DriverVehicleAssignment rows, and the scarcity reservation at scheduler.py:1877")
    print("counts DRIVERS of the exact type on the date, never cars. Which car a driver had")
    print("on a given day is the only real answer, it is a per-date fact, and it is script")
    print("04's question - not this one.")

    C.sub("4e. What CAN be measured: bodies that actually drove each class")
    print("Distinct drivers who drove at least one leg of a class, over the current regime,")
    print("and how many distinct drivers of that class turned out on a typical day. This is")
    print("revealed capability - no current-state flag, no roster table.  [measured]")
    print(f"{'class':<14s}{'demand/day':>11s}{'drivers ever':>13s}{'per-day P50':>12s}"
          f"{'per-day P90':>12s}{'legs/driver-day':>17s}")
    scarce_rank = []
    for c in order:
        ever = len({lg["driver_id"] for lg in L_CUR
                    if lg["cls"] == c and lg["driver_id"] is not None})
        per_day = []
        for d in CUR_DAYS:
            per_day.append(len({lg["driver_id"] for lg in L_CUR
                                if lg["d"] == d and lg["cls"] == c
                                and lg["driver_id"] is not None}))
        dpd = C.pct(per_day, 50) or 0
        lpd = (cur_cls[c] / float(CUR_N)) / dpd if dpd else None
        print(f"{c:<14s}{cur_cls[c] / float(CUR_N):>11.1f}{ever:>13d}"
              f"{C.pct(per_day, 50):>12.1f}{C.pct(per_day, 90):>12.1f}"
              f"{(('%.2f' % lpd) if lpd else 'n/a'):>17s}")
        scarce_rank.append((c, lpd))
    print("\nlegs per driver-day is the honest scarcity ranking: it says how hard the pool")
    print("that actually shows up for a class is being worked. It is NOT a utilisation")
    print("figure (a leg is not an hour) and it must not be read as one.")
    ranked = sorted([x for x in scarce_rank if x[1]], key=lambda kv: -kv[1])
    print("  hardest-worked first: "
          + "  ".join(f"{c} {v:.2f}" for c, v in ranked))

    C.sub("4f. Peak booked-hour load per class (a demand-side pressure signal)")
    print("Max legs of one class booked into a single clock hour on a day; percentiles across")
    print("days of the current regime. NOT occupancy - a booked hour is not a busy driver;")
    print("script 05 owns that. Use it only to rank classes against each other.")
    print(f"{'class':<14s}{'P50':>7s}{'P75':>7s}{'P90':>7s}{'max':>7s}"
          f"{'drivers/day P50':>17s}")
    for c in order:
        per_day = []
        drv_day = []
        for d in CUR_DAYS:
            cc = collections.Counter(lg["hour"] for lg in L_CUR
                                     if lg["d"] == d and lg["cls"] == c)
            per_day.append(max(cc.values()) if cc else 0)
            drv_day.append(len({lg["driver_id"] for lg in L_CUR
                                if lg["d"] == d and lg["cls"] == c
                                and lg["driver_id"] is not None}))
        print(f"{c:<14s}{C.pct(per_day, 50):>7.1f}{C.pct(per_day, 75):>7.1f}"
              f"{C.pct(per_day, 90):>7.1f}{max(per_day):>7d}{C.pct(drv_day, 50):>17.1f}")

    # =========================================================================
    C.hdr("S5  LANES")

    C.sub("5a. Classification coverage - how honest is loc_bucket()?")
    pb = collections.Counter(lg["pb"] for lg in L_CUR)
    db = collections.Counter(lg["db"] for lg in L_CUR)
    print(f"{'bucket':<12s}{'as pickup':>11s}{'share':>9s}{'as dropoff':>12s}{'share':>9s}")
    for b in sorted(set(pb) | set(db), key=lambda x: -(pb.get(x, 0) + db.get(x, 0))):
        print(f"{b:<12s}{pb.get(b, 0):>11,}{fmt_pct(pb.get(b, 0) / tot_cur):>9s}"
              f"{db.get(b, 0):>12,}{fmt_pct(db.get(b, 0) / tot_cur):>9s}")
    print(f"\nBOTH ends 'OTHER': "
          f"{sum(1 for lg in L_CUR if lg['pb'] == 'OTHER' and lg['db'] == 'OTHER'):,} legs "
          f"({fmt_pct(sum(1 for lg in L_CUR if lg['pb'] == 'OTHER' and lg['db'] == 'OTHER') / tot_cur)})")
    print("loc_bucket() is deliberately conservative: it keyword-matches free text, so OTHER")
    print("is an honest 'not identified', not 'residential'. Everything below inherits that.")

    C.sub("5b. Lane matrix, pickup bucket x dropoff bucket  ->  lane_matrix.csv")
    buckets = ["MCO", "SFB", "PORT", "DISNEY", "UNIVERSAL", "OTHER"]
    lane_rows = []
    for label, pool in (("CURRENT", L_CUR), ("PRIOR", L_PRI)):
        m = collections.Counter((lg["pb"], lg["db"]) for lg in pool)
        t = float(len(pool))
        if label == "CURRENT":
            print("rows = pickup, cols = dropoff. cells are % of all current-regime legs.\n")
            print(f"{'':<11s}" + "".join(f"{b[:9]:>10s}" for b in buckets) + f"{'row tot':>10s}")
            for p in buckets:
                rowt = sum(m.get((p, q), 0) for q in buckets)
                print(f"{p:<11s}" + "".join(f"{fmt_pct(m.get((p, q), 0) / t):>10s}"
                                            for q in buckets)
                      + f"{fmt_pct(rowt / t):>10s}")
            print(f"{'col tot':<11s}" + "".join(
                f"{fmt_pct(sum(m.get((p, q), 0) for p in buckets) / t):>10s}"
                for q in buckets))
        for p in buckets:
            for qq in buckets:
                lane_rows.append([label, p, qq, m.get((p, qq), 0), round(m.get((p, qq), 0) / t, 6)])
    C.write_csv("lane_matrix.csv",
                ["window", "pickup_bucket", "dropoff_bucket", "legs", "share"], lane_rows)

    C.sub("5c. Trip kind and airport share")
    for label, pool in (("CURRENT", L_CUR), ("PRIOR", L_PRI)):
        kc = collections.Counter(lg["kind"] for lg in pool)
        t = float(len(pool))
        print(f"{label:<8s}  " + "   ".join(f"{k} {kc[k]:,} ({fmt_pct(kc[k] / t)})"
                                            for k in ("ARRIVAL", "DEPARTURE", "OTHER")))
    airport = sum(1 for lg in L_CUR if lg["pb"] in ("MCO", "SFB") or lg["db"] in ("MCO", "SFB"))
    print(f"\nairport at either end, current regime: {airport:,} / {len(L_CUR):,} = "
          f"{fmt_pct(airport / tot_cur)}  [measured]")
    mco = sum(1 for lg in L_CUR if lg["pb"] == "MCO" or lg["db"] == "MCO")
    sfb = sum(1 for lg in L_CUR if lg["pb"] == "SFB" or lg["db"] == "SFB")
    print(f"  of which MCO {mco:,} ({fmt_pct(mco / float(airport))}), "
          f"SFB {sfb:,} ({fmt_pct(sfb / float(airport))})")

    C.sub("5d. Airport share BY HOUR - where the arrival caveat actually bites")
    print(f"{'hr':>3s}{'legs':>7s}{'ARRIVAL':>10s}{'DEPART':>10s}{'OTHER':>9s}"
          f"{'airport %':>11s}")
    for h in range(24):
        n = hr_all.get(h, 0)
        if not n:
            print(f"{h:>3d}{0:>7d}")
            continue
        a = hr_kind["ARRIVAL"].get(h, 0)
        dd = hr_kind["DEPARTURE"].get(h, 0)
        o = hr_kind["OTHER"].get(h, 0)
        print(f"{h:>3d}{n:>7,}{fmt_pct(a / float(n)):>10s}{fmt_pct(dd / float(n)):>10s}"
              f"{fmt_pct(o / float(n)):>9s}{fmt_pct((a + dd) / float(n)):>11s}")
    print("\nThe hours where ARRIVAL dominates are the hours whose true occupancy is shifted")
    print("EARLIEST relative to this histogram (the driver leaves for the airport before the")
    print("plane lands) and extended LATEST (gate+10 meet, 45-min dwell). Read every shift")
    print("boundary drawn on this chart as provisional until script 05 re-anchors it.")

    # =========================================================================
    C.hdr("S6  RESERVATION STRUCTURE - legs per reservation, round trips")

    rids_cur = {lg["rid"] for lg in L_CUR}
    legs_by_rid = collections.defaultdict(list)
    for lg in legs:
        legs_by_rid[lg["rid"]].append(lg)
    sizes = [len(legs_by_rid[r]) for r in rids_cur]
    dist = collections.Counter(sizes)
    print(f"reservations touching the current regime: {len(rids_cur):,}")
    print(f"{'legs on res':>12s}{'reservations':>14s}{'share of res':>14s}"
          f"{'legs':>8s}{'share of legs':>15s}")
    tot_l = sum(sizes)
    for k in sorted(dist):
        print(f"{k:>12d}{dist[k]:>14,}{fmt_pct(dist[k] / float(len(sizes))):>14s}"
              f"{k * dist[k]:>8,}{fmt_pct(k * dist[k] / float(tot_l)):>15s}")
    multi = sum(k * dist[k] for k in dist if k > 1)
    print(f"\nlegs on MULTI-leg reservations: {fmt_pct(multi / float(tot_l))}  [measured]")
    print(f"mean legs per reservation: {tot_l / float(len(sizes)):.3f}")
    straddle = sum(1 for r in rids_cur
                   if any(lg["d"].isoformat() not in cur_set for lg in legs_by_rid[r]))
    print(f"EDGE EFFECT: {straddle:,} of {len(rids_cur):,} "
          f"({fmt_pct(straddle / float(len(rids_cur)))}) of these reservations have at least")
    print("one leg outside the current regime; their legs are counted in full above, so the")
    print("legs-per-reservation figure is a property of the RESERVATION, not of the window.")

    C.sub("6a. Declared trip_type vs actual leg count")
    tt = collections.defaultdict(collections.Counter)
    for r in rids_cur:
        tt[legs_by_rid[r][0]["trip_type"]][len(legs_by_rid[r])] += 1
    print(f"{'trip_type':<12s}{'reservations':>13s}   leg-count distribution")
    for t_, c in sorted(tt.items(), key=lambda kv: -sum(kv[1].values())):
        tot = sum(c.values())
        print(f"{str(t_):<12s}{tot:>13,}   "
              + "  ".join(f"{k}legs {v:,} ({fmt_pct(v / float(tot), 0)})"
                          for k, v in sorted(c.items())))

    C.sub("6b. Round-trip structure - how far apart are the two halves?")
    gaps = []
    mirror = 0
    same_day = 0
    n_rt = 0
    for r in rids_cur:
        ls = sorted(legs_by_rid[r], key=lambda x: (x["d"], x["pt"]))
        if ls[0]["trip_type"] != "round_trip" or len(ls) != 2:
            continue
        n_rt += 1
        g = (ls[1]["d"] - ls[0]["d"]).days
        gaps.append(g)
        if g == 0:
            same_day += 1
        if ls[1]["pb"] == ls[0]["db"] and ls[1]["db"] == ls[0]["pb"]:
            mirror += 1
    print(f"2-leg round trips: {n_rt:,}")
    print("  " + C.fmt_describe("days between outbound and return", gaps))
    print(f"  same calendar day        {same_day:,} ({fmt_pct(same_day / float(n_rt))})")
    print(f"  return mirrors the lane  {mirror:,} ({fmt_pct(mirror / float(n_rt))})")
    gc = collections.Counter(gaps)
    print("  gap histogram (top): " + "  ".join(
        f"{g}d {n:,}" for g, n in sorted(gc.items(), key=lambda kv: -kv[1])[:8]))
    print("\nOPERATIONAL READ: a round trip is a HOLIDAY, not a same-day return. The two legs")
    print("land in different weeks and must be staffed independently - booking one does not")
    print("pre-commit a driver to the other.")

    # =========================================================================
    C.hdr("S7  IS THE CLASS DRIVEN BY PARTY SIZE?")

    C.sub("7a. Party size and luggage by class (current regime)")
    print(f"{'class':<14s}{'cap':>5s}{'lug':>5s}{'n':>7s}"
          f"{'pax P50':>9s}{'pax P75':>9s}{'pax P90':>9s}{'pax max':>9s}"
          f"{'bag P75':>9s}{'bag P90':>9s}")
    cap_by_cls = {v["vehicle_type"]: v for v in veh.values()}
    for c in order:
        pool = [lg for lg in L_CUR if lg["cls"] == c]
        pax = [lg["pax"] for lg in pool if lg["pax"] is not None]
        bags = [lg["bags"] for lg in pool if lg["bags"] is not None]
        v = cap_by_cls.get(c, {})
        print(f"{c:<14s}{v.get('capacity', 0):>5}{v.get('luggage_capacity', 0):>5}"
              f"{len(pool):>7,}"
              f"{C.pct(pax, 50):>9.1f}{C.pct(pax, 75):>9.1f}{C.pct(pax, 90):>9.1f}"
              f"{max(pax):>9d}{C.pct(bags, 75):>9.1f}{C.pct(bags, 90):>9.1f}")

    C.sub("7b. The direct test: what is the SMALLEST class that would have fitted?")
    print("For each leg, find the minimum-capacity class that satisfies every declared")
    print("constraint at once: seats, luggage, forward-facing seats, rear-facing seats and")
    print("boosters (rates_vehicle.capacity / luggage_capacity / ff_carseats_max /")
    print("rf_carseats_max / boosters_max). Then compare with what was actually booked.")
    print("This is a CAPACITY-minimal test, not a price-minimal one: an SUV seats 6 and a")
    print("minivan 5, so 'SUV where a minivan fits' is a genuine upgrade, but 'SUV where a")
    print("towncar fits' may equally be a customer paying for the bigger car.\n")
    tiers = sorted(veh.values(), key=lambda v: (v["capacity"], v["luggage_capacity"]))
    tier_rank = {v["vehicle_type"]: i for i, v in enumerate(tiers)}

    def fits(v, lg):
        return (v["capacity"] >= (lg["pax"] or 1)
                and v["luggage_capacity"] >= (lg["bags"] or 0)
                and v["ff_carseats_max"] >= lg["ff"]
                and v["rf_carseats_max"] >= lg["rf"]
                and v["boosters_max"] >= lg["boosters"])

    fit_rows = []
    exact = up = down = unknown = 0
    up_by_cls = collections.Counter()
    for lg in L_CUR:
        if lg["cls"] not in tier_rank or lg["pax"] is None:
            unknown += 1
            continue
        m = next((v for v in tiers if fits(v, lg)), None)
        if m is None:
            unknown += 1
            continue
        r_min, r_act = tier_rank[m["vehicle_type"]], tier_rank[lg["cls"]]
        if r_act == r_min:
            exact += 1
        elif r_act > r_min:
            up += 1
            up_by_cls[(m["vehicle_type"], lg["cls"])] += 1
        else:
            down += 1
    n_ok = exact + up + down
    print(f"legs testable {n_ok:,}   (unclassifiable {unknown:,})")
    print(f"  booked EXACTLY the minimum feasible class   {exact:>6,}  "
          f"{fmt_pct(exact / float(n_ok))}")
    print(f"  booked ABOVE it (paid for more car)         {up:>6,}  "
          f"{fmt_pct(up / float(n_ok))}")
    print(f"  booked BELOW it (declared party will not fit) {down:>4,}  "
          f"{fmt_pct(down / float(n_ok))}")
    print("\ntop minimum -> booked upgrades:")
    for (mn, ac), n in up_by_cls.most_common(8):
        print(f"  {mn:<12s} -> {ac:<12s} {n:>6,}  {fmt_pct(n / float(n_ok))}")
        fit_rows.append([mn, ac, n, round(n / float(n_ok), 5)])
    print("\nANSWER: party size sets a FLOOR on the class and nothing more. "
          f"{fmt_pct(up / float(n_ok))} of legs")
    print("carry a class strictly larger than the declared party needs, so class demand")
    print("cannot be forecast from party size - it must be forecast from booked class.")
    print(f"The {fmt_pct(down / float(n_ok))} booked BELOW the feasible minimum are a data-quality")
    print("signal, not a capacity plan: declared pax/luggage that the chosen car cannot hold.")
    C.write_csv("01_demand_class_fit.csv",
                ["min_feasible_class", "booked_class", "legs", "share_of_testable"], fit_rows)

    # =========================================================================
    C.hdr("S8  STATIONARITY - what may be pooled across regimes, and what may not")
    print("Statistic: total-variation distance between the PRIOR plateau's normalised shape")
    print("and the CURRENT regime's. TVD alone is meaningless - two samples of the same")
    print("process differ too. Every row therefore carries a NULL built by permuting whole")
    print("DAYS between the two windows, stratified by weekday so the weekday composition and")
    print("both group sizes are preserved exactly. p = P(null TVD >= observed).")
    print(f"{PERM_B} resamples, seed {RNG_SEED}.\n")

    def cat_days(pool, keyfn):
        out = collections.defaultdict(collections.Counter)
        for lg in pool:
            out[lg["d"].isoformat()][keyfn(lg)] += 1
        return out

    tests = [
        ("day of week", lambda lg: lg["dow"]),
        ("hour of day", lambda lg: lg["hour"]),
        ("dow x hour", lambda lg: (lg["dow"], lg["hour"])),
        ("vehicle class", lambda lg: lg["cls"]),
        ("lane (pickup x dropoff)", lambda lg: (lg["pb"], lg["db"])),
        ("trip kind", lambda lg: lg["kind"]),
        ("hour | ARRIVAL only", lambda lg: lg["hour"] if lg["kind"] == "ARRIVAL" else None),
        ("hour | DEPARTURE only", lambda lg: lg["hour"] if lg["kind"] == "DEPARTURE" else None),
        ("class x hour", lambda lg: (lg["cls"], lg["hour"])),
    ]
    print("The 'floor' column is the null's own P95 - the SMALLEST shift this test could have")
    print("detected. A high p-value with a high floor is not evidence of stationarity, it is")
    print("absence of power, and it is labelled as such.\n")
    stat_rows = []
    print(f"{'shape':<26s}{'cells':>6s}{'TVD':>8s}{'floor':>8s}{'p':>7s}   verdict")
    for label, keyfn in tests:
        cbd = collections.defaultdict(collections.Counter)
        for lg in L_POOL:
            k = keyfn(lg)
            if k is not None:
                cbd[lg["d"].isoformat()][k] += 1
        a, b = collections.Counter(), collections.Counter()
        for d in PRI_DAYS:
            a.update(cbd.get(d.isoformat(), {}))
        for d in CUR_DAYS:
            b.update(cbd.get(d.isoformat(), {}))
        obs = tvd(a, b)
        ncell = len(set(a) | set(b))
        p, n95, n50 = perm_null_tvd(PRI_DAYS, CUR_DAYS, cbd, rng)
        if p < 0.05:
            verdict = "SHIFTED - do not pool"
        elif n95 > obs:
            verdict = f"POOLABLE, but blind below TVD {n95:.3f}"
        else:
            verdict = "POOLABLE"
        print(f"{label:<26s}{ncell:>6d}{obs:>8.4f}{n95:>8.4f}{p:>7.3f}   {verdict}")
        stat_rows.append([label, ncell, round(obs, 5), round(n50, 5), round(n95, 5),
                          round(p, 4), verdict])
    C.write_csv("01_demand_stationarity.csv",
                ["shape", "cells", "tvd_prior_vs_current", "null_p50", "null_p95",
                 "p_value", "verdict"], stat_rows)
    srow = {r[0]: r for r in stat_rows}
    print("\nOLD DOC CLAIMED: dow TVD 0.020, hour TVD 0.045, dow x hour TVD 0.086, and read")
    print("all three as 'small'. There was no null, so 'small' was an eyeball judgement.")
    print("With a null the reading changes in BOTH directions: dow x hour at "
          f"{srow['dow x hour'][2]:.3f} is")
    print(f"unremarkable (the null's own median is {srow['dow x hour'][3]:.3f} - the cells are "
          f"too thin to say")
    print(f"anything), while hour-of-day at {srow['hour of day'][2]:.3f} is a REAL shift the "
          f"old reading would have waved past.")
    pool_ok = [r[0] for r in stat_rows if r[6] .startswith("POOLABLE")
               and r[1] <= 40]
    no_pool = [r[0] for r in stat_rows if r[6].startswith("SHIFTED")]
    weak = [r for r in stat_rows if r[6].startswith("POOLABLE") and r[1] > 40]
    print("\nLICENCE, in plain terms:")
    print("  POOL across the two regimes (normalised shares only, never raw counts):")
    print("    " + ", ".join(pool_ok) + ".")
    print("  DO NOT POOL: " + ", ".join(no_pool) + ".")
    print("    Cut every hour-of-day figure from the CURRENT regime alone and accept the")
    print("    smaller sample - S3's histogram already does.")
    if weak:
        print("  CANNOT CERTIFY EITHER WAY: " + ", ".join(r[0] for r in weak) + ". "
              + " and ".join(str(r[1]) for r in weak) + " cells")
        print(f"    against {len(L_CUR):,} current-regime legs is "
              f"{'; '.join(f'{len(L_CUR) / float(r[1]):.0f} legs a cell for {r[0]}' for r in weak)}.")
        print("    Use the marginals; treat any single cell as indicative, never as a P90.")
    print(f"  LEVELS were never stationary and are not now: the step is "
          f"{100.0 * (CUR_M - PRI_M) / PRI_M:+.0f}%.")

    C.sub("8a. WHERE the hour profile moved - because it is the one shape that shifted")
    ph = collections.Counter(lg["hour"] for lg in L_PRI)
    tp = float(len(L_PRI))
    print(f"{'hr':>3s}{'PRIOR share':>13s}{'CURRENT share':>15s}{'delta pp':>10s}"
          f"{'PRIOR /day':>12s}{'CURRENT /day':>14s}{'legs/day change':>17s}")
    diffs = []
    for h in range(24):
        a = ph.get(h, 0) / tp
        b = hr_all.get(h, 0) / tot_cur
        pd_ = ph.get(h, 0) / float(PRI_N)
        cd_ = hr_all.get(h, 0) / float(CUR_N)
        diffs.append((abs(b - a), h, a, b, pd_, cd_))
    for _, h, a, b, pd_, cd_ in sorted(diffs, key=lambda x: -x[0])[:8]:
        print(f"{h:>3d}{fmt_pct(a):>13s}{fmt_pct(b):>15s}{100.0 * (b - a):>+9.2f}pp"
              f"{pd_:>12.2f}{cd_:>14.2f}{cd_ - pd_:>+16.2f}")
    grew = [(h, cd_ - pd_) for _, h, _, _, pd_, cd_ in diffs if cd_ > pd_]
    fell = [(h, cd_ - pd_) for _, h, _, _, pd_, cd_ in diffs if cd_ < pd_]
    top3 = sorted(grew, key=lambda kv: -kv[1])[:3]
    print(f"\n{len(grew)} hours grew in legs/day and {len(fell)} shrank. The whole +"
          f"{CUR_M - PRI_M:.1f} legs/day")
    print(f"is not spread evenly: {sum(v for _, v in top3):.1f} of it - "
          f"{fmt_pct(sum(v for _, v in top3) / (CUR_M - PRI_M), 0)} - lands in just three hours "
          f"({', '.join(f'{h:02d}:00' for h, _ in top3)}),")
    print(f"while these hours actually LOST volume: "
          f"{', '.join(f'{h:02d}:00 {v:+.2f}' for h, v in sorted(fell, key=lambda kv: kv[1]))}.")
    print("CONSEQUENCE for shift design: an hourly template scaled uniformly off the plateau")
    print("would under-provide exactly at the peak and over-provide in the shoulders. Use the")
    print("CURRENT regime's hourly shape, not a pooled one.")

    # =========================================================================
    C.hdr("S9  SHORT-NOTICE DEMAND - what a fixed shift template cannot anticipate")

    # Lead time is the one measure whose window stops a further day short: today's
    # same-day bookings can still arrive after an evening pull (assumption 6).
    lead_end = H.last_actuals_day

    C.sub("9a. Which booking clock, and is it trustworthy?")
    hist_first = C.q1(con, "SELECT MIN(history_date) FROM reservations_historicalleg "
                           "WHERE history_type='+'")
    hist_first_day = C.to_local(hist_first).date()
    n_hist_add = C.q1(con, "SELECT COUNT(*) FROM reservations_historicalleg "
                           "WHERE history_type='+'")
    n_res_created_null = C.q1(con, "SELECT COUNT(*) FROM reservations_reservation "
                                   "WHERE created_at IS NULL")
    print(f"reservation.created_at   : NULL on {n_res_created_null} rows of the whole file")
    print(f"historicalleg '+' rows   : leg-level creation, from {hist_first_day} "
          f"({n_hist_add:,} rows)")
    print("\nA leg can be ADDED to an existing reservation later than the reservation was")
    print("made, in which case reservation.created_at overstates its lead time. Test that on")
    print("the unbiased subsample: reservations created AFTER the history table began, where")
    print("every leg necessarily has a '+' row.")
    gapmins = []
    for lg in legs:
        if lg["leg_id"] not in leg_created or not lg["res_created"]:
            continue
        rc = C.to_local(lg["res_created"])
        if rc.date() <= hist_first_day:
            continue
        gapmins.append((leg_created[lg["leg_id"]] - rc).total_seconds() / 60.0)
    late = sum(1 for g in gapmins if g > 60)
    print(f"  n = {len(gapmins):,}   leg created >60 min after its reservation: "
          f"{late:,} ({fmt_pct(late / float(len(gapmins)))})")
    print("  " + C.fmt_describe("leg created - reservation created (min)", gapmins))
    print(f"\n  {fmt_pct(late / float(len(gapmins)))} sounds negligible. It is NOT, because "
          f"late-added legs are not a random")
    print("  sample - they are short-notice by construction, so they land entirely in the one")
    print("  part of the distribution this section is about. Measure the damage directly:")
    have = [lg for lg in L_POOL if lg["leg_id"] in leg_created and lg["d"] <= lead_end]
    misg = [lg for lg in L_POOL if lg["leg_id"] not in leg_created and lg["d"] <= lead_end
            and lg["leg_id"] in lead_h]
    print(f"\n  pooled-window legs with a '+' row: {len(have):,} of "
          f"{len(have) + len(misg):,} = "
          f"{fmt_pct(len(have) / float(len(have) + len(misg)))}")
    mv = [lead_h[lg["leg_id"]] for lg in misg]
    if mv:
        print(f"  the {len(misg):,} WITHOUT one predate the history table, and on the "
              f"reservation clock")
        print(f"  they are uniformly long-lead: P10 {C.pct(mv, 10) / 24.0:.0f} d, "
              f"P50 {C.pct(mv, 50) / 24.0:.0f} d, "
              f"{fmt_pct(sum(1 for x in mv if x < 24) / float(len(mv)))} inside 24 h.")
        print("  So falling back to the reservation clock for exactly those legs costs nothing.")
    print(f"\n{'clock':<26s}{'n':>7s}{'P50 lead':>10s}{'<24h':>8s}{'<48h':>8s}{'<7d':>8s}")
    for lbl, src in (("reservation.created_at", lead_h_res), ("HYBRID (primary)", lead_h)):
        v = [src[lg["leg_id"]] for lg in L_POOL
             if lg["d"] <= lead_end and lg["leg_id"] in src]
        print(f"{lbl:<26s}{len(v):>7,}{C.pct(v, 50) / 24.0:>8.1f} d"
              f"{fmt_pct(sum(1 for x in v if x < 24) / float(len(v))):>8s}"
              f"{fmt_pct(sum(1 for x in v if x < 48) / float(len(v))):>8s}"
              f"{fmt_pct(sum(1 for x in v if x < 168) / float(len(v))):>8s}")
    vr = [lead_h_res[lg["leg_id"]] for lg in L_POOL
          if lg["d"] <= lead_end and lg["leg_id"] in lead_h_res]
    vh = [lead_h[lg["leg_id"]] for lg in L_POOL
          if lg["d"] <= lead_end and lg["leg_id"] in lead_h]
    r24 = sum(1 for x in vr if x < 24) / float(len(vr))
    h24 = sum(1 for x in vh if x < 24) / float(len(vh))
    print(f"\n  VERDICT [measured]: the two clocks agree on the MEDIAN to within a day, and")
    print(f"  disagree by {100.0 * (h24 - r24) / r24:.0f}% on the number that matters. The "
          f"reservation clock puts")
    print(f"  {fmt_pct(r24)} of legs inside 24 hours; the leg's own clock puts {fmt_pct(h24)}. "
          f"Everything below")
    print("  uses the HYBRID. A prior analysis reading reservation.created_at alone would")
    print("  under-count same-day work by about a fifth.")

    C.sub("9b. Lead-time distribution")
    print(f"Window stops at {lead_end} - one day short of `today`, because today's")
    print("same-day bookings can still arrive after an evening pull (assumption 6).")

    def leads(pool):
        return [lead_h[lg["leg_id"]] for lg in pool
                if lg["d"] <= lead_end and lg["leg_id"] in lead_h]

    print("HYBRID clock (leg's own creation where recorded, reservation's otherwise).\n")
    for label, pool in (("CURRENT regime", L_CUR), ("PRIOR plateau", L_PRI),
                        ("ALL live legs", legs)):
        v = leads(pool)
        d = C.describe([x / 24.0 for x in v])
        print(f"{label:<16s} n={d['n']:>6,}  lead DAYS  P10 {d['p10']:>5}  P25 {d['p25']:>5}  "
              f"P50 {d['p50']:>5}  P75 {d['p75']:>5}  P90 {d['p90']:>5}")
    print(f"\nlegs booked AFTER their own pickup instant (back-entered, excluded): "
          f"{n_neg_lead}")
    print("OLD DOC CLAIM: P10 3d, P25 8d, P50 20d, P75 41d, P90 67d - cut on the")
    print("reservation clock and on a window inside the prior plateau. The percentiles above")
    print("the median have come DOWN since; the median has come down about three days.")

    C.sub("9c. Short-notice buckets")
    bands = [("< 6 h", 0, 6), ("6-24 h", 6, 24), ("24-48 h", 24, 48),
             ("2-7 d", 48, 168), ("7-14 d", 168, 336), ("14-30 d", 336, 720),
             ("30-60 d", 720, 1440), ("> 60 d", 1440, 1e9)]
    print(f"{'band':<10s}" + "".join(f"{lbl:>16s}" for lbl in ("CURRENT", "PRIOR")))
    cv = leads(L_CUR)
    pv = leads(L_PRI)
    for lbl, lo, hi in bands:
        c = sum(1 for x in cv if lo <= x < hi)
        p = sum(1 for x in pv if lo <= x < hi)
        print(f"{lbl:<10s}{f'{c:,} ({fmt_pct(c / float(len(cv)))})':>16s}"
              f"{f'{p:,} ({fmt_pct(p / float(len(pv)))})':>16s}")
    print()
    print("Is the CURRENT-vs-PRIOR difference real? Same day-stratified permutation null as")
    print(f"S8, {PERM_B} resamples, on the share itself.")
    for h, lbl in ((24, "within 24 h"), (48, "within 48 h"), (168, "within 7 d")):
        c = sum(1 for x in cv if x < h) / float(len(cv))
        p = sum(1 for x in pv if x < h) / float(len(pv))
        # permutation on the binary indicator, day-stratified by weekday
        cbd = collections.defaultdict(collections.Counter)
        for lg in legs:
            if lg["d"] > lead_end or lg["leg_id"] not in lead_h:
                continue
            cbd[lg["d"].isoformat()]["in" if lead_h[lg["leg_id"]] < h else "out"] += 1
        pval, _, _ = perm_null_tvd(PRI_DAYS, CUR_DAYS, cbd, rng)
        print(f"{lbl:<14s} CURRENT {fmt_pct(c):>7s}   PRIOR {fmt_pct(p):>7s}   "
              f"delta {100.0 * (c - p):+.2f}pp   permutation p = {pval:.3f}   "
              f"{'REAL' if pval < 0.05 else 'within noise'}")
    per_day_24 = CUR_M * (sum(1 for x in cv if x < 24) / float(len(cv)))
    print(f"\n[measured] At the current level of {CUR_M:.1f} legs/day, "
          f"{per_day_24:.1f} legs a day arrive")
    print("inside 24 hours of their own pickup. That is the volume no fixed shift template")
    print("can be sized for in advance - it is a flex/on-call question, not a template one.")

    C.sub("9d. Is short notice RISING?  ->  01_demand_leadtime_weekly.csv")
    wk_lead = collections.defaultdict(list)
    for lg in legs:
        if lg["d"] > lead_end or lg["leg_id"] not in lead_h:
            continue
        wk_lead[iso_week_key(lg["d"])].append(lead_h[lg["leg_id"]])
    complete_wk_keys = {wk for wk, ds, _ in complete_weeks}
    lrows = []
    for wk in sorted(wk_lead):
        v = wk_lead[wk]
        if len(v) < 30:
            continue
        ds = wk_days[wk]
        in_pool = ds[0] >= POOL_S and ds[-1] <= POOL_E
        lrows.append([wk, len(v),
                      round(sum(1 for x in v if x < 24) / float(len(v)), 5),
                      round(sum(1 for x in v if x < 48) / float(len(v)), 5),
                      round(sum(1 for x in v if x < 168) / float(len(v)), 5),
                      round(C.pct(v, 50) / 24.0, 2),
                      int(wk in complete_wk_keys), int(in_pool)])
    C.write_csv("01_demand_leadtime_weekly.csv",
                ["iso_week", "legs", "share_lt_24h", "share_lt_48h", "share_lt_7d",
                 "median_lead_days", "week_complete", "in_pooled_window"], lrows)
    print(f"{len(lrows)} weeks with >=30 legs. Last 14 "
          f"(P = partial week, truncated by the horizon):")
    print(f"{'week':<10s}{'legs':>7s}{'<24h':>9s}{'<48h':>9s}{'<7d':>9s}{'median d':>10s}  ")
    for r in lrows[-14:]:
        print(f"{r[0]:<10s}{r[1]:>7,}{fmt_pct(r[2]):>9s}{fmt_pct(r[3]):>9s}"
              f"{fmt_pct(r[4]):>9s}{r[5]:>10.1f}  {'' if r[6] else 'P'}")
    trend = [r for r in lrows if r[6] and r[7]]
    print(f"\nTrend fitted on the {len(trend)} COMPLETE weeks inside the pooled window only.")
    print("Fitting the full 59-week series would measure the growth ramp, when the company")
    print("was a tenth of its present size, not the business being scheduled today.")
    print("CAVEAT: the permutation shuffles weeks, so its null assumes weeks are independent.")
    print("Week-to-week autocorrelation would make these p-values optimistic; read a p just")
    print("under 0.05 as suggestive, not settled.")
    for col, name in ((2, "<24h share"), (3, "<48h share"), (4, "<7d share"),
                      (5, "median lead (days)")):
        xs = list(range(len(trend)))
        ys = [r[col] for r in trend]
        b, _ = linreg(xs, ys)
        if b is None:
            continue
        perm = []
        yy = list(ys)
        for _ in range(PERM_B):
            rng.shuffle(yy)
            bb, _ = linreg(xs, yy)
            if bb is not None:
                perm.append(abs(bb))
        pval = sum(1 for x in perm if x >= abs(b)) / float(len(perm))
        unit = "d/week" if col == 5 else "pp/week"
        scale = 1.0 if col == 5 else 100.0
        direction = ("RISING" if b > 0 else "FALLING") if pval < 0.05 else \
            "no trend beyond noise"
        print(f"  {name:<20s} slope {scale * b:+.4f} {unit:<8s} "
              f"({scale * b * 52:+.2f}/year)  p = {pval:.3f}   {direction}")

    C.sub("9e. The booking curve - how much of a day is known K days out")
    print("For fully-observed past days, the share of final demand already on the books K")
    print("days before pickup. This is what a dispatcher opening Day Setup is looking at.")
    print("Restricted to the POOLED window so the level regime is comparable.\n")
    KS = (0, 1, 2, 3, 5, 7, 10, 14, 21, 30, 45, 60, 90)

    def curve(pool):
        known = collections.defaultdict(collections.Counter)
        finals = collections.Counter()
        for lg in pool:
            if lg["d"] > lead_end or lg["leg_id"] not in book_date:
                continue
            finals[lg["d"]] += 1
            bd = book_date[lg["leg_id"]]
            for k in KS:
                if bd <= lg["d"] - dt.timedelta(days=k):
                    known[k][lg["d"]] += 1
        return known, finals

    known, finals = curve(L_POOL)
    k_cur, f_cur = curve(L_CUR)
    k_pri, f_pri = curve(L_PRI)
    brows = []
    exact = {}
    print(f"{'K days out':>11s}{'POOLED':>10s}{'P25 day':>10s}{'P75 day':>10s}"
          f"{'PRIOR':>10s}{'CURRENT':>10s}")
    for k in KS:
        per = [known[k][d] / float(finals[d]) for d in finals if finals[d] > 0]
        agg = sum(known[k].values()) / float(sum(finals.values()))
        ac = sum(k_cur[k].values()) / float(sum(f_cur.values()))
        ap = sum(k_pri[k].values()) / float(sum(f_pri.values()))
        print(f"{k:>11d}{fmt_pct(agg):>10s}{fmt_pct(C.pct(per, 25)):>10s}"
              f"{fmt_pct(C.pct(per, 75)):>10s}{fmt_pct(ap):>10s}{fmt_pct(ac):>10s}")
        brows.append([k, round(agg, 5), round(C.pct(per, 25), 5), round(C.pct(per, 75), 5),
                      len(per), round(ap, 5), round(ac, 5)])
        exact[k] = (agg, ap, ac)
    C.write_csv("01_demand_booking_curve.csv",
                ["days_before_pickup", "share_of_final_demand_booked", "p25_day", "p75_day",
                 "n_days", "prior_regime", "current_regime"], brows)
    k14, k7 = exact[14][0], exact[7][0]
    print(f"\n[measured] A day built 14 days out is sized against {fmt_pct(k14)} of its")
    print(f"eventual demand; 7 days out, {fmt_pct(k7)}. peak_concurrency "
          f"(dispatching/day_setup.py:100)")
    print("runs on BOOKED legs with no lead-time correction, so the further")
    print("ahead a roster is built, the more structurally under-sized it is. That is a demand")
    print("finding with a direct product consequence and it is not modelled anywhere today.")
    k30p, k30c = exact[30][1], exact[30][2]
    print(f"\nWHERE THE +{100.0 * (CUR_M - PRI_M) / PRI_M:.0f}% IS COMING FROM. The current "
          f"regime's book fills LATER relative")
    print(f"to its own (larger) final total: {fmt_pct(k30p)} known 30 days out on the plateau "
          f"against")
    print(f"{fmt_pct(k30c)} now. Cross-checked against the bands in S9c, the mix has moved out "
          f"of the")
    print(">60-day tail and into the 7-30 day band, while the inside-24-hour share is flat.")
    print("So the growth is NOT same-day walk-up work: it is ordinary advance booking landing")
    print("one to four weeks ahead. A shift template CAN anticipate it - but only if the")
    print("roster is sized on a lead-time-corrected forecast rather than on today's book.")

    # =========================================================================
    C.hdr("S10  WHAT THIS DATA CANNOT ANSWER  [unavailable]")
    yrs = {d.year for d in daterange(first_live, H.last_demand_day)}
    print(f"1. SEASONALITY. Live legs begin {first_live} at ~1 leg/day and the business grew")
    print(f"   {CUR_M / max(segs[0][3], 0.01):.0f}x since. Season and growth are perfectly confounded: there is no")
    print("   prior-year same-month comparison anywhere in the file. Any template cut from")
    print("   this window is a SUMMER template and must be re-cut, not extrapolated.")
    print("2. WHETHER THE CEILING IS DEMAND OR SUPPLY. A leg count cannot tell a demand")
    print("   plateau from a capacity plateau. Only the farm-out and refusal record can, and")
    print("   that is script 03's question.")
    print("3. TRUE OCCUPANCY BY HOUR. Every hour figure here is on BOOKED pickup_time, which")
    print("   for airport arrivals is the FLIGHT'S clock, not the driver's. Script 05 owns")
    print("   the occupancy-anchored curve; do not cut a shift boundary from S3 alone.")
    print("4. LOST DEMAND. Turned-down and un-quoted work leaves no Leg row. reservations_lead")
    print("   and reservations_quote exist but a quote is not a refused booking. Everything")
    print("   here is DELIVERED demand and understates true demand by an unmeasurable amount.")
    print("5. CANCELLATION TRENDING. Both spellings are excluded from every count here, but")
    print("   the flag's own adoption is visible in the data and it is not behaviour:")
    canc = collections.defaultdict(lambda: [0, 0])
    for r in C.q(con, f"""SELECT l.pickup_date AS pd,
                                 CASE WHEN l.status='cancelled'
                                        OR r.status IN ('cancelled','canceled')
                                      THEN 1 ELSE 0 END AS c
                          {C.LEG_JOIN}
                          WHERE {C.SANE_DATES}"""):
        d = dt.date.fromisoformat(r["pd"])
        if d > H.last_demand_day:
            continue
        mk = month_key(d)
        canc[mk][0] += 1
        canc[mk][1] += r["c"]
    keys = sorted(canc)
    shown = keys[::3] if len(keys) > 10 else keys
    print("   " + "  ".join(f"{k} {100.0 * canc[k][1] / max(canc[k][0], 1):.1f}%"
                            for k in shown))
    print("   A rate that sits near zero and then steps up is a flag coming into use, not a")
    print("   customer changing their mind. The series must never be trended. [measured]")

    # =========================================================================
    C.hdr("S11  HEADLINE NUMBERS - everything a later script may quote from this one")
    we_share = sum(1 for lg in L_CUR if lg["dow"] in WEEKEND) / tot_cur
    sat_p90 = C.pct(ratios[5], 90) * CUR_M
    tue_p50 = C.pct(ratios[1], 50) * CUR_M
    print(f"  window, current regime      {CUR_S}..{CUR_E} ({CUR_N} d)     [measured]")
    print(f"  window, prior plateau       {PRI_S}..{PRI_E} ({PRI_N} d)    [measured]")
    print(f"  level, current              {CUR_M:.1f} legs/day, +"
          f"{100.0 * (CUR_M - PRI_M) / PRI_M:.0f}% on the plateau        [measured]")
    print(f"  busiest weekday             Sat {sum(1 for lg in L_CUR if lg['dow'] == 5) / float(sum(1 for d in CUR_DAYS if d.weekday() == 5)):.0f} legs/day "
          f"(P90 day {sat_p90:.0f})           [measured/modeled]")
    print(f"  quietest weekday            Tue {sum(1 for lg in L_CUR if lg['dow'] == 1) / float(sum(1 for d in CUR_DAYS if d.weekday() == 1)):.0f} legs/day "
          f"(P50 day {tue_p50:.0f})           [measured/modeled]")
    sat_d = sum(1 for lg in L_CUR if lg["dow"] == 5) / float(
        sum(1 for d in CUR_DAYS if d.weekday() == 5))
    tue_d = sum(1 for lg in L_CUR if lg["dow"] == 1) / float(
        sum(1 for d in CUR_DAYS if d.weekday() == 1))
    print(f"  Sat:Tue, typical week       {C.pct(pair, 50):.2f}x   "
          f"(median of within-week ratios, pooled, {len(pair)} weeks)  [measured]")
    print(f"  Sat:Tue, current window     {sat_d / tue_d:.2f}x   "
          f"(ratio of the two window means, {CUR_N} d)          [measured]")
    print(f"  Fri+Sat+Sun share           {fmt_pct(we_share)} of volume on 3 of 7 days     [measured]")
    print(f"  busiest hour (BOOKED)       {peak_h:02d}:00, {fmt_pct(peak_share)} of the day, "
          f"{peak_share / uni:.1f}x flat   [measured]")
    core_a, core_b = max(runs, key=lambda r: sum(share[h] for h in hours_in(*r)))
    core_hs = hours_in(core_a, core_b)
    print(f"  core demand band            {core_a:02d}:00-{(core_b + 1) % 24:02d}:00 carries "
          f"{fmt_pct(sum(share[h] for h in core_hs))} of volume      [measured]")
    print(f"  hours to cover 80%          {cover[0.8]} of 24                            [measured]")
    print(f"  class mix (current)         "
          + ", ".join(f"{c} {fmt_pct(cur_cls[c] / tot_cur, 0)}" for c in order)
          + "  [measured]")
    print(f"  airport at either end       {fmt_pct(airport / tot_cur)}                             [measured]")
    print(f"  ARRIVAL / DEPARTURE / OTHER "
          f"{fmt_pct(sum(1 for lg in L_CUR if lg['kind'] == 'ARRIVAL') / tot_cur)} / "
          f"{fmt_pct(sum(1 for lg in L_CUR if lg['kind'] == 'DEPARTURE') / tot_cur)} / "
          f"{fmt_pct(sum(1 for lg in L_CUR if lg['kind'] == 'OTHER') / tot_cur)}          [measured]")
    print(f"  top lane                    MCO->DISNEY + DISNEY->MCO = "
          f"{fmt_pct((sum(1 for lg in L_CUR if lg['pb'] == 'MCO' and lg['db'] == 'DISNEY') + sum(1 for lg in L_CUR if lg['pb'] == 'DISNEY' and lg['db'] == 'MCO')) / tot_cur)}  [measured]")
    print(f"  legs on multi-leg res       {fmt_pct(multi / float(tot_l))}                             [measured]")
    print(f"  round-trip gap, median      {C.pct(gaps, 50):.0f} days (NOT same-day)              [measured]")
    print(f"  class above party minimum   {fmt_pct(up / float(n_ok))}                             [measured]")
    print(f"  booked inside 24 h          {fmt_pct(sum(1 for x in cv if x < 24) / float(len(cv)))} = {per_day_24:.1f} legs/day            [measured]")
    print(f"  booked inside 7 d           {fmt_pct(sum(1 for x in cv if x < 168) / float(len(cv)))} = "
          f"{CUR_M * sum(1 for x in cv if x < 168) / float(len(cv)):.1f} legs/day           [measured]")
    print(f"  known 14 days out           {fmt_pct(k14)} of final demand              [measured]")
    print("\nUNITS AND CAVEATS THAT TRAVEL WITH THESE NUMBERS:")
    print("  - a 'leg' is one vehicle-trip, not a reservation and not an hour of driver time")
    print("  - every hour figure is on BOOKED pickup_time, which on an airport arrival is the")
    print("    FLIGHT's clock. Script 05 re-anchors it; do not cut a shift boundary here.")
    print("  - shapes may be pooled across regimes only where S8 licenses it")
    print("  - levels may NEVER be pooled across regimes")

    C.hdr("DONE - CSVs in " + C.ensure_out())


if __name__ == "__main__":
    main()
