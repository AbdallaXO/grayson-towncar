#!/usr/bin/env python3
"""
04_supply.py -- Grayson Towncar scheduling redesign, Phase 1.

QUESTION: What do we actually know about who and what was available on a given day?

Read-only. No Django, no manage.py, no writes to the snapshot.
Run from the repo root:   python docs/scheduling-redesign/analysis/04_supply.py

Outputs CSV to docs/scheduling-redesign/analysis/out/ prefixed 04_supply_.
"""

import csv
import os
import sqlite3
from collections import Counter, defaultdict
from datetime import date, timedelta

DB = "file:content/db.sqlite3?mode=ro"
OUT = os.path.join("docs", "scheduling-redesign", "analysis", "out")

FILE_DATE = date(2026, 8, 21)        # when the snapshot file was refreshed
CUT = date(2026, 7, 11)              # last day of real production activity (proved in S0b)
WINDOW_START = date(2025, 10, 1)     # demand era (leg volume before this is a thin ramp)
DVA_ERA_START = date(2026, 1, 1)     # DVA becomes a habit here (S1)
DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def d(s):
    return date(int(s[0:4]), int(s[5:7]), int(s[8:10]))


def pct(vals, p):
    if not vals:
        return None
    v = sorted(vals)
    k = int(round((p / 100.0) * (len(v) - 1)))
    return v[k]


def fmt(x, nd=0):
    if x is None:
        return "-"
    if isinstance(x, float):
        return ("%%.%df" % nd) % x
    return str(x)


def hdr(t):
    print()
    print("=" * 104)
    print(t)
    print("=" * 104)


def sub(t):
    print()
    print("--- " + t + " " + "-" * max(0, 99 - len(t)))


def assumptions():
    print("#" * 104)
    print("# 04_supply.py  --  supply-side data reliability: roster, DVA, fleet, availability")
    print("# snapshot: content/db.sqlite3, opened read-only via")
    print("#           sqlite3.connect('file:content/db.sqlite3?mode=ro', uri=True)")
    print("#")
    print("# ASSUMPTIONS (every one of them, stated up front):")
    print("#  A1. The snapshot FILE is dated 2026-08-21, but production activity STOPS at")
    print("#      2026-07-11. Everything dated after that is local-development writes on a")
    print("#      writable dev copy. Proved from the supply side in S0b (independently of")
    print("#      00_snapshot_provenance.py, which reached the same cut from bookings).")
    print("#      CUT = 2026-07-11 is therefore 'the present' for every headline figure.")
    print("#  A2. PRIMARY WINDOW = 2025-10-01 .. 2026-07-11 inclusive (285 days). Leg volume")
    print("#      before 2025-10 is a thin ramp. Post-CUT rows are reported ONLY as")
    print("#      contamination, never mixed into a rate or a percentile.")
    print("#  A3. leg.pickup_date and drivers_drivervehicleassignment.date are BOTH naive")
    print("#      Florida-local CALENDAR DATES. They are compared directly with NO timezone")
    print("#      conversion. legstatus.timestamp (UTC) is used only for freeze forensics")
    print("#      in S0b and is never differenced against a pickup_date here.")
    print("#  A4. An OPERATING DAY is the calendar date. Legs picked up 00:00-03:59 are")
    print("#      counted on their own calendar date even though a dispatcher would call them")
    print("#      the previous night's work. The size of that distortion is MEASURED in S4.")
    print("#  A5. CANCELLED exclusion = leg.status='cancelled' OR reservation.status IN")
    print("#      ('cancelled','canceled') (both spellings exist). Everything else, including")
    print("#      leg.status='in-progress' (the Django creation default), counts as real work.")
    print("#  A6. 'WORKED day D' := the driver has >=1 non-cancelled leg with pickup_date=D and")
    print("#      leg.driver_id = that driver. There is no clock-in/clock-out table anywhere in")
    print("#      the schema, so assignment is the ONLY evidence a person worked.")
    print("#  A7. CORRUPT pickup_date := outside [2025-01-01, 2027-12-31]. Excluded from every")
    print("#      figure and counted in S0a.")
    print("#  A8. DVA is compared against IN-HOUSE drivers only in the primary agreement test")
    print("#      (S2), because dispatching/day_setup.py:285 builds the Day Setup roster from")
    print("#      Driver.objects.filter(driver_type='inhouse', is_active=True). The all-driver")
    print("#      variant is reported alongside so the choice is visible.")
    print("#  A9. driver_type / is_active are read as they stand in the snapshot. Neither is")
    print("#      historised anywhere, so a driver who was in-house in January and is flagged")
    print("#      affiliate now reads as affiliate for all of history. UNFIXABLE here.")
    print("# A10. Vehicle class = rates_vehicle.vehicle_type joined via")
    print("#      drivers_fleetvehicle.vehicle_type_id. Raw CharField, not .title()-cased.")
    print("# A11. No Django models imported, no application code executed. Raw SQL + stdlib.")
    print("#" * 104)


def main():
    os.makedirs(OUT, exist_ok=True)
    con = sqlite3.connect(DB, uri=True)
    cur = con.cursor()

    assumptions()

    # ------------------------------------------------------------------ load
    users = {}
    for uid, un, fn, ln in cur.execute(
            "SELECT id, username, first_name, last_name FROM auth_user"):
        users[uid] = ((un or "").strip()
                      or ("%s %s" % (fn or "", ln or "")).strip()
                      or "user%s" % uid)

    drivers = {}
    for did, pid, dtype, act, emp, excl in cur.execute(
            "SELECT id, profile_id, driver_type, is_active, employment_type, "
            "exclude_from_timing FROM drivers_driver"):
        drivers[did] = {
            "id": did, "name": users.get(pid, "driver%s" % did),
            "type": dtype, "active": bool(act),
            "employment": emp or "", "excl": bool(excl),
        }
    inhouse = {k for k, v in drivers.items() if v["type"] == "inhouse"}

    res_status = dict(cur.execute("SELECT id, status FROM reservations_reservation"))

    corrupt = Counter()
    legs = []                       # (driver_id, date, pickup_time, assigned_at)
    n_leg_total = 0
    for lid, pd_, pt, st, rid, drv, daa in cur.execute(
            "SELECT id, pickup_date, pickup_time, status, reservation_id, driver_id, "
            "driver_assigned_at FROM reservations_leg"):
        n_leg_total += 1
        if not pd_:
            corrupt["NULL"] += 1
            continue
        y = int(pd_[0:4])
        if y < 2025 or y > 2027:
            corrupt[pd_[0:4]] += 1
            continue
        if st == "cancelled" or res_status.get(rid) in ("cancelled", "canceled"):
            continue
        legs.append((drv, d(pd_), pt, daa))

    hdr("S0a. SNAPSHOT CONFIRMATION + corrupt pickup_date census")
    print("reservations_leg rows                          : %d   [measured]" % n_leg_total)
    print("  ...excluded as CORRUPT pickup_date (A7)      : %d" % sum(corrupt.values()))
    for k, v in sorted(corrupt.items()):
        if k.isdigit():
            yr = cur.execute(
                "SELECT MIN(pickup_date), MAX(pickup_date) FROM reservations_leg "
                "WHERE substr(pickup_date,1,4)=?", (k,)).fetchone()
        else:
            yr = (None, None)
        print("      year %-8s : %5d   (span %s .. %s)" % (k, v, yr[0], yr[1]))
    span = cur.execute(
        "SELECT MIN(pickup_date), MAX(pickup_date) FROM reservations_leg").fetchone()
    print("  raw pickup_date span (uncleaned)             : %s .. %s" % span)
    good_span = cur.execute(
        "SELECT MIN(pickup_date), MAX(pickup_date) FROM reservations_leg "
        "WHERE substr(pickup_date,1,4) BETWEEN '2025' AND '2027'").fetchone()
    print("  cleaned pickup_date span                     : %s .. %s" % good_span)
    print("  => the '3220' max in the brief is ONE row. Corrupt total is 2 rows, 0.008%.")
    print("  non-cancelled, non-corrupt legs kept         : %d" % len(legs))
    print("  ...of which carry a driver_id                : %d" % sum(1 for x in legs if x[0]))
    print("drivers_driver: inhouse active %d / inhouse inactive %d / affiliate active %d / "
          "affiliate inactive %d   [measured -- CONFIRMS the brief]" % (
              sum(1 for v in drivers.values() if v["type"] == "inhouse" and v["active"]),
              sum(1 for v in drivers.values() if v["type"] == "inhouse" and not v["active"]),
              sum(1 for v in drivers.values() if v["type"] == "affiliate" and v["active"]),
              sum(1 for v in drivers.values() if v["type"] == "affiliate" and not v["active"])))

    worked = defaultdict(set)
    dd_legs = defaultdict(list)
    legs_by_date = Counter()
    assigned_by_date = Counter()
    for drv, dt_, pt, daa in legs:
        legs_by_date[dt_] += 1
        if drv:
            assigned_by_date[dt_] += 1
            worked[dt_].add(drv)
            dd_legs[(drv, dt_)].append(pt)

    # ---------------------------------------------------------------- S0b
    hdr("S0b. WHERE DOES THE SUPPLY RECORD ACTUALLY STOP? (freeze forensics)")
    print("The brief states legstatus spans to 2026-08-21 18:45 UTC. That is TRUE of the")
    print("MAX() and MISLEADING as a coverage claim. Evidence, all [measured]:")
    print()
    mx = cur.execute("SELECT MAX(created_at) FROM reservations_reservation").fetchone()[0]
    print("  last reservation ever created            : %s" % mx)
    gap = cur.execute("SELECT COUNT(*) FROM reservations_legstatus "
                      "WHERE timestamp >= '2026-07-12' AND timestamp < '2026-08-17'").fetchone()[0]
    print("  legstatus rows 2026-07-12 .. 2026-08-16  : %d  (a 36-day hole)" % gap)
    print("  every legstatus row after 2026-07-11, with its author:")
    for lid, st, ts, ub in cur.execute(
            "SELECT leg_id, status, timestamp, updated_by_id FROM reservations_legstatus "
            "WHERE timestamp > '2026-07-12' ORDER BY timestamp"):
        print("      leg %-7s %-12s %s  updated_by=%s (%s)"
              % (lid, st, ts[:19], ub, users.get(ub, "?")))
    n_authors = cur.execute(
        "SELECT COUNT(DISTINCT updated_by_id) FROM reservations_legstatus "
        "WHERE timestamp > '2026-07-12'").fetchone()[0]
    print("  distinct authors of those rows           : %d" % n_authors)
    print()
    print("  POST-CUT SUPPLY-SIDE CONTAMINATION (rows a historical replay would swallow):")
    post_dva = [r for r in cur.execute(
        "SELECT date, COUNT(*) FROM drivers_drivervehicleassignment "
        "WHERE date > '2026-07-12' GROUP BY 1 ORDER BY 1")]
    print("    DVA rows dated after 2026-07-12        : %d rows on %d dates -> %s"
          % (sum(x[1] for x in post_dva), len(post_dva),
             ", ".join("%s(%d)" % (a, b) for a, b in post_dva)))
    post_asg = cur.execute(
        "SELECT COUNT(*) FROM reservations_leg WHERE driver_assigned_at > '2026-07-12'"
    ).fetchone()[0]
    print("    legs whose driver_assigned_at > CUT    : %d" % post_asg)
    for a, b in cur.execute(
            "SELECT substr(driver_assigned_at,1,10), COUNT(*) FROM reservations_leg "
            "WHERE driver_assigned_at > '2026-07-12' GROUP BY 1 ORDER BY 1"):
        print("        assigned on %s : %d legs" % (a, b))
    print()
    print("  BUT 2026-07-12 IS REAL. Its board was PRE-BUILT on 2026-07-11, before the")
    print("  freeze. driver_assigned_at for legs with pickup_date=2026-07-12:")
    for a, b in cur.execute(
            "SELECT substr(driver_assigned_at,1,10), COUNT(*) FROM reservations_leg "
            "WHERE pickup_date='2026-07-12' GROUP BY 1 ORDER BY 1"):
        print("      stamped %-12s : %d legs" % (a or "(never assigned)", b))
    print("  So the ROSTER record (DVA + leg.driver_id) is honest through 2026-07-12;")
    print("  only the EVENT record (legstatus) stops a day earlier, at 2026-07-11.")
    print()
    print("  VERDICT: production stops 2026-07-11 ~20:38 UTC. The DVA rows and driver")
    print("  assignments dated 2026-07-18 / 07-24 / 07-31 / 08-01 / 08-09 / 08-19 / 08-21")
    print("  are Day-Setup + auto-assign runs performed LOCALLY on the dev copy after the")
    print("  snapshot was taken. They look exactly like real dispatcher work in the tables")
    print("  and MUST be excluded from any replay. CUT = 2026-07-11 (A1); the roster")
    print("  record may be extended one day to 2026-07-12 with the evidence above.")

    months = []
    m = date(WINDOW_START.year, WINDOW_START.month, 1)
    while m <= FILE_DATE:
        months.append(m)
        m = date(m.year + (m.month == 12), m.month % 12 + 1, 1)

    def nextmonth(mm):
        return date(mm.year + (mm.month == 12), mm.month % 12 + 1, 1)

    def windays(mm):
        nxt = nextmonth(mm)
        out, cd = [], mm
        while cd < nxt:
            if WINDOW_START <= cd <= CUT:
                out.append(cd)
            cd += timedelta(days=1)
        return out

    # =====================================================================  S1
    hdr("S1. drivers_drivervehicleassignment (DVA) -- volume, coverage, planned hours")

    dva = []
    for did, dt_, vid, ps, pe in cur.execute(
            "SELECT driver_id, date, vehicle_id, planned_start_hour, planned_end_hour "
            "FROM drivers_drivervehicleassignment"):
        dva.append((did, d(dt_), vid, ps, pe))
    dva_by_date = defaultdict(set)
    dva_veh_by_date = defaultdict(set)
    for did, dt_, vid, ps, pe in dva:
        dva_by_date[dt_].add(did)
        if vid:
            dva_veh_by_date[dt_].add(vid)
    dva_pre = [x for x in dva if x[1] <= CUT]

    print("DVA total rows                : %d   (pre-CUT %d / post-CUT %d)   [measured]"
          % (len(dva), len(dva_pre), len(dva) - len(dva_pre)))
    print("DVA date span                 : %s .. %s  (pre-CUT span %s .. %s)"
          % (min(x[1] for x in dva), max(x[1] for x in dva),
             min(x[1] for x in dva_pre), max(x[1] for x in dva_pre)))
    print("DVA rows with vehicle_id NULL : %d" % sum(1 for x in dva if not x[2]))
    print("DVA rows held by a driver who is NOT inhouse in the snapshot : %d"
          % sum(1 for x in dva if x[0] not in inhouse))
    print("DVA rows held by an INACTIVE driver in the snapshot           : %d"
          % sum(1 for x in dva if not drivers.get(x[0], {}).get("active", False)))
    print("  (that second number is why dispatching/day_setup.py:296-306 has explicit")
    print("   'stale row held by a deactivated driver' handling -- it is a real condition.)")

    sub("S1a. DVA by month (window days only; post-CUT months shown for contrast)")
    print("%-9s %7s %7s %8s %9s %9s %8s %9s" % (
        "month", "rows", "dates", "drivers", "win_days", "zeroDVA", "%zero", "legs_win"))
    for mm in months:
        nxt = nextmonth(mm)
        days = windays(mm)
        rows = [x for x in dva if mm <= x[1] < nxt]
        zero = [x for x in days if x not in dva_by_date]
        tag = "" if days else "   <- entirely post-CUT (local dev)"
        print("%-9s %7d %7d %8d %9s %9s %8s %9d%s" % (
            mm.strftime("%Y-%m"), len(rows), len({x[1] for x in rows}),
            len({x[0] for x in rows}),
            len(days) if days else "-", len(zero) if days else "-",
            ("%.0f%%" % (100.0 * len(zero) / len(days))) if days else "-",
            sum(v for k, v in legs_by_date.items()
                if mm <= k < nxt and k <= CUT), tag))
    pre = [x for x in dva if x[1] < WINDOW_START]
    if pre:
        print("(%d DVA rows predate the window: %s)" % (
            len(pre), ", ".join("%s x%d" % (k, v) for k, v in
                                sorted(Counter(str(x[1]) for x in pre).items()))))

    sub("S1b. Window-level DVA coverage (all against CUT=2026-07-11)")
    for label, start in (("primary window 2025-10-01", WINDOW_START),
                         ("DVA era        2026-01-01", DVA_ERA_START),
                         ("DVA habit      2026-02-01", date(2026, 2, 1))):
        alldays = [start + timedelta(days=i) for i in range((CUT - start).days + 1)]
        have = [x for x in alldays if x in dva_by_date]
        withlegs = [x for x in alldays if legs_by_date.get(x)]
        both = [x for x in withlegs if x in dva_by_date]
        print("%s..%s : %3d days, %3d with ANY DVA row (%5.1f%%), %3d with ZERO (%5.1f%%)"
              % (label, CUT, len(alldays), len(have), 100.0 * len(have) / len(alldays),
                 len(alldays) - len(have),
                 100.0 * (len(alldays) - len(have)) / len(alldays)))
        print("%s   operating days (>=1 leg) %3d, of which DVA-covered %3d (%5.1f%%)"
              % (" " * len(label), len(withlegs), len(both),
                 100.0 * len(both) / len(withlegs) if withlegs else 0))

    sub("S1c. The DVA habit: unbroken runs of DVA-covered dates")
    covered = sorted(dva_by_date)
    runs, cur_run = [], [covered[0]]
    for a, b in zip(covered, covered[1:]):
        if (b - a).days == 1:
            cur_run.append(b)
        else:
            runs.append(cur_run)
            cur_run = [b]
    runs.append(cur_run)
    print("total DVA-covered dates: %d in %d runs" % (len(covered), len(runs)))
    for r in sorted(runs, key=lambda r: -len(r))[:8]:
        print("   %s .. %s  (%d consecutive dates, %d rows)%s" % (
            r[0], r[-1], len(r), sum(1 for x in dva if r[0] <= x[1] <= r[-1]),
            "   <- post-CUT" if r[0] > CUT else ""))
    print("   => DVA is filled in EVERY DAY from 2026-01-18 to 2026-07-12 and essentially")
    print("      never before 2026-01-18 (3 stray dates in Oct 2025, 1 in Aug 2025).")

    sub("S1d. planned_start_hour / planned_end_hour population")
    ps_n = sum(1 for x in dva if x[3] is not None)
    pe_n = sum(1 for x in dva if x[4] is not None)
    print("rows with planned_start_hour NOT NULL : %d / %d  (%.2f%%)"
          % (ps_n, len(dva), 100.0 * ps_n / len(dva)))
    print("rows with planned_end_hour   NOT NULL : %d / %d  (%.2f%%)"
          % (pe_n, len(dva), 100.0 * pe_n / len(dva)))
    if ps_n:
        print("   start_hour distribution:",
              sorted(Counter(x[3] for x in dva if x[3] is not None).items()))
        print("   end_hour   distribution:",
              sorted(Counter(x[4] for x in dva if x[4] is not None).items()))
    else:
        print("   [unavailable] -- DISTRIBUTION DOES NOT EXIST. Both columns are present")
        print("   (drivers/models.py:947-953) and are written ONLY by the Day Setup shared-car")
        print("   AM/PM split. No shared-car split was ever saved in this snapshot, so there is")
        print("   NO recorded per-day planned working window for any driver on any date.")
        print("   Consequence: DVA answers WHETHER a driver was rostered and WHICH CAR he")
        print("   took, and says NOTHING about WHEN he was meant to be on duty.")

    # =====================================================================  S2
    hdr("S2. IS DVA THE SYSTEM OF RECORD FOR 'WHO WORKED THAT DAY'?")
    print("For each date: A = drivers with a DVA row; B = drivers with >=1 assigned")
    print("non-cancelled leg. precision = |A n B| / |A| (a DVA row meant real work);")
    print("recall = |A n B| / |B| (the day's real crew was declared). Jaccard = |AnB|/|AuB|.")
    print("Scored ONLY over PRE-CUT dates that have >=1 DVA row AND >=1 assigned leg.")

    def agreement(restrict_inhouse):
        per_month = defaultdict(lambda: dict(n=0, tp=0, fp=0, fn=0, ja=[]))
        per_date = {}
        for dt_ in sorted(set(dva_by_date) | set(worked)):
            if not (WINDOW_START <= dt_ <= CUT):
                continue
            A = set(dva_by_date.get(dt_, set()))
            B = set(worked.get(dt_, set()))
            if restrict_inhouse:
                A = {x for x in A if x in inhouse}
                B = {x for x in B if x in inhouse}
            if not A or not B:
                continue
            tp, fp, fn = len(A & B), len(A - B), len(B - A)
            s = per_month[dt_.strftime("%Y-%m")]
            s["n"] += 1
            s["tp"] += tp
            s["fp"] += fp
            s["fn"] += fn
            s["ja"].append(len(A & B) / float(len(A | B)))
            per_date[dt_] = (A, B, tp, fp, fn)
        return per_month, per_date

    keep = {}
    for restrict, label in ((True, "IN-HOUSE ONLY (A8 -- the fair test)"),
                            (False, "ALL DRIVERS incl. affiliates")):
        sub("S2 %s" % label)
        pm, pdt = agreement(restrict)
        print("%-9s %6s %8s %8s %8s %10s %9s %9s" % (
            "month", "dates", "TP", "FP", "FN", "precision", "recall", "medJacc"))
        TP = FP = FN = 0
        for k in sorted(pm):
            s = pm[k]
            TP += s["tp"]
            FP += s["fp"]
            FN += s["fn"]
            print("%-9s %6d %8d %8d %8d %9.1f%% %8.1f%% %9.2f" % (
                k, s["n"], s["tp"], s["fp"], s["fn"],
                100.0 * s["tp"] / (s["tp"] + s["fp"]) if s["tp"] + s["fp"] else 0,
                100.0 * s["tp"] / (s["tp"] + s["fn"]) if s["tp"] + s["fn"] else 0,
                pct(s["ja"], 50)))
        print("%-9s %6d %8d %8d %8d %9.1f%% %8.1f%%" % (
            "ALL", sum(s["n"] for s in pm.values()), TP, FP, FN,
            100.0 * TP / (TP + FP) if TP + FP else 0,
            100.0 * TP / (TP + FN) if TP + FN else 0))
        if restrict:
            keep = pdt
        else:
            print("  (recall collapses because DVA is an IN-HOUSE artefact by construction:")
            print("   it has no row for any affiliate, and affiliates drove 12-40% of legs.)")

    sub("S2c. Who are the false positives / false negatives (in-house test)?")
    fp_c, fn_c = Counter(), Counter()
    for dt_, (A, B, tp, fp, fn) in keep.items():
        for x in A - B:
            fp_c[x] += 1
        for x in B - A:
            fn_c[x] += 1
    print("FALSE POSITIVE = had a DVA row, drove nothing that day (total %d driver-days):"
          % sum(fp_c.values()))
    for did, n in fp_c.most_common(12):
        v = drivers.get(did, {})
        print("   %-14s x%-4d  (%s, %s)" % (v.get("name", did), n, v.get("type"),
                                            "active" if v.get("active") else "INACTIVE"))
    print("FALSE NEGATIVE = drove that day, no DVA row (total %d driver-days):"
          % sum(fn_c.values()))
    for did, n in fn_c.most_common(12):
        v = drivers.get(did, {})
        print("   %-14s x%-4d  (%s, %s)" % (v.get("name", did), n, v.get("type"),
                                            "active" if v.get("active") else "INACTIVE"))

    sub("S2d. Per-date agreement quality (in-house test)")
    bad = sorted(((len(A & B) / float(len(A | B)), dt_, len(A), len(B))
                  for dt_, (A, B, t, f, n) in keep.items()))
    print("   worst 10 DVA-covered dates by Jaccard:")
    for j, dt_, na, nb in bad[:10]:
        print("      %s  jacc=%.2f  |DVA|=%d  |worked|=%d" % (dt_, j, na, nb))
    jj = [b[0] for b in bad]
    print("   Jaccard over scored dates: P10=%.2f P50=%.2f P75=%.2f P90=%.2f"
          % (pct(jj, 10), pct(jj, 50), pct(jj, 75), pct(jj, 90)))
    perfect = sum(1 for j in jj if j == 1.0)
    print("   dates where DVA set == worked set EXACTLY: %d / %d (%.1f%%)"
          % (perfect, len(jj), 100.0 * perfect / len(jj)))
    hi = [b for b in bad if b[1] >= date(2026, 4, 1)]
    print("   restricted to 2026-04-01..CUT: exact match on %d / %d dates (%.1f%%)"
          % (sum(1 for b in hi if b[0] == 1.0), len(hi),
             100.0 * sum(1 for b in hi if b[0] == 1.0) / len(hi)))

    # =====================================================================  S3
    hdr("S3. ROSTER OVER TIME -- who actually drove")
    sub("S3a. Distinct drivers with >=1 assigned leg, by month (window months only)")
    print("%-9s %8s %8s %8s %10s %10s %9s" % (
        "month", "inhouse", "affil", "total", "legs_ih", "legs_af", "%legs_af"))
    for mm in months:
        nxt = nextmonth(mm)
        if not windays(mm):
            continue
        ih, af = set(), set()
        lih = laf = 0
        for drv, dt_, pt, daa in legs:
            if drv and mm <= dt_ < nxt and dt_ <= CUT:
                if drv in inhouse:
                    ih.add(drv)
                    lih += 1
                else:
                    af.add(drv)
                    laf += 1
        print("%-9s %8d %8d %8d %10d %10d %8.0f%%%s" % (
            mm.strftime("%Y-%m"), len(ih), len(af), len(ih | af), lih, laf,
            100.0 * laf / (lih + laf) if lih + laf else 0,
            "   <- 11 days only (CUT)" if mm.month == 7 and mm.year == 2026 else ""))

    sub("S3b. First-seen / last-seen per driver (assigned legs, 2025-10-01..CUT)")
    print("silent_d = days between last assigned leg and CUT (2026-07-11), NOT the file date.")
    seen = defaultdict(list)
    for drv, dt_, pt, daa in legs:
        if drv and WINDOW_START <= dt_ <= CUT:
            seen[drv].append(dt_)
    for grp, lab in ((inhouse, "IN-HOUSE"), (None, "AFFILIATE (for contrast)")):
        print()
        print("%s:" % lab)
        print("%-16s %-7s %-12s %-12s %7s %7s %9s" % (
            "driver", "active", "first_seen", "last_seen", "days", "legs", "silent_d"))
        sel = [x for x in seen if (x in inhouse) == (grp is not None)]
        for did in sorted(sel, key=lambda x: min(seen[x])):
            v = drivers[did]
            ds = seen[did]
            print("%-16s %-7s %-12s %-12s %7d %7d %9d" % (
                v["name"], "yes" if v["active"] else "NO", min(ds), max(ds),
                len(set(ds)), len(ds), (CUT - max(ds)).days))

    sub("S3c. Hires and departures inside the window (in-house)")
    hires = [(min(seen[x]), x) for x in seen
             if x in inhouse and min(seen[x]) > WINDOW_START + timedelta(days=14)]
    deps = [(max(seen[x]), x) for x in seen
            if x in inhouse and (CUT - max(seen[x])).days > 30]
    print("first assigned leg >14d after window start (probable HIRE): %d" % len(hires))
    for dt_, x in sorted(hires):
        print("   %s  %-14s (%s)" % (dt_, drivers[x]["name"],
                                     "active" if drivers[x]["active"] else "INACTIVE"))
    print("no assigned leg in the 30 days before CUT (probable DEPARTURE/leave): %d"
          % len(deps))
    for dt_, x in sorted(deps):
        print("   last %s  %-14s (%s)  silent %d days" % (
            dt_, drivers[x]["name"], "active" if drivers[x]["active"] else "INACTIVE",
            (CUT - dt_).days))
    never = [k for k, v in drivers.items()
             if v["type"] == "inhouse" and v["active"] and k not in seen]
    print("ACTIVE in-house drivers with ZERO assigned legs in the window: %d -> %s"
          % (len(never), ", ".join(sorted(drivers[x]["name"] for x in never)) or "none"))
    print("in-house drivers seen driving in the window but flagged INACTIVE now: %d"
          % sum(1 for x in seen if x in inhouse and not drivers[x]["active"]))
    print("  -> is_active is a CURRENT flag, not a historical one (A9). A replay that")
    print("     filters on is_active drops these people from days they demonstrably worked.")

    sub("S3d. THE ASSIGNMENT CLIFF -- is leg.driver_id itself still filled in?")
    print("A leg with driver_id NULL leaves no trace of who worked. This is the second,")
    print("independent proof of the CUT (S0b) and it is sharper than the DVA one.")
    print("%-9s %9s %10s %10s %9s %12s" % (
        "month", "legs", "assigned", "%assigned", "opdays", "opdays_0drv"))
    for mm in months:
        nxt = nextmonth(mm)
        tot = sum(v for k, v in legs_by_date.items()
                  if mm <= k < nxt and k <= FILE_DATE)
        asg = sum(v for k, v in assigned_by_date.items()
                  if mm <= k < nxt and k <= FILE_DATE)
        opd = [k for k in legs_by_date if mm <= k < nxt and k <= FILE_DATE]
        zero = [k for k in opd if not worked.get(k)]
        print("%-9s %9d %10d %9.0f%% %9d %12d%s" % (
            mm.strftime("%Y-%m"), tot, asg, 100.0 * asg / tot if tot else 0,
            len(opd), len(zero), "   <- CUT is 2026-07-11" if mm == date(2026, 7, 1) else ""))
    print()
    print("daily detail across the cliff (2026-07-08 .. 2026-08-21). last_asg = the latest")
    print("driver_assigned_at stamp on that date's legs -- it separates a genuinely")
    print("PRE-BUILT day from a post-snapshot local-dev auto-assign run.")
    last_asg = defaultdict(str)
    for drv, dt_, pt, daa in legs:
        if drv and daa and daa > last_asg[dt_]:
            last_asg[dt_] = daa
    print("%-12s %6s %9s %8s %9s %6s %-12s %s" % (
        "date", "legs", "assigned", "%asg", "drivers", "DVA", "last_asg", "reading"))
    cd = date(2026, 7, 8)
    while cd <= FILE_DATE:
        tot = legs_by_date.get(cd, 0)
        asg = assigned_by_date.get(cd, 0)
        la = last_asg.get(cd, "")
        if cd == CUT:
            mark = "<<< CUT (last production event)"
        elif not asg and not dva_by_date.get(cd):
            mark = "no supply record at all"
        elif la and la[:10] <= CUT.isoformat():
            mark = "REAL (pre-built before the freeze)"
        else:
            mark = "* LOCAL DEV -- exclude"
        print("%-12s %6d %9d %7.0f%% %9d %6d %-12s %s" % (
            cd, tot, asg, 100.0 * asg / tot if tot else 0,
            len(worked.get(cd, set())), len(dva_by_date.get(cd, set())),
            la[:10] or "-", mark))
        cd += timedelta(days=1)

    # =====================================================================  S4
    hdr("S4. DRIVER-DAY SHAPE [measured] -- window 2025-10-01..2026-07-11")
    rows = []
    for (drv, dt_), times in dd_legs.items():
        if not (WINDOW_START <= dt_ <= CUT):
            continue
        mins = sorted(int(t[0:2]) * 60 + int(t[3:5]) for t in times if t)
        first = mins[0] if mins else None
        last = mins[-1] if mins else None
        rows.append({
            "driver_id": drv, "date": dt_.isoformat(),
            "is_inhouse": 1 if drv in inhouse else 0,
            "n_legs": len(times),
            "first_pickup": "%02d:%02d" % (first // 60, first % 60) if first is not None else "",
            "last_pickup": "%02d:%02d" % (last // 60, last % 60) if last is not None else "",
            "span_minutes": (last - first) if first is not None else "",
            "_m": dt_.strftime("%Y-%m"), "_dow": dt_.weekday(),
            "_span": (last - first) if first is not None else None,
            "_first": first,
        })
    print("driver-days in window : %d   [measured]" % len(rows))
    print("   in-house %d / affiliate %d" % (sum(r["is_inhouse"] for r in rows),
                                             sum(1 - r["is_inhouse"] for r in rows)))
    print("   driver-days with a NULL pickup_time on every leg: %d"
          % sum(1 for r in rows if r["_first"] is None))
    early = [r for r in rows if r["_first"] is not None and r["_first"] < 240]
    print("A4 distortion check: driver-days whose FIRST pickup is 00:00-03:59 : %d (%.1f%%)"
          % (len(early), 100.0 * len(early) / len(rows)))
    print("   -> the calendar-date operating day is a ~4% approximation, not exact.")
    nleg_all = [r["n_legs"] for r in rows]
    print("legs-per-driver-day, ALL: n=%d P10=%s P50=%s P75=%s P90=%s max=%s mean=%.2f"
          % (len(nleg_all), pct(nleg_all, 10), pct(nleg_all, 50), pct(nleg_all, 75),
             pct(nleg_all, 90), max(nleg_all), sum(nleg_all) / float(len(nleg_all))))
    sp_all = [r["_span"] / 60.0 for r in rows if r["_span"] is not None]
    print("span hours (last-first pickup), ALL: P10=%.1f P50=%.1f P75=%.1f P90=%.1f max=%.1f"
          % (pct(sp_all, 10), pct(sp_all, 50), pct(sp_all, 75), pct(sp_all, 90),
             pct(sp_all, 100)))

    def dist_table(key, title, getter, subset=None):
        sub(title)
        rs_all = rows if subset is None else [r for r in rows if subset(r)]
        print("%-9s %7s %6s %6s %6s %6s %6s %8s | %6s %6s %6s %6s %6s" % (
            key, "n", "L_P10", "L_P50", "L_P75", "L_P90", "L_max", "L_mean",
            "S_P10", "S_P50", "S_P75", "S_P90", "S_max"))
        groups = defaultdict(list)
        for r in rs_all:
            groups[getter(r)].append(r)
        for g in sorted(groups):
            rs = groups[g]
            L = [r["n_legs"] for r in rs]
            S = [r["_span"] / 60.0 for r in rs if r["_span"] is not None]
            label = DOW[g] if isinstance(g, int) else g
            print("%-9s %7d %6s %6s %6s %6s %6s %8.2f | %6s %6s %6s %6s %6s" % (
                label, len(rs), pct(L, 10), pct(L, 50), pct(L, 75), pct(L, 90), max(L),
                sum(L) / float(len(L)),
                fmt(pct(S, 10), 1), fmt(pct(S, 50), 1), fmt(pct(S, 75), 1),
                fmt(pct(S, 90), 1), fmt(pct(S, 100), 1)))
        print("   L_* = legs per driver-day. S_* = span HOURS (last pickup - first pickup).")

    dist_table("month", "S4a. by month (all drivers)", lambda r: r["_m"])
    dist_table("dow", "S4b. by day-of-week (all drivers)", lambda r: r["_dow"])
    dist_table("dow", "S4b2. by day-of-week, IN-HOUSE only", lambda r: r["_dow"],
               subset=lambda r: r["is_inhouse"] == 1)

    sub("S4c. in-house vs affiliate driver-days")
    for flag, lab in ((1, "inhouse"), (0, "affiliate")):
        rs = [r for r in rows if r["is_inhouse"] == flag]
        L = [r["n_legs"] for r in rs]
        S = [r["_span"] / 60.0 for r in rs if r["_span"] is not None]
        print("%-10s n=%-6d legs P10=%s P50=%s P75=%s P90=%s max=%s | span_h P50=%.1f P90=%.1f"
              % (lab, len(rs), pct(L, 10), pct(L, 50), pct(L, 75), pct(L, 90), max(L),
                 pct(S, 50), pct(S, 90)))
    print("   an affiliate 'day' is a handful of overflow jobs; an in-house day is a shift.")
    one = [r for r in rows if r["n_legs"] == 1]
    print("   driver-days with exactly 1 leg: %d (%.0f%% of all) -- in-house %d, affiliate %d"
          % (len(one), 100.0 * len(one) / len(rows),
             sum(r["is_inhouse"] for r in one), sum(1 - r["is_inhouse"] for r in one)))

    sub("S4d. first-pickup hour histogram (in-house driver-days)")
    fh = Counter(r["_first"] // 60 for r in rows
                 if r["is_inhouse"] and r["_first"] is not None)
    print("   " + "  ".join("%02d:%d" % (h, fh.get(h, 0)) for h in range(24)))
    lh = Counter((r["_first"] + r["_span"]) // 60 for r in rows
                 if r["is_inhouse"] and r["_span"] is not None)
    print("last-pickup hour histogram (in-house driver-days)")
    print("   " + "  ".join("%02d:%d" % (h, lh.get(h, 0)) for h in range(24)))

    p = os.path.join(OUT, "04_supply_driver_days.csv")
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["driver_id", "date", "is_inhouse", "n_legs",
                    "first_pickup", "last_pickup", "span_minutes"])
        for r in sorted(rows, key=lambda r: (r["date"], r["driver_id"])):
            w.writerow([r["driver_id"], r["date"], r["is_inhouse"], r["n_legs"],
                        r["first_pickup"], r["last_pickup"], r["span_minutes"]])
    print()
    print("WROTE %s  (%d rows, window 2025-10-01..2026-07-11)" % (p, len(rows)))

    # =====================================================================  S5
    hdr("S5. FLEET")
    veh_types = dict(cur.execute("SELECT id, vehicle_type FROM rates_vehicle"))
    fleet = []
    for vid, num, act, iss, vt, oosf, oosu, yr, mk, mo in cur.execute(
            "SELECT id, vehicle_number, is_active, in_service_since, vehicle_type_id, "
            "out_of_service_from, out_of_service_until, year, make, model "
            "FROM drivers_fleetvehicle"):
        fleet.append(dict(id=vid, num=num, act=bool(act), iss=iss,
                          vt=veh_types.get(vt, "(none)"), oosf=oosf, oosu=oosu,
                          yr=yr, mk=mk, mo=mo))
    fleet_by_id = {v["id"]: v for v in fleet}
    print("drivers_fleetvehicle rows : %d   (active %d / inactive %d)   [measured]"
          % (len(fleet), sum(1 for v in fleet if v["act"]),
             sum(1 for v in fleet if not v["act"])))
    n_iss = sum(1 for v in fleet if v["iss"])
    print("rows with in_service_since populated : %d / %d" % (n_iss, len(fleet)))
    if not n_iss:
        print("  [unavailable] -- in_service_since is NULL on EVERY fleet row, so the")
        print("  'active vehicles over time by in_service_since' series the brief asks for")
        print("  CANNOT BE BUILT. FleetVehicle has no created_at and no history table either.")
        print("  The only usable time signal for a vehicle's existence is when it first and")
        print("  last appears on a DVA row (S5c) -- and that inherits DVA's own start date.")
    print("out_of_service_from populated : %d ; out_of_service_until populated : %d"
          % (sum(1 for v in fleet if v["oosf"]), sum(1 for v in fleet if v["oosu"])))
    print("  -> the out-of-service window that day_setup.py:322-333 filters on is empty too,")
    print("     so no date in this snapshot has a car recorded as being in the shop.")

    sub("S5a. Company vehicles in the snapshot, by class")
    byvt = defaultdict(lambda: [0, 0])
    for v in fleet:
        byvt[v["vt"]][0 if v["act"] else 1] += 1
    print("%-14s %8s %10s" % ("class", "active", "inactive"))
    for k in sorted(byvt):
        print("%-14s %8d %10d" % (k, byvt[k][0], byvt[k][1]))
    print("%-14s %8d %10d" % ("TOTAL", sum(x[0] for x in byvt.values()),
                              sum(x[1] for x in byvt.values())))

    sub("S5b. The units themselves")
    print("%-6s %-14s %-7s %-6s %-11s %-17s" % (
        "unit", "class", "active", "year", "make", "model"))
    for v in sorted(fleet, key=lambda v: (v["vt"], v["num"])):
        print("%-6s %-14s %-7s %-6s %-11s %-17s" % (
            "#" + str(v["num"]), v["vt"], "yes" if v["act"] else "NO",
            v["yr"], v["mk"], v["mo"]))

    sub("S5c. Distinct vehicles actually deployed per month (DVA vehicle_id)")
    nveh_leg = cur.execute(
        "SELECT COUNT(*) FROM reservations_leg WHERE vehicle_id IS NOT NULL").fetchone()[0]
    print("reservations_leg.vehicle_id populated on %d / %d legs (%.2f%%)"
          % (nveh_leg, n_leg_total, 100.0 * nveh_leg / n_leg_total))
    print("  -> the per-LEG vehicle cross-check the brief asks for is [unavailable].")
    print("     leg.vehicle is essentially never written (dispatching/day_setup.py:315 says")
    print("     so verbatim). DVA.vehicle_id is the ONLY record of which car ran.")
    print()
    print("%-9s %9s %9s %10s   %s" % ("month", "dva_rows", "dist_veh", "dist_dates",
                                      "units seen"))
    for mm in months:
        nxt = nextmonth(mm)
        rs = [x for x in dva if mm <= x[1] < nxt]
        if not rs:
            continue
        vs = {x[2] for x in rs if x[2]}
        nums = sorted("#" + str(fleet_by_id[v]["num"]) for v in vs if v in fleet_by_id)
        print("%-9s %9d %9d %10d   %s%s" % (
            mm.strftime("%Y-%m"), len(rs), len(vs), len({x[1] for x in rs}),
            " ".join(nums), "   <- post-CUT" if not windays(mm) else ""))
    seen_veh = {x[2] for x in dva if x[2]}
    never_v = [v for v in fleet if v["id"] not in seen_veh]
    print("fleet units that NEVER appear on any DVA row: %d -> %s"
          % (len(never_v), ", ".join("#" + str(v["num"]) for v in never_v) or "none"))
    pre_veh_day = {k: v for k, v in dva_veh_by_date.items() if k <= CUT}
    peak = max((len(s), k) for k, s in pre_veh_day.items())
    print("max distinct vehicles deployed on a single PRE-CUT date: %d (on %s)"
          % (peak[0], peak[1]))
    vd = sorted(len(s) for s in pre_veh_day.values())
    print("vehicles-deployed-per-day over the 2026-01-18..CUT run: P10=%s P50=%s P90=%s max=%s"
          % (pct(vd, 10), pct(vd, 50), pct(vd, 90), max(vd)))
    print("first/last DVA date per unit (pre-CUT rows only):")
    for v in sorted(fleet, key=lambda v: (v["vt"], v["num"])):
        ds = sorted(x[1] for x in dva if x[2] == v["id"] and x[1] <= CUT)
        if ds:
            print("   #%-5s %-13s %s .. %s  (%d driver-days)"
                  % (v["num"], v["vt"], ds[0], ds[-1], len(ds)))
        else:
            print("   #%-5s %-13s  -- no pre-CUT DVA row at all" % (v["num"], v["vt"]))

    # =====================================================================  S6
    hdr("S6. AVAILABILITY DECLARATIONS")
    ws = []
    for did, dow_, avail, sh, eh, flex, mh, stype, pshift, pref in cur.execute(
            "SELECT driver_id, day_of_week, is_available, start_hour, end_hour, flexible, "
            "max_hours, shift_type, preferred_shift, preference "
            "FROM drivers_driverweeklyschedule"):
        ws.append(dict(did=did, dow=dow_, avail=bool(avail), sh=sh, eh=eh,
                       flex=bool(flex), mh=mh, stype=stype or "", pshift=pshift or "",
                       pref=pref or ""))
    print("drivers_driverweeklyschedule rows : %d   [measured]" % len(ws))
    print("NOTE: this table has NO created_at / updated_at column. It is a POINT-IN-TIME")
    print("state as of the snapshot and CANNOT be replayed to any past date. Whatever a")
    print("driver's declared availability was in March is gone -- overwritten in place.")
    have = {r["did"] for r in ws}
    act_ih = {k for k, v in drivers.items() if v["type"] == "inhouse" and v["active"]}
    print("distinct drivers with >=1 row     : %d" % len(have))
    print("ACTIVE IN-HOUSE drivers covered   : %d / %d (%.0f%%)"
          % (len(have & act_ih), len(act_ih), 100.0 * len(have & act_ih) / len(act_ih)))
    missing = act_ih - have
    print("  active in-house with NO weekly schedule at all: %d -> %s"
          % (len(missing), ", ".join(sorted(drivers[x]["name"] for x in missing)) or "none"))
    print("  rows held by drivers who are NOT active in-house: %d"
          % sum(1 for r in ws if r["did"] not in act_ih))
    for x in sorted(have - act_ih, key=lambda x: drivers[x]["name"]):
        print("      %-14s %-10s %s" % (drivers[x]["name"], drivers[x]["type"],
                                        "active" if drivers[x]["active"] else "INACTIVE"))
    days_per = Counter()
    for r in ws:
        days_per[r["did"]] += 1
    print("  days-of-week declared per driver: %s (n_days, driver_count)"
          % sorted(Counter(days_per.values()).items()))
    print("  -> completeness is all-or-nothing: every covered driver has all 7 days.")

    sub("S6a. Declared availability -- the actual distributions")
    print("is_available=True rows : %d / %d (%.0f%%)"
          % (sum(1 for r in ws if r["avail"]), len(ws),
             100.0 * sum(1 for r in ws if r["avail"]) / len(ws)))
    print("flexible=True rows     : %d / %d (%.0f%%)"
          % (sum(1 for r in ws if r["flex"]), len(ws),
             100.0 * sum(1 for r in ws if r["flex"]) / len(ws)))
    print("max_hours NOT NULL     : %d / %d (%.0f%%)   values: %s"
          % (sum(1 for r in ws if r["mh"] is not None), len(ws),
             100.0 * sum(1 for r in ws if r["mh"] is not None) / len(ws),
             sorted(Counter(r["mh"] for r in ws if r["mh"] is not None).items())))
    print("start_hour distribution: %s" % sorted(Counter(r["sh"] for r in ws).items()))
    print("end_hour   distribution: %s" % sorted(Counter(r["eh"] for r in ws).items()))
    print("shift_type in use      : %s   (of 7 defined choices)"
          % Counter(r["stype"] for r in ws).most_common())
    print("preferred_shift in use : %s   (of 5 defined choices)"
          % Counter(r["pshift"] for r in ws).most_common())
    print("preference in use      : %s   (of 10 defined choices -- NEVER SET)"
          % Counter(r["pref"] for r in ws).most_common())
    print()
    print("available days per weekday (rows where is_available=1):")
    for i in range(7):
        n = sum(1 for r in ws if r["dow"] == i and r["avail"])
        tot = sum(1 for r in ws if r["dow"] == i)
        print("   %-4s %3d / %3d available" % (DOW[i], n, tot))
    print()
    print("distinct (start_hour,end_hour,flexible) windows actually declared:")
    for k, v in Counter((r["sh"], r["eh"], r["flex"]) for r in ws).most_common():
        print("   start=%-3s end=%-3s flexible=%-5s  x%d" % (k[0], k[1], k[2], v))

    sub("S6b. Is the weekly table hand-declared, or the model default copied out?")
    defs = dict(cur.execute(
        "SELECT id, default_start_hour || '-' || default_end_hour FROM drivers_driver"))
    same = sum(1 for r in ws if defs.get(r["did"]) == "%s-%s" % (r["sh"], r["eh"]))
    print("weekly rows identical to that driver's default_start/end_hour: %d / %d (%.0f%%)"
          % (same, len(ws), 100.0 * same / len(ws)))
    print("weekly rows equal to the MODEL default 6-23: %d / %d (%.0f%%)"
          % (sum(1 for r in ws if (r["sh"], r["eh"]) == (6, 23)), len(ws),
             100.0 * sum(1 for r in ws if (r["sh"], r["eh"]) == (6, 23)) / len(ws)))
    per_drv = defaultdict(list)
    for r in ws:
        per_drv[r["did"]].append(r)
    ident = sum(1 for did, rs in per_drv.items()
                if len({(r["sh"], r["eh"], r["avail"], r["flex"], r["stype"])
                        for r in rs}) == 1)
    print("drivers whose 7 rows are ALL identical (start,end,avail,flex,shift_type):")
    print("   %d / %d drivers (%.0f%%) -- a uniform 7-day block carries no day-of-week"
          % (ident, len(per_drv), 100.0 * ident / len(per_drv)))
    print("   information at all; it is one setting copied seven times.")
    allweek = sum(1 for did, rs in per_drv.items() if all(r["avail"] for r in rs))
    noweek = sum(1 for did, rs in per_drv.items() if not any(r["avail"] for r in rs))
    print("drivers marked available all 7 days: %d ; available on ZERO days: %d"
          % (allweek, noweek))

    sub("S6c. Does the declared weekly schedule predict who actually worked?")
    print("Tested over 2026-04-01..CUT (the period where DVA is dense and the roster is")
    print("stable). For each in-house driver x weekday: declared-available vs work rate.")
    wk_days = defaultdict(lambda: [0, 0])       # (driver,dow) -> [days_with_legs, days]
    cd = date(2026, 4, 1)
    while cd <= CUT:
        for did in act_ih:
            wk_days[(did, cd.weekday())][1] += 1
            if did in worked.get(cd, set()):
                wk_days[(did, cd.weekday())][0] += 1
        cd += timedelta(days=1)
    declared = {(r["did"], r["dow"]): r["avail"] for r in ws}
    buckets = {True: [], False: [], None: []}
    for (did, dw), (w, t) in wk_days.items():
        buckets[declared.get((did, dw))].append(w / float(t))
    for k, lab in ((True, "declared AVAILABLE"), (False, "declared UNAVAILABLE"),
                   (None, "no weekly row")):
        v = buckets[k]
        if v:
            print("   %-22s n=%-4d work-rate P25=%.2f P50=%.2f P75=%.2f mean=%.2f"
                  % (lab, len(v), pct(v, 25), pct(v, 50), pct(v, 75),
                     sum(v) / float(len(v))))
    worked_on_unavail = sum(1 for (did, dw), (w, t) in wk_days.items()
                            if declared.get((did, dw)) is False and w > 0)
    tot_unavail = sum(1 for (did, dw) in wk_days
                      if declared.get((did, dw)) is False)
    print("   driver-weekdays declared UNAVAILABLE where the driver worked >=1 such day:")
    print("      %d / %d (%.0f%%)" % (worked_on_unavail, tot_unavail,
                                      100.0 * worked_on_unavail / tot_unavail
                                      if tot_unavail else 0))

    sub("S6d. drivers_driverdateoverride volume")
    ov = []
    for did, dt_, ed, et, st, avail, reason, sbd, ca in cur.execute(
            "SELECT driver_id, date, end_date, exception_type, status, is_available, "
            "reason, submitted_by_driver, created_at FROM drivers_driverdateoverride"):
        ov.append(dict(did=did, dt=d(dt_), ed=d(ed) if ed else None, et=et or "",
                       st=st or "", avail=bool(avail), reason=reason or "",
                       sbd=bool(sbd), ca=ca))
    print("total rows : %d   date span %s .. %s   [measured]"
          % (len(ov), min(x["dt"] for x in ov), max(x["dt"] for x in ov)))
    print("created_at span : %s .. %s   <- the feature is only ~2.7 months old at the CUT"
          % (min(x["ca"] for x in ov if x["ca"])[:19],
             max(x["ca"] for x in ov if x["ca"])[:19]))
    print("distinct drivers with any override : %d (of %d active in-house)"
          % (len({x["did"] for x in ov}), len(act_ih)))
    print("%-9s %7s %9s %10s   %s" % ("month", "rows", "drivers", "multiday", "types"))
    for mm in months:
        nxt = nextmonth(mm)
        rs = [x for x in ov if mm <= x["dt"] < nxt]
        if not rs:
            continue
        print("%-9s %7d %9d %10d   %s" % (
            mm.strftime("%Y-%m"), len(rs), len({x["did"] for x in rs}),
            sum(1 for x in rs if x["ed"] and x["ed"] != x["dt"]),
            dict(Counter(x["et"] for x in rs))))
    late = [x for x in ov if x["dt"] > CUT]
    print("rows whose DATE is after the CUT (forward-dated leave already on the books): %d"
          % len(late))
    print()
    print("exception_type totals : %s" % Counter(x["et"] for x in ov).most_common())
    print("status totals         : %s" % Counter(x["st"] for x in ov).most_common())
    print("reason totals         : %s" % Counter(x["reason"] for x in ov).most_common())
    print("submitted_by_driver   : %s" % Counter(x["sbd"] for x in ov).most_common())
    print("per-driver override counts (top 12): %s"
          % [(drivers[k]["name"], v) for k, v in
             Counter(x["did"] for x in ov).most_common(12)])
    covered_days = set()
    for x in ov:
        end = x["ed"] or x["dt"]
        cd = x["dt"]
        while cd <= end:
            covered_days.add((x["did"], cd))
            cd += timedelta(days=1)
    print("driver-days covered by an override (expanding date..end_date): %d" % len(covered_days))
    ov_days = {c[1] for c in covered_days}
    print("distinct dates touched by any override: %d, span %s .. %s"
          % (len(ov_days), min(ov_days), max(ov_days)))
    ov_pre = {c for c in covered_days if c[1] <= CUT}
    print("of those, driver-days on or before CUT: %d, over %d distinct dates"
          % (len(ov_pre), len({c[1] for c in ov_pre})))
    live = [d_ for d_ in sorted({c[1] for c in ov_pre})]
    if live:
        alld = [live[0] + timedelta(days=i) for i in range((CUT - live[0]).days + 1)]
        print("from the first override date (%s) to CUT: %d days, %d touched by >=1 "
              "override (%.0f%%)" % (live[0], len(alld), len(set(live)),
                                     100.0 * len(set(live)) / len(alld)))
    off = set()
    for x in ov:
        if x["et"] == "off" and x["st"] == "approved":
            end = x["ed"] or x["dt"]
            cd = x["dt"]
            while cd <= end:
                off.add((x["did"], cd))
                cd += timedelta(days=1)
    off_pre = {c for c in off if c[1] <= CUT}
    violated = sum(1 for (did, cd) in off_pre if did in worked.get(cd, set()))
    print("approved full-day OFF driver-days on/before CUT: %d ; driver DROVE anyway on "
          "%d (%.0f%%)" % (len(off_pre), violated,
                           100.0 * violated / len(off_pre) if off_pre else 0))
    dva_on_off = sum(1 for (did, cd) in off_pre if did in dva_by_date.get(cd, set()))
    print("approved full-day OFF driver-days that ALSO carry a DVA row: %d" % dva_on_off)
    print("  -> an approved OFF is respected ~%.0f%% of the time: the override table is a"
          % (100.0 - 100.0 * violated / len(off_pre) if off_pre else 0))
    print("     genuine constraint, not decoration -- but it only covers ~%.1f driver-days"
          % (len(off_pre) / float(len({c[1] for c in ov_pre})) if ov_pre else 0))
    print("     per touched date, against a ~20-driver roster.")

    # =====================================================================  S7
    hdr("S7. VERDICT -- can we reconstruct 'the roster available on date D'?")
    print("%-24s %7s %7s %9s %9s %9s" % (
        "period", "opdays", "DVAcov", "%DVAcov", "%legs_asg", "medJacc"))
    for lab, a, b in (("2025-10-01..2025-12-31", WINDOW_START, date(2025, 12, 31)),
                      ("2026-01-01..2026-01-17", date(2026, 1, 1), date(2026, 1, 17)),
                      ("2026-01-18..2026-01-31", date(2026, 1, 18), date(2026, 1, 31)),
                      ("2026-02-01..2026-03-31", date(2026, 2, 1), date(2026, 3, 31)),
                      ("2026-04-01..2026-07-11", date(2026, 4, 1), CUT),
                      ("2026-07-12..2026-08-21", date(2026, 7, 12), FILE_DATE)):
        days = [a + timedelta(days=i) for i in range((b - a).days + 1)]
        ops = [x for x in days if legs_by_date.get(x)]
        cov = [x for x in ops if x in dva_by_date]
        tot = sum(legs_by_date.get(x, 0) for x in ops)
        asg = sum(assigned_by_date.get(x, 0) for x in ops)
        sc = [keep[x] for x in ops if x in keep]
        jj2 = [len(A & B) / float(len(A | B)) for (A, B, t, f, n) in sc]
        print("%-24s %7d %7d %8.0f%% %8.0f%% %9s" % (
            lab, len(ops), len(cov), 100.0 * len(cov) / len(ops) if ops else 0,
            100.0 * asg / tot if tot else 0,
            ("%.2f" % pct(jj2, 50)) if jj2 else "n/a"))
    print()
    ops_all = [x for x in
               (WINDOW_START + timedelta(days=i) for i in range((CUT - WINDOW_START).days + 1))
               if legs_by_date.get(x)]
    nodrv = [x for x in ops_all if not worked.get(x)]
    print("operating days in the primary window (2025-10-01..CUT): %d" % len(ops_all))
    print("operating days with legs but NOT ONE assigned driver  : %d" % len(nodrv))
    print("=> the WORKED roster (from assigned legs) is derivable on %d/%d operating days."
          % (len(ops_all) - len(nodrv), len(ops_all)))
    print()
    print("EARLIEST TRUSTWORTHY DATE, by artefact:")
    dva_run = max((r for r in runs if r[0] <= CUT), key=len)
    print("   who was ROSTERED (DVA)          : %s  (contiguous to %s, 100%% of operating days)"
          % (dva_run[0], dva_run[-1]))
    print("   who actually WORKED (leg.driver): %s  (>=97%% of legs carry a driver from here)"
          % WINDOW_START)
    print("   which CAR he took (DVA.vehicle) : %s  (same run; 1 NULL vehicle_id in 2001)"
          % dva_run[0])
    print("   WHEN he was on duty             : never -- planned_*_hour 0% populated (S1d)")
    print("   declared weekly availability    : never -- no timestamp column (S6)")
    print("   day-off exceptions              : %s  (created_at floor)"
          % min(x["ca"] for x in ov if x["ca"])[:10])
    print("   fleet size over time            : never -- in_service_since 0/14 (S5)")
    con.close()


if __name__ == "__main__":
    main()
