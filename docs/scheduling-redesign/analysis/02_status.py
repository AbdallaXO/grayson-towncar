#!/usr/bin/env python
"""
02_status.py -- Re-validation of docs/operational-data-audit.md (generated 2026-07-31,
window 2026-02-08..2026-07-11) against the 2026-08-21 production snapshot, extended to
window 2026-02-08..2026-08-21.

READ-ONLY. Run from the repo root:   python docs/scheduling-redesign/analysis/02_status.py

Outputs: stdout report + CSVs in docs/scheduling-redesign/analysis/out/02_status_*.csv
"""
import csv
import collections
import os
import sqlite3
import statistics
import sys
from datetime import datetime, date, timedelta

sys.path.insert(0, os.getcwd())   # so `import business.settings` resolves when run by path

DB = "file:content/db.sqlite3?mode=ro"
OUT = os.path.join("docs", "scheduling-redesign", "analysis", "out")

AUDIT_START = date(2026, 2, 8)
AUDIT_END = date(2026, 7, 11)      # prior audit window end
NEW_END = date(2026, 8, 21)        # snapshot date
DST_FLIP = date(2026, 3, 8)        # US DST: UTC-5 before, UTC-4 on/after

LADDER = ["confirmed", "on-the-way", "on-location", "picked-up", "completed"]
TAP_LABEL = {"confirmed": "accept(confirmed)", "on-the-way": "on-the-way",
             "on-location": "on-location", "picked-up": "picked-up",
             "completed": "completed"}

# Audit-nominated gold cohort (docs/operational-data-audit.md:489)
GOLD_AUDIT_NAMES = ["Michael", "sereen", "yovanny", "steven", "junaid", "angel", "runer",
                    "roberto", "lev", "george", "davide", "Charlie", "Aftab", "oualid"]

# Prior-audit figures we are re-running against (doc line refs in the report text)
PRIOR_COVERAGE = {  # audit sec 2.1: month -> (legs, full-ladder, coverage%)
    "2026-02": (1999, 1208, 60), "2026-03": (2707, 1878, 69), "2026-04": (2808, 2001, 71),
    "2026-05": (2914, 2284, 78), "2026-06": (2799, 2367, 85), "2026-07": (989, 783, 79)}
PRIOR_DISCIPLINE = {  # audit sec 8.4: name -> (legs, fullladder%, instant%, median_ride)
    "ernesto": (115, 95, 95, 76), "ken": (572, 99, 93, 43), "AldoH": (67, 94, 73, 40),
    "Idrees": (140, 99, 68, 36), "Francisco": (80, 100, 68, 43), "placeholder": (34, 9, 67, 44),
    "Raymond": (272, 89, 66, 38), "mesfin": (143, 99, 65, 8), "neuma": (616, 2, 54, 50),
    "shelley": (125, 50, 29, 35), "Rayyan": (42, 48, 25, 41), "sereen": (411, 98, 20, 34),
    "rizwan": (453, 17, 18, 36), "Hasan": (128, 99, 17, 38), "runer": (608, 98, 14, 34),
    "angel": (481, 99, 13, 37), "abdi": (25, 32, 12, 45), "george": (581, 100, 12, 36),
    "Seline": (500, 100, 12, 37), "davide": (722, 97, 12, 35), "yovanny": (675, 99, 8, 35),
    "shipo": (251, 98, 8, 37), "lev": (41, 98, 8, 32), "Aftab": (387, 100, 6, 37),
    "Michael": (468, 100, 5, 32), "junaid": (599, 99, 5, 34), "HassanA": (135, 99, 4, 35),
    "alex": (376, 38, 4, 34), "roberto": (870, 89, 3, 32), "julio": (178, 99, 3, 34),
    "steven": (399, 99, 2, 35), "carlos": (36, 97, 0, 38), "Charlie": (30, 100, 0, 35)}
# audit sec 9.3 gold arrival anatomy: metric -> (n, p25, p50, p75, p90)
PRIOR_GOLD_ANATOMY = {"approach": (6404, 10, 25, 43, 67), "curb": (6425, 7, 19, 37, 58),
                      "occupancy": (2483, 53, 69, 88, 111)}
# audit sec 9.2 gold lane ride medians/p75
PRIOR_GOLD_LANES = {("Disney Resort", "MCO Terminal"): (2388, 33, 36),
                    ("MCO Terminal", "Disney Resort"): (2129, 36, 44),
                    ("Port Canaveral Area", "MCO Terminal"): (192, 50, 55),
                    ("Universal Resort", "MCO Terminal"): (155, 24, 27),
                    ("MCO Terminal", "Port Canaveral Area"): (145, 51, 59),
                    ("MCO Terminal", "Universal Resort"): (133, 29, 37),
                    ("Disney Resort", "SFB Terminal"): (130, 59, 63),
                    ("SFB Terminal", "Disney Resort"): (89, 60, 72)}

# ---------------------------------------------------------------- helpers
def P(s):
    if s is None:
        return None
    s = str(s).replace("T", " ").split("+")[0].strip()
    if "." in s:
        s = s.split(".")[0]
    if len(s) == 10:
        s += " 00:00:00"
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


def pct(vals, q):
    """Nearest-rank on a sorted list -- SAME index arithmetic the prior audit used
    (v[len(v)//2] for p50, v[int(q*len(v))] for the rest) so figures are comparable."""
    if not vals:
        return None
    v = sorted(vals)
    if q == 0.5:
        return v[len(v) // 2]
    i = min(int(q * len(v)), len(v) - 1)
    return v[i]


def fmt(x, nd=1):
    return "-" if x is None else ("%.*f" % (nd, x))


def local_to_utc(d, t_str):
    """Naive Florida-local pickup -> UTC. Offset flips at 2026-03-08 (verified in TEST 5)."""
    if t_str is None:
        return None
    t_str = str(t_str).split(".")[0]
    parts = t_str.split(":")
    while len(parts) < 3:
        parts.append("00")
    try:
        dt = datetime(d.year, d.month, d.day, int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError:
        return None
    return dt + timedelta(hours=5 if d < DST_FLIP else 4)


def w(name, header, rows):
    p = os.path.join(OUT, "02_status_%s.csv" % name)
    with open(p, "w", newline="", encoding="utf-8") as f:
        cw = csv.writer(f)
        cw.writerow(header)
        cw.writerows(rows)
    return p


def hr(title):
    print("\n" + "=" * 100)
    print(title)
    print("=" * 100)


# ---------------------------------------------------------------- setup
os.makedirs(OUT, exist_ok=True)
con = sqlite3.connect(DB, uri=True)
cur = con.cursor()

CATEGORIZER = "unavailable"
try:
    os.environ.setdefault("ENABLE_DEBUG_TOOLBAR", "0")
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "business.settings")
    import django
    django.setup()
    from dispatching.analytics import categorize_location
    CATEGORIZER = "dispatching.analytics.categorize_location (production function)"
except Exception as e:                                                # pragma: no cover
    _err = repr(e)

    def categorize_location(s):
        s = (s or "").lower()
        if any(k in s for k in ("port canaveral", "cocoa beach", "cruise terminal")):
            return "Port Canaveral Area"
        if "sanford" in s or "sfb" in s:
            return "SFB Terminal"
        if "mco" in s or "orlando international" in s:
            return "MCO Terminal"
        if "disney" in s or "epcot" in s or "magic kingdom" in s:
            return "Disney Resort"
        if "universal" in s:
            return "Universal Resort"
        return "Other"
    CATEGORIZER = "STRING-MATCH APPROXIMATION [inferred] -- django import failed: %s" % _err

print("=" * 100)
print("02_status -- IS THE PRIOR AUDIT'S RELIABILITY VERDICT STILL VALID? (window extended to 2026-08-21)")
print("=" * 100)
print("ASSUMPTIONS DECLARED UP FRONT:")
print("  A1  Snapshot opened read-only: %s" % DB)
print("  A2  Window = leg.pickup_date in [2026-02-08, 2026-08-21]. Prior-audit window = [2026-02-08, 2026-07-11].")
print("  A3  'accept' tap is stored as status='confirmed' (drivers/views.py:739 accept_job); there is no")
print("      literal 'accept' string in reservations_legstatus.")
print("  A4  Non-cancelled leg = leg.status NOT IN ('cancelled') AND reservation.status NOT IN ('cancelled','canceled').")
print("  A5  First occurrence only: MIN(timestamp) per (leg,status) -- prior audit filter 1.4.1.")
print("  A6  legstatus.timestamp is UTC; leg.pickup_date/pickup_time are naive Florida local.")
print("      Local->UTC offset = +5h before 2026-03-08, +4h on/after (empirically re-verified in TEST 5).")
print("  A7  Percentiles use the prior audit's nearest-rank indexing (v[len//2], v[int(q*len)]) for comparability.")
print("  A8  Ride/approach/curb/occupancy plausibility bound = 2..240 min (prior audit filter 1.4.3).")
print("  A9  Driver attribution uses leg.driver_id (the CURRENT assignee), as the prior audit did. Taps made")
print("      by a previously-assigned driver are therefore credited to the current one; TEST 3d quantifies this.")
print("  A10 Location bucketing: %s" % CATEGORIZER)
print("  A11 August 2026 is doubly partial: legs end 2026-08-21 AND the tap log ends 2026-08-21 18:45 UTC")
print("      (=14:45 local), so late-day 08-21 legs cannot have completed taps yet.")
print("  A12 Driver display name = auth_user.username joined via drivers_driver.profile_id (profile FK is to User).")

# ---------------------------------------------------------------- load
drivers = {}
for did, uname, dtype, active, excl in cur.execute(
        "SELECT d.id, u.username, d.driver_type, d.is_active, d.exclude_from_timing "
        "FROM drivers_driver d LEFT JOIN auth_user u ON d.profile_id = u.id"):
    drivers[did] = {"name": uname or ("driver#%s" % did), "type": dtype,
                    "active": active, "excl": bool(excl)}
name2id = {v["name"]: k for k, v in drivers.items()}
prof2driver = {}
for did, pid in cur.execute("SELECT id, profile_id FROM drivers_driver"):
    prof2driver[pid] = did

legs = {}
for (lid, pd, pt, pl, dl, did, st, rst, exan) in cur.execute(
        "SELECT l.id, l.pickup_date, l.pickup_time, l.pickup_location, l.dropoff_location, "
        "l.driver_id, l.status, r.status, l.exclude_from_analytics "
        "FROM reservations_leg l LEFT JOIN reservations_reservation r ON l.reservation_id = r.id"):
    legs[lid] = dict(pd=pd, pt=pt, pl=pl, dl=dl, did=did, st=st, rst=rst, exan=exan)

# events: leg -> status -> [min_ts, row_count]; plus note tallies
ev = collections.defaultdict(dict)
dupe_rows = collections.Counter()
note_rows = collections.Counter()
author = collections.defaultdict(set)
for lid, s, ts, n, ub in cur.execute(
        "SELECT leg_id, status, timestamp, COALESCE(notes,''), updated_by_id FROM reservations_legstatus"):
    d = ev[lid]
    p = P(ts)
    if s not in d or p < d[s][0]:
        d[s] = [p, d.get(s, [None, 0])[1]]
    d[s][1] = d[s][1] + 1
    note_rows[n] += 1
    if s in LADDER and ub:
        author[(lid, s)].add(ub)

# ---------------------------------------------------------------- TEST 0
hr("TEST 0 -- CONFIRM/REFUTE THE STATED GROUND TRUTH  [measured]")
n_legs = cur.execute("SELECT COUNT(*) FROM reservations_leg").fetchone()[0]
mn, mx = cur.execute("SELECT MIN(pickup_date), MAX(pickup_date) FROM reservations_leg").fetchone()
n_ls = cur.execute("SELECT COUNT(*) FROM reservations_legstatus").fetchone()[0]
lmn, lmx = cur.execute("SELECT MIN(timestamp), MAX(timestamp) FROM reservations_legstatus").fetchone()
print("reservations_leg rows          : %d   (stated 24,124) -> %s" % (n_legs, "CONFIRMED" if n_legs == 24124 else "REFUTED"))
print("leg.pickup_date span           : %s .. %s" % (mn, mx))
print("reservations_legstatus rows    : %d   (stated 69,219) -> %s" % (n_ls, "CONFIRMED" if n_ls == 69219 else "REFUTED"))
print("legstatus.timestamp span (UTC) : %s .. %s" % (lmn, lmx))
first_tap = cur.execute("SELECT MIN(timestamp) FROM reservations_legstatus").fetchone()[0]
before = cur.execute("SELECT COUNT(*) FROM reservations_legstatus WHERE timestamp < '2026-02-08'").fetchone()[0]
print("taps before 2026-02-08         : %d  -> pre-2026-02-08 tap history %s" %
      (before, "DOES NOT EXIST (confirmed)" if before == 0 else "EXISTS (refuted)"))

# corrupt / implausible pickup dates
horizon = date(2026, 8, 21) + timedelta(days=550)   # ~18 months of legitimate advance booking
buckets = collections.Counter()
corrupt_rows = []
for lid, L in legs.items():
    d = P(L["pd"]).date() if L["pd"] else None
    if d is None:
        buckets["NULL pickup_date"] += 1
        continue
    if d > horizon:
        buckets["beyond 2027-08 horizon (implausible)"] += 1
        corrupt_rows.append((lid, L["pd"], L["st"], L["pl"], L["dl"]))
    elif d > date(2026, 12, 31):
        buckets["2027 (plausible advance booking)"] += 1
    elif d < date(2025, 4, 26):
        buckets["before first real leg"] += 1
print("\nImplausible pickup_date quantification [measured]:")
for k, v in sorted(buckets.items(), key=lambda x: -x[1]):
    print("   %-42s %d" % (k, v))
print("   -> the 3220-03-06 max is %d row(s); full implausible set:" % len([r for r in corrupt_rows]))
for r in corrupt_rows:
    print("      leg %s  pickup_date=%s  leg.status=%s  %.28s -> %.28s" % (r[0], r[1], r[2], str(r[3]), str(r[4])))
print("   2027-dated legs are NOT corrupt -- they are real advance bookings (%d rows spread over 2027-01..2027-11)."
      % buckets.get("2027 (plausible advance booking)", 0))

print("\nDriver roster [measured]:")
roster = collections.Counter()
for d in drivers.values():
    roster[(d["type"], "active" if d["active"] else "inactive")] += 1
for k in sorted(roster):
    print("   %-10s %-9s %d" % (k[0], k[1], roster[k]))
print("   -> matches stated 26 active inhouse / 9 active affiliate / 7 inactive inhouse / 9 inactive affiliate: %s"
      % ("CONFIRMED" if roster[("inhouse", "active")] == 26 and roster[("affiliate", "active")] == 9
         and roster[("inhouse", "inactive")] == 7 and roster[("affiliate", "inactive")] == 9 else "REFUTED"))
print("   exclude_from_timing=1 today: %d drivers (%s)" % (
    sum(1 for d in drivers.values() if d["excl"]),
    ", ".join(sorted(d["name"] for d in drivers.values() if d["excl"]))))
print("   max drivers_driver.id = %d -> no driver rows created since the prior audit."
      % max(drivers))

print("\nLegs per month [measured] (stated figures in parens):")
STATED = {"2025-10": 740, "2025-11": 1004, "2025-12": 1164, "2026-01": 1480, "2026-02": 1999,
          "2026-03": 2707, "2026-04": 2808, "2026-05": 2914, "2026-06": 2799, "2026-07": 2224, "2026-08": 847}
permonth = collections.Counter()
for L in legs.values():
    if L["pd"] and str(L["pd"])[:10] <= "2026-08-21":
        permonth[str(L["pd"])[:7]] += 1
for m in sorted(STATED):
    got = permonth.get(m, 0)
    print("   %s  %5d  (%5d)  %s" % (m, got, STATED[m], "ok" if got == STATED[m] else "MISMATCH"))
print("   (counted with pickup_date <= 2026-08-21; the calendar month of 2026-08 actually holds %d legs,"
      % sum(1 for L in legs.values() if L["pd"] and str(L["pd"])[:7] == "2026-08"))
print("    the remainder being future-dated advance bookings.)")

# ---- TEST 0b: write-stream recency, the finding that reframes everything below
hr("TEST 0b -- WRITE-STREAM RECENCY  [measured]  (NEW -- the prior audit could not have asked this)")
print("Tap rows per calendar month of legstatus.timestamp:")
tapmonth = collections.Counter()
for (ts,) in cur.execute("SELECT timestamp FROM reservations_legstatus"):
    tapmonth[str(ts)[:7]] += 1
for m in sorted(tapmonth):
    print("   %s  %6d" % (m, tapmonth[m]))
print("\nTap rows per DAY from 2026-07-05 onward:")
for d, n in cur.execute("SELECT substr(timestamp,1,10), COUNT(*) FROM reservations_legstatus "
                        "WHERE timestamp >= '2026-07-05' GROUP BY 1 ORDER BY 1"):
    print("   %s  %6d" % (d, n))
post = cur.execute("SELECT COUNT(*) FROM reservations_legstatus WHERE timestamp > '2026-07-12'").fetchone()[0]
print("\n   tap rows after 2026-07-12: %d  (of %d, = %.3f%%)" % (post, n_ls, 100 * post / n_ls))
print("   the whole post-07-11 tail, verbatim:")
for r in cur.execute("SELECT id, leg_id, status, timestamp, COALESCE(notes,''), updated_by_id "
                     "FROM reservations_legstatus WHERE timestamp > '2026-07-12' ORDER BY timestamp"):
    print("      %s" % (r,))
print("\nOther write streams, latest activity [measured]:")
for tbl, col in (("reservations_reservation", "created_at"), ("reservations_reservation", "updated_at"),
                 ("reservations_quote", "created_at"), ("reservations_lead", "created_at"),
                 ("ops_operationaltask", "created_at"), ("reservations_leg", "driver_assigned_at")):
    mx = cur.execute("SELECT MAX(%s) FROM %s" % (col, tbl)).fetchone()[0]
    print("   %-32s %-18s max = %s" % (tbl, col, mx))
print("\nDistinct days with ANY driver_assigned_at write, 2026-07-12 .. 2026-08-21 [measured]:")
dd = [r for r in cur.execute("SELECT substr(driver_assigned_at,1,10), COUNT(*) FROM reservations_leg "
                             "WHERE driver_assigned_at >= '2026-07-12' GROUP BY 1 ORDER BY 1")]
print("   " + ", ".join("%s(%d)" % r for r in dd))
print("\n   VERDICT: the operational event stream ENDS 2026-07-11 -- the exact end of the prior audit's window.")
print("   The snapshot's leg/reservation rows extend to 2026-08 and beyond only because they are ADVANCE")
print("   BOOKINGS created on or before 2026-07-11 (reservations_reservation.created_at max = 2026-07-11).")
print("   After 07-11 the only writes land on ~11 scattered days that look like local/admin sessions, not")
print("   continuous production. There is therefore NO new driver-tap evidence to extend the audit with.")

print("\nlegstatus notes tally [measured]:")
for k, v in note_rows.most_common(6):
    print("   %-45s %d" % ("(empty)" if k == "" else k, v))
print("   prior audit: 1,806 Auto-reset rows and 337 payroll-bulk rows.")

# ---------------------------------------------------------------- window sets
def in_window(L, end):
    if not L["pd"]:
        return False
    d = P(L["pd"]).date()
    return AUDIT_START <= d <= end


def alive(L):
    return (L["st"] or "") != "cancelled" and (L["rst"] or "") not in ("cancelled", "canceled")


WIN_NEW = [lid for lid, L in legs.items() if in_window(L, NEW_END) and alive(L)]
WIN_OLD = [lid for lid, L in legs.items() if in_window(L, AUDIT_END) and alive(L)]
print("\nEligible (non-cancelled) legs: new window %d, prior-audit window %d [measured]" % (len(WIN_NEW), len(WIN_OLD)))
_added = set(WIN_NEW) - set(WIN_OLD)
_added_tapped = [l for l in _added if any(s in ev.get(l, {}) for s in LADDER)]
_added_full = [l for l in _added if all(s in ev.get(l, {}) for s in ("on-the-way", "on-location", "picked-up", "completed"))]
print("Legs the window EXTENSION adds (2026-07-12..2026-08-21): %d, of which %d carry ANY ladder tap and"
      " %d carry a full ladder [measured]." % (len(_added), len(_added_tapped), len(_added_full)))


def period(pd):
    """Month key, but July is split at the 11th because the tap stream dies there."""
    s = str(pd)[:10]
    if s[:7] == "2026-07":
        return "2026-07a(1-11)" if s <= "2026-07-11" else "2026-07b(12-31)"
    if s[:7] == "2026-08":
        return "2026-08(1-21)"
    return s[:7]

# ---------------------------------------------------------------- TEST 1
hr("TEST 1 -- STATUS-TAP COVERAGE  (re-runs audit sec 2.1 'Status-event coverage over time')")
print("Exact status strings present in reservations_legstatus (whole table) [measured]:")
tot = 0
for s, c in cur.execute("SELECT status, COUNT(*) FROM reservations_legstatus GROUP BY status ORDER BY 2 DESC"):
    print("   %-14s %6d   %s" % (s, c, "<- ladder" if s in LADDER else "<- NOT a ladder tap"))
    tot += c
print("   %-14s %6d" % ("TOTAL", tot))
print("   NOTE: 'assigned' is a declared choice (reservations/models.py:3345) but has ZERO rows [measured].")
print("   NOTE: 'in-progress' is the Leg model default + the driver-unassign auto-reset, NOT a driver tap.")

rows = []
print("\nCoverage by month -- share of eligible legs carrying each tap [measured]")
print("%-15s %6s | %8s %8s %8s %8s %8s | %9s %9s" %
      ("period", "legs", "accept", "on-way", "on-loc", "pickedup", "complete", "fullladder", "prior%"))
for m in sorted({period(legs[l]["pd"]) for l in WIN_NEW}):
    ids = [l for l in WIN_NEW if period(legs[l]["pd"]) == m]
    n = len(ids)
    counts = {s: sum(1 for l in ids if s in ev.get(l, {})) for s in LADDER}
    full = sum(1 for l in ids if all(s in ev.get(l, {}) for s in ("on-the-way", "on-location", "picked-up", "completed")))
    prior = PRIOR_COVERAGE.get(m[:7]) if m.startswith("2026-07a") or len(m) == 7 else None
    print("%-15s %6d | %7.1f%% %7.1f%% %7.1f%% %7.1f%% %7.1f%% | %8.1f%% %9s" %
          (m, n, 100 * counts["confirmed"] / n, 100 * counts["on-the-way"] / n, 100 * counts["on-location"] / n,
           100 * counts["picked-up"] / n, 100 * counts["completed"] / n, 100 * full / n,
           ("%d%%" % prior[2]) if prior else "n/a"))
    rows.append([m, n] + [counts[s] for s in LADDER] + [full])
w("coverage_by_month", ["month", "eligible_legs"] + LADDER + ["full_ladder"], rows)

print("\nSame table restricted to the PRIOR-AUDIT window (reproduction check vs audit sec 2.1) [measured]")
print("%-9s %6s %12s %10s %10s" % ("month", "legs", "full-ladder", "cover%", "audit-said"))
for m in sorted({str(legs[l]["pd"])[:7] for l in WIN_OLD}):
    ids = [l for l in WIN_OLD if str(legs[l]["pd"])[:7] == m]
    full = sum(1 for l in ids if all(s in ev.get(l, {}) for s in ("on-the-way", "on-location", "picked-up", "completed")))
    p = PRIOR_COVERAGE.get(m)
    print("%-9s %6d %12d %9.0f%% %10s" % (m, len(ids), full, 100 * full / len(ids),
                                          ("%d legs/%d/%d%%" % p) if p else "n/a"))

print("\nCoverage by driver type (new window) [measured]")
print("%-22s %7s | %8s %8s %8s %8s %8s | %10s" %
      ("driver cohort", "legs", "accept", "on-way", "on-loc", "pickedup", "complete", "fullladder"))
rows = []
def cohort_of(L):
    if not L["did"]:
        return "unassigned"
    d = drivers.get(L["did"])
    if not d:
        return "unknown driver id"
    if d["type"] != "inhouse":
        return "affiliate"
    return "in-house (excluded)" if d["excl"] else "in-house (trustworthy)"
groups = collections.defaultdict(list)
for l in WIN_NEW:
    groups[cohort_of(legs[l])].append(l)
for g in ["in-house (trustworthy)", "in-house (excluded)", "affiliate", "unassigned", "unknown driver id"]:
    ids = groups.get(g, [])
    if not ids:
        continue
    n = len(ids)
    counts = {s: sum(1 for l in ids if s in ev.get(l, {})) for s in LADDER}
    full = sum(1 for l in ids if all(s in ev.get(l, {}) for s in ("on-the-way", "on-location", "picked-up", "completed")))
    print("%-22s %7d | %7.1f%% %7.1f%% %7.1f%% %7.1f%% %7.1f%% | %9.1f%%" %
          (g, n, 100 * counts["confirmed"] / n, 100 * counts["on-the-way"] / n, 100 * counts["on-location"] / n,
           100 * counts["picked-up"] / n, 100 * counts["completed"] / n, 100 * full / n))
    rows.append([g, n] + [counts[s] for s in LADDER] + [full])
w("coverage_by_cohort", ["cohort", "eligible_legs"] + LADDER + ["full_ladder"], rows)

print("\nAffiliate coverage by month (new -- the prior audit never broke this out) [measured]")
print("%-9s %7s %12s %10s" % ("month", "aff legs", "full-ladder", "cover%"))
for m in sorted({period(legs[l]["pd"]) for l in WIN_NEW}):
    ids = [l for l in groups.get("affiliate", []) if period(legs[l]["pd"]) == m]
    if not ids:
        continue
    full = sum(1 for l in ids if all(s in ev.get(l, {}) for s in ("on-the-way", "on-location", "picked-up", "completed")))
    print("%-9s %7d %12d %9.1f%%" % (m, len(ids), full, 100 * full / len(ids)))

# ---------------------------------------------------------------- TEST 2
hr("TEST 2 -- DUPLICATE TAPS  (re-runs audit sec 1.4.1 / 8.2 'Duplicate status rows 3.7-5.9%')")
print("Two definitions reported: (a) share of (leg,status) LADDER PAIRS with >1 row -- the literal question;")
print("(b) share of eligible legs with >=1 duplicated ladder status -- the audit's '3.7-5.9% of legs' figure.")
print("(c) same as (b) but ignoring the accept/confirmed tap -- the four TIMING statuses only.")
print("%-15s %10s %10s %8s | %10s %10s %8s %10s" %
      ("period", "pairs", "dupe pairs", "(a)%", "legs", "legs w/dup", "(b)%", "(c)timing%"))
rows = []
for m in sorted({period(legs[l]["pd"]) for l in WIN_NEW}):
    ids = [l for l in WIN_NEW if period(legs[l]["pd"]) == m]
    pairs = dup_pairs = legdup = legdup4 = 0
    for l in ids:
        d = ev.get(l, {})
        any_d = any_d4 = False
        for s in LADDER:
            if s in d:
                pairs += 1
                if d[s][1] > 1:
                    dup_pairs += 1
                    any_d = True
                    if s != "confirmed":
                        any_d4 = True
        if any_d:
            legdup += 1
        if any_d4:
            legdup4 += 1
    print("%-15s %10d %10d %7.2f%% | %10d %10d %7.2f%% %9.2f%%" %
          (m, pairs, dup_pairs, 100 * dup_pairs / max(pairs, 1), len(ids), legdup,
           100 * legdup / len(ids), 100 * legdup4 / len(ids)))
    rows.append([m, pairs, dup_pairs, round(100 * dup_pairs / max(pairs, 1), 2), len(ids), legdup,
                 round(100 * legdup / len(ids), 2), legdup4, round(100 * legdup4 / len(ids), 2)])
w("duplicate_taps", ["period", "ladder_pairs", "dupe_pairs", "pct_pairs", "legs", "legs_with_dupe", "pct_legs",
                     "legs_with_dupe_timing_only", "pct_legs_timing_only"], rows)

dup_by_status = collections.Counter()
tot_by_status = collections.Counter()
for l in WIN_NEW:
    for s, v in ev.get(l, {}).items():
        if s in LADDER:
            tot_by_status[s] += 1
            if v[1] > 1:
                dup_by_status[s] += 1
print("\nWhich status gets re-tapped (new window) [measured]:")
for s in LADDER:
    print("   %-14s %6d pairs, %5d duplicated (%.2f%%)" %
          (s, tot_by_status[s], dup_by_status[s], 100 * dup_by_status[s] / max(tot_by_status[s], 1)))

# ---------------------------------------------------------------- TEST 3
hr("TEST 3 -- THE FABRICATING-DRIVER COHORT  (re-runs audit sec 8.4 'Driver status discipline')")
print("Detection replicated from the audit: instant%% = share of full-ladder trips where picked-up -> completed")
print("was under 2 minutes. Reported for in-house drivers with >=25 eligible legs in the window (audit's cut).")


def discipline(ids_by_driver):
    out = {}
    for did, ids in ids_by_driver.items():
        n = len(ids)
        full = [l for l in ids if all(s in ev.get(l, {}) for s in ("on-the-way", "on-location", "picked-up", "completed"))]
        rides = []
        inst = 0
        for l in full:
            d = ev[l]
            v = (d["completed"][0] - d["picked-up"][0]).total_seconds() / 60.0
            if v < 2:
                inst += 1
            if 2 <= v <= 240:
                rides.append(v)
        # secondary fabrication signal: whole ladder collapsed into <120s
        collapsed = 0
        for l in full:
            d = ev[l]
            span = (d["completed"][0] - d["on-the-way"][0]).total_seconds()
            if span < 120:
                collapsed += 1
        out[did] = dict(n=n, full=len(full), fullpct=100 * len(full) / n,
                        instant=inst, instpct=100 * inst / max(len(full), 1),
                        collapsed=collapsed, colpct=100 * collapsed / max(len(full), 1),
                        medride=pct(rides, 0.5), nride=len(rides))
    return out


by_drv_new = collections.defaultdict(list)
by_drv_old = collections.defaultdict(list)
for l in WIN_NEW:
    if legs[l]["did"]:
        by_drv_new[legs[l]["did"]].append(l)
for l in WIN_OLD:
    if legs[l]["did"]:
        by_drv_old[legs[l]["did"]].append(l)
dnew = discipline(by_drv_new)
dold = discipline(by_drv_old)

rows = []
print("\n%-13s %-10s %6s %7s %8s %8s %9s %8s | %-24s %s" %
      ("driver", "type", "legs", "full%", "instant%", "collaps%", "med ride", "excl?", "audit sec8.4 (legs/full/inst)", "delta inst"))
print("      PRIOR-WIN columns = recomputed on the audit's own window (the reproduction check);")
print("      NEW-WIN columns   = recomputed on 2026-02-08..2026-08-21 (full%% is diluted by tapless legs).")
print("%-13s %-10s | %6s %6s %8s | %6s %6s %8s %8s %8s | %s" %
      ("driver", "type", "legsP", "fullP%", "instP%", "legsN", "fullN%", "instN%", "collaps%", "medride",
       "audit doc legs/full/inst"))
ordered = sorted([d for d in dnew if drivers.get(d, {}).get("type") == "inhouse" and dnew[d]["n"] >= 25],
                 key=lambda d: -dnew[d]["instpct"])
for did in ordered:
    r = dnew[did]
    o = dold.get(did)
    nm = drivers[did]["name"]
    pr = PRIOR_DISCIPLINE.get(nm)
    print("%-13s %-10s%s| %6s %5s %7s | %6d %5.0f%% %7.0f%% %7.0f%% %8s | %s" %
          (nm, drivers[did]["type"], "*" if drivers[did]["excl"] else " ",
           o["n"] if o else "-", ("%.0f%%" % o["fullpct"]) if o else "-",
           ("%.0f%%" % o["instpct"]) if o else "-",
           r["n"], r["fullpct"], r["instpct"], r["colpct"], fmt(r["medride"], 0),
           ("%d/%d%%/%d%%" % (pr[0], pr[1], pr[2])) if pr else "not in audit"))
    rows.append([nm, drivers[did]["type"], drivers[did]["active"], drivers[did]["excl"],
                 o["n"] if o else "", round(o["fullpct"], 1) if o else "", round(o["instpct"], 1) if o else "",
                 r["n"], round(r["fullpct"], 1),
                 round(r["instpct"], 1), round(r["colpct"], 1), r["medride"], r["nride"],
                 pr[0] if pr else "", pr[1] if pr else "", pr[2] if pr else ""])
print("(* = exclude_from_timing is set on this driver today)")
w("driver_discipline", ["driver", "type", "is_active", "exclude_from_timing",
                        "legs_prior_window", "full_ladder_pct_prior", "instant_pct_prior",
                        "legs_new_window", "full_ladder_pct", "instant_pct", "collapsed_ladder_pct",
                        "median_ride_min", "n_rides", "audit_legs", "audit_full_pct", "audit_instant_pct"], rows)

print("\nTEST 3b -- EXACT reproduction of the command that produced audit sec 8.4 [measured]")
print("dispatching/management/commands/driver_data_quality.py -- its rules, not mine:")
print("  * denominator = legs with pickup_date >= cutoff, driver set, driver_type='inhouse', AND at least one of")
print("    REQUIRED_ANALYTICS_STATUSES present (dispatching/analytics.py:430 = {on-the-way, picked-up, completed}")
print("    -- NOTE: 'on-location' is NOT part of the production full-chain test). Cancelled legs are NOT excluded.")
print("  * full chain = all THREE of those statuses present; instant = picked-up->completed < 2 min")
print("    (MIN_PICKUP_TO_COMPLETE, dispatching/analytics.py:434); median ride EXCLUDES the instant trips.")
print("  * cut-offs: MIN_LEGS_TO_JUDGE=25 (:63), INSTANT_SHARE_EXCLUDE=0.40 (:60), SPARSE_FULL_CHAIN=0.50 (:67)")
REQ3 = ("on-the-way", "picked-up", "completed")


def cmd_repro(cutoff):
    st = collections.defaultdict(lambda: {"legs": 0, "full": 0, "instant": 0, "rides": []})
    for lid, L in legs.items():
        if not L["did"] or drivers.get(L["did"], {}).get("type") != "inhouse":
            continue
        if not L["pd"] or str(L["pd"])[:10] < cutoff:
            continue
        d = ev.get(lid, {})
        t = {s: d[s][0] for s in REQ3 if s in d}
        if not t:
            continue
        s = st[L["did"]]
        s["legs"] += 1
        if len(t) < 3:
            continue
        s["full"] += 1
        ride = (t["completed"] - t["picked-up"]).total_seconds() / 60.0
        if ride < 2:
            s["instant"] += 1
        else:
            s["rides"].append(ride)
    return st


for cutoff, label in (("2026-01-12", "--days 200 as of 2026-07-31 (the audit's own run)"),
                      ("2026-02-02", "--days 200 as of 2026-08-21 (today)")):
    st = cmd_repro(cutoff)
    print("\n  cutoff %s  (%s)" % (cutoff, label))
    print("  %-13s %6s %7s %8s %8s %9s | %-22s %s" %
          ("driver", "legs", "full%", "instant%", "medride", "verdict", "audit doc legs/full/inst", "match?"))
    for did in sorted([d for d in st if st[d]["legs"] >= 25], key=lambda d: -st[d]["instant"] / max(st[d]["full"], 1)):
        s = st[did]
        fs, ins = 100 * s["full"] / s["legs"], 100 * s["instant"] / max(s["full"], 1)
        mr = sorted(s["rides"])[len(s["rides"]) // 2] if s["rides"] else 0.0
        verdict = "EXCLUDE" if ins >= 40 else ("sparse" if fs < 50 else "good")
        pr = PRIOR_DISCIPLINE.get(drivers[did]["name"])
        ok = ""
        if pr:
            ok = "EXACT" if (s["legs"] == pr[0] and round(fs) == pr[1] and round(ins) == pr[2]) else \
                 ("legs%+d full%+d inst%+d" % (s["legs"] - pr[0], round(fs) - pr[1], round(ins) - pr[2]))
        print("  %-13s %6d %6.0f%% %7.0f%% %8.0f %9s | %-22s %s" %
              (drivers[did]["name"], s["legs"], fs, ins, mr, verdict,
               ("%d/%d%%/%d%%" % (pr[0], pr[1], pr[2])) if pr else "not in audit", ok))

print("\nAffiliate drivers with >=25 legs (audit excluded affiliates wholesale; shown for completeness) [measured]")
print("%-13s %6s %7s %8s %8s %8s" % ("driver", "legs", "full%", "instant%", "collaps%", "medride"))
for did in sorted([d for d in dnew if drivers.get(d, {}).get("type") == "affiliate" and dnew[d]["n"] >= 25],
                  key=lambda d: -dnew[d]["instpct"]):
    r = dnew[did]
    print("%-13s %6d %6.0f%% %7.0f%% %7.0f%% %8s" %
          (drivers[did]["name"], r["n"], r["fullpct"], r["instpct"], r["colpct"], fmt(r["medride"], 0)))

FAB_THRESHOLD = 40.0
fab_new = {drivers[d]["name"] for d in dnew
           if drivers.get(d, {}).get("type") == "inhouse" and dnew[d]["n"] >= 25 and dnew[d]["instpct"] >= FAB_THRESHOLD}
fab_old_audit = {n for n, v in PRIOR_DISCIPLINE.items() if v[2] >= FAB_THRESHOLD}
excl_flagged = {d["name"] for d in drivers.values() if d["excl"] and d["type"] == "inhouse"}
print("\nCohort membership at the audit's implicit >=%.0f%% instant cut [measured]:" % FAB_THRESHOLD)
print("   prior audit (2026-02-08..07-11): %s" % ", ".join(sorted(fab_old_audit)))
print("   this run  (2026-02-08..08-21) : %s" % ", ".join(sorted(fab_new)) if fab_new else "   this run: (none)")
print("   ADDED since audit  : %s" % (", ".join(sorted(fab_new - fab_old_audit)) or "(none)"))
print("   DROPPED since audit: %s" % (", ".join(sorted(fab_old_audit - fab_new)) or "(none)"))
print("   exclude_from_timing=1 in-house today: %s" % ", ".join(sorted(excl_flagged)))
print("   flagged but NOT fabricating by this test: %s" % (", ".join(sorted(excl_flagged - fab_new)) or "(none)"))
print("   fabricating but NOT flagged            : %s" % (", ".join(sorted(fab_new - excl_flagged)) or "(none)"))

# 3d: taps authored by someone other than the current assignee
mismatch = same = noauth = 0
prof_of_driver = {did: pid for did, pid in cur.execute("SELECT id, profile_id FROM drivers_driver")}
for l in WIN_NEW:
    L = legs[l]
    if not L["did"] or "completed" not in ev.get(l, {}):
        continue
    a = author.get((l, "completed"), set())
    if not a:
        noauth += 1
    elif prof_of_driver.get(L["did"]) in a:
        same += 1
    else:
        mismatch += 1
tt = same + mismatch + noauth
print("\n[NEW, not in the prior audit] Who actually authored the 'completed' tap vs the leg's current driver:")
print("   authored by the current assignee : %d (%.1f%%)" % (same, 100 * same / max(tt, 1)))
print("   authored by SOMEONE ELSE         : %d (%.1f%%)  <- reassignment / dispatcher / payroll closeout" %
      (mismatch, 100 * mismatch / max(tt, 1)))
print("   no author recorded               : %d (%.1f%%)" % (noauth, 100 * noauth / max(tt, 1)))
print("   -> assumption A9 leaks by ~%.1f%% of completed legs; timing figures inherit that." % (100 * mismatch / max(tt, 1)))

# ---------------------------------------------------------------- TEST 4
hr("TEST 4 -- THE GOLD COHORT  (re-runs audit sec 9 'GOLD COHORT')")
gold_ids = [name2id[n] for n in GOLD_AUDIT_NAMES if n in name2id]
missing = [n for n in GOLD_AUDIT_NAMES if n not in name2id]
print("Audit-nominated cohort (14 drivers, audit reported 8,656 legs): %s" % ", ".join(GOLD_AUDIT_NAMES))
if missing:
    print("   NOT FOUND in drivers_driver: %s" % missing)

rows = []
print("\n%-11s %-10s %6s %7s %8s %8s %9s %8s | %s" %
      ("driver", "type", "legs", "full%", "instant%", "collaps%", "med ride", "share%", "audit sec9.1 legs/full/inst"))
gold_total = 0
for did in sorted(gold_ids, key=lambda d: -dnew.get(d, {"n": 0})["n"] if d in dnew else 0):
    r = dnew.get(did)
    if not r:
        print("%-11s %-10s   NO LEGS IN WINDOW" % (drivers[did]["name"], drivers[did]["type"]))
        continue
    gold_total += r["n"]
    pr = PRIOR_DISCIPLINE.get(drivers[did]["name"])
    print("%-11s %-10s %6d %6.0f%% %7.0f%% %7.0f%% %9s %7.1f%% | %s" %
          (drivers[did]["name"], drivers[did]["type"], r["n"], r["fullpct"], r["instpct"], r["colpct"],
           fmt(r["medride"], 0), 100 * r["n"] / len(WIN_NEW),
           ("%d/%d%%/%d%%" % (pr[0], pr[1], pr[2])) if pr else "not in audit sec8.4"))
    rows.append([drivers[did]["name"], drivers[did]["type"], r["n"], round(r["fullpct"], 1),
                 round(r["instpct"], 1), round(r["colpct"], 1), r["medride"], round(100 * r["n"] / len(WIN_NEW), 2)])
w("gold_cohort", ["driver", "type", "legs_window", "full_ladder_pct", "instant_pct", "collapsed_pct",
                  "median_ride_min", "share_of_window_pct"], rows)
print("\nAudit gold cohort share of eligible legs in the NEW window: %d / %d = %.1f%% [measured]"
      % (gold_total, len(WIN_NEW), 100 * gold_total / len(WIN_NEW)))

# data-driven re-vet: who passes EVERY plausibility test on the new window
print("\nData-driven re-vet -- drivers passing EVERY plausibility test on the new window [measured]:")
print("   gates: >=25 legs; full-ladder >=80%; instant <15%; collapsed-ladder <5%;")
print("          median ride 20-60 min; ladder strictly ordered on >=90% of full-ladder legs.")
def ordered_pct(ids):
    ok = tot_ = 0
    for l in ids:
        d = ev.get(l, {})
        if not all(s in d for s in ("on-the-way", "on-location", "picked-up", "completed")):
            continue
        tot_ += 1
        ts = [d[s][0] for s in ("on-the-way", "on-location", "picked-up", "completed")]
        if all(ts[i] <= ts[i + 1] for i in range(3)):
            ok += 1
    return 100 * ok / max(tot_, 1), tot_
qual = []
for did, ids in by_drv_new.items():
    r = dnew[did]
    if r["n"] < 25:
        continue
    op, nfl = ordered_pct(ids)
    passes = (r["fullpct"] >= 80 and r["instpct"] < 15 and r["colpct"] < 5
              and r["medride"] is not None and 20 <= r["medride"] <= 60 and op >= 90)
    qual.append((did, r, op, passes))
print("\n%-13s %-10s %6s %7s %8s %8s %8s %8s %7s %s" %
      ("driver", "type", "legs", "full%", "instant%", "collaps%", "ordered%", "medride", "share%", "PASS"))
new_gold = []
for did, r, op, passes in sorted(qual, key=lambda x: -x[1]["n"]):
    print("%-13s %-10s %6d %6.0f%% %7.1f%% %7.1f%% %7.1f%% %8s %6.1f%% %s" %
          (drivers[did]["name"], drivers[did]["type"], r["n"], r["fullpct"], r["instpct"], r["colpct"], op,
           fmt(r["medride"], 0), 100 * r["n"] / len(WIN_NEW), "PASS" if passes else "fail"))
    if passes:
        new_gold.append(did)
ng_legs = sum(dnew[d]["n"] for d in new_gold)
print("\nData-driven gold set (%d drivers): %s" % (len(new_gold), ", ".join(drivers[d]["name"] for d in new_gold)))
print("Share of eligible legs in window: %d / %d = %.1f%% [measured]" % (ng_legs, len(WIN_NEW), 100 * ng_legs / len(WIN_NEW)))
print("vs audit cohort: added %s ; dropped %s" % (
    ", ".join(sorted(drivers[d]["name"] for d in new_gold if d not in gold_ids)) or "(none)",
    ", ".join(sorted(drivers[d]["name"] for d in gold_ids if d not in new_gold)) or "(none)"))

GOLD = [d for d in gold_ids if d in by_drv_new]   # audit cohort, used for TEST 6/7 comparability
GOLD_SET = set(GOLD)

# ---------------------------------------------------------------- TEST 5
hr("TEST 5 -- DST BOUNDARY, RE-VERIFIED EMPIRICALLY (re-runs audit sec 1.3)")
print("Audit's method: modal whole-hour offset between the on-location tap (UTC) and the scheduled pickup")
print("(naive local), by date. Audit reported +5h on 2026-03-07 (n=65) and +4h on 2026-03-08 (n=62).")
print("\n%-12s %5s %7s %7s %7s %9s" % ("date", "n", "modal", "share", "median", "verdict"))
rows = []
for off in range(-14, 15):
    d = DST_FLIP + timedelta(days=off)
    vals = []
    for lid, L in legs.items():
        if not L["pd"] or P(L["pd"]).date() != d:
            continue
        e = ev.get(lid, {})
        if "on-location" not in e or not L["pt"]:
            continue
        parts = str(L["pt"]).split(".")[0].split(":")
        while len(parts) < 3:
            parts.append("00")
        try:
            naive = datetime(d.year, d.month, d.day, int(parts[0]), int(parts[1]), int(parts[2]))
        except ValueError:
            continue
        vals.append((e["on-location"][0] - naive).total_seconds() / 3600.0)
    if not vals:
        continue
    c = collections.Counter(round(v) for v in vals)
    modal, mc = c.most_common(1)[0]
    med = statistics.median(vals)
    exp = 5 if d < DST_FLIP else 4
    print("%-12s %5d %+7d %6.0f%% %+7.2f %9s" % (d, len(vals), modal, 100 * mc / len(vals), med,
                                                 "ok" if modal == exp else "OFF"))
    rows.append([str(d), len(vals), modal, round(100 * mc / len(vals), 1), round(med, 3), exp])
w("dst_boundary", ["date", "n", "modal_offset_h", "modal_share_pct", "median_offset_h", "expected_h"], rows)

# residual punctuality check after applying the split offset
resid = collections.defaultdict(list)
for lid in WIN_NEW:
    L = legs[lid]
    e = ev.get(lid, {})
    if "on-location" not in e or not L["pt"]:
        continue
    d = P(L["pd"]).date()
    u = local_to_utc(d, L["pt"])
    if u is None:
        continue
    v = (e["on-location"][0] - u).total_seconds() / 60.0
    if -300 <= v <= 300:
        resid[str(d)[:7]].append(v)
print("\nAfter applying the split offset -- on-location minus scheduled pickup, by month [measured]")
print("(audit's confirmation test: median should land within ~2 min of 0 every month)")
print("%-9s %7s %8s %8s %8s %8s" % ("month", "n", "p05", "p50", "p95", "mean"))
for m in sorted(resid):
    v = resid[m]
    print("%-9s %7d %8s %8s %8s %8s" % (m, len(v), fmt(pct(v, 0.05), 0), fmt(pct(v, 0.5), 1),
                                        fmt(pct(v, 0.95), 0), fmt(statistics.mean(v), 1)))

# ---------------------------------------------------------------- TEST 6
hr("TEST 6 -- CORE DURATIONS, GOLD COHORT, PRIOR WINDOW vs NEW WINDOW (re-runs audit sec 9.3)")
DEFS = [("approach", "on-the-way", "on-location"),
        ("curb wait", "on-location", "picked-up"),
        ("ride time", "picked-up", "completed"),
        ("occupancy", "on-location", "completed")]


def durations(ids, a, b, lo=2, hi=240):
    out = []
    for l in ids:
        d = ev.get(l, {})
        if a in d and b in d:
            v = (d[b][0] - d[a][0]).total_seconds() / 60.0
            if lo <= v <= hi:
                out.append(v)
    return out


gold_new = [l for l in WIN_NEW if legs[l]["did"] in GOLD_SET]
gold_old = [l for l in WIN_OLD if legs[l]["did"] in GOLD_SET]
print("gold legs: prior-audit window %d, new window %d [measured]" % (len(gold_old), len(gold_new)))

# arrival subset (audit sec 6 / 9.3 rows are ARRIVAL-only: airport pickup with both gate times)
arrival, arrival_lf, gate = set(), set(), {}
for lid, sg, ag in cur.execute(
        "SELECT l.id, f.scheduled_gate_arrival_local, f.actual_gate_arrival_local FROM reservations_leg l "
        "JOIN reservations_flight f ON l.flight_information_id = f.id "
        "WHERE f.scheduled_gate_arrival_local IS NOT NULL AND f.actual_gate_arrival_local IS NOT NULL"):
    if lid in legs and categorize_location(legs[lid]["pl"]) in ("MCO Terminal", "SFB Terminal"):
        arrival.add(lid)
        gate[lid] = P(ag)
for lid, ag in cur.execute(
        "SELECT lf.leg_id, f.actual_gate_arrival_local FROM reservations_legflight lf "
        "JOIN reservations_flight f ON lf.flight_id = f.id WHERE lf.is_controlling = 1 "
        "AND f.actual_gate_arrival_local IS NOT NULL"):
    if lid in legs and categorize_location(legs[lid]["pl"]) in ("MCO Terminal", "SFB Terminal"):
        arrival_lf.add(lid)
print("arrival-anatomy subset. TWO flight joins exist and they DISAGREE [measured]:")
print("   via reservations_leg.flight_information_id (legacy FK, what the audit used): %d legs, %d in gold+window"
      % (len(arrival), len(arrival & set(gold_new))))
print("   via reservations_legflight.is_controlling=1 (the modern link)            : %d legs, %d in gold+window"
      % (len(arrival_lf), len(arrival_lf & set(gold_new))))
print("   legs in the FK set but NOT the legflight set: %d  (the legflight table only covers %d legs at all)"
      % (len(arrival - arrival_lf),
         cur.execute("SELECT COUNT(DISTINCT leg_id) FROM reservations_legflight").fetchone()[0]))
print("")
print("NOTE ON COMPARABILITY: the audit sec 9.3 approach/curb rows evidently did NOT apply the 2-min floor")
print("(its n is larger and its percentiles lower than a 2..240 computation), and its occupancy row is")
print("ARRIVAL-ONLY. Both variants are printed below so each doc figure has a like-for-like partner.")
print("\n%-11s %-8s %7s %7s %7s %7s %7s | %s" % ("metric", "window", "n", "p50", "p75", "p90", "p95", "vs prior-audit doc"))
rows = []
for name, a, b in DEFS:
    vo = durations(gold_old, a, b)
    vn = durations(gold_new, a, b)
    key = {"approach": "approach", "curb wait": "curb", "occupancy": "occupancy"}.get(name)
    doc = PRIOR_GOLD_ANATOMY.get(key)
    docs = ("doc n=%d p50=%d p75=%d p90=%d" % (doc[0], doc[2], doc[3], doc[4])) if doc else "doc: per-lane only"
    print("%-11s %-8s %7d %7s %7s %7s %7s | %s" %
          (name, "prior", len(vo), fmt(pct(vo, .5), 0), fmt(pct(vo, .75), 0), fmt(pct(vo, .9), 0), fmt(pct(vo, .95), 0), docs))
    print("%-11s %-8s %7d %7s %7s %7s %7s | %s" %
          ("", "NEW", len(vn), fmt(pct(vn, .5), 0), fmt(pct(vn, .75), 0), fmt(pct(vn, .9), 0), fmt(pct(vn, .95), 0),
           "delta p50 %+.0f  p75 %+.0f  p90 %+.0f" % (pct(vn, .5) - pct(vo, .5), pct(vn, .75) - pct(vo, .75),
                                                      pct(vn, .9) - pct(vo, .9))))
    rows.append([name, "prior_window_floor2", len(vo), pct(vo, .5), pct(vo, .75), pct(vo, .9), pct(vo, .95)])
    rows.append([name, "new_window_floor2", len(vn), pct(vn, .5), pct(vn, .75), pct(vn, .9), pct(vn, .95)])

print("")
print("LIKE-FOR-LIKE variants that reproduce the doc's own n (new window, gold cohort) [measured]")
print("%-34s %7s %7s %7s %7s %7s | %s" % ("variant", "n", "p25", "p50", "p75", "p90", "audit sec9.3 doc row"))
for label, a, b, lo, sub, doc in (
        ("approach, NO 2-min floor", "on-the-way", "on-location", 0, None, PRIOR_GOLD_ANATOMY["approach"]),
        ("curb wait, NO 2-min floor", "on-location", "picked-up", 0, None, PRIOR_GOLD_ANATOMY["curb"]),
        ("occupancy, arrivals only, floor 2", "on-location", "completed", 2, arrival, PRIOR_GOLD_ANATOMY["occupancy"]),
        ("occupancy, arrivals only, no floor", "on-location", "completed", 0, arrival, PRIOR_GOLD_ANATOMY["occupancy"])):
    ids = gold_new if sub is None else [l for l in gold_new if l in sub]
    v = durations(ids, a, b, lo=lo)
    print("%-34s %7d %7s %7s %7s %7s | n=%d p25=%d p50=%d p75=%d p90=%d" %
          (label, len(v), fmt(pct(v, .25), 0), fmt(pct(v, .5), 0), fmt(pct(v, .75), 0), fmt(pct(v, .9), 0),
           doc[0], doc[1], doc[2], doc[3], doc[4]))
    rows.append([label, "new_window", len(v), pct(v, .5), pct(v, .75), pct(v, .9), pct(v, .95)])

# true dwell, gold, arrivals -- audit sec 9.3 said n=2442 p25=+30 p50=+37 p75=+47 p90=+64
dw = []
for l in gold_new:
    if l in arrival and l in gate and "picked-up" in ev.get(l, {}):
        v = (ev[l]["picked-up"][0] - gate[l]).total_seconds() / 60.0
        if -120 <= v <= 240:
            dw.append(v)
print("%-34s %7d %7s %7s %7s %7s | n=2442 p25=+30 p50=+37 p75=+47 p90=+64" %
      ("true dwell (gate->picked-up)", len(dw), fmt(pct(dw, .25), 0), fmt(pct(dw, .5), 0),
       fmt(pct(dw, .75), 0), fmt(pct(dw, .9), 0)))
rows.append(["true_dwell_arrivals", "new_window", len(dw), pct(dw, .5), pct(dw, .75), pct(dw, .9), pct(dw, .95)])
w("gold_durations", ["metric", "window", "n", "p50", "p75", "p90", "p95"], rows)

print("\nSame four metrics month by month (gold cohort, new window) -- is anything drifting? [measured]")
months = sorted({period(legs[l]["pd"]) for l in gold_new})
print("%-11s %s" % ("metric", "".join("%13s" % m for m in months)))
for name, a, b in DEFS:
    cells = []
    for m in months:
        ids = [l for l in gold_new if period(legs[l]["pd"]) == m]
        v = durations(ids, a, b)
        cells.append("%5s/%-6d" % (fmt(pct(v, .5), 0), len(v)) if v else "     -/0    ")
    print("%-11s %s" % (name + " p50/n", "".join("%13s" % c for c in cells)))

print("\nRide time by lane, gold cohort (re-runs audit sec 9.2) [measured]")
print("%-42s %6s %6s %6s | %6s %6s %6s | %s" %
      ("lane", "n_old", "p50", "p75", "n_NEW", "p50", "p75", "audit doc n/p50/p75"))
lane_old = collections.defaultdict(list)
lane_new = collections.defaultdict(list)
for ids, sink in ((gold_old, lane_old), (gold_new, lane_new)):
    for l in ids:
        d = ev.get(l, {})
        if "picked-up" in d and "completed" in d:
            v = (d["completed"][0] - d["picked-up"][0]).total_seconds() / 60.0
            if 2 <= v <= 240:
                sink[(categorize_location(legs[l]["pl"]), categorize_location(legs[l]["dl"]))].append(v)
rows = []
for k in sorted(lane_new, key=lambda k: -len(lane_new[k]))[:14]:
    o, n = lane_old.get(k, []), lane_new[k]
    doc = PRIOR_GOLD_LANES.get(k)
    print("%-42s %6d %6s %6s | %6d %6s %6s | %s" %
          (" -> ".join(k), len(o), fmt(pct(o, .5), 0), fmt(pct(o, .75), 0),
           len(n), fmt(pct(n, .5), 0), fmt(pct(n, .75), 0),
           ("%d/%d/%d" % doc) if doc else "n/a"))
    rows.append([k[0], k[1], len(o), pct(o, .5), pct(o, .75), len(n), pct(n, .5), pct(n, .75)])
w("gold_lanes", ["pickup_bucket", "dropoff_bucket", "n_prior", "p50_prior", "p75_prior",
                 "n_new", "p50_new", "p75_new"], rows)

# ---------------------------------------------------------------- TEST 7
hr("TEST 7 -- TURNAROUND BETWEEN CONSECUTIVE LEGS (NEW WORK -- not in the prior audit)")
print("Definition: for the same driver on the same pickup_date, legs ordered by scheduled pickup_time;")
print("for each consecutive pair (A,B), gap = B.on-the-way tap  MINUS  A.completed tap (both UTC, no conversion).")
print("Gold cohort only. Bucketing by %s" % CATEGORIZER)
print("Reported bounds: gaps kept in [-120, +720] min; negatives are reported separately, not dropped silently.")

day = collections.defaultdict(list)
for l in WIN_NEW:
    L = legs[l]
    if L["did"] in GOLD_SET and L["pt"]:
        day[(L["did"], str(L["pd"]))].append(l)

pairs_all = 0
pairs_usable = 0
same_b, diff_b = [], []
by_transition = collections.defaultdict(list)
neg_same = neg_diff = 0
gap_to_onloc = []
for key, ids in day.items():
    ids.sort(key=lambda l: (str(legs[l]["pt"]), l))
    for i in range(len(ids) - 1):
        A, B = ids[i], ids[i + 1]
        pairs_all += 1
        ea, eb = ev.get(A, {}), ev.get(B, {})
        if "completed" not in ea or "on-the-way" not in eb:
            continue
        g = (eb["on-the-way"][0] - ea["completed"][0]).total_seconds() / 60.0
        if not (-120 <= g <= 720):
            continue
        pairs_usable += 1
        da, pb = categorize_location(legs[A]["dl"]), categorize_location(legs[B]["pl"])
        if da == pb:
            same_b.append(g)
            neg_same += 1 if g < 0 else 0
        else:
            diff_b.append(g)
            neg_diff += 1 if g < 0 else 0
        by_transition[(da, pb)].append(g)
        if "on-location" in eb:
            gap_to_onloc.append((eb["on-location"][0] - ea["completed"][0]).total_seconds() / 60.0)

print("\nconsecutive same-driver same-day pairs in window : %d" % pairs_all)
print("pairs with BOTH A.completed and B.on-the-way taps : %d (%.1f%%)  <- the usable turnaround sample"
      % (pairs_usable, 100 * pairs_usable / max(pairs_all, 1)))
print("\n%-46s %7s %8s %8s %8s %9s %9s" % ("cell", "n", "p50", "p75", "p90", "neg gaps", "min/max"))
for lbl, v, neg in (("A dropoff bucket == B pickup bucket (same)", same_b, neg_same),
                    ("A dropoff bucket != B pickup bucket (diff)", diff_b, neg_diff)):
    print("%-46s %7d %8s %8s %8s %8.1f%% %9s" %
          (lbl, len(v), fmt(pct(v, .5), 0), fmt(pct(v, .75), 0), fmt(pct(v, .9), 0),
           100 * neg / max(len(v), 1), "%.0f/%.0f" % (min(v), max(v)) if v else "-"))
allg = same_b + diff_b
print("%-46s %7d %8s %8s %8s" % ("ALL pairs", len(allg), fmt(pct(allg, .5), 0), fmt(pct(allg, .75), 0), fmt(pct(allg, .9), 0)))
print("%-46s %7d %8s %8s %8s" % ("(variant) A.completed -> B.on-LOCATION", len(gap_to_onloc),
                                 fmt(pct(gap_to_onloc, .5), 0), fmt(pct(gap_to_onloc, .75), 0), fmt(pct(gap_to_onloc, .9), 0)))

print("\nPer bucket-transition, n>=20 [measured]")
print("%-46s %7s %8s %8s %8s" % ("A dropoff -> B pickup", "n", "p50", "p75", "p90"))
rows = []
for k in sorted(by_transition, key=lambda k: -len(by_transition[k])):
    v = by_transition[k]
    rows.append([k[0], k[1], "same" if k[0] == k[1] else "diff", len(v), pct(v, .5), pct(v, .75), pct(v, .9)])
    if len(v) >= 20:
        print("%-46s %7d %8s %8s %8s" % ("%s -> %s%s" % (k[0], k[1], "  (SAME)" if k[0] == k[1] else ""),
                                         len(v), fmt(pct(v, .5), 0), fmt(pct(v, .75), 0), fmt(pct(v, .9), 0)))
w("turnaround", ["a_dropoff_bucket", "b_pickup_bucket", "same_or_diff", "n", "p50", "p75", "p90"], rows)

print("\nSlack framing: gap MINUS the scheduled interval, i.e. how much of the planned gap was real [measured]")
sched_gaps = []
for key, ids in day.items():
    ids.sort(key=lambda l: (str(legs[l]["pt"]), l))
    for i in range(len(ids) - 1):
        A, B = ids[i], ids[i + 1]
        ea, eb = ev.get(A, {}), ev.get(B, {})
        if "completed" not in ea or "on-the-way" not in eb:
            continue
        ua = local_to_utc(P(legs[A]["pd"]).date(), legs[A]["pt"])
        ub = local_to_utc(P(legs[B]["pd"]).date(), legs[B]["pt"])
        if ua is None or ub is None:
            continue
        sched = (ub - ua).total_seconds() / 60.0
        if 0 < sched <= 1440:
            sched_gaps.append(sched)
print("   scheduled pickup-to-pickup interval on those same pairs: n=%d p50=%s p75=%s p90=%s"
      % (len(sched_gaps), fmt(pct(sched_gaps, .5), 0), fmt(pct(sched_gaps, .75), 0), fmt(pct(sched_gaps, .9), 0)))

# ---------------------------------------------------------------- TEST 8 support
hr("TEST 8 -- SUPPORTING NUMBERS FOR THE VERDICT")
adj = tot_adj = 0
for l in WIN_NEW:
    d = ev.get(l, {})
    if not all(s in d for s in ("on-the-way", "on-location", "picked-up", "completed")):
        continue
    tot_adj += 1
    ts = [d[s][0] for s in ("on-the-way", "on-location", "picked-up", "completed")]
    if any((ts[i + 1] - ts[i]).total_seconds() < 60 for i in range(3)):
        adj += 1
print("Double-tapping (audit sec 8.2 said 36%% of full-ladder legs have >=1 adjacent gap <60s):")
print("   new window: %d / %d full-ladder legs = %.1f%% [measured]" % (adj, tot_adj, 100 * adj / max(tot_adj, 1)))

no_complete = sum(1 for l in WIN_NEW if legs[l]["st"] == "completed" and "completed" not in ev.get(l, {}))
print("\nLegs with leg.status='completed' but NO completed tap (audit found 3 in the whole era): %d [measured]" % no_complete)

impossible = collections.Counter()
tot_lane = collections.Counter()
FLOOR = {("SFB Terminal", "Disney Resort"): 35, ("Disney Resort", "SFB Terminal"): 35,
         ("MCO Terminal", "Port Canaveral Area"): 30, ("Port Canaveral Area", "MCO Terminal"): 30,
         ("MCO Terminal", "Disney Resort"): 12, ("Disney Resort", "MCO Terminal"): 12}
trust = {d for d, v in drivers.items() if v["type"] == "inhouse" and not v["excl"]}
for l in WIN_NEW:
    if legs[l]["did"] not in trust:
        continue
    d = ev.get(l, {})
    if "picked-up" in d and "completed" in d:
        v = (d["completed"][0] - d["picked-up"][0]).total_seconds() / 60.0
        if 2 <= v <= 240:
            k = (categorize_location(legs[l]["pl"]), categorize_location(legs[l]["dl"]))
            if k in FLOOR:
                tot_lane[k] += 1
                if v < FLOOR[k]:
                    impossible[k] += 1
print("\nPhysically-impossible ride times still present (audit sec 4.2), trustworthy in-house, new window [measured]")
print("%-46s %7s %7s %7s" % ("lane", "n", "below", "share"))
for k in sorted(FLOOR, key=lambda k: -tot_lane[k]):
    if tot_lane[k]:
        print("%-46s %7d %7d %6.1f%%" % ("%s -> %s (<%d min)" % (k[0], k[1], FLOOR[k]), tot_lane[k],
                                         impossible[k], 100 * impossible[k] / tot_lane[k]))

reset_by_month = collections.Counter()
for lid, ts in cur.execute("SELECT leg_id, timestamp FROM reservations_legstatus WHERE notes='Auto-reset: driver unassigned'"):
    reset_by_month[str(ts)[:7]] += 1
print("\nAuto-reset (driver-unassign) rows by tap month [measured] -- erases real taps AND is a churn metric:")
for m in sorted(reset_by_month):
    print("   %s  %d" % (m, reset_by_month[m]))

bulk_by_month = collections.Counter()
for lid, ts in cur.execute("SELECT leg_id, timestamp FROM reservations_legstatus WHERE notes LIKE 'Bulk status%'"):
    bulk_by_month[str(ts)[:7]] += 1
print("\nPayroll bulk-complete rows by tap month [measured]:")
for m in sorted(bulk_by_month):
    print("   %s  %d" % (m, bulk_by_month[m]))
print("   first/last bulk row: %s .. %s (total %d) -- still in use through the end of the event stream." %
      cur.execute("SELECT MIN(timestamp), MAX(timestamp), COUNT(*) FROM reservations_legstatus "
                  "WHERE notes LIKE 'Bulk status%'").fetchone())
print("   first/last auto-reset row: %s .. %s" %
      cur.execute("SELECT MIN(timestamp), MAX(timestamp) FROM reservations_legstatus "
                  "WHERE notes='Auto-reset: driver unassigned'").fetchone())

print("\nWritten CSVs -> %s/02_status_*.csv" % OUT)
con.close()
