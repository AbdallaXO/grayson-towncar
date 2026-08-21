#!/usr/bin/env python3
"""
01_window.py -- Grayson Towncar scheduling redesign, Phase 1.

QUESTION: What is the defensible analysis window, and what is the shape of
demand growth inside it?

Read-only. No Django, no manage.py, no writes to the snapshot.
Run from the repo root:   python docs/scheduling-redesign/analysis/01_window.py

Outputs CSVs to docs/scheduling-redesign/analysis/out/ prefixed 01_window_.
"""

import csv
import os
import sqlite3
import statistics
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

DB = "file:content/db.sqlite3?mode=ro"
OUT = os.path.join("docs", "scheduling-redesign", "analysis", "out")

TODAY = date(2026, 8, 21)          # "today" per the engagement brief
REPORT_START = date(2025, 9, 1)    # start of the reporting range asked for
DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def d(s):
    return date(int(s[0:4]), int(s[5:7]), int(s[8:10]))


def dt(s):
    return datetime.fromisoformat(s.replace(" ", "T"))


def pct(vals, p):
    if not vals:
        return None
    v = sorted(vals)
    k = int(round((p / 100.0) * (len(v) - 1)))
    return v[k]


def hdr(t):
    print()
    print("=" * 100)
    print(t)
    print("=" * 100)


def assumptions():
    print("#" * 100)
    print("# 01_window.py  --  analysis window + demand growth")
    print("# snapshot: content/db.sqlite3, opened read-only via "
          "sqlite3.connect('file:content/db.sqlite3?mode=ro', uri=True)")
    print("#")
    print("# ASSUMPTIONS (every one of them, stated up front):")
    print("#  A1. 'Today' is 2026-08-21 (given by the engagement brief). Nothing in the")
    print("#      snapshot is assumed to be current as of that date -- see Section 1.")
    print("#  A2. leg.pickup_date / leg.pickup_time are NAIVE FLORIDA LOCAL wall-clock.")
    print("#      Demand is counted in local time with NO timezone conversion, because")
    print("#      demand-by-hour is a local-clock question. (legstatus.timestamp is UTC and")
    print("#      is used ONLY for freeze-point forensics here, never differenced against")
    print("#      pickup_date.)")
    print("#  A3. One Leg = one unit of demand (one vehicle-trip). Reservations with a")
    print("#      round trip contribute 2 legs. No de-duplication of same-time legs.")
    print("#  A4. CANCELLED/VOID exclusion set (variant b) =")
    print("#        leg.status IN ('cancelled')")
    print("#        OR reservation.status IN ('cancelled','canceled')   <- both spellings exist")
    print("#      Rationale: a cancelled leg was never driven, so it is not delivered demand.")
    print("#      Note this is deliberately BROADER than leg.status alone: 79 legs sit at")
    print("#      leg.status='in-progress' under a cancelled reservation (the leg row was")
    print("#      never touched again after the reservation was killed).")
    print("#      leg.status IN ('completed','confirmed','in-progress','on-the-way',")
    print("#      'on-location','picked-up', NULL) are all treated as REAL demand.")
    print("#      'in-progress' is the Django model default for a newly created Leg")
    print("#      (reservations/models.py:1067 per docs/operational-data-audit.md 8.3) --")
    print("#      it means 'not started', NOT 'anomalous', and must be kept as demand.")
    print("#  A5. exclude_from_analytics=1 (variant c) is a per-leg timing-analysis flag set")
    print("#      by dispatchers via the analytics UI. It marks legs whose *timings* are")
    print("#      untrustworthy, not legs that did not happen -- so it is shown separately")
    print("#      and is NOT recommended for demand counting.")
    print("#  A6. CORRUPT pickup_date := pickup_date outside [2025-01-01, 2027-12-31].")
    print("#      2027 dates are kept as plausible (real long-lead cruise/holiday bookings).")
    print("#  A7. BOOKING LEAD TIME := leg.pickup_date - date(reservation.created_at).")
    print("#      reservations_leg has NO created_at column, so a leg ADDED to an existing")
    print("#      reservation after the fact is credited to the reservation's original")
    print("#      creation instant. This biases measured lead time UPWARD (longer) for such")
    print("#      legs. reservations_historicalleg exists but holds only 223 rows, all of")
    print("#      them local post-snapshot writes, so it cannot correct this.")
    print("#  A8. The completeness model in Section 4 assumes the booking lead-time")
    print("#      distribution is stationary between the reference cohort (pickup_date in")
    print("#      2026-02-01..2026-06-30) and the truncated tail. Seasonal lead-time drift")
    print("#      would bias it; the direction is tested by splitting the reference cohort.")
    print("#  A9. ISO weeks are datetime.date.isocalendar() (Mon-start).")
    print("# A10. No Django models are imported and no application code is executed; every")
    print("#      figure is raw SQL + stdlib arithmetic.")
    print("#" * 100)


# --------------------------------------------------------------------------------------
def main():
    os.makedirs(OUT, exist_ok=True)
    con = sqlite3.connect(DB, uri=True)
    cur = con.cursor()

    # ---------------------------------------------------------------- load
    res_status = {}
    res_created = {}
    for rid, st, ca in cur.execute(
            "SELECT id, status, created_at FROM reservations_reservation"):
        res_status[rid] = st
        res_created[rid] = dt(ca) if ca else None

    legs = []
    for lid, pd_, pt, st, rid, xa in cur.execute(
            "SELECT id, pickup_date, pickup_time, status, reservation_id, "
            "exclude_from_analytics FROM reservations_leg"):
        legs.append((lid, pd_, pt, st, rid, xa))

    def is_cancelled(st, rid):
        return st == "cancelled" or res_status.get(rid) in ("cancelled", "canceled")

    # ================================================================ SECTION 1
    hdr("SECTION 1 -- SNAPSHOT FREEZE POINT  (this governs everything else)")
    print("Max timestamp per table, and how many rows sit after 2026-07-12:")
    probes = [
        ("reservations_lead", "created_at"),
        ("reservations_quote", "created_at"),
        ("reservations_reservation", "created_at"),
        ("reservations_reservation", "last_modified_at"),
        ("reservations_legstatus", "timestamp"),
        ("reservations_leg", "driver_assigned_at"),
        ("drivers_legpayment", "updated_at"),
        ("reservations_auditlog", "timestamp"),
        ("ops_operationaltask", "created_at"),
        ("reservations_historicalleg", "history_date"),
        ("django_admin_log", "action_time"),
    ]
    print(f"  {'table.column':<48} {'rows':>8} {'max value':<30} {'rows>2026-07-12':>15}")
    for t, c in probes:
        try:
            n, mx = cur.execute(f"SELECT COUNT({c}), MAX({c}) FROM {t}").fetchone()
            after = cur.execute(
                f"SELECT COUNT(*) FROM {t} WHERE {c} > '2026-07-12'").fetchone()[0]
            print(f"  {t + '.' + c:<48} {n:>8} {str(mx):<30} {after:>15}")
        except Exception as e:  # pragma: no cover
            print(f"  {t + '.' + c:<48} ERROR {e}")

    print()
    print("  Every purely-customer-driven table (leads, quotes, reservations) stops dead at")
    print("  2026-07-11 ~20:34 UTC. Nothing exists between 2026-07-11 20:37 UTC and")
    print("  2026-08-17 20:32 UTC. Rows after that gap, in full:")
    for r in cur.execute(
            "SELECT id, leg_id, status, timestamp, updated_by_id, notes "
            "FROM reservations_legstatus WHERE timestamp > '2026-07-12' ORDER BY timestamp"):
        print("    legstatus", r)

    # microsecond-precision fingerprint
    tot = whole_ms = 0
    for (ts,) in cur.execute("SELECT timestamp FROM reservations_legstatus"):
        tot += 1
        if "." in ts and ts.split(".")[1].ljust(6, "0")[3:] == "000":
            whole_ms += 1
    print()
    print(f"  Fingerprint: {whole_ms}/{tot} legstatus timestamps "
          f"({100.0*whole_ms/tot:.2f}%) have microseconds ending in '000' -- the")
    print("  millisecond truncation of the Postgres->SQLite export. The 7 post-gap rows do")
    print("  NOT: they carry full 6-digit microseconds, i.e. they were written by a locally")
    print("  running Django against this file, not exported from production.")
    print("  reservations_historicalleg (223 rows) and reservations_auditlog (275 rows) are")
    print("  ENTIRELY post-2026-07-18 -- those tables did not exist in production at export.")
    print()
    print("  >>> PRODUCTION FREEZE POINT = 2026-07-11 20:37 UTC = 2026-07-11 16:37 America/New_York.")
    print("  >>> The brief's 'refreshed 2026-08-21' is the FILE mtime, not the data horizon.")

    FREEZE = date(2026, 7, 11)

    # ================================================================ SECTION 2
    hdr("SECTION 2 -- CORRUPT / IMPLAUSIBLE pickup_date")
    yrs = Counter(l[1][:4] for l in legs)
    print("  legs by pickup year:", dict(sorted(yrs.items())))
    corrupt = [l for l in legs if l[1] is None or d(l[1]) < date(2025, 1, 1)
               or d(l[1]) > date(2027, 12, 31)]
    print(f"  corrupt rows (outside 2025-01-01..2027-12-31): {len(corrupt)}")
    for lid, pd_, pt, st, rid, xa in corrupt:
        print(f"    leg id={lid} pickup={pd_} {pt} status={st} reservation_id={rid}")
        rr = cur.execute(
            "SELECT status, created_at, trip_type, total_price FROM reservations_reservation "
            "WHERE id=?", (rid,)).fetchone()
        ll = cur.execute(
            "SELECT id, pickup_date, pickup_time, pickup_location, dropoff_location, status "
            "FROM reservations_leg WHERE reservation_id=?", (rid,)).fetchall()
        print(f"      reservation {rid}: status={rr[0]} created={rr[1]} "
              f"trip_type={rr[2]} total_price={rr[3]}")
        for x in ll:
            print(f"      sibling leg {x}")
    print()
    print("  2027 pickup_dates:", sum(1 for l in legs if l[1][:4] == "2027"),
          "-- these are NOT corrupt: they are Jan-Mar 2027 cruise/holiday advance bookings")
    print("  spread over 60+ distinct reservations with real locations. Keep them.")
    print("  POLLUTION ASSESSMENT: both corrupt rows belong to reservation 5370, a $195")
    print("  round trip whose pickup/dropoff strings are '-' and '1'. They are inert in any")
    print("  GROUP BY month (they land in their own 2029/3220 buckets) but they DESTROY any")
    print("  MIN/MAX/range/axis-scaling query -- which is exactly how the '3220-03-06' max")
    print("  got into the brief. Filter with an explicit date range, never with MAX().")

    # ================================================================ SECTION 3
    hdr("SECTION 3 -- LEGS BY MONTH AND BY ISO WEEK, 2025-09-01 .. 2026-08-21")
    print("  (a) all rows   (b) excl. cancelled/void   (c) excl. cancelled/void AND "
          "exclude_from_analytics")
    xa_rows = [l for l in legs if l[5]]
    xa_months = Counter(l[1][:7] for l in xa_rows)
    print(f"  exclude_from_analytics=1 total: {len(xa_rows)} rows, all leg.status="
          f"{sorted(set(l[3] for l in xa_rows))}, months {dict(sorted(xa_months.items()))}")
    print("  -> variant (c) differs from (b) by at most 32 legs in Feb/Mar 2026. It is a")
    print("     TIMING-quality flag, not a demand flag; do not use it to count demand.")
    print()

    cancel_leg_only = sum(1 for l in legs if l[3] == "cancelled")
    cancel_res_only = sum(1 for l in legs
                          if l[3] != "cancelled"
                          and res_status.get(l[4]) in ("cancelled", "canceled"))
    print(f"  Cancellation accounting over all {len(legs)} legs:")
    print(f"    leg.status='cancelled'                                : {cancel_leg_only}")
    print(f"    leg not cancelled but reservation cancelled/canceled  : {cancel_res_only}")
    print(f"    total excluded by variant (b)                         : "
          f"{cancel_leg_only + cancel_res_only}")

    mon_a, mon_b, mon_c = Counter(), Counter(), Counter()
    wk_a, wk_b, wk_c = Counter(), Counter(), Counter()
    for lid, pd_, pt, st, rid, xa in legs:
        if pd_ is None:
            continue
        try:
            dd = d(pd_)
        except ValueError:
            continue
        if not (REPORT_START <= dd <= TODAY):
            continue
        m = pd_[:7]
        iy, iw, _ = dd.isocalendar()
        w = f"{iy}-W{iw:02d}"
        mon_a[m] += 1
        wk_a[w] += 1
        if not is_cancelled(st, rid):
            mon_b[m] += 1
            wk_b[w] += 1
            if not xa:
                mon_c[m] += 1
                wk_c[w] += 1

    print()
    print(f"  {'month':<9} {'(a) all':>9} {'(b) live':>9} {'(c) b-xa':>9} "
          f"{'cancel%':>8}   note")
    for m in sorted(mon_a):
        c_pct = 100.0 * (mon_a[m] - mon_b[m]) / mon_a[m] if mon_a[m] else 0
        note = ""
        if m >= "2026-07":
            note = "<-- TRUNCATED by snapshot freeze"
        print(f"  {m:<9} {mon_a[m]:>9} {mon_b[m]:>9} {mon_c[m]:>9} {c_pct:>7.1f}%   {note}")
    print(f"  {'TOTAL':<9} {sum(mon_a.values()):>9} {sum(mon_b.values()):>9} "
          f"{sum(mon_c.values()):>9}")

    print()
    print(f"  {'iso_week':<10} {'week_start':<12} {'(a) all':>9} {'(b) live':>9} "
          f"{'(c) b-xa':>9}   note")
    for w in sorted(wk_a):
        iy, iw = int(w[:4]), int(w[6:])
        ws = date.fromisocalendar(iy, iw, 1)
        note = ""
        if ws + timedelta(days=6) > FREEZE:
            note = "TRUNCATED"
        print(f"  {w:<10} {ws.isoformat():<12} {wk_a[w]:>9} {wk_b[w]:>9} "
              f"{wk_c[w]:>9}   {note}")

    with open(os.path.join(OUT, "01_window_weekly.csv"), "w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["iso_week", "week_start", "legs_all", "legs_live",
                     "legs_live_excl_analytics_flag", "truncated_by_freeze"])
        for w in sorted(wk_a):
            iy, iw = int(w[:4]), int(w[6:])
            ws = date.fromisocalendar(iy, iw, 1)
            wr.writerow([w, ws.isoformat(), wk_a[w], wk_b[w], wk_c[w],
                         int(ws + timedelta(days=6) > FREEZE)])
    with open(os.path.join(OUT, "01_window_monthly.csv"), "w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["month", "legs_all", "legs_live", "legs_live_excl_analytics_flag"])
        for m in sorted(mon_a):
            wr.writerow([m, mon_a[m], mon_b[m], mon_c[m]])

    # ================================================================ SECTION 4
    hdr("SECTION 4 -- IS THE AUGUST COLLAPSE REAL? (the consequential question)")

    live = [(d(pd_), pt, rid) for lid, pd_, pt, st, rid, xa in legs
            if pd_ and d(pd_) >= date(2025, 1, 1) and d(pd_) <= date(2027, 12, 31)
            and not is_cancelled(st, rid)]
    per_day = Counter(x[0] for x in live)
    per_day_all = Counter(d(l[1]) for l in legs
                          if l[1] and date(2025, 1, 1) <= d(l[1]) <= date(2027, 12, 31))

    print("4.1 -- August 2026 day by day vs the same weekday in June / July")
    print(f"  {'date':<12} {'dow':<4} {'aug legs':>9} | {'jun same-dow avg':>17} "
          f"{'jul(1-11) same-dow avg':>23} {'aug / jun':>10}")
    jun_by_dow, jul_by_dow = defaultdict(list), defaultdict(list)
    dd = date(2026, 6, 1)
    while dd <= date(2026, 6, 30):
        jun_by_dow[dd.weekday()].append(per_day.get(dd, 0))
        dd += timedelta(days=1)
    dd = date(2026, 7, 1)
    while dd <= FREEZE:
        jul_by_dow[dd.weekday()].append(per_day.get(dd, 0))
        dd += timedelta(days=1)
    aug_total = 0
    dd = date(2026, 8, 1)
    while dd <= TODAY:
        n = per_day.get(dd, 0)
        aug_total += n
        jm = statistics.mean(jun_by_dow[dd.weekday()])
        lm = statistics.mean(jul_by_dow[dd.weekday()]) if jul_by_dow[dd.weekday()] else 0
        print(f"  {dd.isoformat():<12} {DOW[dd.weekday()]:<4} {n:>9} | {jm:>17.1f} "
              f"{lm:>23.1f} {(100.0*n/jm if jm else 0):>9.0f}%")
        dd += timedelta(days=1)
    jun_daily = sum(per_day.get(date(2026, 6, x), 0) for x in range(1, 31)) / 30.0
    print(f"  Aug 1-21 total (live legs): {aug_total}   -> {aug_total/21.0:.1f} legs/day")
    print(f"  Jun 2026 (live legs):       {int(jun_daily*30)}   -> {jun_daily:.1f} legs/day")
    print(f"  Aug is running at {100.0*(aug_total/21.0)/jun_daily:.0f}% of June's daily rate.")

    print()
    print("4.2 -- WHY: booking lead-time truncation")
    print("  Every leg in the snapshot belongs to a reservation created on or before the")
    print("  freeze (2026-07-11). A pickup_date K days after the freeze can therefore only")
    print("  contain legs that were booked at least K days in advance.")
    lead_ref = []
    lead_by_month = defaultdict(list)
    neg = 0
    for lid, pd_, pt, st, rid, xa in legs:
        if not pd_ or is_cancelled(st, rid):
            continue
        try:
            dd = d(pd_)
        except ValueError:
            continue
        ca = res_created.get(rid)
        if not ca:
            continue
        k = (dd - ca.date()).days
        if k < 0:
            neg += 1
            continue
        if dd > date(2027, 12, 31):
            continue
        lead_by_month[pd_[:7]].append(k)
        if date(2026, 2, 1) <= dd <= date(2026, 6, 30):
            lead_ref.append(k)
    print(f"  legs with pickup_date BEFORE reservation.created_at (excluded): {neg}")
    print(f"  reference cohort = live legs, pickup_date 2026-02-01..2026-06-30: "
          f"n={len(lead_ref)}")
    for p in (10, 25, 50, 75, 90, 95, 99):
        print(f"    p{p:<3} lead = {pct(lead_ref,p):>5} days")
    print(f"    mean = {statistics.mean(lead_ref):.1f} days, "
          f"max = {max(lead_ref)} days")

    print()
    print("  Lead-time stationarity check (median / p75 / p90 by pickup month, live legs):")
    print(f"  {'month':<9} {'n':>7} {'p50':>6} {'p75':>6} {'p90':>6}  "
          f"{'%booked >=41d ahead':>21}")
    for m in sorted(lead_by_month):
        if m < "2025-09" or m > "2026-08":
            continue
        v = lead_by_month[m]
        far = 100.0 * sum(1 for x in v if x >= 41) / len(v)
        print(f"  {m:<9} {len(v):>7} {pct(v,50):>6} {pct(v,75):>6} {pct(v,90):>6}  "
              f"{far:>20.1f}%")

    # survival function S(k) = P(lead >= k) from the reference cohort
    n_ref = len(lead_ref)
    sorted_ref = sorted(lead_ref)

    def S(k):
        if k <= 0:
            return 1.0
        lo, hi = 0, n_ref
        while lo < hi:
            mid = (lo + hi) // 2
            if sorted_ref[mid] < k:
                lo = mid + 1
            else:
                hi = mid
        return (n_ref - lo) / n_ref

    print()
    print("4.3 -- Completeness model")
    print("  completeness(pickup_date) := S(pickup_date - 2026-07-11), where S(k) is the")
    print("  share of the reference cohort booked at least k days ahead. [modeled]")
    print(f"  {'pickup_date':<12} {'days past':>9} {'S(k)=modeled':>13} {'observed':>9} "
          f"{'implied true':>13} {'jun same-dow':>13}")
    rows_cm = []
    dd = date(2026, 7, 12)
    while dd <= TODAY:
        k = (dd - FREEZE).days
        s = S(k)
        obs = per_day.get(dd, 0)
        imp = obs / s if s > 0.005 else float("nan")
        jm = statistics.mean(jun_by_dow[dd.weekday()])
        rows_cm.append((dd, k, s, obs, imp, jm))
        if dd.day in (12, 15, 20, 25) or dd >= date(2026, 8, 1) or dd.day == 1:
            print(f"  {dd.isoformat():<12} {k:>9} {s:>12.1%} {obs:>9} "
                  f"{imp:>13.0f} {jm:>13.1f}")
        dd += timedelta(days=1)
    with open(os.path.join(OUT, "01_window_truncation.csv"), "w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["pickup_date", "days_past_freeze", "modeled_completeness",
                     "observed_live_legs", "implied_true_legs", "june_same_dow_mean"])
        for dd_, k, s, obs, imp, jm in rows_cm:
            wr.writerow([dd_.isoformat(), k, f"{s:.4f}", obs,
                         "" if imp != imp else f"{imp:.1f}", f"{jm:.2f}"])

    aug_obs = sum(per_day.get(date(2026, 8, x), 0) for x in range(1, 22))
    aug_imp = sum(r[4] for r in rows_cm if r[0] >= date(2026, 8, 1) and r[4] == r[4])
    print()
    print(f"  Aug 1-21 observed live legs      : {aug_obs}")
    print(f"  Aug 1-21 implied true legs       : {aug_imp:.0f}  [modeled]")
    print(f"  Aug 1-21 at June's daily rate    : {jun_daily*21:.0f}  [inferred]")
    print(f"  -> the model recovers {100.0*aug_imp/(jun_daily*21):.0f}% of the June-rate")
    print("     expectation from a 3-11% surviving booking tail. Recovering ~an order of")
    print("     magnitude from a sliver that thin is arithmetically unstable, so treat the")
    print("     implied figure as a SANITY CHECK, not an estimate. The point it makes is")
    print("     unambiguous: the observed August number carries no information about")
    print("     August demand.")

    print()
    print("4.4 -- Falsifying the alternative explanations")
    aug_cancel = sum(1 for l in legs if l[1] and l[1][:7] == "2026-08"
                     and is_cancelled(l[3], l[4]))
    aug_all = sum(1 for l in legs if l[1] and l[1][:7] == "2026-08")
    jun_cancel = sum(1 for l in legs if l[1] and l[1][:7] == "2026-06"
                     and is_cancelled(l[3], l[4]))
    jun_all = sum(1 for l in legs if l[1] and l[1][:7] == "2026-06")
    print(f"  (i) mass cancellation?  Aug cancel rate {100.0*aug_cancel/aug_all:.1f}% "
          f"({aug_cancel}/{aug_all}) vs Jun {100.0*jun_cancel/jun_all:.1f}% "
          f"({jun_cancel}/{jun_all}).")
    print("      REFUTED -- August is if anything LESS cancelled, because post-freeze")
    print("      cancellations were never recorded either.")
    aug_created = Counter(res_created[l[4]].date().isoformat()[:7] for l in legs
                          if l[1] and l[1][:7] == "2026-08" and res_created.get(l[4]))
    print(f"  (ii) real seasonal collapse?  booking months of the 2026-08 legs we DO hold:")
    print(f"      {dict(sorted(aug_created.items()))}")
    print("      Not one August leg was booked after 2026-07-11. A genuine demand collapse")
    print("      would still show late-booked legs. REFUTED.")
    late = sum(1 for x in lead_ref if x <= 41)
    print(f"  (iii) is late booking rare enough that Aug could be nearly complete?")
    print(f"      {100.0*late/n_ref:.1f}% of reference-cohort legs were booked <=41 days")
    print("      ahead (41 = 2026-08-21 minus the freeze). The bulk of any August book")
    print("      had not been placed yet. REFUTED.")
    ls_days = cur.execute(
        "SELECT COUNT(DISTINCT substr(timestamp,1,10)) FROM reservations_legstatus "
        "WHERE timestamp >= '2026-07-12'").fetchone()[0]
    print(f"  (iv) did operations continue?  distinct legstatus DAYS after 2026-07-11: "
          f"{ls_days} (3 of them, 7 rows, all local dev writes).")
    print("      There is no operational record of July 12 onward AT ALL. REFUTED.")
    print()
    print("  >>> VERDICT: the August collapse is 100% a SNAPSHOT ARTIFACT. It is not")
    print("  >>> seasonality, not cancellation, not slow booking. Any window ending after")
    print("  >>> 2026-07-11 fabricates a demand cliff.")

    print()
    print("4.5 -- Where exactly does truncation start to bite?")
    print(f"  {'pickup_date':<12} {'days past freeze':>17} {'modeled completeness':>21}")
    for target in (0.99, 0.97, 0.95, 0.90, 0.75, 0.50):
        k = 0
        while S(k) > target and k < 400:
            k += 1
        print(f"  {(FREEZE + timedelta(days=k)).isoformat():<12} {k:>17} "
              f"{S(k):>20.1%}   <- first day below {target:.0%}")
    print("  July 2026 partial-month reality:")
    for x in range(1, 21):
        dd = date(2026, 7, x)
        flag = "" if dd <= FREEZE else "  <-- past freeze"
        print(f"    {dd.isoformat()}  live legs = {per_day.get(dd,0):>3}"
              f"  (all rows {per_day_all.get(dd,0):>3}){flag}")

    # ================================================================ SECTION 5
    hdr("SECTION 5 -- FORWARD-BOOKED LEGS (pickup_date > 2026-08-21)")
    fwd = Counter()
    fwd_live = Counter()
    for lid, pd_, pt, st, rid, xa in legs:
        if not pd_:
            continue
        try:
            dd = d(pd_)
        except ValueError:
            continue
        if dd > TODAY:
            key = pd_[:7] if dd <= date(2027, 12, 31) else f"CORRUPT({pd_})"
            fwd[key] += 1
            if not is_cancelled(st, rid):
                fwd_live[key] += 1
    print(f"  total legs with pickup_date > 2026-08-21: {sum(fwd.values())} "
          f"({sum(fwd_live.values())} live)")
    print(f"  {'month':<20} {'all':>7} {'live':>7}")
    for m in sorted(fwd):
        print(f"  {m:<20} {fwd[m]:>7} {fwd_live[m]:>7}")
    print()
    print("  These are the ONLY future days Day Setup could be opened on with real content")
    print("  in this snapshot -- and they are the residue of a book frozen 41+ days ago, so")
    print("  they under-represent every future day by the same truncation as Section 4.")
    print("  Day Setup on a real production DB will see 5-20x these numbers.")

    # ================================================================ SECTION 6
    hdr("SECTION 6 -- GROWTH: TRAILING-28-DAY MEAN LEGS/DAY")
    print("  Series computed on LIVE legs (variant b). Truncated once the trailing window")
    print("  overlaps the freeze, so the curve is only readable to 2026-07-11.")
    series = []
    dd = date(2025, 5, 24)
    while dd <= TODAY:
        tot = sum(per_day.get(dd - timedelta(days=i), 0) for i in range(28))
        series.append((dd, tot / 28.0))
        dd += timedelta(days=1)
    smap = dict(series)
    print(f"  {'date':<12} {'trail28 legs/day':>17} {'vs 28d earlier':>15} {'note':<28}")
    for dd_, v in series:
        if dd_.weekday() != 6:  # print Sundays only
            continue
        prev = smap.get(dd_ - timedelta(days=28))
        delta = f"{100.0*(v/prev-1):+.0f}%" if prev and prev > 0.5 else "--"
        note = ""
        if dd_ > FREEZE:
            note = "CONTAMINATED by freeze"
        elif dd_ - timedelta(days=27) <= FREEZE <= dd_:
            note = "partially contaminated"
        print(f"  {dd_.isoformat():<12} {v:>17.1f} {delta:>15} {note:<28}")

    with open(os.path.join(OUT, "01_window_trailing28.csv"), "w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["date", "trailing28_legs_per_day", "legs_that_day",
                     "contaminated_by_freeze"])
        for dd_, v in series:
            wr.writerow([dd_.isoformat(), f"{v:.3f}", per_day.get(dd_, 0),
                         int(dd_ > FREEZE)])

    print()
    print("  Month-anchor readings (trailing-28 mean on the last day of each month):")
    print(f"  {'as of':<12} {'trail28 legs/day':>17} {'MoM':>8}")
    anchors = []
    for y, m in [(2025, x) for x in range(6, 13)] + [(2026, x) for x in range(1, 8)]:
        nxt = date(y + (m // 12), (m % 12) + 1, 1)
        eom = nxt - timedelta(days=1)
        if eom > FREEZE:
            eom = FREEZE
        if eom in smap:
            anchors.append((eom, smap[eom]))
    for i, (dd_, v) in enumerate(anchors):
        mom = f"{100.0*(v/anchors[i-1][1]-1):+.0f}%" if i else "--"
        print(f"  {dd_.isoformat():<12} {v:>17.1f} {mom:>8}")

    print()
    print("  Largest 28-day-over-28-day step-ups in the trailing mean (>=25 legs/day base):")
    steps = []
    for dd_, v in series:
        prev = smap.get(dd_ - timedelta(days=28))
        if prev and prev >= 25 and dd_ <= FREEZE:
            steps.append((v / prev - 1, dd_, prev, v))
    steps.sort(reverse=True)
    for g, dd_, prev, v in steps[:8]:
        print(f"    {dd_.isoformat()}  {prev:6.1f} -> {v:6.1f} legs/day  ({100*g:+.0f}%)")
    print()
    lo = min((v for dd_, v in series if date(2025, 10, 1) <= dd_ <= date(2025, 12, 31)))
    hi = max((v for dd_, v in series if dd_ <= FREEZE))
    print(f"  Trough (Oct-Dec 2025) {lo:.1f} legs/day  ->  peak (<=freeze) {hi:.1f} legs/day"
          f"  = {hi/lo:.1f}x in ~7 months. [measured]")

    # ================================================================ SECTION 7
    hdr("SECTION 7 -- DAY-OF-WEEK SHAPE BY MONTH (did the weekly shape itself move?)")
    print("  Share of the month's live legs falling on each weekday, plus the")
    print("  peak-to-trough ratio of the weekday means. Months <= 2026-06 only are")
    print("  trustworthy; 2026-07 is 11/31 days and is shown greyed by the TRUNC marker.")
    print(f"  {'month':<9} {'n':>6} " + " ".join(f"{x:>6}" for x in DOW)
          + f" {'peak/trough':>12}  flag")
    dowrows = []
    for m in sorted(mon_b):
        if m < "2025-09":
            continue
        cnt = Counter()
        days_of_dow = Counter()
        y, mm = int(m[:4]), int(m[5:])
        dd = date(y, mm, 1)
        while dd.month == mm:
            if dd <= TODAY:
                cnt[dd.weekday()] += per_day.get(dd, 0)
                days_of_dow[dd.weekday()] += 1
            dd += timedelta(days=1)
        n = sum(cnt.values())
        if not n:
            continue
        means = {k: cnt[k] / days_of_dow[k] for k in range(7) if days_of_dow[k]}
        ratio = max(means.values()) / min(means.values()) if min(means.values()) else 0
        flag = "TRUNC" if m >= "2026-07" else ""
        print(f"  {m:<9} {n:>6} "
              + " ".join(f"{100.0*cnt[k]/n:>5.1f}%" for k in range(7))
              + f" {ratio:>12.2f}  {flag}")
        dowrows.append([m, n] + [cnt[k] for k in range(7)]
                       + [f"{100.0*cnt[k]/n:.2f}" for k in range(7)] + [f"{ratio:.3f}"])
    with open(os.path.join(OUT, "01_window_dow_by_month.csv"), "w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["month", "legs_live"] + [f"n_{x}" for x in DOW]
                    + [f"pct_{x}" for x in DOW] + ["peak_trough_ratio"])
        wr.writerows(dowrows)
    print()
    print("  Same table restricted to the recommended window (2026-01-01..2026-07-11),")
    print("  aggregated -- this is the shape a shift template should be cut against:")
    cnt = Counter(); days_of_dow = Counter()
    dd = date(2026, 1, 1)
    while dd <= FREEZE:
        cnt[dd.weekday()] += per_day.get(dd, 0)
        days_of_dow[dd.weekday()] += 1
        dd += timedelta(days=1)
    tot = sum(cnt.values())
    print(f"  {'dow':<5} {'legs':>7} {'days':>6} {'legs/day':>9} {'share':>8} {'index':>7}")
    avg = tot / sum(days_of_dow.values())
    for k in range(7):
        print(f"  {DOW[k]:<5} {cnt[k]:>7} {days_of_dow[k]:>6} "
              f"{cnt[k]/days_of_dow[k]:>9.1f} {100.0*cnt[k]/tot:>7.1f}% "
              f"{100.0*(cnt[k]/days_of_dow[k])/avg:>6.0f}")

    # ================================================================ SECTION 7B
    hdr("SECTION 7B -- IS THE SHAPE STATIONARY? (level vs shape, the window-start test)")
    print("  Level moved 5.8x. The question for a shift template is whether the SHAPE moved")
    print("  too. If it did not, an early, low-volume period can still contribute shape;")
    print("  if it did, it must be cut off. Test: normalise each era to a probability")
    print("  distribution over the dimension, then report total-variation distance")
    print("  TVD = 0.5 * sum|p-q| (0 = identical, 1 = disjoint) and cosine similarity.")

    eras = [
        ("2025-Q4  2025-09-01..2025-12-31", date(2025, 9, 1), date(2025, 12, 31)),
        ("ramp     2026-01-01..2026-03-31", date(2026, 1, 1), date(2026, 3, 31)),
        ("plateau  2026-04-01..2026-07-11", date(2026, 4, 1), FREEZE),
    ]

    def profile(start, end, dim):
        c = Counter()
        for lid, pd_, pt, st, rid, xa in legs:
            if not pd_ or is_cancelled(st, rid):
                continue
            try:
                dd = d(pd_)
            except ValueError:
                continue
            if not (start <= dd <= end):
                continue
            if dim == "hour":
                c[int(pt[:2])] += 1
            elif dim == "dow":
                c[dd.weekday()] += 1
            else:
                c[(dd.weekday(), int(pt[:2]))] += 1
        n = sum(c.values())
        return {k: v / n for k, v in c.items()}, n

    def tvd(p, q):
        ks = set(p) | set(q)
        return 0.5 * sum(abs(p.get(k, 0) - q.get(k, 0)) for k in ks)

    def cosine(p, q):
        ks = set(p) | set(q)
        num = sum(p.get(k, 0) * q.get(k, 0) for k in ks)
        a = sum(p.get(k, 0) ** 2 for k in ks) ** 0.5
        b = sum(q.get(k, 0) ** 2 for k in ks) ** 0.5
        return num / (a * b) if a and b else 0.0

    for dim in ("dow", "hour", "dow_hour"):
        print()
        print(f"  dimension = {dim}")
        profs = [(lbl, profile(s, e, dim)) for lbl, s, e in eras]
        for i in range(len(profs)):
            for j in range(i + 1, len(profs)):
                (la, (pa, na)), (lb, (pb, nb)) = profs[i], profs[j]
                print(f"    {la}  vs  {lb}   n={na}/{nb}   "
                      f"TVD={tvd(pa,pb):.3f}   cosine={cosine(pa,pb):.4f}")

    print()
    print("  Hour profile (% of era's live legs) by era:")
    ph = [(lbl, profile(s, e, "hour")[0]) for lbl, s, e in eras]
    print("    hour " + " ".join(f"{h:>6}" for h in range(3, 23)))
    for lbl, p in ph:
        print(f"    {lbl[:8]:<8}" + " ".join(f"{100*p.get(h,0):>5.1f}%" for h in range(3, 23)))
    print()
    print("  Cumulative share of legs picked up before each hour, by era:")
    print("    era      " + " ".join(f"{h:>6}" for h in (6, 8, 10, 12, 14, 16, 18, 20)))
    for lbl, p in ph:
        print(f"    {lbl[:8]:<8}" + " ".join(
            f"{100*sum(p.get(x,0) for x in range(0,h)):>5.1f}%"
            for h in (6, 8, 10, 12, 14, 16, 18, 20)))

    print()
    print("  Monthly LEVEL stationarity inside the plateau (live legs/day, whole months):")
    for m in ("2026-04", "2026-05", "2026-06"):
        y, mm = int(m[:4]), int(m[5:])
        nxt = date(y + mm // 12, mm % 12 + 1, 1)
        nd = (nxt - date(y, mm, 1)).days
        v = sum(per_day.get(date(y, mm, x), 0) for x in range(1, nd + 1))
        print(f"    {m}  {v:>5} legs / {nd} days = {v/nd:>5.1f} legs/day")
    v = sum(per_day.get(date(2026, 7, x), 0) for x in range(1, 12))
    print(f"    2026-07 (1-11 only)  {v:>5} legs / 11 days = {v/11:>5.1f} legs/day")

    # ================================================================ SECTION 8
    hdr("SECTION 8 -- DEMAND CURVE, day_of_week x pickup_hour")
    dowhour_csv = []
    for label, start, end in (
            ("last 90 days ending at the FREEZE (recommended)",
             FREEZE - timedelta(days=89), FREEZE),
            ("last 90 days ending at nominal today 2026-08-21 (CONTAMINATED, do not use)",
             TODAY - timedelta(days=89), TODAY),
            ("SHAPE window 2026-01-01..2026-07-11 (larger sample, same shape)",
             date(2026, 1, 1), FREEZE)):
        grid = defaultdict(int)
        ndow = Counter()
        dd = start
        while dd <= end:
            ndow[dd.weekday()] += 1
            dd += timedelta(days=1)
        n = 0
        for lid, pd_, pt, st, rid, xa in legs:
            if not pd_ or is_cancelled(st, rid):
                continue
            try:
                dd = d(pd_)
            except ValueError:
                continue
            if not (start <= dd <= end):
                continue
            grid[(dd.weekday(), int(pt[:2]))] += 1
            n += 1
        print()
        print(f"  {label}   {start} .. {end}   n={n} live legs")
        print("      " + "".join(f"{h:>4}" for h in range(24)) + "   total")
        for k in range(7):
            row = [grid.get((k, h), 0) for h in range(24)]
            print(f"  {DOW[k]:<4}" + "".join(f"{v:>4}" if v else "   ." for v in row)
                  + f"   {sum(row):>5}")
        print("  tot " + "".join(f"{sum(grid.get((k,h),0) for k in range(7)):>4}"
                                 for h in range(24))
              + f"   {n:>5}")
        for k in range(7):
            for h in range(24):
                v = grid.get((k, h), 0)
                dowhour_csv.append([label.split(" (")[0], start.isoformat(),
                                    end.isoformat(), k, DOW[k], h, v, ndow[k],
                                    f"{v/ndow[k]:.3f}",
                                    int(end > FREEZE)])
        if end == FREEZE and (end - start).days == 89:
            print()
            print("  Per-occurrence intensity (legs per that-weekday, 1 dp) -- the number a")
            print("  shift template is actually cut against:")
            print("      " + "".join(f"{h:>5}" for h in range(24)))
            for k in range(7):
                print(f"  {DOW[k]:<4}" + "".join(
                    f"{grid.get((k,h),0)/ndow[k]:>5.1f}" if grid.get((k, h)) else "    ."
                    for h in range(24)))
            hours = sorted(range(24),
                           key=lambda h: -sum(grid.get((k, h), 0) for k in range(7)))
            print()
            print("  Hours ranked by volume:",
                  ", ".join(f"{h:02d}h={sum(grid.get((k,h),0) for k in range(7))}"
                            for h in hours[:10]))

    with open(os.path.join(OUT, "01_window_dow_hour.csv"), "w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["window_label", "window_start", "window_end", "dow_index", "dow",
                     "pickup_hour_local", "legs", "n_that_weekday_in_window",
                     "legs_per_occurrence", "contaminated_by_freeze"])
        wr.writerows(dowhour_csv)

    # ================================================================ SECTION 9
    hdr("SECTION 9 -- RECOMMENDED WINDOW")
    cands = [
        ("PRIMARY (staffing LEVELS)", date(2026, 4, 1), FREEZE,
         "level is stationary here (91/89/93/90 legs/day); this is the only regime"),
        ("SECONDARY (SHAPE, dow x hour)", date(2026, 1, 1), FREEZE,
         "adds 6k legs of shape sample; shape is stationary vs the plateau"),
        ("STATUS-TAP window (timings, idle, handoffs)", date(2026, 2, 8), date(2026, 7, 10),
         "reservations_legstatus starts 2026-02-08 04:19 UTC; end 07-10 for whole days"),
        ("REJECTED -- full history", date(2025, 4, 26), FREEZE,
         "pre-2026 is a 2-16 legs/day company; different business"),
    ]
    print(f"  {'window':<44} {'start':<12} {'end':<12} {'days':>5} {'legs':>7} "
          f"{'legs/day':>9}")
    for lbl, s, e, why in cands:
        nd = (e - s).days + 1
        v = sum(per_day.get(s + timedelta(days=i), 0) for i in range(nd))
        print(f"  {lbl:<44} {s.isoformat():<12} {e.isoformat():<12} {nd:>5} {v:>7} "
              f"{v/nd:>9.1f}")
        print(f"      why: {why}")
    print()
    print("  END is HARD at 2026-07-11 for demand counts and 2026-07-10 for anything")
    print("  needing a full day of driver taps (freeze is 16:37 local on the 11th).")
    print("  Nothing dated 2026-07-12 or later may enter any aggregate, any average, any")
    print("  chart axis, or any replay.")
    con.close()
    print()
    print("CSV written to", OUT)
    for f in sorted(os.listdir(OUT)):
        if f.startswith("01_window"):
            print("  ", os.path.join(OUT, f))


if __name__ == "__main__":
    assumptions()
    main()
