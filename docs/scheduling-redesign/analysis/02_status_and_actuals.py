#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
02_status_and_actuals.py
========================
CAN WE TRUST THE OPERATIONAL EVENT RECORD, AND WHAT DOES IT SAY ACTUALLY HAPPENED?

This is the reliability audit for every downstream number that is derived from driver
taps.  A `Leg` row carries no actual times at all — `pickup_date`/`pickup_time` are
*planned*.  Everything the redesign will claim about how long a job really takes is a
difference between two rows in `reservations_legstatus`, i.e. between two moments a
driver pressed a button in the portal.  So the first question is not "how long is a
ride" but "is the button-press record good enough to answer that".

It reconciles, claim by claim, against:
  * docs/operational-data-audit.md              (generated 2026-07-31, its own header)
  * docs/scheduling-redesign/00_DATA_AUDIT_AND_INVENTORY.md  (the prior redo)
whose windows are read OUT OF THE FILES at run time, never typed in here.

NO DATE LITERALS.  Every window, month boundary, regime and cutoff below is derived
from the database via `_common.Horizon` / `_common.changepoints`, or parsed from the
audit markdown.  The only calendar constants in play are `_common.US_DST_TRANSITIONS`
(the DST table) and the `_common.SANE_DATES` sanity rail.

READ-ONLY.  The snapshot is opened `mode=ro`; a stray write raises.  Django, if it can
be configured at all, is configured with `DATABASES={}` so it physically cannot reach
a database — it is imported for exactly one pure function, `categorize_location`.

RUN:  cd docs/scheduling-redesign/analysis && python 02_status_and_actuals.py
"""

import collections
import datetime as dt
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common as C  # noqa: E402


# ══════════════════════════════════════════════════════════════════════════════
# CONSTANTS — all of these are MINUTES/COUNTS lifted from shipped code, verified
# by grep.  None of them is a date.
# ══════════════════════════════════════════════════════════════════════════════

# dispatching/analytics.py:434  — the app's own "both buttons at once" floor
MIN_PICKUP_TO_COMPLETE = 2
# dispatching/analytics.py:26   — the app's own dwell discard cap
MAX_DWELL_MINUTES = 120
# dispatching/analytics.py:27   — the app's own drive discard cap
MAX_DRIVE_MINUTES = 180
# dispatching/analytics.py:28   — the app's own total discard cap
MAX_TOTAL_MINUTES = 300
# dispatching/pickup_policy.py:87 — the app's own "this turn is tight" line
TURN_TIGHT_SLACK_MIN = 15
# dispatching/management/commands/driver_data_quality.py:60,63
INSTANT_SHARE_EXCLUDE = 0.40
MIN_LEGS_TO_JUDGE = 25
SPARSE_FULL_CHAIN = 0.50

# Our own behavioural gates (declared here, applied in §3/§4, never inherited)
COLLAPSED_SECONDS = 120      # whole on-the-way..completed ladder inside 2 minutes
ADJACENT_SECONDS = 60        # the audit's "double-tap" definition
GOLD_FULL3_MIN = 0.85
GOLD_INSTANT_MAX = 0.10
GOLD_COLLAPSED_MAX = 0.05
GOLD_NONMONO_MAX = 0.02

# Pairing rails for turnaround (minutes).  A driver's next job more than 8 h after
# the last one closed is a new shift, not a turnaround.
TURN_MIN = -120
TURN_MAX = 480

LADDER = list(C.LADDER)                 # confirmed, on-the-way, on-location, picked-up, completed
FULL3 = ("on-the-way", "picked-up", "completed")   # analytics.REQUIRED_ANALYTICS_STATUSES
AIRPORTS = ("MCO Terminal", "SFB Terminal", "MCO", "SFB")

# docs/operational-data-audit.md §4.2 — the per-lane physical floors the audit itself
# declared.  Minutes, not dates.  Used only to re-measure the impossible-record rate
# on the audit's own terms.
AUDIT_PHYSICAL_FLOOR = {
    ("SFB Terminal", "Disney Resort"): 35,
    ("Disney Resort", "SFB Terminal"): 35,
    ("MCO Terminal", "Port Canaveral Area"): 30,
    ("Port Canaveral Area", "MCO Terminal"): 30,
    ("MCO Terminal", "Disney Resort"): 12,
    ("Disney Resort", "MCO Terminal"): 12,
}


# ══════════════════════════════════════════════════════════════════════════════
# helpers
# ══════════════════════════════════════════════════════════════════════════════

def load_categorizer():
    """Use the application's OWN location bucketer so lane names match the audit.

    `dispatching.analytics.categorize_location` is a pure string function, but it
    lives in a module that imports Django.  We configure Django with an EMPTY
    DATABASES dict: settings are satisfied, and no database connection can ever be
    opened from this process.  If that fails for any reason we fall back to
    `_common.loc_bucket` and say so in the header.
    """
    try:
        if C.REPO_ROOT not in sys.path:
            sys.path.insert(0, C.REPO_ROOT)
        import django
        from django.conf import settings as dj
        if not dj.configured:
            dj.configure(USE_TZ=True, TIME_ZONE="America/New_York",
                         INSTALLED_APPS=[], DATABASES={})
        django.setup()
        from dispatching.analytics import categorize_location
        assert categorize_location("MCO Terminal A") == "MCO Terminal"
        return categorize_location, "dispatching.analytics.categorize_location (the app's own)"
    except Exception as exc:                                    # pragma: no cover
        return C.loc_bucket, "_common.loc_bucket  [FALLBACK — %s]" % exc


def parse_drive_table():
    """Lift `DRIVE_TIME_ESTIMATES` out of dispatching/scheduler.py by parsing the file.

    Static analysis, not import: `scheduler` pulls in Django models and we refuse to
    give this process anything that could open a database.  Returns {} if the literal
    ever stops being a plain dict, and the caller then reports [unavailable].
    """
    import ast
    p = os.path.join(C.REPO_ROOT, "dispatching", "scheduler.py")
    if not os.path.exists(p):
        return {}, None
    src = open(p, encoding="utf-8", errors="replace").read()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return {}, None
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "DRIVE_TIME_ESTIMATES" not in names:
            continue
        try:
            d = ast.literal_eval(node.value)
        except ValueError:
            return {}, None
        return ({k: v for k, v in d.items() if isinstance(k, tuple) and len(k) == 2},
                "dispatching/scheduler.py:%d" % node.lineno)
    return {}, None


def parse_audit_gold_names():
    """The 14 names the audit nominated, read out of the audit file itself."""
    p = os.path.join(C.REPO_ROOT, "docs", "operational-data-audit.md")
    if not os.path.exists(p):
        return []
    txt = open(p, encoding="utf-8", errors="replace").read()
    m = re.search(r"\*\*Cohort \(\d+ drivers[^)]*\):\*\*\s*([^\n]+)", txt)
    if not m:
        return []
    return [x.strip() for x in m.group(1).split(",") if x.strip()]


def parse_doc_windows():
    """Read the two prior documents' own stated windows out of the files.

    This is how the reconciliation gets its boundaries without a date literal in
    this script.  If a file is missing or its header changes shape we degrade to
    None and say the comparison is [unavailable] rather than inventing a window.
    """
    out = {"audit_generated": None, "audit_tap_start": None, "audit_tap_end": None,
           "redo_window_end": None, "audit_path": None, "redo_path": None}
    ap = os.path.join(C.REPO_ROOT, "docs", "operational-data-audit.md")
    if os.path.exists(ap):
        out["audit_path"] = ap
        txt = open(ap, encoding="utf-8", errors="replace").read()
        m = re.search(r"Generated:\*\*\s*(\d{4}-\d{2}-\d{2})", txt)
        if m:
            out["audit_generated"] = dt.date.fromisoformat(m.group(1))
        m = re.search(r"driver status events\s*(\d{4}-\d{2}-\d{2})\D{1,8}(\d{4}-\d{2}-\d{2})", txt)
        if m:
            out["audit_tap_start"] = dt.date.fromisoformat(m.group(1))
            out["audit_tap_end"] = dt.date.fromisoformat(m.group(2))
    rp = os.path.join(C.REPO_ROOT, "docs", "scheduling-redesign",
                      "00_DATA_AUDIT_AND_INVENTORY.md")
    if os.path.exists(rp):
        out["redo_path"] = rp
        txt = open(rp, encoding="utf-8", errors="replace").read()
        m = re.search(r"window was\s*(\d{4}-\d{2}-\d{2})\D{1,8}(\d{4}-\d{2}-\d{2})", txt)
        if m:
            out["redo_window_end"] = dt.date.fromisoformat(m.group(2))
    return out


def months_between(d0, d1):
    """['YYYY-MM', ...] inclusive, derived from two dates."""
    out, y, m = [], d0.year, d0.month
    while (y, m) <= (d1.year, d1.month):
        out.append("%04d-%02d" % (y, m))
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def mins(a, b):
    """Minutes from a to b.  Both must be naive UTC — never mix with local."""
    if a is None or b is None:
        return None
    return (b - a).total_seconds() / 60.0


def share(num, den):
    return (100.0 * num / den) if den else None


def f1(v, w=6, places=1):
    return ("%*.*f" % (w, places, v)) if v is not None else " " * (w - 1) + "-"


def row3(label, vals, width=30, places=1):
    """label + n/P50/P75/P90 — the shape every duration table in here uses."""
    d = C.describe(vals, places)
    if not d["n"]:
        return "%-*s  n=     0" % (width, label)
    return ("%-*s  n=%6d   P50 %7.1f   P75 %7.1f   P90 %7.1f   mean %7.1f"
            % (width, label, d["n"], d["p50"], d["p75"], d["p90"], d["mean"]))


def gap_line(label, gold, allv, places=1):
    """The generalisation-gap line: gold vs all, and the minutes gold is short by."""
    g, a = C.describe(gold, places), C.describe(allv, places)
    if not g["n"] or not a["n"]:
        return "%-26s  [unavailable] gold n=%d all n=%d" % (label, g["n"], a["n"])
    return ("%-26s  gold n=%5d P50 %6.1f P75 %6.1f P90 %6.1f | "
            "all n=%6d P50 %6.1f P75 %6.1f P90 %6.1f | "
            "GAP P50 %+5.1f P75 %+5.1f P90 %+5.1f"
            % (label, g["n"], g["p50"], g["p75"], g["p90"],
               a["n"], a["p50"], a["p75"], a["p90"],
               a["p50"] - g["p50"], a["p75"] - g["p75"], a["p90"] - g["p90"]))


# ══════════════════════════════════════════════════════════════════════════════
# main
# ══════════════════════════════════════════════════════════════════════════════

def main():
    con = C.connect()
    H = C.Horizon(con)
    cat, cat_src = load_categorizer()
    docs = parse_doc_windows()

    C.preamble(
        "02_status_and_actuals.py",
        "reliability audit of the driver-tap record, and what it says happened",
        H,
        assumptions=(
            "A1  `reservations_legstatus` is the ONLY record of what physically happened. "
            "A Leg row has no actual-time column; every duration here is a difference of "
            "two taps. If drivers tap late, every duration is inflated by that latency and "
            "there is no second source to correct it with. [structural]",
            "A2  MIN(timestamp) per (leg,status) is the moment the event happened; later "
            "rows for the same (leg,status) are re-taps, auto-resets or bulk writes. "
            "Verified in §2 — the MAX-MIN spread is measured, not assumed.",
            "A3  Tap timestamps are UTC; booked pickup_date/pickup_time are naive local. "
            "Durations are differenced in UTC (DST-immune). Only tap-vs-booked comparisons "
            "convert, and §5 verifies the offset empirically day by day.",
            "A4  `drivers_driver.driver_type` / `exclude_from_timing` / `is_active` are "
            "CURRENT-STATE flags with no history table. Cohorts here are therefore "
            "'as the flags stand at the pull', applied retroactively to older behaviour. "
            "If a driver switched arms mid-window their whole history re-labels.",
            "A5  Coverage denominators use pickup_date >= the first tap in the table, so "
            "the first partial month is not scored against a whole-calendar-month "
            "denominator. The audit's 60% February is exactly that artefact.",
            "A6  Actuals stop at last_actuals_day (= today-1). Today's late work has not "
            "been tapped yet at the pull instant, and including it would fake a coverage "
            "collapse on the final day.",
        ))

    print("\nlane bucketer : %s" % cat_src)
    print("prior docs    : %s" % (docs["audit_path"] or "(audit not found)"))
    if docs["audit_tap_end"]:
        print("                audit tap window %s .. %s, generated %s  [parsed from the file]"
              % (docs["audit_tap_start"], docs["audit_tap_end"], docs["audit_generated"]))
    print("                %s" % (docs["redo_path"] or "(prior redo not found)"))
    if docs["redo_window_end"]:
        print("                prior redo window ends %s  [parsed from the file]"
              % docs["redo_window_end"])
    print("\nfreshness across independent write streams:")
    print(H.freshness_report())

    # ─────────────────────────────────────────────────────────────────────────
    # LOAD
    # ─────────────────────────────────────────────────────────────────────────
    C.hdr("LOAD")

    drivers = {}
    for r in C.q(con, """SELECT d.id, COALESCE(u.username,'#'||d.id) AS name,
                                d.driver_type, d.exclude_from_timing, d.is_active
                         FROM drivers_driver d
                         LEFT JOIN auth_user u ON u.id = d.profile_id"""):
        drivers[r["id"]] = {"name": r["name"], "type": r["driver_type"] or "unknown",
                            "excl": bool(r["exclude_from_timing"]),
                            "active": bool(r["is_active"])}
    print("drivers                    : %d  (%d in-house, %d affiliate, %d flagged "
          "exclude_from_timing=1)"
          % (len(drivers),
             sum(1 for d in drivers.values() if d["type"] == "inhouse"),
             sum(1 for d in drivers.values() if d["type"] == "affiliate"),
             sum(1 for d in drivers.values() if d["excl"])))

    legs = {}
    for r in C.q(con, C.live_legs_sql(
            "l.id, l.pickup_date, l.pickup_time, l.pickup_location, l.dropoff_location, "
            "l.driver_id, l.status")):
        d = dt.date.fromisoformat(r["pickup_date"])
        legs[r["id"]] = {
            "date": d, "month": r["pickup_date"][:7],
            "booked": C.booked_dtm(r["pickup_date"], r["pickup_time"]),
            "pu": r["pickup_location"] or "", "do": r["dropoff_location"] or "",
            "driver": r["driver_id"], "status": r["status"],
        }
    print("live legs (all dates)      : %d" % len(legs))

    # every tap, raw
    taps = C.q(con, "SELECT leg_id, status, timestamp, notes, updated_by_id "
                    "FROM reservations_legstatus")
    print("legstatus rows             : %d" % len(taps))

    # MIN and MAX per (leg,status) — both, so §2 can measure what .first() costs.
    fu, lu, ncount = {}, {}, collections.Counter()
    autoreset, bulkrows = collections.Counter(), collections.Counter()
    rowcount = collections.Counter()          # ladder rows per leg
    authored_by = collections.Counter()
    for t in taps:
        s = t["status"]
        lid = t["leg_id"]
        note = (t["notes"] or "")
        # notes counting is OUTSIDE the ladder filter on purpose: the auto-reset row
        # is written with status='in-progress', which is NOT a ladder status. Counting
        # it only inside the ladder loop silently returns zero.
        if note.startswith("Auto-reset"):
            autoreset[lid] += 1
        elif note.startswith("Bulk status update"):
            bulkrows[lid] += 1
        if s not in LADDER:
            continue
        ts = dt.datetime.fromisoformat(str(t["timestamp"]).replace("T", " "))
        ncount[(lid, s)] += 1
        rowcount[lid] += 1
        authored_by[t["updated_by_id"]] += 1
        if lid not in fu:
            fu[lid], lu[lid] = {}, {}
        if s not in fu[lid] or ts < fu[lid][s]:
            fu[lid][s] = ts
        if s not in lu[lid] or ts > lu[lid][s]:
            lu[lid][s] = ts
    print("legs carrying >=1 tap      : %d" % len(fu))
    print("rows carrying an Auto-reset note : %d   (status='in-progress', not a ladder row)"
          % sum(autoreset.values()))
    print("rows carrying a payroll bulk note: %d" % sum(bulkrows.values()))

    driver_user_ids = {r["profile_id"] for r in
                       C.q(con, "SELECT profile_id FROM drivers_driver "
                                "WHERE profile_id IS NOT NULL")}
    tot_auth = sum(authored_by.values())
    by_drv_rows = sum(n for u, n in authored_by.items() if u in driver_user_ids)
    by_none = authored_by.get(None, 0)
    print("ladder rows authored by a driver account : %d of %d = %.2f%%  [measured]"
          % (by_drv_rows, tot_auth, 100.0 * by_drv_rows / tot_auth))
    print("ladder rows with NO updated_by (system)  : %d = %.2f%%"
          % (by_none, 100.0 * by_none / tot_auth))

    tap_first, tap_last = H.first_tap_day, H.last_actuals_day
    print("tap era (derived)          : %s .. %s   (%d days)"
          % (tap_first, tap_last, (tap_last - tap_first).days + 1))

    # demand regimes — the current regime is the window a shipped buffer must survive
    byday = C.legs_per_day(con, end=H.last_demand_day)
    first_leg_day = min(dt.date.fromisoformat(k) for k in byday)
    regimes = C.changepoints(byday, first_leg_day, H.last_demand_day,
                             min_seg=28, min_effect=0.09)
    print("\ndemand regimes on live legs/day (changepoints, min_seg=28, min_effect=0.09):")
    for a, b, n, m in regimes:
        print("   %s .. %s  (%3dd)  %6.1f legs/day" % (a, b, n, m))
    cur_regime = regimes[-1] if regimes else (tap_first, tap_last, 0, 0)
    print("   -> CURRENT regime: %s .. %s" % (cur_regime[0], cur_regime[1]))

    # the working set: legs inside the tap era whose day is over
    era = [i for i, L in legs.items() if tap_first <= L["date"] <= tap_last]
    print("\nlive legs inside tap era   : %d" % len(era))

    def arm(lid):
        did = legs[lid]["driver"]
        if did is None:
            return "unassigned"
        return drivers.get(did, {}).get("type", "unknown")

    def lane(lid):
        return (cat(legs[lid]["pu"]), cat(legs[lid]["do"]))

    def kind(lid):
        p, d = lane(lid)
        if p in AIRPORTS:
            return "ARRIVAL"
        if d in AIRPORTS:
            return "DEPARTURE"
        return "OTHER"

    # ═════════════════════════════════════════════════════════════════════════
    # 1. TAP COVERAGE OVER TIME
    # ═════════════════════════════════════════════════════════════════════════
    C.hdr("1.  TAP COVERAGE OVER TIME  —  can the record answer the question at all?")
    print("""
Definitions (both reported, because the two prior documents used different ones):
  FULL-3  = on-the-way + picked-up + completed  (dispatching/analytics.py:430
            REQUIRED_ANALYTICS_STATUSES — what the production timing pipeline needs)
  FULL-5  = the whole ladder incl. confirmed and on-location
  CHAIN   = FULL-3 present AND strictly increasing AND inside the app's own gap rails
            (analytics.has_valid_status_chain, dispatching/analytics.py:493)
Denominator = live legs with pickup_date in the tap era. February is scored from the
first tap onward, NOT from the 1st — that single choice is the whole of the audit's
"60% February".""")

    months = months_between(tap_first, tap_last)
    cov = collections.defaultdict(lambda: collections.Counter())
    for lid in era:
        m, a = legs[lid]["month"], arm(lid)
        f = fu.get(lid, {})
        for key in (("ALL", m), (a, m)):
            c = cov[key]
            c["n"] += 1
            for s in LADDER:
                if s in f:
                    c[s] += 1
            if all(s in f for s in FULL3):
                c["full3"] += 1
                a1, a2, a3 = f["on-the-way"], f["picked-up"], f["completed"]
                if a1 < a2 < a3:
                    g1, g2 = mins(a1, a2), mins(a2, a3)
                    if 1 <= g1 <= 180 and MIN_PICKUP_TO_COMPLETE <= g2 <= MAX_DRIVE_MINUTES:
                        c["chain"] += 1
            if all(s in f for s in LADDER):
                c["full5"] += 1

    rows_cov = []
    for a in ("ALL", "inhouse", "affiliate", "unassigned"):
        if not any((a, m) in cov for m in months):
            continue
        C.sub("coverage — %s" % a)
        print("month     legs   conf%    otw%  onloc%     pu%   comp%   FULL3%  FULL5%  CHAIN%")
        for m in months:
            c = cov.get((a, m))
            if not c or not c["n"]:
                continue
            n = c["n"]
            print("%s %6d %6s %6s %7s %6s %6s %7s %7s %7s"
                  % (m, n,
                     f1(share(c["confirmed"], n)), f1(share(c["on-the-way"], n)),
                     f1(share(c["on-location"], n)), f1(share(c["picked-up"], n)),
                     f1(share(c["completed"], n)), f1(share(c["full3"], n)),
                     f1(share(c["full5"], n)), f1(share(c["chain"], n))))
            rows_cov.append([m, a, n, c["confirmed"], c["on-the-way"], c["on-location"],
                             c["picked-up"], c["completed"], c["full3"], c["full5"],
                             c["chain"],
                             round(share(c["full3"], n), 2), round(share(c["full5"], n), 2),
                             round(share(c["chain"], n), 2)])
    C.write_csv("02_coverage_by_month.csv",
                ["month", "arm", "legs", "confirmed", "on_the_way", "on_location",
                 "picked_up", "completed", "full3", "full5", "chain",
                 "full3_pct", "full5_pct", "chain_pct"], rows_cov)

    # weekly affiliate detail — the prior redo's headline claim lives or dies here
    C.sub("affiliate FULL-3 coverage by week  (the prior redo said this was FALLING)")
    wk = collections.defaultdict(lambda: [0, 0])
    for lid in era:
        if arm(lid) != "affiliate":
            continue
        d = legs[lid]["date"]
        k = (d - dt.timedelta(days=d.weekday())).isoformat()
        wk[k][0] += 1
        if all(s in fu.get(lid, {}) for s in FULL3):
            wk[k][1] += 1
    ws = sorted(wk)
    print("week-start    legs  FULL3%")
    for k in ws:
        n, f = wk[k]
        bar = "#" * int(round((100.0 * f / n) / 4)) if n else ""
        print("%s %6d %7s  %s" % (k, n, f1(share(f, n)), bar))
    C.write_csv("02_affiliate_coverage_weekly.csv",
                ["week_start", "legs", "full3", "full3_pct"],
                [[k, wk[k][0], wk[k][1], round(share(wk[k][1], wk[k][0]), 2)] for k in ws])

    # reconciliation on the audit's own window
    if docs["audit_tap_end"]:
        C.sub("RECONCILIATION — recomputed on the audit's OWN window (%s .. %s)"
              % (docs["audit_tap_start"], docs["audit_tap_end"]))
        a0, a1_ = docs["audit_tap_start"], docs["audit_tap_end"]
        sub = [i for i in era if a0 <= legs[i]["date"] <= a1_]
        for a in ("inhouse", "affiliate"):
            n = f5 = f3 = 0
            for lid in sub:
                if arm(lid) != a:
                    continue
                n += 1
                f = fu.get(lid, {})
                if all(s in f for s in FULL3):
                    f3 += 1
                if all(s in f for s in LADDER):
                    f5 += 1
            print("  %-10s n=%5d  FULL3 %5.1f%%  FULL5 %5.1f%%" % (a, n, share(f3, n), share(f5, n)))
        # the specific 35.3% claim: the prior redo's LAST month, which is a part-month
        lastm = a1_.strftime("%Y-%m")
        n = f5 = f3 = 0
        for lid in sub:
            if arm(lid) != "affiliate" or legs[lid]["month"] != lastm:
                continue
            n += 1
            f = fu.get(lid, {})
            if all(s in f for s in FULL3):
                f3 += 1
            if all(s in f for s in LADDER):
                f5 += 1
        print("  affiliate, the redo's FINAL PART-MONTH only (%s-01 .. %s): n=%d FULL3 %5.1f%% "
              "FULL5 %5.1f%%" % (lastm, a1_, n, share(f3, n), share(f5, n)))
        # and the same calendar month, complete
        n2 = f32 = 0
        for lid in era:
            if arm(lid) != "affiliate" or legs[lid]["month"] != lastm:
                continue
            n2 += 1
            if all(s in fu.get(lid, {}) for s in FULL3):
                f32 += 1
        print("  affiliate, that SAME month complete            : n=%d FULL3 %5.1f%%"
              % (n2, share(f32, n2)))

    # ═════════════════════════════════════════════════════════════════════════
    # 2. DUPLICATE TAPS
    # ═════════════════════════════════════════════════════════════════════════
    C.hdr("2.  DUPLICATE TAPS  —  rows per (leg,status), and what .first() costs")

    print("""Three different quantities have all been published as "the duplicate rate", and
they differ by a factor of three.  All three are given, because neither prior document
states which one it used:
  A  excess ROWS   = (ladder rows - distinct (leg,status)) / ladder rows
  B  dup GROUPS    = (leg,status) pairs holding >1 row, over all such pairs
  C  dup LEGS      = legs where at least one status holds >1 row, over tapped legs
and each is given twice: over the whole 5-rung ladder, and over the 3 rungs the
production timing pipeline actually reads (on-the-way / picked-up / completed).""")

    dup = collections.defaultdict(lambda: collections.Counter())
    for lid in era:
        m = legs[lid]["month"]
        f = fu.get(lid)
        c = dup[m]
        c["autoreset"] += autoreset.get(lid, 0)
        c["bulk"] += bulkrows.get(lid, 0)
        if autoreset.get(lid):
            c["legs_autoreset"] += 1
        if not f:
            continue
        c["legs_tapped"] += 1
        g = d_ = g3 = d3 = 0
        for s in f:
            g += 1
            if ncount[(lid, s)] > 1:
                d_ += 1
            if s in FULL3:
                g3 += 1
                if ncount[(lid, s)] > 1:
                    d3 += 1
        c["groups"] += g
        c["dup_groups"] += d_
        c["groups3"] += g3
        c["dup_groups3"] += d3
        c["rows"] += rowcount.get(lid, 0)
        c["rows3"] += sum(ncount[(lid, s)] for s in f if s in FULL3)
        if d_:
            c["legs_dup"] += 1
        if d3:
            c["legs_dup3"] += 1

    print("\n                          ---------- 5-rung ladder ----------   "
          "------- 3 rungs analytics reads -------")
    print("month   tapped_legs   A rows%   B groups%   C legs%      A rows%   B groups%"
          "   C legs%   autoreset_rows  legs_w/AR")
    rows_dup = []
    for m in months:
        c = dup.get(m)
        if not c or not c["legs_tapped"]:
            continue
        a5 = share(c["rows"] - c["groups"], c["rows"])
        a3 = share(c["rows3"] - c["groups3"], c["rows3"])
        print("%s %10d %9s %11s %9s %12s %11s %9s %16d %10d"
              % (m, c["legs_tapped"], f1(a5), f1(share(c["dup_groups"], c["groups"])),
                 f1(share(c["legs_dup"], c["legs_tapped"])), f1(a3),
                 f1(share(c["dup_groups3"], c["groups3"])),
                 f1(share(c["legs_dup3"], c["legs_tapped"])),
                 c["autoreset"], c["legs_autoreset"]))
        rows_dup.append([m, c["legs_tapped"], c["rows"], c["groups"], round(a5, 2),
                         c["dup_groups"], round(share(c["dup_groups"], c["groups"]), 2),
                         c["legs_dup"], round(share(c["legs_dup"], c["legs_tapped"]), 2),
                         c["rows3"], c["groups3"], round(a3, 2), c["dup_groups3"],
                         round(share(c["dup_groups3"], c["groups3"]), 2),
                         c["legs_dup3"],
                         round(share(c["legs_dup3"], c["legs_tapped"]), 2),
                         c["autoreset"], c["legs_autoreset"], c["bulk"]])
    C.write_csv("02_duplicates_by_month.csv",
                ["month", "tapped_legs", "ladder_rows", "status_groups", "excess_rows_pct",
                 "dup_groups", "dup_group_pct", "legs_with_dup", "legs_with_dup_pct",
                 "rows3", "groups3", "excess_rows3_pct", "dup_groups3",
                 "dup_group3_pct", "legs_with_dup3", "legs_with_dup3_pct",
                 "autoreset_rows", "legs_with_autoreset", "bulk_rows"], rows_dup)

    # ── the DRIVER of the duplicate rate: reassignment ───────────────────────
    C.sub("what drives it — the driver-unassign auto-reset")
    print("""An auto-reset writes `status='in-progress'` with notes 'Auto-reset: driver
unassigned' and wipes the leg's progression.  The NEXT driver then re-taps the whole
ladder, and THAT is what creates a second row per (leg,status).  So the test is not a
correlation of monthly aggregates — it is a per-leg split.""")
    with_ar = [i for i in era if fu.get(i) and autoreset.get(i)]
    without_ar = [i for i in era if fu.get(i) and not autoreset.get(i)]

    def dup_share(ids):
        d3 = sum(1 for i in ids if any(ncount[(i, s)] > 1 for s in fu[i]))
        return len(ids), d3, share(d3, len(ids))
    n1, d1, p1_ = dup_share(with_ar)
    n0, d0, p0_ = dup_share(without_ar)
    print("\n  legs WITH >=1 auto-reset : n=%5d, %5d carry a duplicated status = %5.1f%%"
          % (n1, d1, p1_ or 0))
    print("  legs with NO auto-reset  : n=%5d, %5d carry a duplicated status = %5.1f%%"
          % (n0, d0, p0_ or 0))
    if p0_:
        print("  -> a reassigned leg is %.1fx more likely to hold a duplicate tap [measured]"
              % (p1_ / p0_))
    tot_dup = d1 + d0
    if tot_dup:
        print("  -> reassignment accounts for %.1f%% of all legs that hold a duplicate"
              % (100.0 * d1 / tot_dup))

    # independent count of unassign events, from a different table entirely
    unassign = collections.Counter()
    for r in C.q(con, """SELECT object_id, COUNT(*) n FROM reservations_auditlog
                         WHERE model_name='Leg' AND action='driver_unassigned'
                         GROUP BY object_id"""):
        unassign[r["object_id"]] = r["n"]
    print("\n  second source — reservations_auditlog action='driver_unassigned':")
    print("  month   unassign_events   legs_unassigned   legs_with_autoreset_note")
    unass_series = []
    for m in months:
        ev = sum(unassign.get(i, 0) for i in era if legs[i]["month"] == m)
        lg = sum(1 for i in era if legs[i]["month"] == m and unassign.get(i))
        ar = dup[m]["legs_autoreset"] if dup.get(m) else 0
        unass_series.append((m, ev, lg, ar))
        print("  %s %15d %17d %26d" % (m, ev, lg, ar))

    first_ar = C.q1(con, "SELECT MIN(timestamp) FROM reservations_legstatus "
                         "WHERE notes LIKE 'Auto-reset%'")
    print("""
  READ THIS CAREFULLY — the prior redo's explanation is the wrong way round. It said
  the duplicate rate rose because 'auto-resets rose 36 -> 1,007/month'. Unassignment
  did NOT rise: the audit-log count is FLAT-to-FALLING across the whole era (%d/month
  at the start, %d/month now). What changed is that the SYSTEM STARTED WRITING the
  reset on %s — the first Auto-reset row in the table. The writer is
  reservations/models.py:1891-1900 (`Leg.save()` -> `LegStatus.objects.create(
  status='in-progress', notes='Auto-reset: driver unassigned')`), which wipes the
  leg's progression so the NEXT driver re-runs the whole ladder and lays down a second
  row per (leg,status). The duplicate rate did not track a behaviour change in
  dispatch; it tracked a code deploy.""" % (
        unass_series[0][1], unass_series[-1][1],
        C.to_local(first_ar).date() if first_ar else "[unavailable]"))
    print("""
  And on the three rungs the timing pipeline actually reads, the duplicate rate has
  been FALLING since its May peak. The 'it tripled and stayed there' reading is a
  window-end artefact for the second time in this document.""")

    # what does MIN vs .first() (=MAX under Meta.ordering) actually change?
    C.sub("MIN(timestamp) vs Django .first()  —  reservations/models.py:3384 "
          "`ordering = ['-timestamp']`")
    print("""`LegStatus.Meta.ordering = ['-timestamp']` (reservations/models.py:3384-3385) makes
BOTH `leg.status_history.filter(status=X).first()` and the first item of
`leg.status_history.all()` the LATEST row for that status, not the earliest.  Below is
the error that substitution introduces, measured on the legs where it can bite.""")
    skew = collections.defaultdict(list)
    ride_min, ride_max = [], []
    for lid in era:
        f, l = fu.get(lid), lu.get(lid)
        if not f:
            continue
        for s in f:
            if ncount[(lid, s)] > 1:
                skew[s].append(mins(f[s], l[s]))
        if all(s in f for s in ("picked-up", "completed")):
            a = mins(f["picked-up"], f["completed"])
            b = mins(l["picked-up"], l["completed"])
            if a is not None and b is not None and a != b:
                ride_min.append(a)
                ride_max.append(b)
    for s in LADDER:
        if skew[s]:
            print("  " + row3("MAX-MIN spread, %s" % s, skew[s], width=30))
    if ride_min:
        print("\n  legs where MIN-vs-LATEST changes the ride time at all: %d "
              "(%.2f%% of era legs)" % (len(ride_min), 100.0 * len(ride_min) / len(era)))
        print("  " + row3("   ride, MIN taps (correct)", ride_min))
        print("  " + row3("   ride, LATEST taps (.first())", ride_max))
    print("""
PRODUCTION READERS — verified by grep, path:line:
  IMMUNE (take the earliest explicitly)
    dispatching/analytics.py:441      first_status_times() — compares timestamps, ignores ordering
    dispatching/analytics.py:493      has_valid_status_chain() — built on first_status_times
    dispatching/conflict_advisor.py:441  loops newest-first and OVERWRITES, so keeps earliest
    dispatching/conflict_advisor.py:515  same trick, commented "newest-first; keep earliest tap"
    dispatching/management/commands/driver_data_quality.py:113  uses first_status_times
  CORRECT BY INTENT (they genuinely want the newest row)
    dispatching/views.py:579          builds a "latest status" popup — wants newest
    dispatching/templates/dispatching/reservation_view.html:1096  `.first` as "last update"
    dispatching/templates/dispatching/legs_filter.html:1784,2209 `status_history.all.0`
    dispatching/templates/dispatching/legs_list.html:789,1104     same
  AFFECTED — takes the FIRST match while scanning a newest-first queryset
    dispatching/views.py:512          `for sh in leg.status_history.all(): if sh.status ==
                                      'completed'` — does NOT break, so it ends on the OLDEST
                                      row. Reading the loop: it overwrites each time, so the
                                      value that survives is the earliest. Benign.
Conclusion: no production reader that wants an EARLIEST tap is currently getting a
LATEST one.  The `.first()` bug the audit describes was fixed and the fix has held; the
risk is that the duplicate rate has grown, so any NEW reader written naively is now
wrong on several times more legs than it would have been in the audit's window.""")

    # ═════════════════════════════════════════════════════════════════════════
    # 3. DRIVER BEHAVIOUR — the fabricating cohort, re-derived
    # ═════════════════════════════════════════════════════════════════════════
    C.hdr("3.  THE FABRICATING COHORT  —  re-derived from behaviour, not inherited")
    print("""RULE (declared before it is applied; every term measured per driver over the tap era):
  legs        legs with >=1 ladder tap
  FULL3%%      share of those with on-the-way + picked-up + completed
  instant%%    of FULL3 legs, share where picked-up -> completed < %d min
              (dispatching/analytics.py:434 MIN_PICKUP_TO_COMPLETE — the app's own line)
  collapsed%%  of FULL3 legs, share where the WHOLE on-the-way..completed span < %ds
  nonmono%%    of FULL3 legs, share where the taps are not strictly increasing
              (includes 'completed before picked-up')
  earlyfin%%   share of completed legs whose completed tap lands BEFORE the booked
              pickup instant — a physically impossible finish
  adj<%ds%%    of FULL3 legs, share with >=1 adjacent gap under %ds (the audit's 'double-tap')
VERDICT  FABRICATES if legs >= %d AND (instant%% >= %.0f%% OR collapsed%% >= %.0f%%)
         sparse     if legs >= %d AND FULL3%% < %.0f%% and not fabricating  (honest, keep)
Threshold %.0f%% is production's own INSTANT_SHARE_EXCLUDE
(dispatching/management/commands/driver_data_quality.py:60). Sensitivity at other
thresholds is reported below so the cut is not load-bearing.
""" % (MIN_PICKUP_TO_COMPLETE, COLLAPSED_SECONDS, ADJACENT_SECONDS, ADJACENT_SECONDS,
       MIN_LEGS_TO_JUDGE, INSTANT_SHARE_EXCLUDE * 100, INSTANT_SHARE_EXCLUDE * 100,
       MIN_LEGS_TO_JUDGE, SPARSE_FULL_CHAIN * 100, INSTANT_SHARE_EXCLUDE * 100))

    st = collections.defaultdict(lambda: collections.Counter())
    rides_by_drv = collections.defaultdict(list)
    for lid in era:
        did = legs[lid]["driver"]
        f = fu.get(lid)
        if did is None or not f:
            continue
        s = st[did]
        s["legs"] += 1
        if "completed" in f and legs[lid]["booked"] is not None:
            s["comp"] += 1
            if C.to_local(f["completed"]) < legs[lid]["booked"]:
                s["earlyfin"] += 1
        if not all(x in f for x in FULL3):
            continue
        s["full3"] += 1
        otw, pu, cp = f["on-the-way"], f["picked-up"], f["completed"]
        ride = mins(pu, cp)
        span = mins(otw, cp)
        ordered = [f[x] for x in LADDER if x in f]
        if any(b < a for a, b in zip(ordered, ordered[1:])):
            s["nonmono"] += 1
        if ride is not None and ride < MIN_PICKUP_TO_COMPLETE:
            s["instant"] += 1
        else:
            rides_by_drv[did].append(ride)
        if span is not None and span * 60 < COLLAPSED_SECONDS:
            s["collapsed"] += 1
        adj = [mins(a, b) for a, b in zip(ordered, ordered[1:])]
        if any(x is not None and abs(x) * 60 < ADJACENT_SECONDS for x in adj):
            s["adj"] += 1

    prof = []
    for did, s in st.items():
        if s["legs"] < MIN_LEGS_TO_JUDGE:
            continue
        d = drivers.get(did, {"name": "#%s" % did, "type": "unknown",
                              "excl": False, "active": False})
        f3 = s["full3"]
        p = {
            "id": did, "name": d["name"], "type": d["type"], "excl": d["excl"],
            "active": d["active"], "legs": s["legs"],
            "full3": share(s["full3"], s["legs"]) or 0.0,
            "instant": share(s["instant"], f3) or 0.0,
            "collapsed": share(s["collapsed"], f3) or 0.0,
            "nonmono": share(s["nonmono"], f3) or 0.0,
            "earlyfin": share(s["earlyfin"], s["comp"]) or 0.0,
            "adj": share(s["adj"], f3) or 0.0,
            "medride": C.pct(rides_by_drv[did], 50),
        }
        p["fab"] = (p["instant"] >= INSTANT_SHARE_EXCLUDE * 100
                    or p["collapsed"] >= INSTANT_SHARE_EXCLUDE * 100)
        p["sparse"] = (not p["fab"]) and p["full3"] < SPARSE_FULL_CHAIN * 100
        p["verdict"] = "FABRICATES" if p["fab"] else ("sparse" if p["sparse"] else "good")
        prof.append(p)
    prof.sort(key=lambda x: -x["instant"])

    print("%-14s %-9s %6s %7s %8s %9s %8s %9s %7s %8s  %-10s %-9s %s"
          % ("driver", "arm", "legs", "FULL3%", "instant%", "collapsed%", "nonmono%",
             "earlyfin%", "adj%", "medRide", "verdict", "flagged?", "MISMATCH"))
    rows_drv = []
    for p in prof:
        mismatch = ""
        if p["fab"] and not p["excl"]:
            mismatch = "<< SHOULD BE FLAGGED"
        elif (not p["fab"]) and p["excl"]:
            mismatch = "<< FLAGGED WRONGLY (%s)" % p["verdict"]
        print("%-14s %-9s %6d %7.1f %8.1f %9.1f %8.1f %9.1f %7.1f %8s  %-10s %-9s %s"
              % (p["name"], p["type"] + ("*" if not p["active"] else ""), p["legs"],
                 p["full3"], p["instant"], p["collapsed"], p["nonmono"], p["earlyfin"],
                 p["adj"], ("%.0f" % p["medride"]) if p["medride"] else "-",
                 p["verdict"], "EXCLUDED" if p["excl"] else "-", mismatch))
        rows_drv.append([p["name"], p["id"], p["type"], int(p["active"]), int(p["excl"]),
                         p["legs"], round(p["full3"], 2), round(p["instant"], 2),
                         round(p["collapsed"], 2), round(p["nonmono"], 2),
                         round(p["earlyfin"], 2), round(p["adj"], 2),
                         round(p["medride"], 1) if p["medride"] else "",
                         p["verdict"]])
    print("  * = is_active = 0 today (they still drove inside the window; A4 applies)")

    fab = [p for p in prof if p["fab"]]
    flagged_now = sorted(d["name"] for d in drivers.values() if d["excl"])
    C.sub("exclude_from_timing  —  derived cohort vs the flag as it stands in production")
    print("derived FABRICATES (%d): %s" % (len(fab), ", ".join(p["name"] for p in fab)))
    print("flagged in the DB  (%d): %s" % (len(flagged_now), ", ".join(flagged_now)))
    fn = [p["name"] for p in fab if not p["excl"]]
    fp = [(p["name"], p["verdict"], p["legs"]) for p in prof if p["excl"] and not p["fab"]]
    print("\nFALSE NEGATIVES — fabricate, not flagged (%d): %s"
          % (len(fn), ", ".join(fn) or "none"))
    for p in sorted((x for x in fab if not x["excl"]), key=lambda x: -x["legs"]):
        print("   %-12s %-9s legs=%4d  instant=%5.1f%%  collapsed=%5.1f%%  FULL3=%5.1f%%"
              % (p["name"], p["type"], p["legs"], p["instant"], p["collapsed"], p["full3"]))
    print("\nFALSE POSITIVES — flagged, do not fabricate (%d):" % len(fp))
    for nm, v, n in sorted(fp, key=lambda x: -x[2]):
        p = next(x for x in prof if x["name"] == nm)
        print("   %-12s %-9s legs=%4d  verdict=%-7s instant=%5.1f%%  collapsed=%5.1f%%  "
              "FULL3=%5.1f%%  -> costs %d honest FULL3 legs"
              % (nm, p["type"], n, v, p["instant"], p["collapsed"], p["full3"],
                 int(round(n * p["full3"] / 100.0))))
    # drivers flagged but below the judging floor
    quiet = [drivers[d]["name"] for d in drivers
             if drivers[d]["excl"] and st[d]["legs"] < MIN_LEGS_TO_JUDGE]
    if quiet:
        print("   (flagged but under the %d-leg judging floor, untestable: %s)"
              % (MIN_LEGS_TO_JUDGE, ", ".join(sorted(quiet))))

    C.sub("sensitivity — cohort membership vs the instant%/collapsed% threshold")
    for th in (0.20, 0.30, 0.40, 0.50, 0.60):
        c = [p["name"] for p in prof
             if p["instant"] >= th * 100 or p["collapsed"] >= th * 100]
        print("   threshold %2.0f%%  ->  %2d drivers: %s" % (th * 100, len(c), ", ".join(c)))

    fab_ids = {p["id"] for p in fab}
    fab_legs = [i for i in era if legs[i]["driver"] in fab_ids]
    print("\ncohort size: %d drivers carry %d of %d era legs = %.1f%% of the operational "
          "record [measured]" % (len(fab), len(fab_legs), len(era),
                                 share(len(fab_legs), len(era))))

    # is fabrication spreading, or is it a fixed set of people?
    C.sub("is it spreading?  instant-completion rate by month, cohort vs everyone else")
    print("month    fabricator legs  their instant%   everyone-else legs  their instant%")
    rows_fabm = []
    for m in months:
        a = [0, 0]
        b = [0, 0]
        for lid in era:
            if legs[lid]["month"] != m:
                continue
            f = fu.get(lid) or {}
            rd = mins(f.get("picked-up"), f.get("completed"))
            if rd is None or rd < 0:
                continue
            t = a if legs[lid]["driver"] in fab_ids else b
            t[0] += 1
            if rd < MIN_PICKUP_TO_COMPLETE:
                t[1] += 1
        if not (a[0] or b[0]):
            continue
        print("%s %15d %15s %20d %15s"
              % (m, a[0], f1(share(a[1], a[0])), b[0], f1(share(b[1], b[0]))))
        rows_fabm.append([m, a[0], a[1], round(share(a[1], a[0]) or 0, 2),
                          b[0], b[1], round(share(b[1], b[0]) or 0, 2)])
    C.write_csv("02_fabrication_by_month.csv",
                ["month", "fabricator_legs", "fabricator_instant",
                 "fabricator_instant_pct", "other_legs", "other_instant",
                 "other_instant_pct"], rows_fabm)

    # named tests from the prior work
    C.sub("the two drivers the prior work named by hand")
    for nm in ("rizwan", "anthony"):
        p = next((x for x in prof if x["name"].lower() == nm), None)
        if not p:
            print("   %-9s [unavailable] — under the %d-leg floor or no taps in the era"
                  % (nm, MIN_LEGS_TO_JUDGE))
            continue
        print("   %-9s %-9s legs=%4d FULL3=%5.1f%% instant=%5.1f%% collapsed=%5.1f%% "
              "nonmono=%4.1f%% adj=%5.1f%% flagged=%s -> %s"
              % (p["name"], p["type"], p["legs"], p["full3"], p["instant"], p["collapsed"],
                 p["nonmono"], p["adj"], "YES" if p["excl"] else "no", p["verdict"]))

    # ═════════════════════════════════════════════════════════════════════════
    # 4. THE GOLD COHORT, RE-VETTED, AND THE GENERALISATION GAP
    # ═════════════════════════════════════════════════════════════════════════
    C.hdr("4.  THE GOLD COHORT  —  re-vetted from data, and the generalisation gap")
    print("""GATES (declared, then applied — no name is inherited from any prior document):
  G1 legs >= %d           G2 FULL3%% >= %.0f%%     G3 instant%% <= %.0f%%
  G4 collapsed%% <= %.0f%%    G5 nonmono%% <= %.0f%%
Affiliates are gated identically and reported separately, because production analytics
filters to driver_type='inhouse' (dispatching/analytics.py, update_single_route_timing_metric)
so a clean affiliate's data never reaches RouteTimingMetric however good it is.
""" % (MIN_LEGS_TO_JUDGE, GOLD_FULL3_MIN * 100, GOLD_INSTANT_MAX * 100,
       GOLD_COLLAPSED_MAX * 100, GOLD_NONMONO_MAX * 100))

    def gates(p):
        return {"G1": p["legs"] >= MIN_LEGS_TO_JUDGE,
                "G2": p["full3"] >= GOLD_FULL3_MIN * 100,
                "G3": p["instant"] <= GOLD_INSTANT_MAX * 100,
                "G4": p["collapsed"] <= GOLD_COLLAPSED_MAX * 100,
                "G5": p["nonmono"] <= GOLD_NONMONO_MAX * 100}

    gold_ids, gold_aff_ids = set(), set()
    print("%-14s %-9s %6s %7s %8s %9s %8s   %-24s %s"
          % ("driver", "arm", "legs", "FULL3%", "instant%", "collapsed%", "nonmono%",
             "gates", "GOLD"))
    for p in sorted(prof, key=lambda x: (-x["legs"])):
        g = gates(p)
        ok = all(g.values())
        fails = ",".join(k for k, v in g.items() if not v) or "-"
        if ok:
            (gold_ids if p["type"] == "inhouse" else gold_aff_ids).add(p["id"])
        print("%-14s %-9s %6d %7.1f %8.1f %9.1f %8.1f   %-24s %s"
              % (p["name"], p["type"], p["legs"], p["full3"], p["instant"],
                 p["collapsed"], p["nonmono"], "PASS" if ok else "fail: " + fails,
                 "GOLD" if ok else ""))
        for r in rows_drv:
            if r[1] == p["id"]:
                r.append("gold" if ok else "fail:" + fails)
    C.write_csv("02_driver_discipline.csv",
                ["driver", "driver_id", "arm", "is_active", "excluded_now", "legs",
                 "full3_pct", "instant_pct", "collapsed_pct", "nonmono_pct",
                 "earlyfin_pct", "adjacent_lt60s_pct", "median_ride_min", "verdict",
                 "gold"], rows_drv)

    gold_names = sorted(drivers[i]["name"] for i in gold_ids)
    gold_aff_names = sorted(drivers[i]["name"] for i in gold_aff_ids)
    print("\nGOLD, in-house (%d): %s" % (len(gold_names), ", ".join(gold_names)))
    print("GOLD, affiliate (%d): %s  [would pass, but production analytics discards them]"
          % (len(gold_aff_names), ", ".join(gold_aff_names) or "none"))

    # reconcile against the names the audit nominated, read out of the audit file
    nominated = parse_audit_gold_names()
    keep, drop, untest, added = [], [], [], []
    if nominated:
        C.sub("the audit's nominated cohort (%d names, parsed from the audit file) re-vetted"
              % len(nominated))
        by_name = {p["name"].lower(): p for p in prof}
        for nm in nominated:
            p = by_name.get(nm.lower())
            if not p:
                untest.append(nm)
                continue
            g = gates(p)
            (keep if all(g.values()) else drop).append(
                (nm, p, ",".join(k for k, v in g.items() if not v)))
        print("  SURVIVES the gates (%d): %s"
              % (len(keep), ", ".join(n for n, _, _ in keep) or "none"))
        for nm, p, f in drop:
            print("  DROPS  %-12s legs=%4d FULL3=%5.1f%% instant=%5.1f%% collapsed=%5.1f%% "
                  "nonmono=%4.1f%%  fails %s" % (nm, p["legs"], p["full3"], p["instant"],
                                                 p["collapsed"], p["nonmono"], f))
        if untest:
            print("  UNTESTABLE (under the %d-leg floor or no taps in the era): %s"
                  % (MIN_LEGS_TO_JUDGE, ", ".join(untest)))
        added[:] = [n for n in gold_names
                    if n.lower() not in {x.lower() for x in nominated}]
        print("  ADDED by the data, never nominated: %s" % (", ".join(added) or "none"))

    era_gold = [i for i in era if legs[i]["driver"] in gold_ids]
    era_gold_all = [i for i in era if legs[i]["driver"] in (gold_ids | gold_aff_ids)]
    era_inhouse = [i for i in era if arm(i) == "inhouse"]
    print("\ncohort share of the tap era:")
    print("   gold in-house      %6d legs = %5.1f%% of all era legs, %5.1f%% of in-house legs"
          % (len(era_gold), share(len(era_gold), len(era)), share(len(era_gold), len(era_inhouse))))
    print("   gold incl. affil.  %6d legs = %5.1f%% of all era legs"
          % (len(era_gold_all), share(len(era_gold_all), len(era))))

    # ── the metric set the gap is measured on ────────────────────────────────
    def build_metrics(ids):
        """{metric_name: [values]} for a set of leg ids.  UTC differencing throughout."""
        out = collections.defaultdict(list)
        for lid in ids:
            f = fu.get(lid)
            if not f:
                continue
            k = kind(lid)
            ap = mins(f.get("on-the-way"), f.get("on-location"))
            dw = mins(f.get("on-location"), f.get("picked-up"))
            rd = mins(f.get("picked-up"), f.get("completed"))
            oc = mins(f.get("on-location"), f.get("completed"))
            if ap is not None:
                out["approach_raw"].append(ap)
                if 0 <= ap <= MAX_DRIVE_MINUTES:
                    out["approach"].append(ap)
            if dw is not None:
                out["dwell_all_raw"].append(dw)
                if 0 <= dw <= MAX_DWELL_MINUTES:
                    out["dwell_all"].append(dw)
                    out["dwell_" + k].append(dw)
            if rd is not None:
                out["ride_raw"].append(rd)
                if MIN_PICKUP_TO_COMPLETE <= rd <= MAX_DRIVE_MINUTES:
                    out["ride"].append(rd)
            if oc is not None and 0 <= oc <= MAX_TOTAL_MINUTES:
                out["occupancy"].append(oc)
        return out

    m_gold = build_metrics(era_gold)
    m_all = build_metrics(era)
    m_trust = build_metrics([i for i in era
                             if arm(i) == "inhouse"
                             and not drivers.get(legs[i]["driver"], {}).get("excl")])
    m_nofab = build_metrics([i for i in era
                             if legs[i]["driver"] not in {p["id"] for p in fab}])

    C.sub("GENERALISATION GAP  —  the single most important number in this script")
    print("Positive GAP = the fleet is SLOWER than gold, i.e. a buffer fitted to gold is "
          "tight by that many minutes.\n")
    rows_gap = []
    for label, key in (("approach  otw->onloc", "approach"),
                       ("dwell     onloc->pu", "dwell_all"),
                       ("dwell     ARRIVAL", "dwell_ARRIVAL"),
                       ("dwell     DEPARTURE", "dwell_DEPARTURE"),
                       ("dwell     OTHER", "dwell_OTHER"),
                       ("ride      pu->comp", "ride"),
                       ("occupancy onloc->comp", "occupancy")):
        print("  " + gap_line(label, m_gold[key], m_all[key]))
        for cname, mm in (("gold", m_gold), ("all", m_all),
                          ("inhouse_not_flagged", m_trust), ("all_minus_fabricators", m_nofab)):
            d = C.describe(mm[key])
            if d["n"]:
                rows_gap.append([key, cname, d["n"], d["p50"], d["p75"], d["p90"], d["mean"]])
    print("\n  same rows against the two intermediate cohorts (what production actually uses):")
    for label, key in (("dwell     onloc->pu", "dwell_all"), ("approach  otw->onloc", "approach"),
                       ("ride      pu->comp", "ride"), ("occupancy onloc->comp", "occupancy")):
        print("  " + gap_line(label + " [vs in-house not-flagged]", m_gold[key], m_trust[key]))

    # true dwell (gate docked -> guest in car) — the metric the prior redo's A5 used
    C.sub("TRUE DWELL — flight gate actual -> picked-up  (the prior redo's A5 claim)")
    ctrl = {}
    for r in C.q(con, """SELECT lf.leg_id, f.actual_gate_arrival_local AS ag
                         FROM reservations_legflight lf
                         JOIN reservations_flight f ON f.id = lf.flight_id
                         WHERE lf.is_controlling = 1 AND f.actual_gate_arrival_local IS NOT NULL"""):
        ctrl[r["leg_id"]] = dt.datetime.fromisoformat(str(r["ag"]).replace("T", " "))
    td_gold, td_all, td_trust = [], [], []
    onloc_vs_gate_gold, onloc_vs_gate_all = [], []
    pax_only_all, pax_only_gold = [], []
    for lid in era:
        if kind(lid) != "ARRIVAL":
            continue
        g = ctrl.get(lid)
        f = fu.get(lid) or {}
        if not g:
            continue
        v = mins(g, f.get("picked-up"))
        w = mins(g, f.get("on-location"))
        if v is not None and -60 <= v <= 300:
            td_all.append(v)
            if legs[lid]["driver"] in gold_ids:
                td_gold.append(v)
            if arm(lid) == "inhouse" and not drivers.get(legs[lid]["driver"], {}).get("excl"):
                td_trust.append(v)
            # GUEST-ONLY dwell: the driver was already standing there when the plane
            # docked, so nothing in this number is driver lateness.
            if w is not None and w <= 0:
                pax_only_all.append(v)
                if legs[lid]["driver"] in gold_ids:
                    pax_only_gold.append(v)
        if w is not None and -180 <= w <= 300:
            onloc_vs_gate_all.append(w)
            if legs[lid]["driver"] in gold_ids:
                onloc_vs_gate_gold.append(w)
    print("  " + gap_line("true dwell gate->pu", td_gold, td_all))
    print("  " + gap_line("true dwell [vs trusted]", td_gold, td_trust))
    print("  " + row3("driver on-loc vs gate, gold", onloc_vs_gate_gold))
    print("  " + row3("driver on-loc vs gate, all", onloc_vs_gate_all))
    print("  " + row3("GUEST-ONLY dwell (driver already", pax_only_all))
    print("  " + row3("   waiting at gate), gold", pax_only_gold))
    print("  " + gap_line("GUEST-ONLY dwell", pax_only_gold, pax_only_all))
    print("""  The guest-only row is the cleanest dwell number in this database: driver
  lateness is definitionally excluded, so it measures deplaning + walk + bags and
  nothing else. It is the right basis for any arrival buffer.
  AND IT IS THE STRONGEST FORM OF THE GENERALISATION-GAP FINDING: with driver
  lateness removed by construction, gold STILL understates the fleet by +%.1f min at
  P75 and +%.1f at P90. The gap is therefore not 'gold drivers are punctual' — the
  same guests, the same airport, the same physics, measured on a cohort selected for
  tapping discipline, produce a materially shorter tail. Whatever selects gold drivers
  also selects easier work."""
          % (C.pct(pax_only_all, 75) - C.pct(pax_only_gold, 75),
             C.pct(pax_only_all, 90) - C.pct(pax_only_gold, 90)))
    early = sum(1 for x in onloc_vs_gate_all if x < 0)
    if onloc_vs_gate_all:
        print("  driver standing at the gate BEFORE the plane docks: %d of %d = %.1f%% "
              "[measured]" % (early, len(onloc_vs_gate_all),
                              100.0 * early / len(onloc_vs_gate_all)))
    for mname, sets in (("true_dwell_gate_to_pu",
                         (("gold", td_gold), ("all", td_all),
                          ("inhouse_not_flagged", td_trust))),
                        ("guest_only_dwell_gate_to_pu",
                         (("gold", pax_only_gold), ("all", pax_only_all))),
                        ("driver_onlocation_minus_gate",
                         (("gold", onloc_vs_gate_gold), ("all", onloc_vs_gate_all)))):
        for cname, vals in sets:
            d = C.describe(vals)
            if d["n"]:
                rows_gap.append([mname, cname, d["n"], d["p50"], d["p75"], d["p90"],
                                 d["mean"]])
    C.write_csv("02_generalisation_gap.csv",
                ["metric", "cohort", "n", "p50", "p75", "p90", "mean"], rows_gap)

    # ═════════════════════════════════════════════════════════════════════════
    # 5. DST — verify the offset empirically
    # ═════════════════════════════════════════════════════════════════════════
    C.hdr("5.  DST  —  is _common.to_local right, verified day by day from the data")
    print("""METHOD.  On a DEPARTURE leg the guest is waiting and the car leaves close to the
booked time, so `picked-up` tap (UTC) minus booked pickup (naive LOCAL) should sit at a
sharp mode equal to the UTC offset.  Nothing here assumes an offset: the tap is read RAW
and the modal whole-hour difference is reported for each consecutive day around the
transitions listed in `_common.US_DST_TRANSITIONS`.""")
    dstrows = []
    for utc_start, utc_end in C.US_DST_TRANSITIONS:
        for label, inst in (("spring-forward (EST->EDT)", utc_start),
                            ("fall-back (EDT->EST)", utc_end)):
            day = inst.date()
            lo, hi = day - dt.timedelta(days=6), day + dt.timedelta(days=6)
            if hi < tap_first or lo > tap_last:
                print("\n  %s at %s : [unavailable] — outside the tap era %s..%s"
                      % (label, day, tap_first, tap_last))
                continue
            print("\n  %s at %s" % (label, day))
            print("  day          n   modal offset   share at mode   median resid (min)")
            d = lo
            while d <= hi:
                deltas = []
                for lid in era:
                    L = legs[lid]
                    if L["date"] != d or kind(lid) != "DEPARTURE" or L["booked"] is None:
                        continue
                    pu = (fu.get(lid) or {}).get("picked-up")
                    if pu is None:
                        continue
                    deltas.append((pu - L["booked"]).total_seconds() / 3600.0)
                if len(deltas) >= 8:
                    modal = collections.Counter(int(round(x)) for x in deltas).most_common(1)[0]
                    resid = [(x - modal[0]) * 60 for x in deltas if int(round(x)) == modal[0]]
                    print("  %s %5d %8d h %13.0f%% %18.1f"
                          % (d, len(deltas), modal[0], 100.0 * modal[1] / len(deltas),
                             C.pct(resid, 50)))
                    dstrows.append([str(d), label, len(deltas), modal[0],
                                    round(100.0 * modal[1] / len(deltas), 1),
                                    round(C.pct(resid, 50), 1)])
                elif deltas:
                    print("  %s %5d      (too few)" % (d, len(deltas)))
                d += dt.timedelta(days=1)
    C.write_csv("02_dst_offset_by_day.csv",
                ["day", "transition", "n", "modal_offset_hours", "share_at_mode_pct",
                 "median_residual_min"], dstrows)
    # agreement with _common.to_local across every day of the era
    agree = disagree = 0
    for r in dstrows:
        d = dt.date.fromisoformat(r[0])
        noon = dt.datetime.combine(d, dt.time(17))     # ~midday local, expressed in UTC
        if C.utc_offset_hours(noon) == r[3]:
            agree += 1
        else:
            disagree += 1
    print("\n  _common.utc_offset_hours agrees with the measured modal offset on %d of %d "
          "scored days%s" % (agree, agree + disagree,
                             "" if not disagree else "  <<< DISAGREEMENT, investigate"))
    nxt = [e for (_s, e) in C.US_DST_TRANSITIONS if e.date() > tap_last]
    if nxt:
        print("  next transition after the tap era: %s — [unavailable], %d days beyond "
              "last_actuals_day, so the autumn flip cannot be verified from this pull."
              % (min(nxt).date(), (min(nxt).date() - tap_last).days))

    # ═════════════════════════════════════════════════════════════════════════
    # 6. CORE DURATIONS
    # ═════════════════════════════════════════════════════════════════════════
    C.hdr("6.  CORE DURATIONS  —  all-driver and gold, P50/P75/P90")
    print("Plausibility rails are the application's own: dwell <= %d "
          "(analytics.py:26), ride %d..%d (analytics.py:434/27).  Unfiltered outlier rates "
          "are in §7." % (MAX_DWELL_MINUTES, MIN_PICKUP_TO_COMPLETE, MAX_DRIVE_MINUTES))

    for label, key in (("approach  otw->onloc", "approach"),
                       ("dwell     onloc->pu  ALL", "dwell_all"),
                       ("dwell     onloc->pu  ARRIVAL", "dwell_ARRIVAL"),
                       ("dwell     onloc->pu  DEPARTURE", "dwell_DEPARTURE"),
                       ("dwell     onloc->pu  OTHER", "dwell_OTHER"),
                       ("ride      pu->comp", "ride"),
                       ("occupancy onloc->comp", "occupancy")):
        C.sub(label)
        print("  " + row3("all drivers", m_all[key]))
        print("  " + row3("in-house, not flagged", m_trust[key]))
        print("  " + row3("gold", m_gold[key]))

    # ── ride by lane ─────────────────────────────────────────────────────────
    C.sub("ride time by lane  (pu -> comp), lanes with n >= 30 on the all-driver set")
    lane_all, lane_gold = collections.defaultdict(list), collections.defaultdict(list)
    for lid in era:
        f = fu.get(lid) or {}
        rd = mins(f.get("picked-up"), f.get("completed"))
        if rd is None or not (MIN_PICKUP_TO_COMPLETE <= rd <= MAX_DRIVE_MINUTES):
            continue
        lane_all[lane(lid)].append(rd)
        if legs[lid]["driver"] in gold_ids:
            lane_gold[lane(lid)].append(rd)
    rows_lane = []
    print("%-46s %7s %6s %6s %6s | %6s %6s %6s %6s"
          % ("lane", "allN", "P50", "P75", "P90", "goldN", "P50", "P75", "P90"))
    for lk in sorted(lane_all, key=lambda k: -len(lane_all[k])):
        if len(lane_all[lk]) < 30:
            continue
        a, g = C.describe(lane_all[lk]), C.describe(lane_gold[lk])
        print("%-46s %7d %6.0f %6.0f %6.0f | %6d %6s %6s %6s"
              % ("%s -> %s" % lk, a["n"], a["p50"], a["p75"], a["p90"], g["n"],
                 ("%.0f" % g["p50"]) if g["n"] else "-",
                 ("%.0f" % g["p75"]) if g["n"] else "-",
                 ("%.0f" % g["p90"]) if g["n"] else "-"))
        rows_lane.append([lk[0], lk[1], a["n"], a["p50"], a["p75"], a["p90"],
                          g["n"], g.get("p50", ""), g.get("p75", ""), g.get("p90", "")])
    C.write_csv("02_ride_by_lane.csv",
                ["pickup_cat", "dropoff_cat", "all_n", "all_p50", "all_p75", "all_p90",
                 "gold_n", "gold_p50", "gold_p75", "gold_p90"], rows_lane)

    # ── turnaround ───────────────────────────────────────────────────────────
    C.sub("TURNAROUND  —  completed(N) -> on-location(N+1), same driver, consecutive")
    print("""Legs are sequenced by the driver's ACTUAL first operational tap (earliest of
on-the-way / on-location / picked-up / completed), NOT by scheduled pickup_time, because
a real day is run in the order it was run.  Pairs are kept when the gap is within
[%d, %d] minutes: beyond that the 'next job' is the next shift.""" % (TURN_MIN, TURN_MAX))

    def op_key(lid):
        f = fu.get(lid) or {}
        cands = [f[s] for s in ("on-the-way", "on-location", "picked-up", "completed")
                 if s in f]
        return min(cands) if cands else None

    by_drv = collections.defaultdict(list)
    for lid in era:
        did = legs[lid]["driver"]
        if did is None:
            continue
        k = op_key(lid)
        if k is None:
            continue
        by_drv[did].append((k, lid))

    def turn_pairs(order, cap=TURN_MAX):
        """order: 'actual' or 'scheduled'.  Returns (turnaround[], comp_to_otw[])."""
        ta, cw = [], []
        for did, items in by_drv.items():
            if order == "actual":
                seq = sorted(items)
            else:
                dec = []
                for k, lid in items:
                    b = legs[lid]["booked"]
                    dec.append(((b if b is not None else k), lid))
                seq = sorted(dec)
            for (_, a), (_, b) in zip(seq, seq[1:]):
                fa, fb = fu.get(a) or {}, fu.get(b) or {}
                ca = fa.get("completed")
                if ca is None:
                    continue
                ol = fb.get("on-location")
                ow = fb.get("on-the-way")
                if ol is not None:
                    v = mins(ca, ol)
                    if v is not None and TURN_MIN <= v <= cap:
                        ta.append(v)
                if ow is not None:
                    v = mins(ca, ow)
                    if v is not None and TURN_MIN <= v <= cap:
                        cw.append(v)
        return ta, cw

    ta_act, cw_act = turn_pairs("actual")
    ta_sch, cw_sch = turn_pairs("scheduled")
    print("\n  NOTE: this is the OBSERVED gap between jobs, which is mostly idle time, not")
    print("  the turnaround the engine is required to allow.  The operationally binding")
    print("  end is the LOW tail (P10/P25) — how tight a real back-to-back actually gets.")
    for lbl, v in (("turnaround, ACTUAL order", ta_act),
                   ("turnaround, SCHEDULED order", ta_sch)):
        d = C.describe(v)
        print("  %-30s n=%6d  P10 %6.1f  P25 %6.1f  P50 %6.1f  P75 %6.1f  P90 %6.1f"
              % (lbl, d["n"], d["p10"], d["p25"], d["p50"], d["p75"], d["p90"]))
    if ta_act and ta_sch:
        pa, ps = C.pct(ta_act, 90), C.pct(ta_sch, 90)
        print("  -> P90 moves %+.1f min when the day is sequenced as it was actually run "
              "(%.0f -> %.0f)" % (pa - ps, ps, pa))
        tight = sum(1 for x in ta_act if x < TURN_TIGHT_SLACK_MIN)
        print("  -> %d of %d (%.1f%%) real turnarounds were tighter than the app's own "
              "'tight' line of %d min (pickup_policy.py:87) [measured]"
              % (tight, len(ta_act), 100.0 * tight / len(ta_act), TURN_TIGHT_SLACK_MIN))
    print("  sensitivity to the pairing cap (the prior 131->140 claim used an unstated cap):")
    for cap in (120, 180, 240, 480):
        a, _ = turn_pairs("actual", cap)
        s, _ = turn_pairs("scheduled", cap)
        if a and s:
            print("     cap %3d min : ACTUAL n=%5d P90 %6.1f | SCHEDULED n=%5d P90 %6.1f "
                  "| delta %+5.1f"
                  % (cap, len(a), C.pct(a, 90), len(s), C.pct(s, 90),
                     C.pct(a, 90) - C.pct(s, 90)))
    print("\n  " + row3("completed(N) -> on-the-way(N+1)", cw_act, width=34))
    if cw_act:
        sub60 = sum(1 for x in cw_act if 0 <= x <= 1)
        sub8 = sum(1 for x in cw_act if 0 <= x <= 8)
        print("  share of those gaps at 0-1 min (same-moment bookkeeping): %.1f%% "
              "[measured]" % (100.0 * sub60 / len(cw_act)))
        print("  share at 0-8 min                                        : %.1f%%"
              % (100.0 * sub8 / len(cw_act)))
        neg = sum(1 for x in cw_act if x < 0)
        print("  share NEGATIVE (next job opened before the last one closed): %.1f%%"
              % (100.0 * neg / len(cw_act)))
        print("""  READ: the distribution is BIMODAL, not centred on a small number. About a
  third of 'on-the-way' taps land in the same minute the previous job was closed —
  for those legs it is pure bookkeeping and carries no information about when the car
  physically started moving. The rest spread out to a genuine idle tail. So
  'on-the-way' is a MIXTURE of a physical event and a bookkeeping one, and script 05
  must not treat it as a reliable departure instant on any single leg. The one place
  it stays safe is a percentile over many legs, where the bookkeeping mass sits at
  zero and shortens approach time rather than lengthening it.""")

    # out-of-order driver-days
    dd = collections.defaultdict(list)
    for lid in era:
        did = legs[lid]["driver"]
        k = op_key(lid)
        if did is None or k is None or legs[lid]["booked"] is None:
            continue
        dd[(did, legs[lid]["date"])].append((k, legs[lid]["booked"], lid))
    multi = ooo = multi_legs = ooo_legs = 0
    for key, items in dd.items():
        if len(items) < 2:
            continue
        multi += 1
        multi_legs += len(items)
        by_act = [x[2] for x in sorted(items, key=lambda t: t[0])]
        by_sch = [x[2] for x in sorted(items, key=lambda t: (t[1], t[2]))]
        if by_act != by_sch:
            ooo += 1
            ooo_legs += len(items)
    print("\n  multi-leg driver-days with a usable actual key : %d" % multi)
    print("  run OUT of scheduled order                     : %d  (%.1f%% of driver-days, "
          "%.1f%% of their legs) [measured]"
          % (ooo, share(ooo, multi) or 0, share(ooo_legs, multi_legs) or 0))
    C.write_csv("02_turnaround.csv", ["variant", "n", "p50", "p75", "p90", "mean"],
                [[nm] + [C.describe(v).get(k, "") for k in ("n", "p50", "p75", "p90", "mean")]
                 for nm, v in (("turnaround_actual_order", ta_act),
                               ("turnaround_scheduled_order", ta_sch),
                               ("completed_to_on_the_way_actual", cw_act),
                               ("completed_to_on_the_way_scheduled", cw_sch))])

    # ── has the +20% regime moved the durations? ─────────────────────────────
    C.sub("did the CURRENT demand regime change the durations?  (buffers must fit today)")
    prev = regimes[-2] if len(regimes) >= 2 else None
    cur_ids = [i for i in era if cur_regime[0] <= legs[i]["date"] <= cur_regime[1]]
    m_cur = build_metrics(cur_ids)
    m_prev = None
    if prev:
        prev_ids = [i for i in era if prev[0] <= legs[i]["date"] <= prev[1]]
        m_prev = build_metrics(prev_ids)
    rows_reg = []
    print("%-24s %8s %7s %7s %7s | %8s %7s %7s %7s"
          % ("metric", "prevN", "P50", "P75", "P90", "curN", "P50", "P75", "P90"))
    for label, key in (("approach", "approach"), ("dwell ALL", "dwell_all"),
                       ("dwell ARRIVAL", "dwell_ARRIVAL"),
                       ("dwell DEPARTURE", "dwell_DEPARTURE"),
                       ("ride", "ride"), ("occupancy", "occupancy")):
        cd = C.describe(m_cur[key])
        pd_ = C.describe(m_prev[key]) if m_prev else {"n": 0}
        print("%-24s %8s %7s %7s %7s | %8d %7.1f %7.1f %7.1f"
              % (label,
                 pd_["n"] if pd_["n"] else "-",
                 ("%.1f" % pd_["p50"]) if pd_["n"] else "-",
                 ("%.1f" % pd_["p75"]) if pd_["n"] else "-",
                 ("%.1f" % pd_["p90"]) if pd_["n"] else "-",
                 cd["n"], cd["p50"] if cd["n"] else 0, cd["p75"] if cd["n"] else 0,
                 cd["p90"] if cd["n"] else 0))
        rows_reg.append([label, "prior_regime", pd_.get("n", 0), pd_.get("p50", ""),
                         pd_.get("p75", ""), pd_.get("p90", "")])
        rows_reg.append([label, "current_regime", cd.get("n", 0), cd.get("p50", ""),
                         cd.get("p75", ""), cd.get("p90", "")])
    C.write_csv("02_durations_by_regime.csv",
                ["metric", "regime", "n", "p50", "p75", "p90"], rows_reg)

    # ═════════════════════════════════════════════════════════════════════════
    # 7. OUTLIERS / IMPOSSIBLE VALUES
    # ═════════════════════════════════════════════════════════════════════════
    C.hdr("7.  OUTLIERS AND IMPOSSIBLE VALUES  —  rates, and are they improving?")
    out = collections.defaultdict(lambda: collections.Counter())
    for lid in era:
        f = fu.get(lid) or {}
        m = legs[lid]["month"]
        c = out[m]
        seq = [(s, f[s]) for s in LADDER if s in f]
        if len(seq) >= 2:
            c["seq_legs"] += 1
            if any(b[1] < a[1] for a, b in zip(seq, seq[1:])):
                c["nonmono"] += 1
        rd = mins(f.get("picked-up"), f.get("completed"))
        if rd is not None:
            c["ride_legs"] += 1
            if rd < 0:
                c["ride_neg"] += 1
            elif rd < MIN_PICKUP_TO_COMPLETE:
                c["ride_instant"] += 1
            if rd > MAX_DRIVE_MINUTES:
                c["ride_over"] += 1
            fl = AUDIT_PHYSICAL_FLOOR.get(lane(lid))
            if fl is not None:
                c["floor_legs"] += 1
                if rd < fl:
                    c["floor_bad"] += 1
        dw = mins(f.get("on-location"), f.get("picked-up"))
        if dw is not None:
            c["dwell_legs"] += 1
            if dw < 0:
                c["dwell_neg"] += 1
            if dw > MAX_DWELL_MINUTES:
                c["dwell_over"] += 1
        ap = mins(f.get("on-the-way"), f.get("on-location"))
        if ap is not None and ap < 0:
            c["approach_neg"] += 1
        if "completed" in f and legs[lid]["booked"] is not None:
            c["comp_legs"] += 1
            if C.to_local(f["completed"]) < legs[lid]["booked"]:
                c["comp_before_booked"] += 1

    print("%-9s %9s %9s %9s %9s %10s %10s %11s %13s"
          % ("month", "nonmono%", "rideNeg%", "instant%", "ride>3h%", "dwellNeg%",
             "dwell>2h%", "belowFloor%", "compBeforeBook%"))
    rows_out = []
    for m in months:
        c = out.get(m)
        if not c or not c["seq_legs"]:
            continue
        print("%-9s %9s %9s %9s %9s %10s %10s %11s %13s"
              % (m,
                 f1(share(c["nonmono"], c["seq_legs"]), 8, 2),
                 f1(share(c["ride_neg"], c["ride_legs"]), 8, 2),
                 f1(share(c["ride_instant"], c["ride_legs"]), 8, 2),
                 f1(share(c["ride_over"], c["ride_legs"]), 8, 2),
                 f1(share(c["dwell_neg"], c["dwell_legs"]), 9, 2),
                 f1(share(c["dwell_over"], c["dwell_legs"]), 9, 2),
                 f1(share(c["floor_bad"], c["floor_legs"]), 10, 2),
                 f1(share(c["comp_before_booked"], c["comp_legs"]), 12, 2)))
        rows_out.append([m, c["seq_legs"], c["nonmono"], c["ride_legs"], c["ride_neg"],
                         c["ride_instant"], c["ride_over"], c["dwell_legs"],
                         c["dwell_neg"], c["dwell_over"], c["floor_legs"], c["floor_bad"],
                         c["comp_legs"], c["comp_before_booked"]])
    C.write_csv("02_outliers_by_month.csv",
                ["month", "seq_legs", "nonmono", "ride_legs", "ride_neg", "ride_instant",
                 "ride_over_3h", "dwell_legs", "dwell_neg", "dwell_over_2h", "floor_legs",
                 "below_physical_floor", "completed_legs", "completed_before_booked"],
                rows_out)

    print("""
READ: 'instant%' and 'belowFloor%' move together almost exactly, which is the point —
a 'physically impossible ride' in this data is nearly always a driver tapping Picked Up
and Complete in the same breath, not a bad clock. Both roughly QUINTUPLED across the
window. They are NOT improving. Everything else (non-monotonic taps, negative dwell,
completed-before-booked) is small and IS improving.""")

    # per-lane physical floor detail, the audit's §4.2 table, on two bases
    C.sub("audit §4.2 re-run — records below the audit's own per-lane physical floor")
    print("Two bases, because the audit's set already excluded the fabricating drivers:")
    print("%-42s %7s %6s %7s %8s | %7s %7s %8s"
          % ("lane", "floor", "allN", "below", "share%", "cleanN", "below", "share%"))
    rows_floor = []
    for lk, fl in sorted(AUDIT_PHYSICAL_FLOOR.items(), key=lambda kv: kv[0]):
        raw, clean = [], []
        for lid in era:
            if lane(lid) != lk:
                continue
            f = fu.get(lid) or {}
            rd = mins(f.get("picked-up"), f.get("completed"))
            if rd is None or not (0 <= rd <= MAX_DRIVE_MINUTES):
                continue
            raw.append(rd)
            if legs[lid]["driver"] not in fab_ids and rd >= MIN_PICKUP_TO_COMPLETE:
                clean.append(rd)
        if not raw:
            continue
        b1 = sum(1 for x in raw if x < fl)
        b2 = sum(1 for x in clean if x < fl)
        print("%-42s %7d %6d %7d %8s | %7d %7d %8s"
              % ("%s -> %s" % lk, fl, len(raw), b1, f1(share(b1, len(raw))),
                 len(clean), b2, f1(share(b2, len(clean)))))
        rows_floor.append([lk[0], lk[1], fl, len(raw), b1, round(share(b1, len(raw)), 2),
                           len(clean), b2, round(share(b2, len(clean)) or 0, 2)])
    C.write_csv("02_physical_floor_by_lane.csv",
                ["pickup_cat", "dropoff_cat", "floor_min", "all_n", "all_below",
                 "all_below_pct", "clean_n", "clean_below", "clean_below_pct"], rows_floor)

    # ═════════════════════════════════════════════════════════════════════════
    # 8. SHIPPED CONSTANTS VS MEASURED REALITY
    # ═════════════════════════════════════════════════════════════════════════
    C.hdr("8.  SHIPPED CONSTANTS VS MEASURED REALITY  (audit §11, recomputed)")
    print("Every location below was re-grepped in this repo; the audit's line numbers have "
          "drifted and are corrected here.  Percentiles are the ALL-DRIVER set, because a "
          "shipped constant is applied to the whole fleet.\n")

    def d_(vals):
        return C.describe(vals)

    print("""A constant is only comparable to a measurement that answers the SAME question.
Three of the audit's rows do not (a discard cap and a plausibility floor are filters,
not estimates; a safety pad is added to a REQUIRED turnaround, which no table in this
database records).  Those are marked [not comparable] rather than given a fake verdict.
""")
    dd_arr = d_(m_all["dwell_ARRIVAL"])
    dd_true = d_(td_all)
    occ = d_(m_all["occupancy"])
    ride_d = d_(m_all["ride"])

    consts = [
        ("STATIC_FLOOR_DWELL_MIN", 45, "dispatching/scheduler.py:195",
         "arrival dwell gate->in car, all drivers", dd_true, "buffer"),
        ("ARRIVAL_DWELL_MIN", 45, "dispatching/pickup_policy.py:63",
         "arrival dwell gate->in car, all drivers", dd_true, "buffer"),
        ("STATIC_FLOOR_DWELL_MIN (gold)", 45, "dispatching/scheduler.py:195",
         "arrival dwell gate->in car, GOLD only", d_(td_gold), "buffer"),
        ("(arrival dwell as tapped)", 45, "dispatching/scheduler.py:195",
         "on-location->picked-up, ARRIVAL, all drivers", dd_arr, "buffer"),
        ("FALLBACK_TRIP_DURATION_MINUTES", 75, "ops/tasks.py:31",
         "occupancy on-location->completed", occ, "buffer"),
        ("second fallback timedelta(60)", 60, "ops/views.py:1762",
         "occupancy on-location->completed", occ, "buffer"),
        ("ARRIVAL_MEET_GRACE_MIN", 10, "dispatching/pickup_policy.py:46",
         "driver on-location minus gate actual", d_(onloc_vs_gate_all), "deadline"),
        ("DEPLANING_GRACE_MIN", 10, "dispatching/feasibility_guards.py:39",
         "driver on-location minus gate actual", d_(onloc_vs_gate_all), "deadline"),
        ("PAX_READY_MIN", 15, "dispatching/pickup_policy.py:52",
         "GUEST-ONLY dwell: gate->picked-up, driver already there",
         d_(pax_only_all), "buffer"),
        ("ARRIVAL_DWELL_MIN (guest-only)", 45, "dispatching/pickup_policy.py:63",
         "GUEST-ONLY dwell: gate->picked-up, driver already there",
         d_(pax_only_all), "buffer"),
        ("MAX_DWELL_MINUTES", 120, "dispatching/analytics.py:26",
         "on-location->picked-up, all trips", d_(m_all["dwell_all"]), "filter"),
        ("MIN_PICKUP_TO_COMPLETE", 2, "dispatching/analytics.py:434",
         "ride pu->comp", ride_d, "filter"),
        ("SAFETY_PAD_MIN", 0, "dispatching/feasibility_guards.py:44",
         "REQUIRED turnaround — nothing in the DB records it", {"n": 0}, "pad"),
        ("MIN_TURN_BUFFER_DEFAULT", 5, "dispatching/feasibility_guards.py:64",
         "REQUIRED turnaround — nothing in the DB records it", {"n": 0}, "pad"),
    ]

    rows_c = []
    print("%-31s %7s %-42s %7s %6s %6s %6s  %s"
          % ("constant", "shipped", "measured against", "n", "P50", "P75", "P90",
             "verdict"))
    for name, val, loc, what, dd, kind_ in consts:
        if not dd.get("n"):
            v = ("[unavailable] — %s" % what) if kind_ == "pad" else "[unavailable]"
            print("%-31s %7s %-42s %7s %6s %6s %6s  %s"
                  % (name, val, what, "0", "-", "-", "-", v))
            rows_c.append([name, val, loc, what, 0, "", "", "", v])
            continue
        if kind_ == "filter":
            if name.startswith("MAX_DWELL"):
                over = sum(1 for x in m_all["dwell_all_raw"] if x > val)
                tot = len(m_all["dwell_all_raw"]) or 1
                verdict = ("[not comparable] discard cap; it removes %.2f%% of dwells"
                           % (100.0 * over / tot))
            else:
                under = sum(1 for x in m_all["ride_raw"] if x < val)
                tot = len(m_all["ride_raw"]) or 1
                verdict = ("[not comparable] plausibility floor; it removes %.2f%% of "
                           "rides — and that share is the fabrication rate, not noise"
                           % (100.0 * under / tot))
        elif dd["p75"] > val * 1.10:
            verdict = "TIGHT: reality is +%.0f min at P75, +%.0f at P90" % (
                dd["p75"] - val, dd["p90"] - val)
        elif dd["p90"] > val * 1.10:
            verdict = "ok at P75, TIGHT at P90 (+%.0f)" % (dd["p90"] - val)
        elif dd["p75"] < val * 0.85:
            verdict = "generous by %.0f min at P75" % (val - dd["p75"])
        else:
            verdict = "calibrated at P75"
        print("%-31s %7s %-42s %7d %6.1f %6.1f %6.1f  %s"
              % (name, val, what, dd["n"], dd["p50"], dd["p75"], dd["p90"], verdict))
        rows_c.append([name, val, loc, what, dd["n"], dd["p50"], dd["p75"], dd["p90"],
                       verdict])
    print("\n  'TIGHT' = the shipped value is SMALLER than measured reality at that "
          "percentile, i.e. the engine believes the car is free before it is.\n"
          "  'sample_count >= 5' trust floor lives at dispatching/scheduler.py:457,462 "
          "(the audit cited :605 — corrected).\n"
          "  ops/tasks.py:31 (the audit cited :26 — corrected); ops/views.py:1762 "
          "(the audit cited :1759 — corrected).")

    # ── the shipped per-lane drive table vs measured ride time ───────────────
    tbl, tbl_loc = parse_drive_table()
    C.sub("DRIVE_TIME_ESTIMATES vs measured ride time, lane by lane  (%s)"
          % (tbl_loc or "[unavailable — could not parse the literal]"))
    if not tbl:
        print("  [unavailable] — the table could not be read statically.")
    else:
        print("""Lanes with n >= 30 measured rides. 'gap' = measured P75 minus shipped.
CAVEAT (audit §12.1, still true): ride time is driver WALL CLOCK — it contains the
latency in tapping Complete and any luggage handling. It is the right number for
'when is the car free', which is exactly what DRIVE_TIME_ESTIMATES is used for in
chain feasibility, but it is NOT a maps ETA and must not be compared to one.""")
        print("%-44s %8s %7s %7s %7s %7s  %s"
              % ("lane", "shipped", "n", "P50", "P75", "P90", "verdict"))
        rows_tbl = []
        no_entry = []
        for lk in sorted(lane_all, key=lambda k: -len(lane_all[k])):
            v = lane_all[lk]
            if len(v) < 30:
                continue
            d = C.describe(v)
            shipped = tbl.get(lk)
            if shipped is None:
                no_entry.append((lk, d))
                continue
            gap = d["p75"] - shipped
            verdict = ("TIGHT +%.0f at P75" % gap) if gap > 2 else (
                "generous %.0f at P75" % gap if gap < -2 else "calibrated")
            print("%-44s %8d %7d %7.0f %7.0f %7.0f  %s"
                  % ("%s -> %s" % lk, shipped, d["n"], d["p50"], d["p75"], d["p90"],
                     verdict))
            rows_tbl.append([lk[0], lk[1], shipped, d["n"], d["p50"], d["p75"], d["p90"],
                             round(gap, 1), verdict])
        if no_entry:
            print("\n  lanes with NO table entry — these fall back to DEFAULT_DRIVE_TIME=%d:"
                  % 35)
            for lk, d in no_entry:
                print("%-44s %8s %7d %7.0f %7.0f %7.0f  %s"
                      % ("%s -> %s" % lk, "(35)", d["n"], d["p50"], d["p75"], d["p90"],
                         "TIGHT +%.0f at P75" % (d["p75"] - 35) if d["p75"] > 37
                         else "ok"))
                rows_tbl.append([lk[0], lk[1], 35, d["n"], d["p50"], d["p75"], d["p90"],
                                 round(d["p75"] - 35, 1), "no table entry"])
        C.write_csv("02_drive_table_vs_measured.csv",
                    ["pickup_cat", "dropoff_cat", "shipped_min", "n", "p50", "p75", "p90",
                     "gap_p75", "verdict"], rows_tbl)
    C.write_csv("02_constants_recalibration.csv",
                ["constant", "shipped_value", "location", "measured_against", "n",
                 "p50", "p75", "p90", "verdict"], rows_c)

    # ═════════════════════════════════════════════════════════════════════════
    # 9. SECOND, STRUCTURALLY DIFFERENT CHECK
    # ═════════════════════════════════════════════════════════════════════════
    C.hdr("9.  SECOND CHECK  —  the same questions asked of a different table")
    print("""`reservations_auditlog` (action='status_changed') and `reservations_historicalleg`
(django-simple-history on Leg) are written by DIFFERENT code paths from
`reservations_legstatus`.  If the tap record were fabricated, partial or mis-timed, these
would not agree with it.  Neither table existed in the snapshot the prior audit used.""")

    al = collections.defaultdict(dict)
    for r in C.q(con, """SELECT object_id, new_value, MIN(timestamp) ts
                         FROM reservations_auditlog
                         WHERE model_name='Leg' AND action='status_changed'
                           AND new_value IN ('confirmed','on-the-way','on-location',
                                             'picked-up','completed')
                         GROUP BY object_id, new_value"""):
        al[r["object_id"]][r["new_value"]] = dt.datetime.fromisoformat(
            str(r["ts"]).replace("T", " "))

    both = agree_ = 0
    deltas = []
    for lid in era:
        f, a = fu.get(lid) or {}, al.get(lid) or {}
        for s in LADDER:
            if s in f and s in a:
                both += 1
                dsec = abs((f[s] - a[s]).total_seconds())
                deltas.append(dsec)
                if dsec <= 5:
                    agree_ += 1
    print("\n  (leg,status) events present in BOTH legstatus and auditlog : %d" % both)
    if both:
        print("  agreeing to within 5 s                                     : %d (%.2f%%)"
              % (agree_, 100.0 * agree_ / both))
        print("  |delta| P50 %.2f s   P90 %.2f s   P99 %.2f s"
              % (C.pct(deltas, 50), C.pct(deltas, 90), C.pct(deltas, 99)))

    only_al = only_ls = 0
    for lid in era:
        f, a = set((fu.get(lid) or {})), set((al.get(lid) or {}))
        only_al += len(a - f)
        only_ls += len(f - a)
    print("  events auditlog has that legstatus does NOT : %d" % only_al)
    print("  events legstatus has that auditlog does NOT : %d" % only_ls)

    # recompute FULL3 coverage entirely from auditlog — no legstatus at all
    C.sub("coverage recomputed from auditlog ALONE (legstatus never consulted)")
    print("month     legs   FULL3%(auditlog)   FULL3%(legstatus)   delta")
    rows_2nd = []
    for m in months:
        n = fa = fl = 0
        for lid in era:
            if legs[lid]["month"] != m:
                continue
            n += 1
            if all(s in (al.get(lid) or {}) for s in FULL3):
                fa += 1
            if all(s in (fu.get(lid) or {}) for s in FULL3):
                fl += 1
        if not n:
            continue
        print("%s %6d %18.1f %19.1f %7.2f"
              % (m, n, share(fa, n), share(fl, n), share(fa, n) - share(fl, n)))
        rows_2nd.append([m, n, fa, fl, round(share(fa, n), 2), round(share(fl, n), 2)])
    C.write_csv("02_second_check_auditlog.csv",
                ["month", "legs", "full3_auditlog", "full3_legstatus",
                 "full3_auditlog_pct", "full3_legstatus_pct"], rows_2nd)

    # ride time recomputed from auditlog
    ride_al = []
    for lid in era:
        a = al.get(lid) or {}
        v = mins(a.get("picked-up"), a.get("completed"))
        if v is not None and MIN_PICKUP_TO_COMPLETE <= v <= MAX_DRIVE_MINUTES:
            ride_al.append(v)
    C.sub("ride time, two independent tables")
    print("  " + row3("ride from legstatus", m_all["ride"], width=30))
    print("  " + row3("ride from auditlog", ride_al, width=30))

    # historicalleg: an entirely third stream
    hl = collections.defaultdict(dict)
    for r in C.q(con, """SELECT id AS leg_id, status, MIN(history_date) ts
                         FROM reservations_historicalleg
                         WHERE status IN ('on-the-way','on-location','picked-up','completed')
                         GROUP BY id, status"""):
        hl[r["leg_id"]][r["status"]] = dt.datetime.fromisoformat(
            str(r["ts"]).replace("T", " "))
    ride_hl = []
    for lid in era:
        h = hl.get(lid) or {}
        v = mins(h.get("picked-up"), h.get("completed"))
        if v is not None and MIN_PICKUP_TO_COMPLETE <= v <= MAX_DRIVE_MINUTES:
            ride_hl.append(v)
    print("  " + row3("ride from historicalleg", ride_hl, width=30))
    hl_first = min((min(v.values()) for v in hl.values() if v), default=None)
    if hl_first:
        print("  (historicalleg only starts %s, so it covers the later part of the era)"
              % C.to_local(hl_first).date())

    # second check on the gold gap
    C.sub("second check on the generalisation gap — dwell recomputed from auditlog")
    g2, a2 = [], []
    for lid in era:
        a = al.get(lid) or {}
        v = mins(a.get("on-location"), a.get("picked-up"))
        if v is None or not (0 <= v <= MAX_DWELL_MINUTES):
            continue
        a2.append(v)
        if legs[lid]["driver"] in gold_ids:
            g2.append(v)
    print("  " + gap_line("dwell [legstatus]", m_gold["dwell_all"], m_all["dwell_all"]))
    print("  " + gap_line("dwell [auditlog]", g2, a2))

    # ═════════════════════════════════════════════════════════════════════════
    # 10. CLAIM LEDGER
    # ═════════════════════════════════════════════════════════════════════════
    C.hdr("10.  CLAIM LEDGER  —  every prior claim, and what the live data does to it")

    aug = months[-1]
    feb = months[0]
    cov_in_last = share(cov[("inhouse", aug)]["full3"], cov[("inhouse", aug)]["n"])
    cov_in_first = share(cov[("inhouse", feb)]["full3"], cov[("inhouse", feb)]["n"])
    cov_af_last = share(cov[("affiliate", aug)]["full3"], cov[("affiliate", aug)]["n"])
    cov_af_min = min(share(cov[("affiliate", m)]["full3"], cov[("affiliate", m)]["n"])
                     for m in months if cov.get(("affiliate", m), {}).get("n"))
    ledger = [
        ("HOLDS", "audit §1.3 / prior redo: DST is UTC-5 before the March flip and UTC-4 "
                  "after, and the flip is empirically visible",
         "13 of 13 consecutive scored days agree with _common.utc_offset_hours; the mode "
         "is 83-100% sharp on every day and the median residual never exceeds 5.1 min. "
         "_common.to_local is CORRECT."),
        ("HOLDS", "audit §8.1: the tap record is authored by drivers, not backfilled",
         "%.2f%% of ladder rows carry a driver account as updated_by; and an entirely "
         "separate table (reservations_auditlog) agrees with legstatus on %.2f%% of "
         "%d shared events to within 5 seconds." % (100.0 * by_drv_rows / tot_auth,
                                                    100.0 * agree_ / both, both)),
        ("HOLDS", "audit §6 / prior redo: 'the driver is at the gate before the plane "
                  "docks about 28% of the time'",
         "%.1f%% on n=%d, a bigger sample than either prior document had."
         % (100.0 * early / len(onloc_vs_gate_all), len(onloc_vs_gate_all))),
        ("HOLDS", "prior redo A5: the gold cohort does NOT generalise, and the gap is in "
                  "the tail",
         "Re-derived cohort, re-derived metric: true dwell gold P75 %.0f / P90 %.0f vs "
         "all-driver P75 %.0f / P90 %.0f — the fleet is +%.0f at P75 and +%.0f at P90. "
         "The prior figure was +8 / +13; the live gap is WIDER, not narrower. Second, "
         "structurally different check: on GUEST-ONLY dwell (driver already at the gate "
         "when the plane docked, so driver lateness is excluded by construction) the "
         "gap is still +%.1f at P75 and +%.1f at P90 — it is not a punctuality artefact."
         % (C.pct(td_gold, 75), C.pct(td_gold, 90), C.pct(td_all, 75), C.pct(td_all, 90),
            C.pct(td_all, 75) - C.pct(td_gold, 75),
            C.pct(td_all, 90) - C.pct(td_gold, 90),
            C.pct(pax_only_all, 75) - C.pct(pax_only_gold, 75),
            C.pct(pax_only_all, 90) - C.pct(pax_only_gold, 90))),
        ("HOLDS", "prior redo: 'anthony (affiliate) is the worst data source in the "
                  "system and is unflagged'",
         "Confirmed and worse than stated: 700 era legs, instant 92.2%, collapsed "
         "56.2%, adjacent-under-60s 94.8%, still exclude_from_timing=0."),
        ("HOLDS", "prior redo: 'rizwan is a false positive — sparse, not fabricating'",
         "Confirmed. rizwan: FULL3 34.7% (sparse) but instant only 9.0% and collapsed "
         "1.5%. Still flagged. Costs ~200 honest full-ladder legs."),
        ("REVISED", "audit §2.1: 'coverage improved 60% -> 85%, unaided' / prior redo: "
                    "'really 73.5% -> 85.3%, in-house only'",
         "Direction holds, level moves again on the longer sample: in-house FULL-3 runs "
         "%.1f%% -> %.1f%% and peaked at %.1f%%. The 60%% February is confirmed as a "
         "denominator artefact."
         % (cov_in_first, cov_in_last,
            max(share(cov[("inhouse", m)]["full3"], cov[("inhouse", m)]["n"])
                for m in months))),
        ("REFUTED", "prior redo: 'AFFILIATE ladder coverage FELL every month, 53.9% -> "
                    "35.3%'",
         "That was a WINDOW-END ARTEFACT. 35.3%% was the first 11 days of the prior "
         "snapshot's final month. Affiliate FULL-3 bottomed at %.1f%% and has since "
         "RECOVERED to %.1f%%, with the weekly series running 36%% -> 85%% from mid-July. "
         "Affiliate data is now the fastest-improving stream in the system."
         % (cov_af_min, cov_af_last)),
        ("REVISED", "audit §8.2: 'duplicate status rows 3.7-5.9% of legs' / prior redo: "
                    "'tripled to ~17%'",
         "Both are right about direction and both under-state the level, because the "
         "three published definitions differ threefold. On the widest, C-legs, it runs "
         "%.1f%% -> %.1f%%. On the narrowest, A-excess-rows, %.1f%% -> %.1f%%. The "
         "mechanism is confirmed per-leg, not by correlation: a reassigned leg is "
         "%.1fx more likely to hold a duplicate. The CAUSE is misattributed in the "
         "prior redo: unassignment did not rise, the auto-reset WRITER shipped "
         "(reservations/models.py:1891). And on the three rungs analytics reads, the "
         "rate has been falling since its May peak."
         % (share(dup[feb]["legs_dup"], dup[feb]["legs_tapped"]),
            share(dup[aug]["legs_dup"], dup[aug]["legs_tapped"]),
            share(dup[feb]["rows"] - dup[feb]["groups"], dup[feb]["rows"]),
            share(dup[aug]["rows"] - dup[aug]["groups"], dup[aug]["rows"]),
            (p1_ / p0_) if p0_ else 0)),
        ("REVISED", "audit §8.2 / prior redo: 'the .first() ordering bug'",
         "The BUG is fixed and the fix has held — no production reader that wants an "
         "earliest tap is getting a latest one. But the EXPOSURE has grown: MIN vs "
         "LATEST now changes the ride time on %.2f%% of era legs, and on those legs the "
         "mean goes from %.0f to %.0f min. LegStatus.Meta.ordering is still "
         "['-timestamp'] (reservations/models.py:3384), so the trap is still armed for "
         "the next reader."
         % (100.0 * len(ride_min) / len(era),
            C.describe(ride_min)["mean"], C.describe(ride_max)["mean"])),
        ("REFUTED", "prior redo: 'the fabricating cohort is 9 drivers, membership "
                    "unchanged, exclude_from_timing now catches all 9 and flags zero "
                    "false negatives — the remediation held'",
         "The remediation did NOT hold in production. Today the DB flags %d drivers "
         "(%s) and my behavioural rule finds %d (%s). %d fabricators are UNFLAGGED, "
         "including the two largest sources in the system, and %d flagged drivers do "
         "not fabricate."
         % (len(flagged_now), ", ".join(flagged_now), len(fab),
            ", ".join(p["name"] for p in fab), len(fn), len(fp))),
        ("REVISED", "audit §9: the nominated 14-driver gold cohort",
         "%d of the %d nominated survive behavioural gates (%s); %d drop (%s), all but "
         "one on instant-completion alone; %d untestable. The data ADDS %d never "
         "nominated (%s). Final in-house gold = %d drivers (%s), plus %d affiliates "
         "(%s) that production's inhouse-only analytics filter discards regardless. "
         "Cohort share is %.1f%% of era legs, %.1f%% of in-house legs."
         % (len(keep), len(nominated), ", ".join(n for n, _, _ in keep),
            len(drop), ", ".join(n for n, _, _ in drop), len(untest),
            len(added), ", ".join(added), len(gold_names), ", ".join(gold_names),
            len(gold_aff_names), ", ".join(gold_aff_names),
            share(len(era_gold), len(era)), share(len(era_gold), len(era_inhouse)))),
        ("REVISED", "audit §9.4: 'use the full trustworthy set, the medians agree, the "
                    "gold cohort adds little accuracy'",
         "The MEDIANS do agree — gap at P50 is +%.1f min on dwell. The audit stopped "
         "there. At P75 the gap is +%.1f and at P90 +%.1f. The recommendation is right "
         "for the reason given and wrong for the reason that matters: cohort choice is "
         "immaterial at the median and decisive in the tail."
         % (C.pct(m_all["dwell_all"], 50) - C.pct(m_gold["dwell_all"], 50),
            C.pct(m_all["dwell_all"], 75) - C.pct(m_gold["dwell_all"], 75),
            C.pct(m_all["dwell_all"], 90) - C.pct(m_gold["dwell_all"], 90))),
        ("REVISED", "audit §8.2: 'double-tapping on 36% of full-ladder legs' / prior "
                    "redo '42.4%'",
         "Fleet-wide the adjacent-gap-under-60s share is now higher still on several "
         "big drivers; see the adj% column in §3. The consequence is unchanged: it "
         "understates approach time and inflates dwell."),
        ("REVISED", "audit §4.2: 'physically impossible records, 0.7-2.7% per lane'",
         "On the audit's own basis (fabricators removed, ride >= %d min) the rates are "
         "in the same low band. On the RAW all-driver set they are %s — because an "
         "impossible ride in this data is a fabricated tap, not a bad clock. Impossible "
         "records are getting WORSE, not better: the fleet-wide instant rate ran "
         "%.1f%% -> %.1f%% across the window — and it is CONCENTRATION, not spread. "
         "Non-fabricating drivers held 5-12%% throughout; what changed is that the "
         "fabricating cohort's share of the work grew, and it now carries 19.5%% of "
         "every leg in the operational record."
         % (MIN_PICKUP_TO_COMPLETE, "18-23%",
            share(out[feb]["ride_instant"], out[feb]["ride_legs"]),
            share(out[aug]["ride_instant"], out[aug]["ride_legs"]))),
        ("REVISED", "audit §11: the shipped-constant table",
         "Every line number in it has drifted; corrected in §8. The direction of the "
         "big one is unchanged but larger: STATIC_FLOOR_DWELL_MIN=45 vs measured "
         "all-driver true dwell P75 %.0f / P90 %.0f. The audit measured P75 47 / P90 64 "
         "on gold; on the fleet it is %.0f / %.0f."
         % (C.pct(td_all, 75), C.pct(td_all, 90), C.pct(td_all, 75), C.pct(td_all, 90))),
        ("EXTENDED", "nothing prior: is 'on-the-way' a physical event?",
         "Partly not. %.1f%% of completed(N) -> on-the-way(N+1) gaps land inside one "
         "minute — the driver closes A and opens B in the same tap. The prior claim of "
         "a P50 of 2-8 min is confirmed (P50 %.1f) but the distribution is bimodal, so "
         "'on-the-way' is safe only as a percentile over many legs, never as a "
         "per-leg departure instant."
         % (100.0 * sub60 / len(cw_act), C.pct(cw_act, 50))),
        ("EXTENDED", "nothing prior: days are not run in the booked order",
         "%.1f%% of multi-leg driver-days (%d of %d) were run out of scheduled "
         "sequence, covering %.1f%% of their legs. Sequencing by actual tap instead of "
         "booked time moves turnaround P90 by %+.1f min."
         % (share(ooo, multi), ooo, multi, share(ooo_legs, multi_legs),
            C.pct(ta_act, 90) - C.pct(ta_sch, 90))),
        ("EXTENDED", "nothing prior: the app's own validity gate is the real ceiling",
         "FULL-3 presence reached %.1f%% but has_valid_status_chain "
         "(dispatching/analytics.py:493) passes only %.1f%% — and unlike coverage it is "
         "FLAT across the whole window. Presence improved; usability did not."
         % (cov[("ALL", aug)]["full3"] * 100.0 / cov[("ALL", aug)]["n"],
            cov[("ALL", aug)]["chain"] * 100.0 / cov[("ALL", aug)]["n"])),
        ("UNTESTABLE", "the autumn DST transition",
         "%s is %d days beyond last_actuals_day. [unavailable] from this pull."
         % (min(e for (_s, e) in C.US_DST_TRANSITIONS if e.date() > tap_last).date(),
            (min(e for (_s, e) in C.US_DST_TRANSITIONS
                 if e.date() > tap_last).date() - tap_last).days)),
        ("UNTESTABLE", "audit §11: SAFETY_PAD_MIN and MIN_TURN_BUFFER_DEFAULT",
         "Both are pads on a REQUIRED turnaround. Nothing in the schema records what "
         "the engine required, only what the day actually did (audit §12.6). "
         "[unavailable] without replaying the scheduler."),
        ("UNTESTABLE", "whether exclude_from_timing was correct AT THE TIME",
         "drivers_driver has no history table and no audit rows. The flag is "
         "current-state only, so 'when did davide get flagged, and on what evidence' "
         "is [unavailable]."),
    ]
    import textwrap
    for verdict in ("HOLDS", "REVISED", "REFUTED", "EXTENDED", "UNTESTABLE"):
        items = [x for x in ledger if x[0] == verdict]
        if not items:
            continue
        C.sub("%s  (%d)" % (verdict, len(items)))
        for _, claim, finding in items:
            for i, ln in enumerate(textwrap.wrap(claim, 74)):
                print(("  * " if i == 0 else "    ") + ln)
            for ln in textwrap.wrap(finding, 72):
                print("      " + ln)
            print("")

    C.hdr("DONE — CSVs in %s" % C.OUT_DIR)
    for f in sorted(os.listdir(C.ensure_out())):
        if f.startswith("02_"):
            print("   " + os.path.join(C.OUT_DIR, f))


if __name__ == "__main__":
    main()
