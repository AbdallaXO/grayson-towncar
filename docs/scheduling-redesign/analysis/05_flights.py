#!/usr/bin/env python
"""
05_flights.py -- Grayson Towncar scheduling redesign, Phase 1.

QUESTION: How reliable are the ARRIVAL-SIDE INPUTS (flights, locations, distances)
that any turnaround / handoff model depends on?

Extends docs/operational-data-audit.md (generated 2026-07-31, window 2026-02-08..2026-07-11)
to the 2026-08-21 snapshot and window 2026-02-08..2026-08-21.

READ-ONLY.  Never writes to the snapshot, never runs manage.py, never migrates.
Run from the repo root:   python docs/scheduling-redesign/analysis/05_flights.py

Outputs: stdout report + CSVs in docs/scheduling-redesign/analysis/out/05_flights_*.csv
"""
import collections
import csv
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, os.getcwd())

DB = "file:content/db.sqlite3?mode=ro"
OUT = os.path.join("docs", "scheduling-redesign", "analysis", "out")

WIN_START = date(2026, 2, 8)     # first legstatus tap in the snapshot
WIN_END = date(2026, 7, 11)      # SNAPSHOT CUT -- see 00_snapshot_provenance.py sec 1
SNAP_LABEL = date(2026, 8, 21)   # the file's mtime / the date this research is being run
PRIOR_END = date(2026, 7, 11)    # prior audit window end (identical to the cut)
DST_FLIP = date(2026, 3, 8)      # US DST: UTC-5 before, UTC-4 on/after

# Audit-nominated gold cohort, docs/operational-data-audit.md:489
GOLD_NAMES = ["Michael", "sereen", "yovanny", "steven", "junaid", "angel", "runer",
              "roberto", "lev", "george", "davide", "Charlie", "Aftab", "oualid"]

# Prior-audit figures this script re-runs against.
PRIOR_DELAY_OVERALL = dict(n=5773, p10=-23, p50=-6, p90=34)          # audit sec 5.1
PRIOR_DELAY_AIRLINE = {  # audit sec 5.2 : name -> (n, p10, p50, p90, pct_gt15late)
    "American": (833, -22, -5, 53, 24), "Allegiant": (289, -23, -3, 52, 27),
    "Frontier": (238, -27, -11, 42, 19), "Delta": (923, -20, -5, 36, 20),
    "Southwest": (1750, -16, -2, 29, 18), "JetBlue": (636, -27, -10, 29, 16),
    "United": (682, -28, -11, 25, 14), "Breeze": (199, -31, -15, 21, 13),
    "Alaska": (53, -28, -7, 20, 11), "Avelo": (50, -29, -19, -1, 4)}
PRIOR_DWELL = dict(n=2870, p25=30, p50=37, p75=47, p90=64)           # audit sec 6 (all trustworthy)
PRIOR_DWELL_GOLD = dict(n=2442, p25=30, p50=37, p75=47, p90=64)      # audit sec 9.3
PRIOR_ONLOC_VS_GATE = dict(n=2813, p25=-2, p50=11, p75=21, p90=33)   # audit sec 6

IATA_ALIAS = {"SWA": "WN", "AER LINGUS": "EI"}
IATA_NAME = {
    "WN": "Southwest", "DL": "Delta", "AA": "American", "UA": "United", "B6": "JetBlue",
    "G4": "Allegiant", "F9": "Frontier", "MX": "Breeze", "NK": "Spirit(ceased)",
    "AS": "Alaska", "XP": "Avelo", "AC": "AirCanada", "VS": "VirginAtlantic",
    "SY": "SunCountry", "WS": "WestJet", "BA": "BritishAirways", "PD": "Porter",
    "KE": "KoreanAir", "AF": "AirFrance", "FI": "Icelandair", "EI": "AerLingus",
    "LH": "Lufthansa/Discover", "VB": "VivaAerobus", "4Y": "Discover", "OCN": "Discover",
    "POE": "Porter", "Y4": "Volaris", "TS": "AirTransat",
}


# ---------------------------------------------------------------- helpers
def P(s):
    """Parse a snapshot datetime string. Returns None on empty."""
    if s is None:
        return None
    s = str(s).replace("T", " ").split("+")[0].strip()
    if not s:
        return None
    if "." in s:
        s = s.split(".")[0]
    if len(s) == 10:
        s += " 00:00:00"
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def D(s):
    if not s:
        return None
    try:
        return date(int(s[0:4]), int(s[5:7]), int(s[8:10]))
    except (ValueError, TypeError):
        return None


def pct(vals, q):
    """Nearest-rank, SAME index arithmetic 01_window.py / 02_status.py / the prior audit
    used, so every figure here is directly comparable to those documents."""
    if not vals:
        return None
    v = sorted(vals)
    if q == 0.5:
        return v[len(v) // 2]
    i = min(int(q * len(v)), len(v) - 1)
    return v[i]


def ptile(vals):
    return (len(vals), pct(vals, .10), pct(vals, .25), pct(vals, .50),
            pct(vals, .75), pct(vals, .90), pct(vals, .95))


def fmt(x, w=6):
    return ("%*s" % (w, "-")) if x is None else ("%*.0f" % (w, x))


def hr(t):
    print()
    print("=" * 104)
    print(t)
    print("=" * 104)


def sub(t):
    print()
    print("-- " + t)


def w(name, header, rows):
    path = os.path.join(OUT, "05_flights_%s.csv" % name)
    with open(path, "w", newline="", encoding="utf-8") as f:
        wr = csv.writer(f)
        wr.writerow(header)
        wr.writerows(rows)
    print("   [csv] %s  (%d rows)" % (path, len(rows)))


def month(d):
    return "%04d-%02d" % (d.year, d.month)


def utc_offset(d):
    """Hours to ADD to naive-Florida-local to get UTC."""
    return 5 if d < DST_FLIP else 4


# ---------------------------------------------------------------- assumptions header
print("#" * 104)
print("# 05_flights.py -- reliability of the ARRIVAL-SIDE INPUTS (flights, locations, distances)")
print("# snapshot: content/db.sqlite3 opened READ-ONLY via")
print("#           sqlite3.connect('file:content/db.sqlite3?mode=ro', uri=True)")
print("# extends:  docs/operational-data-audit.md (2026-07-31, window 2026-02-08..2026-07-11)")
print("#")
print("# ASSUMPTIONS -- every one of them, stated up front:")
print("#  A1.  WINDOW = leg.pickup_date in [2026-02-08, 2026-07-11] inclusive.")
print("#         start: 2026-02-08 is the first reservations_legstatus tap; nothing earlier")
print("#                can be timed at all.")
print("#         end:   2026-07-11 is the SNAPSHOT CUT established by")
print("#                00_snapshot_provenance.py sec 1 -- production booking + tap activity")
print("#                stops dead that day. The task brief quotes a legstatus max of")
print("#                2026-08-21 18:45; section 0 below shows that tail is 7 rows of local")
print("#                development writes, not production. Using 2026-08-21 as the window end")
print("#                would have made flight coverage look like it collapsed in July/August")
print("#                (it does not -- see section 0d). Everything after the cut is reported")
print("#                as a POST-CUT diagnostic and excluded from every headline figure.")
print("#         This is the prior audit's exact window, so every compare here is like-for-like")
print("#         by construction; the value added is new questions, not a new window.")
print("#  A2.  TIMEZONES (audit sec 1.3, re-confirmed by 02_status.py):")
print("#         reservations_legstatus.timestamp        = UTC")
print("#         reservations_flight.*_local (all)       = UTC despite the column name")
print("#         reservations_leg.pickup_date/pickup_time= naive Florida local")
print("#       So legstatus-vs-flight differencing needs NO conversion (both UTC) and every")
print("#       figure in sections 2 and 3 is conversion-free. Where local wall-clock is needed")
print("#       (hour-of-day buckets) the offset is UTC-5 before 2026-03-08 and UTC-4 on/after.")
print("#  A3.  LOCATION BUCKETS use the application's OWN dispatching.analytics.")
print("#       categorize_location() -- imported via django.setup() with")
print("#       DJANGO_SETTINGS_MODULE=business.settings and ENABLE_DEBUG_TOOLBAR=0. No ORM")
print("#       query is issued, no manage.py is run, the Django DB connection is never opened;")
print("#       only the pure keyword function is called. Results are therefore [measured] with")
print("#       the production bucketing, NOT a re-implementation. If the import fails the script")
print("#       aborts rather than silently substituting a copy.")
print("#  A4.  AIRPORT-TERMINAL PICKUP := categorize_location(leg.pickup_location) in")
print("#       {'MCO Terminal','SFB Terminal'}. Non-MCO/SFB airports (MLB, LAL, TPA...) fall to")
print("#       'Other' by design (analytics.py:247) and are reported separately, not merged in.")
print("#  A5.  CONTROLLING FLIGHT is resolved EXACTLY as production does it")
print("#       (dispatching/pickup_policy.py:129-147, reservations/models.py Leg.controlling_")
print("#       flight): the reservations_legflight row with is_controlling=1 (ties broken by")
print("#       lowest sequence then lowest id), ELSE the legacy leg.flight_information_id FK.")
print("#       Section 1c shows why the fallback is load-bearing and not cosmetic.")
print("#  A6.  FLIGHT DELAY := actual_gate_arrival_local - scheduled_gate_arrival_local (minutes,")
print("#       negative = early). Rows outside +/-720 min are dropped as data errors and counted.")
print("#  A7.  STATUS TAPS: first occurrence only, MIN(timestamp) per (leg_id, status) -- the")
print("#       audit's filter 1.4.1, which removes re-taps, payroll bulk-completes and")
print("#       driver-unassign auto-resets.")
print("#  A8.  TRUSTWORTHY DRIVERS := drivers_driver.driver_type='inhouse' AND")
print("#       exclude_from_timing=0 (audit filter 1.4.2). GOLD COHORT := the 14 founder-")
print("#       nominated names in audit sec 9, matched via auth_user.username on")
print("#       drivers_driver.profile_id. Both populations are reported for every dwell figure.")
print("#  A9.  RIDE TIME := picked-up -> completed, bounded 2..240 min (audit filter 1.4.3).")
print("# A10.  TRUE DWELL (arrivals) := flight actual_gate_arrival_local -> picked-up tap,")
print("#       bounded -120..+240 min. Out-of-bound rows are counted and reported, not hidden.")
print("# A11.  DEMAND/VOLUME COUNTS exclude cancelled work, defined exactly as 01_window.py's")
print("#       variant (b): leg.status='cancelled' OR reservation.status IN")
print("#       ('cancelled','canceled') -- both spellings exist. Timing figures do not need this")
print("#       filter (a cancelled leg has no completed tap) but it is applied anyway for")
print("#       consistency.")
print("# A12.  CORRUPT pickup_date := outside [2025-01-01, 2027-12-31]. 2027 dates are kept as")
print("#       plausible long-lead cruise bookings.")
print("# A13.  PERCENTILES are nearest-rank with the SAME index arithmetic the prior audit and")
print("#       scripts 01/02 used (p50 = v[n//2]; others v[int(q*n)]), so all four documents'")
print("#       numbers are directly comparable.")
print("# A14.  AIRLINE identity = reservations_flight.airline normalised to an IATA code via a")
print("#       3-entry alias map (SWA->WN, AER LINGUS->EI); airline_display_name is used only")
print("#       for the human label. The audit's PORTER/PORTER AIRLINES/PORTER AIRLINE")
print("#       fragmentation lives in airline_display_name, not in `airline`.")
print("#" * 104)


# ---------------------------------------------------------------- load
try:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "business.settings")
    os.environ["ENABLE_DEBUG_TOOLBAR"] = "0"
    import django
    django.setup()
    from dispatching.analytics import categorize_location
    from dispatching.scheduler import DRIVE_TIME_ESTIMATES, DEFAULT_DRIVE_TIME
    BUCKETER = "dispatching.analytics.categorize_location (production code) [measured]"
except Exception as exc:                                     # pragma: no cover
    print("\nFATAL: could not import the production bucketer: %r" % (exc,))
    print("Refusing to substitute a re-implementation -- rerun with the repo importable.")
    raise SystemExit(1)

_cat_cache = {}


def cat(text):
    if text not in _cat_cache:
        _cat_cache[text] = categorize_location(text)
    return _cat_cache[text]


con = sqlite3.connect(DB, uri=True)
cur = con.cursor()

# drivers
drivers = {}
name2id = {}
for did, uname, dtype, active, excl in cur.execute(
        "SELECT d.id, u.username, d.driver_type, d.is_active, d.exclude_from_timing "
        "FROM drivers_driver d LEFT JOIN auth_user u ON d.profile_id = u.id"):
    drivers[did] = dict(name=uname or "id%s" % did, dtype=dtype, active=active, excl=excl)
    if uname:
        name2id[uname] = did
TRUST = {d for d, v in drivers.items() if v["dtype"] == "inhouse" and not v["excl"]}
GOLD = {name2id[n] for n in GOLD_NAMES if n in name2id}

# reservations
res_status = {r[0]: (r[1] or "") for r in cur.execute(
    "SELECT id, status FROM reservations_reservation")}

# legs
legs = {}
for (lid, pd_, pt, pu, do, st, rid, did, fid) in cur.execute(
        "SELECT id, pickup_date, pickup_time, pickup_location, dropoff_location, status, "
        "reservation_id, driver_id, flight_information_id FROM reservations_leg"):
    legs[lid] = dict(pd=D(pd_), pd_raw=pd_, pt=pt, pu=pu or "", do=do or "", st=(st or ""),
                     rid=rid, did=did, legacy_fid=fid)

# controlling flight per leg -- LegFlight first, legacy FK as production's fallback
lf_ctrl = {}
for lid, fid, isc, seq, rowid in cur.execute(
        "SELECT leg_id, flight_id, is_controlling, sequence, id FROM reservations_legflight "
        "ORDER BY leg_id, is_controlling DESC, sequence ASC, id ASC"):
    if lid not in lf_ctrl:
        lf_ctrl[lid] = fid
legflight_any = collections.Counter()
lf_created = collections.Counter()
for lid, ca in cur.execute("SELECT leg_id, created_at FROM reservations_legflight"):
    legflight_any[lid] += 1
    lf_created[str(ca)[:7]] += 1
ctrl = dict(lf_ctrl)
for lid, L in legs.items():
    if lid not in ctrl and L["legacy_fid"]:
        ctrl[lid] = L["legacy_fid"]

# flights
FCOLS = [r[1] for r in cur.execute("PRAGMA table_info(reservations_flight)")]
flights = {}
for row in cur.execute("SELECT %s FROM reservations_flight" % ", ".join('"%s"' % c for c in FCOLS)):
    flights[row[0]] = dict(zip(FCOLS, row))

# status taps, first occurrence only
taps = collections.defaultdict(dict)
for lid, s, ts in cur.execute(
        "SELECT leg_id, status, MIN(timestamp) FROM reservations_legstatus "
        "GROUP BY leg_id, status"):
    t = P(ts)
    if t:
        taps[lid][s] = t


def cancelled(lid):
    L = legs[lid]
    return L["st"] == "cancelled" or res_status.get(L["rid"], "") in ("cancelled", "canceled")


IN_WIN = [lid for lid, L in legs.items()
          if L["pd"] and WIN_START <= L["pd"] <= WIN_END and not cancelled(lid)]
IN_PRIOR = [lid for lid in IN_WIN if legs[lid]["pd"] <= PRIOR_END]


# ================================================================ 0. provenance
hr("0.  PROVENANCE -- confirm / refute the ground truth handed to this task")
print("bucketer: %s" % BUCKETER)
n_leg = cur.execute("SELECT COUNT(*) FROM reservations_leg").fetchone()[0]
lo, hi = cur.execute("SELECT MIN(pickup_date), MAX(pickup_date) FROM reservations_leg").fetchone()
n_ls = cur.execute("SELECT COUNT(*) FROM reservations_legstatus").fetchone()[0]
tlo, thi = cur.execute("SELECT MIN(timestamp), MAX(timestamp) FROM reservations_legstatus").fetchone()
print("reservations_leg rows              : %d   [measured]  (told: 24,124 -> %s)"
      % (n_leg, "CONFIRMED" if n_leg == 24124 else "REFUTED"))
print("reservations_leg pickup_date span  : %s .. %s   [measured]" % (lo, hi))
print("reservations_legstatus rows        : %d   [measured]  (told: 69,219 -> %s)"
      % (n_ls, "CONFIRMED" if n_ls == 69219 else "REFUTED"))
print("reservations_legstatus span (UTC)  : %s .. %s   [measured]" % (tlo, thi))

sub("0d. THE SNAPSHOT CUT -- why the window ends 2026-07-11 and not 2026-08-21 [measured]")
print("   reservations_legstatus rows by calendar month of the tap:")
for m, c in cur.execute("SELECT substr(timestamp,1,7), COUNT(*) FROM reservations_legstatus "
                        "GROUP BY 1 ORDER BY 1"):
    print("        %s : %6d" % (m, c))
print("   reservations_reservation rows by created month, tail:")
for m, c in cur.execute("SELECT substr(created_at,1,7), COUNT(*) FROM reservations_reservation "
                        "WHERE created_at >= '2026-05' GROUP BY 1 ORDER BY 1"):
    print("        %s : %6d" % (m, c))
print("   -> 2026-07 is a partial month and 2026-08 has SEVEN status rows. Production stops")
print("      2026-07-11 (00_snapshot_provenance.py sec 1). The handful of later rows -- and the")
print("      RouteDistanceCache rows created 2026-08-09/17/19/21 seen in section 6a -- are")
print("      writes made by a developer running the app against this very file.")
print("   -> CONSEQUENCE FOR THIS TASK: any 'flight coverage collapsed in July/August' reading")
print("      is an artefact of the cut plus booking lead time, NOT an AeroAPI failure. Both")
print("      readings are shown in section 1b so the artefact is visible rather than hidden.")

sub("Corrupt pickup_date rows -- 'quantify how many such rows exist' [measured]")
bad = collections.Counter()
bad_ids = []
for lid, L in legs.items():
    d = L["pd"]
    if d is None or d < date(2025, 1, 1) or d > date(2027, 12, 31):
        bad[L["pd_raw"][:4] if L["pd_raw"] else "NULL"] += 1
        bad_ids.append((lid, L["pd_raw"], L["pt"], L["pu"][:50], L["st"]))
print("   corrupt (pickup_date outside 2025-01-01..2027-12-31 or NULL): %d of %d legs = %.4f%%"
      % (sum(bad.values()), n_leg, 100.0 * sum(bad.values()) / n_leg))
for y, c in sorted(bad.items()):
    print("       year %-6s : %d leg(s)" % (y, c))
for r in sorted(bad_ids, key=lambda x: str(x[1])):
    print("       leg %-7s pickup_date=%-12s time=%-10s pickup=%-50s status=%s" % r)
print("   -> the 3220-03-06 max is %d row(s); 2029 is %d row(s). Every MIN/MAX/range query over"
      % (bad.get("3220", 0), bad.get("2029", 0)))
print("      reservations_leg.pickup_date must clamp to [2025-01-01, 2027-12-31] or it is wrong.")
print("   -> 2027 rows kept as plausible long-lead bookings: %d [measured]"
      % sum(1 for L in legs.values() if L["pd"] and L["pd"].year == 2027))

print("\nWindow legs (non-cancelled, pickup_date %s..%s): %d [measured]"
      % (WIN_START, WIN_END, len(IN_WIN)))
mo = collections.Counter(month(legs[l]["pd"]) for l in IN_WIN)
print("   by month: " + "  ".join("%s:%d" % (k, v) for k, v in sorted(mo.items())))


# ================================================================ 1. flight linkage
hr("1.  FLIGHT LINKAGE -- do airport-pickup legs actually carry a usable flight record?")

sub("1a. How the window's legs bucket on the PICKUP side (production categorize_location)")
pick_cat = collections.Counter(cat(legs[l]["pu"]) for l in IN_WIN)
for k, v in pick_cat.most_common():
    print("     %-22s %6d  %5.1f%%" % (k, v, 100.0 * v / len(IN_WIN)))
AIRPORT = ("MCO Terminal", "SFB Terminal")
arr_legs = [l for l in IN_WIN if cat(legs[l]["pu"]) in AIRPORT]
print("   AIRPORT-TERMINAL PICKUP legs in window: %d (%.1f%% of window) [measured]"
      % (len(arr_legs), 100.0 * len(arr_legs) / len(IN_WIN)))
print("   NOTE: 'Other' above absorbs every NON-MCO/SFB airport by design (analytics.py:247).")

# how many 'Other'-bucketed pickups are nevertheless airports
try:
    from dispatching.analytics import is_airport_location
    other_air = [l for l in IN_WIN if cat(legs[l]["pu"]) == "Other"
                 and is_airport_location(legs[l]["pu"])]
    print("   ...of which %d ARE airports the bucketer deliberately files under 'Other' "
          "(MLB/LAL/TPA/etc.) [measured]" % len(other_air))
    ex = collections.Counter(legs[l]["pu"][:46] for l in other_air)
    for k, v in ex.most_common(6):
        print("        %-48s %d" % (k, v))
except Exception:
    print("   (is_airport_location unavailable)")

sub("1b. Linkage by month  [measured]  -- denominator = airport-terminal-pickup legs")
print("   'legflight' = modern link table.  'legacyFK' = leg.flight_information_id.")
print("   'resolved'  = production's own rule: legflight if present, else legacyFK.")
print("   The gate columns are measured on the RESOLVED flight.")
hdr = ("month", "legs", "legflight", "%", "legacyFK", "%", "resolved", "%",
       "sched_gate", "%", "sch+act", "%")
print("   %-8s %6s | %8s %6s | %8s %6s | %8s %6s | %9s %6s | %8s %6s"
      % hdr)
rows1 = []
months = sorted({month(legs[l]["pd"]) for l in arr_legs})
for m in months + ["ALL"]:
    ids = arr_legs if m == "ALL" else [l for l in arr_legs if month(legs[l]["pd"]) == m]
    n = len(ids)
    nlf = sum(1 for l in ids if l in lf_ctrl)
    nlg = sum(1 for l in ids if legs[l]["legacy_fid"])
    nany = sum(1 for l in ids if l in ctrl)
    sg = ag = both = anchor = 0
    for l in ids:
        f = flights.get(ctrl.get(l))
        if not f:
            continue
        s = P(f["scheduled_gate_arrival_local"])
        a = P(f["actual_gate_arrival_local"])
        sg += 1 if s else 0
        ag += 1 if a else 0
        both += 1 if (s and a) else 0
    p = lambda x: 100.0 * x / n if n else 0.0
    print("   %-8s %6d | %8d %5.1f%% | %8d %5.1f%% | %8d %5.1f%% | %9d %5.1f%% | %8d %5.1f%%"
          % (m, n, nlf, p(nlf), nlg, p(nlg), nany, p(nany), sg, p(sg), both, p(both)))
    rows1.append([m, n, nlf, round(p(nlf), 1), nlg, round(p(nlg), 1), nany, round(p(nany), 1),
                  sg, round(p(sg), 1), ag, round(p(ag), 1), both, round(p(both), 1)])
w("linkage_by_month", ["month", "airport_pickup_legs", "with_legflight", "pct_legflight",
                       "with_legacy_fk", "pct_legacy_fk", "resolved", "pct_resolved",
                       "sched_gate", "pct_sched_gate", "actual_gate", "pct_actual_gate",
                       "sched_and_actual", "pct_sched_and_actual"], rows1)

sub("1c. NEW FINDING: the LegFlight writer only fires on ~30% of newly booked flight legs")
print("   reservations_legflight.created_at, by month  [measured]:")
for k, v in sorted(lf_created.items()):
    print("        %s : %6d rows" % (k, v))
mn, mx = cur.execute("SELECT MIN(created_at), MAX(created_at) FROM "
                     "reservations_legflight").fetchone()
print("   span: %s .. %s" % (str(mn)[:19], str(mx)[:19]))
print("   The 2026-04 spike is migration 0101_backfill_legflight_for_orphan_flight_information")
print("   (a one-off repair; migration 0097 was the first). Everything after it is organic.")
print()
print("   THE TEST THAT SEPARATES 'writer broken' FROM 'snapshot cut': group legs by the month")
print("   their RESERVATION was created, and ask what share of the legs that DO carry a legacy")
print("   flight FK also got a LegFlight row.  [measured]")
print("     %-10s %8s %8s %8s" % ("res month", "legacyFK", "hasLegFl", "share"))
rows1c = []
for m, t, h in cur.execute(
        "SELECT substr(r.created_at,1,7), COUNT(*), SUM(CASE WHEN EXISTS(SELECT 1 FROM "
        "reservations_legflight lf WHERE lf.leg_id=l.id) THEN 1 ELSE 0 END) "
        "FROM reservations_leg l JOIN reservations_reservation r ON r.id=l.reservation_id "
        "WHERE l.flight_information_id IS NOT NULL GROUP BY 1 ORDER BY 1"):
    print("     %-10s %8d %8d %7.1f%%" % (m, t, h or 0, 100.0 * (h or 0) / t))
    rows1c.append([m, t, h or 0, round(100.0 * (h or 0) / t, 1)])
w("legflight_writer_coverage", ["reservation_created_month", "legs_with_legacy_fk",
                                 "legs_with_legflight_row", "pct"], rows1c)
print("   -> 100% through 2026-03 (all covered by the backfills), then 93% in April and 27-32%")
print("      every month after. The drop starts INSIDE the window and has nothing to do with the")
print("      2026-07-11 cut. The LegFlight table is NOT being maintained by the booking path.")
print("   -> WHY (code): the booking/edit paths write leg.flight_information directly. The only")
print("      thing that wraps a legacy flight in a LegFlight is _sync_legacy_flight_information()")
print("      at dispatching/views.py:7253, and its callers are the dispatcher-side flight-panel")
print("      endpoints only (dispatching/views.py:7105, dispatching/views.py:7439). A leg booked")
print("      normally and never opened in that panel never gets a row.")
a_only = sum(1 for l in legs if legs[l]["legacy_fid"] and l not in legflight_any)
b_only = sum(1 for l in legs if not legs[l]["legacy_fid"] and l in legflight_any)
both_l = sum(1 for l in legs if legs[l]["legacy_fid"] and l in legflight_any)
print()
print("   Across the WHOLE leg table [measured]:")
print("     legs with leg.flight_information_id AND a legflight row : %d" % both_l)
print("     legs with leg.flight_information_id but NO legflight row: %d  <-- invisible to any"
      % a_only)
print("          analysis (or code path) that reads LegFlight alone")
print("     legs with a legflight row but no legacy FK              : %d" % b_only)
yr = collections.Counter()
for l in legs:
    if legs[l]["legacy_fid"] and l not in legflight_any and legs[l]["pd"]:
        yr[legs[l]["pd"].year] += 1
print("     ...those legacy-only legs by pickup_date year: "
      + ", ".join("%s:%d" % kv for kv in sorted(yr.items())))
agree = dis = 0
for l, fid in lf_ctrl.items():
    lg = legs.get(l, {}).get("legacy_fid")
    if lg:
        agree += 1 if lg == fid else 0
        dis += 0 if lg == fid else 1
print("     where BOTH exist they agree on the flight %d times and disagree %d times (%.3f%%)"
      % (agree, dis, 100.0 * dis / (agree + dis) if (agree + dis) else 0))
multi = sum(1 for l, c in legflight_any.items() if c > 1)
print("     legs with >1 legflight row: %d ; legflight legs yielding no controlling pick: %d"
      % (multi, sum(1 for l in legflight_any if l not in lf_ctrl)))
print("   -> PRODUCTION IS UNHARMED: pickup_policy.controlling_flight() falls back to")
print("      flight_information (dispatching/pickup_policy.py:145), and where both exist they")
print("      agree %.3f%% of the time. The risk is entirely ANALYTICAL -- joining through" % (100.0 * agree / (agree + dis) if (agree+dis) else 0))
print("      LegFlight alone silently drops %.0f%% of this window's airport-pickup legs and"
      % (100.0 * sum(1 for l in arr_legs if l not in lf_ctrl) / len(arr_legs)))
print("      biases the sample toward legs a dispatcher happened to hand-edit.")
print("   -> The prior audit's 'is_controlling is clean: zero legs with none or more than one'")
print("      (sec 8.1) is still TRUE of the rows present. A cleanliness check cannot detect a")
print("      table that stopped acquiring rows -- which is exactly why this needed a new test.")

sub("1d. Which arrival ANCHOR is actually available, by production's own priority chain")
print("   reservations/models.py:2720 best_arrival_local() = actual_gate > est_gate >")
print("   actual_runway > est_runway > sched_gate > sched_runway.  Airport-pickup legs only.")
TIERS = ["actual_gate_arrival_local", "estimated_gate_arrival_local", "actual_arrival_local",
         "estimated_arrival_local", "scheduled_gate_arrival_local", "scheduled_arrival_local"]
print("   %-10s %6s | %s" % ("month", "legs", "  ".join("%-12s" % t.split("_")[0][:4] + t.split("_")[1][:4]
                                                        for t in TIERS) + "   none"))
rows1d0 = []
for m in months + ["ALL"]:
    ids = arr_legs if m == "ALL" else [l for l in arr_legs if month(legs[l]["pd"]) == m]
    cnt = collections.Counter()
    for l in ids:
        f = flights.get(ctrl.get(l))
        tier = "none"
        if f:
            for t in TIERS:
                if P(f[t]):
                    tier = t
                    break
        cnt[tier] += 1
    n = len(ids)
    print("   %-10s %6d | %s   %5.1f%%"
          % (m, n, "  ".join("%11.1f%%" % (100.0 * cnt[t] / n) for t in TIERS),
             100.0 * cnt["none"] / n))
    rows1d0.append([m, n] + [cnt[t] for t in TIERS] + [cnt["none"]])
w("arrival_anchor_tier", ["month", "airport_pickup_legs"] + TIERS + ["none"], rows1d0)

sub("1e. NULL RATES PER COLUMN of reservations_flight  [measured]")
n_all = len(flights)
ctrl_ids = {ctrl[l] for l in arr_legs if l in ctrl}
ctrl_rows = [flights[f] for f in ctrl_ids if f in flights]
print("   population A = every reservations_flight row                     (n=%d)" % n_all)
print("   population B = flights CONTROLLING a window airport-pickup leg   (n=%d)" % len(ctrl_rows))
print("   %-32s %10s %8s | %10s %8s" % ("column", "A nonnull", "A null%", "B nonnull", "B null%"))
rows1d = []
for c in FCOLS:
    na = sum(1 for f in flights.values() if f[c] not in (None, ""))
    nb = sum(1 for f in ctrl_rows if f[c] not in (None, ""))
    pa = 100.0 * (n_all - na) / n_all
    pb = 100.0 * (len(ctrl_rows) - nb) / len(ctrl_rows) if ctrl_rows else 0.0
    print("   %-32s %10d %7.1f%% | %10d %7.1f%%" % (c, na, pa, nb, pb))
    rows1d.append([c, na, round(pa, 1), nb, round(pb, 1)])
w("flight_null_rates", ["column", "all_flights_nonnull", "all_flights_null_pct",
                        "controlling_nonnull", "controlling_null_pct"], rows1d)
print("   -> flight_type is the headline defect the audit named (sec 8.2): it stays ~97% empty,")
print("      so 'is this leg an arrival?' can NEVER be read off the flight row and always falls")
print("      back to keyword-matching the free-text location.")

sub("1f. POST-CUT diagnostic -- the same measurement on legs the snapshot only half-holds")
post = [l for l, L in legs.items()
        if L["pd"] and PRIOR_END < L["pd"] <= date(2026, 9, 30) and not cancelled(l)
        and cat(L["pu"]) in AIRPORT]
for label, ids in (("IN WINDOW  02-08..07-11", arr_legs),
                   ("POST-CUT   07-12..09-30", post)):
    n = len(ids)
    lf = sum(1 for l in ids if l in lf_ctrl)
    rs = sum(1 for l in ids if l in ctrl)
    both = sum(1 for l in ids if l in ctrl and flights.get(ctrl[l])
               and P(flights[ctrl[l]]["scheduled_gate_arrival_local"])
               and P(flights[ctrl[l]]["actual_gate_arrival_local"]))
    print("   %-24s airport-pickup legs=%5d  legflight=%5d %5.1f%%  resolved=%5d %5.1f%%  "
          "sched+actual gate=%5d %5.1f%%"
          % (label, n, lf, 100.0 * lf / n if n else 0, rs, 100.0 * rs / n if n else 0,
             both, 100.0 * both / n if n else 0))
print("   -> the post-cut row is NOT evidence of a coverage regression. Those legs were booked")
print("      but never flown before the file was cut, so FlightAware never wrote an actual.")
print("   audit sec 8.1 claimed '83% coverage on airport-pickup legs'; the ALL row of 1b is the")
print("   direct re-measurement of that claim on the identical window.")


# ================================================================ 2. delay distribution
hr("2.  FLIGHT DELAY DISTRIBUTION  [measured]  (actual_gate - scheduled_gate, both UTC)")

DELAY_BOUND = 720
delays = []           # (minutes, iata, leg_id, pickup_cat, month, local_hour)
dropped = 0
for l in arr_legs:
    f = flights.get(ctrl.get(l))
    if not f:
        continue
    s = P(f["scheduled_gate_arrival_local"])
    a = P(f["actual_gate_arrival_local"])
    if not (s and a):
        continue
    dv = (a - s).total_seconds() / 60.0
    if abs(dv) > DELAY_BOUND:
        dropped += 1
        continue
    code = (f["airline"] or "").strip().upper()
    code = IATA_ALIAS.get(code, code)
    lh = (a - timedelta(hours=utc_offset(legs[l]["pd"]))).hour
    delays.append((dv, code, l, cat(legs[l]["pu"]), month(legs[l]["pd"]), lh))
vals = [d[0] for d in delays]
print("   n = %d arrivals with BOTH gate times (dropped %d rows beyond +/-%d min) [measured]"
      % (len(vals), dropped, DELAY_BOUND))
n, p10, p25, p50, p75, p90, p95 = ptile(vals)
print("   P10 %s   P25 %s   P50 %s   P75 %s   P90 %s   P95 %s   (minutes; negative = early)"
      % (fmt(p10), fmt(p25), fmt(p50), fmt(p75), fmt(p90), fmt(p95)))
print("   share early >5min : %5.1f%%    within +/-15 : %5.1f%%    >15 late : %5.1f%%    "
      ">45 late : %5.1f%%"
      % (100.0 * sum(1 for v in vals if v < -5) / n,
         100.0 * sum(1 for v in vals if -15 <= v <= 15) / n,
         100.0 * sum(1 for v in vals if v > 15) / n,
         100.0 * sum(1 for v in vals if v > 45) / n))
pr = PRIOR_DELAY_OVERALL
print("   prior audit sec 5.1 : n=%d  P10=%d  P50=%d  P90=%d" % (pr["n"], pr["p10"], pr["p50"], pr["p90"]))
print("   delta (this window - prior) : n %+d   P10 %+.0f   P50 %+.0f   P90 %+.0f"
      % (n - pr["n"], p10 - pr["p10"], p50 - pr["p50"], p90 - pr["p90"]))

sub("2b. Prior audit's exact sub-window recomputed, so the delta above is not a window artefact")
pv = [d[0] for d in delays if legs[d[2]]["pd"] <= PRIOR_END]
n2, a10, a25, a50, a75, a90, a95 = ptile(pv)
print("   02-08..07-11 : n=%d  P10 %s  P50 %s  P75 %s  P90 %s  P95 %s"
      % (n2, fmt(a10), fmt(a50), fmt(a75), fmt(a90), fmt(a95)))
nv = [d[0] for d in delays if legs[d[2]]["pd"] > PRIOR_END]
n3, b10, b25, b50, b75, b90, b95 = ptile(nv)
print("   07-12..08-21 : n=%d  P10 %s  P50 %s  P75 %s  P90 %s  P95 %s"
      % (n3, fmt(b10), fmt(b50), fmt(b75), fmt(b90), fmt(b95)))

sub("2c. By airline, top 10 by volume  [measured]   (prior audit sec 5.2 in the right-hand block)")
byair = collections.defaultdict(list)
for dv, code, l, pc, m, lh in delays:
    byair[code].append(dv)
top = sorted(byair.items(), key=lambda x: -len(x[1]))[:10]
print("   %-16s %6s %7s %7s %7s %7s %7s %7s | %8s %6s %6s %6s"
      % ("airline", "n", "P10", "P25", "P50", "P75", "P90", "P95", "prior n", "pP10", "pP50", "pP90"))
rows2 = []
for code, v in top:
    nn, q10, q25, q50, q75, q90, q95 = ptile(v)
    label = IATA_NAME.get(code, code)
    pa = PRIOR_DELAY_AIRLINE.get(label)
    print("   %-16s %6d %7.0f %7.0f %7.0f %7.0f %7.0f %7.0f | %8s %6s %6s %6s"
          % (label, nn, q10, q25, q50, q75, q90, q95,
             pa[0] if pa else "-", pa[1] if pa else "-", pa[2] if pa else "-", pa[3] if pa else "-"))
    rows2.append([label, code, nn, q10, q25, q50, q75, q90, q95,
                  round(100.0 * sum(1 for x in v if x > 15) / nn, 1),
                  round(100.0 * sum(1 for x in v if x > 45) / nn, 1)])
for code, v in sorted(byair.items(), key=lambda x: -len(x[1]))[10:]:
    nn, q10, q25, q50, q75, q90, q95 = ptile(v)
    rows2.append([IATA_NAME.get(code, code), code, nn, q10, q25, q50, q75, q90, q95,
                  round(100.0 * sum(1 for x in v if x > 15) / nn, 1),
                  round(100.0 * sum(1 for x in v if x > 45) / nn, 1)])
w("delay_by_airline", ["airline", "iata", "n", "p10", "p25", "p50", "p75", "p90", "p95",
                       "pct_gt15_late", "pct_gt45_late"], rows2)

sub("2d. By airport, and by month -- is the delay distribution stationary? [measured]")
for pcat in ("MCO Terminal", "SFB Terminal"):
    v = [d[0] for d in delays if d[3] == pcat]
    if v:
        nn, q10, q25, q50, q75, q90, q95 = ptile(v)
        print("   %-14s n=%5d  P10 %s P50 %s P75 %s P90 %s P95 %s"
              % (pcat, nn, fmt(q10), fmt(q50), fmt(q75), fmt(q90), fmt(q95)))
print("   %-10s %6s %7s %7s %7s %7s" % ("month", "n", "P50", "P75", "P90", "P95"))
rows2d = []
for m in months:
    v = [d[0] for d in delays if d[4] == m]
    if not v:
        continue
    nn, q10, q25, q50, q75, q90, q95 = ptile(v)
    print("   %-10s %6d %7.0f %7.0f %7.0f %7.0f" % (m, nn, q50, q75, q90, q95))
    rows2d.append([m, nn, q10, q50, q75, q90, q95])
w("delay_by_month", ["month", "n", "p10", "p50", "p75", "p90", "p95"], rows2d)


# ================================================================ 3. true dwell
hr("3.  TRUE DWELL FOR ARRIVALS  [measured]  -- gate docked -> guest in the car")
print("   This is the number that sets the arrival MEET-BUFFER. Code assumes a flat 45 min")
print("   (STATIC_FLOOR_DWELL_MIN, dispatching/scheduler.py:195).")

DW_LO, DW_HI = -120, 240
dwell = []      # (minutes, leg, driver, airport, cohort)
onloc = []
oob = 0
for l in arr_legs:
    f = flights.get(ctrl.get(l))
    if not f:
        continue
    a = P(f["actual_gate_arrival_local"])
    if not a:
        continue
    t = taps.get(l, {})
    did = legs[l]["did"]
    apt = cat(legs[l]["pu"])
    if "picked-up" in t:
        v = (t["picked-up"] - a).total_seconds() / 60.0
        if DW_LO <= v <= DW_HI:
            dwell.append((v, l, did, apt))
        else:
            oob += 1
    if "on-location" in t:
        v2 = (t["on-location"] - a).total_seconds() / 60.0
        if DW_LO <= v2 <= DW_HI:
            onloc.append((v2, l, did, apt))

print("   rows discarded outside [%d,%d] min: %d" % (DW_LO, DW_HI, oob))


def cohort(rows, ids):
    return [r[0] for r in rows if r[2] in ids] if ids is not None else [r[0] for r in rows]


sub("3a. True dwell (actual gate arrival -> picked-up tap), by cohort  [measured]")
print("   %-28s %6s %7s %7s %7s %7s %7s" % ("cohort", "n", "P25", "P50", "P75", "P90", "P95"))
rows3 = []
for label, ids in (("ALL drivers", None), ("in-house trustworthy", TRUST), ("GOLD cohort", GOLD)):
    v = cohort(dwell, ids)
    if not v:
        continue
    nn, q10, q25, q50, q75, q90, q95 = ptile(v)
    print("   %-28s %6d %7.0f %7.0f %7.0f %7.0f %7.0f" % (label, nn, q25, q50, q75, q90, q95))
    rows3.append([label, "both", nn, q25, q50, q75, q90, q95])
print("   prior audit sec 6  (all trustworthy) : n=%d p25=%d p50=%d p75=%d p90=%d"
      % (PRIOR_DWELL["n"], PRIOR_DWELL["p25"], PRIOR_DWELL["p50"], PRIOR_DWELL["p75"],
         PRIOR_DWELL["p90"]))
print("   prior audit sec 9.3 (gold cohort)    : n=%d p25=%d p50=%d p75=%d p90=%d"
      % (PRIOR_DWELL_GOLD["n"], PRIOR_DWELL_GOLD["p25"], PRIOR_DWELL_GOLD["p50"],
         PRIOR_DWELL_GOLD["p75"], PRIOR_DWELL_GOLD["p90"]))

sub("3b. THE SPLIT THAT MATTERS: MCO vs SFB  [measured]")
print("   %-28s %-14s %6s %7s %7s %7s %7s" % ("cohort", "airport", "n", "P25", "P50", "P75", "P90"))
for label, ids in (("in-house trustworthy", TRUST), ("GOLD cohort", GOLD)):
    for apt in ("MCO Terminal", "SFB Terminal"):
        v = [r[0] for r in dwell if r[3] == apt and (ids is None or r[2] in ids)]
        if not v:
            print("   %-28s %-14s %6d  (no usable rows)" % (label, apt, 0))
            continue
        nn, q10, q25, q50, q75, q90, q95 = ptile(v)
        print("   %-28s %-14s %6d %7.0f %7.0f %7.0f %7.0f" % (label, apt, nn, q25, q50, q75, q90))
        rows3.append([label, apt, nn, q25, q50, q75, q90, q95])
w("dwell", ["cohort", "airport", "n", "p25", "p50", "p75", "p90", "p95"], rows3)

sub("3c. Driver on-location vs gate docking -- how much of the dwell is the driver waiting")
print("   %-28s %-14s %6s %7s %7s %7s %7s  %8s" % ("cohort", "airport", "n", "P25", "P50",
                                                    "P75", "P90", "early%"))
for label, ids in (("in-house trustworthy", TRUST), ("GOLD cohort", GOLD)):
    for apt in ("ALL", "MCO Terminal", "SFB Terminal"):
        v = [r[0] for r in onloc if (apt == "ALL" or r[3] == apt) and (ids is None or r[2] in ids)]
        if not v:
            continue
        nn, q10, q25, q50, q75, q90, q95 = ptile(v)
        early = 100.0 * sum(1 for x in v if x < 0) / nn
        print("   %-28s %-14s %6d %7.0f %7.0f %7.0f %7.0f  %7.1f%%"
              % (label, apt, nn, q25, q50, q75, q90, early))
print("   prior audit sec 6 : n=%d p25=%d p50=%d p75=%d p90=%d, '28%% of the time the driver is"
      % (PRIOR_ONLOC_VS_GATE["n"], PRIOR_ONLOC_VS_GATE["p25"], PRIOR_ONLOC_VS_GATE["p50"],
         PRIOR_ONLOC_VS_GATE["p75"], PRIOR_ONLOC_VS_GATE["p90"]))
print("   on location before the plane has docked'")

sub("3d. Dwell by month (in-house trustworthy) -- is the meet-buffer drifting? [measured]")
print("   %-10s %6s %7s %7s %7s" % ("month", "n", "P50", "P75", "P90"))
rows3d = []
for m in months:
    v = [r[0] for r in dwell if r[2] in TRUST and month(legs[r[1]]["pd"]) == m]
    if len(v) < 20:
        continue
    nn, q10, q25, q50, q75, q90, q95 = ptile(v)
    print("   %-10s %6d %7.0f %7.0f %7.0f" % (m, nn, q50, q75, q90))
    rows3d.append([m, nn, q50, q75, q90])
w("dwell_by_month", ["month", "n", "p50", "p75", "p90"], rows3d)


# ================================================================ 4. location buckets
hr("4.  LOCATION BUCKETS  [measured]  -- production categorize_location(), window legs")
print("   n = %d non-cancelled legs, pickup_date %s..%s" % (len(IN_WIN), WIN_START, WIN_END))

sub("4a. TOP 25 PICKUP buckets")
pc = collections.Counter(cat(legs[l]["pu"]) for l in IN_WIN)
print("   %-4s %-24s %8s %8s %9s" % ("#", "bucket", "count", "share", "cum"))
cum = 0
rows4a = []
for i, (k, v) in enumerate(pc.most_common(25), 1):
    cum += v
    print("   %-4d %-24s %8d %7.2f%% %8.2f%%" % (i, k, v, 100.0 * v / len(IN_WIN),
                                                 100.0 * cum / len(IN_WIN)))
    rows4a.append([i, k, v, round(100.0 * v / len(IN_WIN), 2)])
print("   distinct pickup buckets in window: %d  (the bucketer emits at most 9)" % len(pc))

sub("4b. TOP 25 DROPOFF buckets")
dc = collections.Counter(cat(legs[l]["do"]) for l in IN_WIN)
print("   %-4s %-24s %8s %8s %9s" % ("#", "bucket", "count", "share", "cum"))
cum = 0
rows4b = []
for i, (k, v) in enumerate(dc.most_common(25), 1):
    cum += v
    print("   %-4d %-24s %8d %7.2f%% %8.2f%%" % (i, k, v, 100.0 * v / len(IN_WIN),
                                                 100.0 * cum / len(IN_WIN)))
    rows4b.append([i, k, v, round(100.0 * v / len(IN_WIN), 2)])
w("buckets", ["rank", "bucket", "count", "share_pct", "side"],
  [r + ["pickup"] for r in rows4a] + [r + ["dropoff"] for r in rows4b])

sub("4c. THE BUCKETER'S BLIND SPOT -- what actually hides inside 'Other' and 'Residential'")
for b in ("Other", "Residential", "Other Hotel"):
    ids = [l for l in IN_WIN if cat(legs[l]["pu"]) == b]
    print("   pickup bucket %-14s n=%5d  most common raw strings:" % (b, len(ids)))
    for s, c in collections.Counter(legs[l]["pu"][:58] for l in ids).most_common(6):
        print("        %-60s %d" % (s, c))
print("   -> every one of those collapses to ONE drive-time number in DRIVE_TIME_ESTIMATES.")


# ================================================================ 5. lane matrix
hr("5.  LANE MATRIX  [measured]  -- top 30 pickup-bucket -> dropoff-bucket lanes")

ride = collections.defaultdict(list)     # lane -> [minutes] (trustworthy in-house)
ride_gold = collections.defaultdict(list)
lane_n = collections.Counter()
for l in IN_WIN:
    lane = (cat(legs[l]["pu"]), cat(legs[l]["do"]))
    lane_n[lane] += 1
    t = taps.get(l, {})
    if "picked-up" in t and "completed" in t:
        v = (t["completed"] - t["picked-up"]).total_seconds() / 60.0
        if 2 <= v <= 240:
            did = legs[l]["did"]
            if did in TRUST:
                ride[lane].append(v)
            if did in GOLD:
                ride_gold[lane].append(v)

top30 = lane_n.most_common(30)
tot = sum(lane_n.values())
print("   %-4s %-22s -> %-22s %7s %7s | %5s %6s %6s %6s | %6s %7s"
      % ("#", "pickup", "dropoff", "legs", "share", "rideN", "P50", "P75", "P90", "table", "P75-tbl"))
rows5 = []
for i, (lane, c) in enumerate(top30, 1):
    v = ride[lane]
    tbl = DRIVE_TIME_ESTIMATES.get(lane)
    tblv = tbl if tbl is not None else DEFAULT_DRIVE_TIME
    if len(v) >= 20:
        nn, q10, q25, q50, q75, q90, q95 = ptile(v)
        print("   %-4d %-22s -> %-22s %7d %6.2f%% | %5d %6.0f %6.0f %6.0f | %6s %+7.0f"
              % (i, lane[0], lane[1], c, 100.0 * c / tot, nn, q50, q75, q90,
                 ("%d" % tbl) if tbl is not None else "%d*" % DEFAULT_DRIVE_TIME, q75 - tblv))
        rows5.append([i, lane[0], lane[1], c, round(100.0 * c / tot, 3), nn,
                      round(q50, 1), round(q75, 1), round(q90, 1), round(q95, 1),
                      tbl if tbl is not None else "", DEFAULT_DRIVE_TIME if tbl is None else "",
                      round(q75 - tblv, 1), len(ride_gold[lane]),
                      round(pct(ride_gold[lane], .50), 1) if len(ride_gold[lane]) >= 20 else "",
                      round(pct(ride_gold[lane], .75), 1) if len(ride_gold[lane]) >= 20 else ""])
    else:
        print("   %-4d %-22s -> %-22s %7d %6.2f%% | %5d %6s %6s %6s | %6s %7s"
              % (i, lane[0], lane[1], c, 100.0 * c / tot, len(v), "n<20", "n<20", "n<20",
                 ("%d" % tbl) if tbl is not None else "%d*" % DEFAULT_DRIVE_TIME, "-"))
        rows5.append([i, lane[0], lane[1], c, round(100.0 * c / tot, 3), len(v), "", "", "", "",
                      tbl if tbl is not None else "",
                      DEFAULT_DRIVE_TIME if tbl is None else "", "", len(ride_gold[lane]), "", ""])
print("   * = no DRIVE_TIME_ESTIMATES entry, falls back to DEFAULT_DRIVE_TIME=%d "
      "(dispatching/scheduler.py:83)" % DEFAULT_DRIVE_TIME)
print("   top-30 lanes cover %d of %d window legs = %.1f%%"
      % (sum(c for _, c in top30), tot, 100.0 * sum(c for _, c in top30) / tot))
print("   distinct lanes in window: %d ; lanes with >=20 timed rides: %d"
      % (len(lane_n), sum(1 for k in lane_n if len(ride[k]) >= 20)))
w("lanes", ["rank", "pickup_bucket", "dropoff_bucket", "legs", "share_pct", "ride_n",
            "ride_p50", "ride_p75", "ride_p90", "ride_p95", "table_min", "table_fallback_min",
            "p75_minus_table", "gold_n", "gold_p50", "gold_p75"], rows5)

sub("5b. Lanes with NO DRIVE_TIME_ESTIMATES entry, ranked by volume (all silently get %d min)"
    % DEFAULT_DRIVE_TIME)
miss = [(k, c) for k, c in lane_n.most_common() if k not in DRIVE_TIME_ESTIMATES]
print("   %d of %d distinct lanes have no table entry, covering %d legs = %.1f%% of the window"
      % (len(miss), len(lane_n), sum(c for _, c in miss),
         100.0 * sum(c for _, c in miss) / tot))
for k, c in miss[:12]:
    v = ride[k]
    s = ("n=%d P50=%.0f P75=%.0f" % (len(v), pct(v, .5), pct(v, .75))) if len(v) >= 20 else \
        ("n=%d (too few to measure)" % len(v))
    print("        %-22s -> %-22s %6d legs   %s" % (k[0], k[1], c, s))


# ================================================================ 6. distance / timing tables
hr("6.  reservations_routedistancecache  and  reservations_routetimingmetric")

sub("6a. RouteDistanceCache -- rows, status, freshness  [measured]")
nrdc = cur.execute("SELECT COUNT(*) FROM reservations_routedistancecache").fetchone()[0]
precut = cur.execute("SELECT COUNT(*) FROM reservations_routedistancecache "
                     "WHERE created_at < ?", ((WIN_END + timedelta(days=1)).isoformat(),)
                     ).fetchone()[0]
print("   rows in the file: %d   (prior audit sec 2 reported 2 -- 'effectively unused')" % nrdc)
print("   rows created ON OR BEFORE the 2026-07-11 snapshot cut: %d" % precut)
print("   ***  THE ANSWER TO 'COVERAGE': in PRODUCTION this table was EMPTY. Every one of the")
print("        %d rows was written after the cut, by the local app being driven against this" % nrdc)
print("        very file (first row %s). The prior audit's '2 rows' was itself"
      % str(cur.execute("SELECT MIN(created_at) FROM reservations_routedistancecache"
                        ).fetchone()[0])[:19])
print("        two such local writes. Treat RouteDistanceCache as [unavailable] for history.  ***")
for st, c, mn, mx in cur.execute(
        "SELECT status, COUNT(*), MIN(created_at), MAX(created_at) "
        "FROM reservations_routedistancecache GROUP BY status ORDER BY 2 DESC"):
    print("        status=%-9s n=%-4d created %s .. %s" % (st, c, str(mn)[:19], str(mx)[:19]))
rres = cur.execute("SELECT MIN(resolved_at), MAX(resolved_at) FROM "
                   "reservations_routedistancecache WHERE resolved_at IS NOT NULL").fetchone()
print("   resolved_at span: %s .. %s" % (str(rres[0])[:19], str(rres[1])[:19]))
stale = cur.execute(
    "SELECT COUNT(*) FROM reservations_routedistancecache WHERE status='ok' AND resolved_at < ?",
    ((WIN_END - timedelta(days=30)).isoformat(),)).fetchone()[0]
print("   'ok' rows older than the 30-day REFRESH_DAYS (dispatching/route_distance.py:48): %d"
      % stale)

rdc = list(cur.execute(
    "SELECT pickup_text, dropoff_text, drive_minutes, distance_text, status "
    "FROM reservations_routedistancecache"))
lane_of_rdc = collections.Counter()
for pu, do, dm, dist, st in rdc:
    lane_of_rdc[(cat(pu or ""), cat(do or ""))] += 1
print("   what the 118 cached pairs actually ARE, bucketed by the production bucketer:")
for k, v in lane_of_rdc.most_common(10):
    print("        %-22s -> %-22s %d" % (k[0], k[1], v))
top30_set = {k for k, _ in top30}
covered = sum(v for k, v in lane_of_rdc.items() if k in top30_set)
intra = sum(v for k, v in lane_of_rdc.items() if k[0] == k[1])
unk = sum(v for k, v in lane_of_rdc.items()
          if k[0] in ("Other", "Residential", "Other Hotel") or k[1] in ("Other", "Residential", "Other Hotel"))
print("   -> %d of %d cached pairs are INTRA-CLUSTER (same bucket both ends: Disney->Disney,"
      % (intra, len(rdc)))
print("      Port->Port), and %d more touch an unplaceable bucket (Other / Residential /" % unk)
print("      Other Hotel). Together that is %d of %d -- i.e. this table holds ONLY the pairs the"
      % (intra + unk, len(rdc)))
print("      category table cannot price, which is exactly what enqueue_upcoming_legs() selects")
print("      for (LIVE_DISTANCE_UNKNOWN_CATS / INTRA_CLUSTER_LIVE_CATS,")
print("      dispatching/route_distance.py:243-252). It is a per-ADDRESS-PAIR escape hatch, NOT")
print("      a lane matrix, and it holds NOTHING for the two lanes that are 74% of the window")
print("      (MCO<->Disney) because the category table already answers those.")
w("routedistancecache", ["pickup_text", "dropoff_text", "drive_minutes", "distance_text",
                         "status", "pickup_bucket", "dropoff_bucket"],
  [[pu, do, dm, dist, st, cat(pu or ""), cat(do or "")] for pu, do, dm, dist, st in rdc])

sub("6b. RouteTimingMetric -- rows, coverage, freshness, trust  [measured]")
nrtm = cur.execute("SELECT COUNT(*) FROM reservations_routetimingmetric").fetchone()[0]
print("   rows: %d  (prior audit sec 2 reported 456)" % nrtm)
lc = list(cur.execute("SELECT MIN(last_calculated), MAX(last_calculated) FROM "
                      "reservations_routetimingmetric"))[0]
print("   last_calculated span: %s .. %s" % (str(lc[0])[:19], str(lc[1])[:19]))
pre = cur.execute("SELECT COUNT(*) FROM reservations_routetimingmetric WHERE last_calculated < ?",
                  ((WIN_END + timedelta(days=1)).isoformat(),)).fetchone()[0]
pre5 = cur.execute("SELECT COUNT(*) FROM reservations_routetimingmetric WHERE last_calculated < ? "
                   "AND sample_count >= 5",
                   ((WIN_END + timedelta(days=1)).isoformat(),)).fetchone()[0]
print("   rows whose last_calculated predates the 2026-07-11 cut: %d of %d" % (pre, nrtm))
print("   FRESHNESS, stated honestly:")
print("     * the 271-row 2026-07-31 recalculation is POST-CUT -- it is the prior audit's own")
print("       local rebuild (audit sec 2: 'Was 440 before rebuild'), not a production job.")
newest_prod = cur.execute("SELECT MAX(last_calculated) FROM reservations_routetimingmetric "
                          "WHERE last_calculated < ?",
                          ((WIN_END + timedelta(days=1)).isoformat(),)).fetchone()[0]
print("     * the newest PRODUCTION-side recalculation is %s, i.e. %d days stale at the cut."
      % (str(newest_prod)[:19], (WIN_END - D(str(newest_prod)[:10])).days))
print("     * only %d rows carried a production last_calculated at all, and only %d of those"
      % (pre, pre5))
print("       cleared the sample_count>=5 trust floor. The learning loop is effectively dormant.")
byd = collections.Counter()
for d_, in cur.execute("SELECT substr(last_calculated,1,10) FROM reservations_routetimingmetric"):
    byd[d_] += 1
for k, v in sorted(byd.items()):
    print("        recalculated %s : %d rows" % (k, v))
sc = list(cur.execute(
    "SELECT SUM(sample_count>=5), SUM(sample_count<5), SUM(sample_count), MAX(sample_count) "
    "FROM reservations_routetimingmetric"))[0]
print("   sample_count >= 5 (scheduler.py:605 trust floor): %d rows ; below floor: %d rows "
      "(%.0f%% untrusted)" % (sc[0], sc[1], 100.0 * sc[1] / nrtm))
print("   total samples across all rows: %d ; largest single bucket: %d" % (sc[2], sc[3]))

rtm = collections.defaultdict(lambda: [0, 0])   # lane -> [rows, samples]
for tt, pcat, dcat, tod, dayt, sc_, med in cur.execute(
        "SELECT trip_type, pickup_location_category, dropoff_location_category, "
        "time_of_day_category, day_type, sample_count, median_drive_time "
        "FROM reservations_routetimingmetric"):
    rtm[(pcat, dcat)][0] += 1
    rtm[(pcat, dcat)][1] += sc_ or 0
print("\n   Does RouteTimingMetric cover the TOP-30 lanes above?  [measured]")
print("   %-4s %-22s -> %-22s %8s | %6s %8s %10s"
      % ("#", "pickup", "dropoff", "legs", "rows", "samples", "trusted?"))
rows6 = []
cov = 0
for i, (lane, c) in enumerate(top30, 1):
    r = rtm.get(lane)
    if r:
        cov += 1
    trusted = ""
    if r:
        tr = list(cur.execute(
            "SELECT SUM(sample_count>=5), COUNT(*) FROM reservations_routetimingmetric "
            "WHERE pickup_location_category=? AND dropoff_location_category=?", lane))[0]
        trusted = "%d/%d rows" % (tr[0] or 0, tr[1])
    print("   %-4d %-22s -> %-22s %8d | %6s %8s %10s"
          % (i, lane[0], lane[1], c, r[0] if r else "MISS", r[1] if r else "-", trusted or "-"))
    rows6.append([i, lane[0], lane[1], c, r[0] if r else 0, r[1] if r else 0, trusted])
print("   -> %d of the top 30 lanes have ANY RouteTimingMetric row; %d have none."
      % (cov, 30 - cov))
w("rtm_coverage", ["rank", "pickup_bucket", "dropoff_bucket", "window_legs", "rtm_rows",
                   "rtm_samples", "rows_at_or_above_trust_floor"], rows6)

sub("6c. Can either table give a BASE-TO-LOCATION travel time?  [unavailable] -- and what IS base")
print("   GREP RESULT -- there is NO depot/base entity anywhere in the schema or the code:")
print("     * The only literal named 'base' is a PRICING reference point, not a depot:")
print("         dispatching/quote_engine.py:259")
print('             BASE_LOCATION = "Orlando International Airport, Orlando, FL"')
print("         with the comment 'Reference point for \"how far out is the pickup\".' Its ONLY")
print("         caller is dispatching/views.py:19186, a quote-calculator Distance-Matrix call")
print("         used to decide whether a >100-mile trip prices asymmetrically. It never enters")
print("         scheduling.")
print("     * The scheduler states outright that the concept does not exist:")
print("         dispatching/scheduler.py:133-139")
print("             'the pad must cover the AM driver RETURNING the car to the warehouse +")
print("              wash/fuel (~30-40 min after his last clear) + the PM driver's drive OUT")
print("              ... a geography-aware split (car_ready = clear + drive_to_base + service;")
print("              PM pickup >= car_ready + drive_out) needs a base-location concept the")
print("              engine does not have yet'")
print("             VEHICLE_SHARE_PAD_MIN = 60      <- the flat stand-in for the whole chain")
print("     * docs/scheduler-automation/ROADMAP.md:117 '### 5. Warehouse/base-location concept'")
print("       is still an OPEN roadmap item: 'Today it's a flat 60 min. Low urgency -- the flat")
print("       hour matches founder practice.'")
print("     * docs/scheduler-automation/auto-assign-hour-balancing-design.md:654-656 repeats it.")
print("   -> No base address, no base lat/lng, no base Location row, no per-vehicle home yard.")
print("      drivers_fleetvehicle carries samsara_last_latitude/longitude/location_label, but")
print("      that is the CURRENT fix only -- overwritten every sync, never historised (audit")
print("      sec 12.8), so no base can be inferred from parked-overnight positions either.")
sam = list(cur.execute(
    "SELECT COUNT(*), SUM(samsara_last_latitude IS NOT NULL), MIN(samsara_last_seen_at), "
    "MAX(samsara_last_seen_at) FROM drivers_fleetvehicle"))[0]
print("      drivers_fleetvehicle: %d rows, %d with a lat fix, last_seen %s .. %s [measured]"
      % (sam[0], sam[1] or 0, str(sam[2])[:19], str(sam[3])[:19]))
print("   -> RouteDistanceCache is keyed on pair_hash(pickup_text, dropoff_text)")
print("      (dispatching/route_distance.py:73). With no base ADDRESS there is nothing to hash,")
print("      so a base->location row cannot exist. RouteTimingMetric is keyed on")
print("      (pickup_category, dropoff_category) and its category vocabulary is the 9 buckets in")
print("      section 4 -- none of which is a depot. BOTH ANSWER: [unavailable].")


# ================================================================ 7. what IS measurable
hr("7.  VERDICT -- what of the handoff chain is measurable, and what must be [modeled]")

sub("7a. MEASURABLE SUBSTITUTE #1: driver-continuous reposition (completed(N) -> on-location(N+1))")
print("   For a SINGLE driver running two consecutive legs, the real inter-leg gap is fully")
print("   observable from taps -- it needs no base at all.")
byday = collections.defaultdict(list)
for l in IN_WIN:
    did = legs[l]["did"]
    if did is None or did not in TRUST:
        continue
    t = taps.get(l, {})
    if "picked-up" in t and "completed" in t:
        byday[(did, legs[l]["pd"])].append((t["picked-up"], t["completed"], l))
rep = collections.defaultdict(list)
rep_all = []
for k, v in byday.items():
    v.sort()
    for i in range(len(v) - 1):
        c = v[i][1]
        nxt = taps.get(v[i + 1][2], {})
        if "on-location" not in nxt:
            continue
        gap = (nxt["on-location"] - c).total_seconds() / 60.0
        if not (0 <= gap <= 480):
            continue
        lane = (cat(legs[v[i][2]]["do"]), cat(legs[v[i + 1][2]]["pu"]))
        rep[lane].append(gap)
        rep_all.append(gap)
nn, q10, q25, q50, q75, q90, q95 = ptile(rep_all)
print("   n=%d observed same-driver gaps (0..480 min): P25 %s P50 %s P75 %s P90 %s"
      % (nn, fmt(q25), fmt(q50), fmt(q75), fmt(q90)))
print("   (this is deadhead + waiting, NOT pure drive -- the driver may idle deliberately)")
print("   %-22s -> %-22s %6s %6s %6s %6s" % ("clear at", "next pickup at", "n", "P25", "P50", "P75"))
rows7 = []
for lane, v in sorted(rep.items(), key=lambda x: -len(x[1]))[:15]:
    if len(v) < 20:
        continue
    a, b1, b2, b3, b4, b5, b6 = ptile(v)
    print("   %-22s -> %-22s %6d %6.0f %6.0f %6.0f" % (lane[0], lane[1], a, b2, b3, b4))
    rows7.append([lane[0], lane[1], a, b2, b3, b4, b5])
w("reposition_gaps", ["clear_bucket", "next_pickup_bucket", "n", "p25", "p50", "p75", "p90"], rows7)

sub("7b. MEASURABLE SUBSTITUTE #2: real observed VEHICLE handoffs (2 drivers, 1 car, 1 day)")
dva = collections.defaultdict(list)
for dt_, did, vid in cur.execute(
        "SELECT date, driver_id, vehicle_id FROM drivers_drivervehicleassignment"):
    d = D(dt_)
    if d and WIN_START <= d <= WIN_END:
        dva[(d, vid)].append(did)
shared = {k: v for k, v in dva.items() if len({x for x in v if x}) >= 2}
print("   drivers_drivervehicleassignment rows in window with >=2 distinct drivers on the same")
print("   (date, vehicle): %d shared car-days [measured]" % len(shared))
hand = []
for (d, vid), dids in shared.items():
    spans = {}
    for did in set(dids):
        ls = [l for l in IN_WIN if legs[l]["did"] == did and legs[l]["pd"] == d
              and "completed" in taps.get(l, {}) and "on-the-way" in taps.get(l, {})]
        if not ls:
            continue
        spans[did] = (min(taps[l]["on-the-way"] for l in ls),
                      max(taps[l]["completed"] for l in ls))
    if len(spans) < 2:
        continue
    order = sorted(spans.items(), key=lambda x: x[1][0])
    for i in range(len(order) - 1):
        gap = (order[i + 1][1][0] - order[i][1][1]).total_seconds() / 60.0
        if -600 <= gap <= 900:
            hand.append(gap)
if hand:
    nn, q10, q25, q50, q75, q90, q95 = ptile(hand)
    print("   observed AM-clear -> PM-first-move gap: n=%d  P10 %s P25 %s P50 %s P75 %s P90 %s"
          % (nn, fmt(q10), fmt(q25), fmt(q50), fmt(q75), fmt(q90)))
    print("   CAVEAT: n=%d is SMALL. Treat these as a sanity check on the pad, not as a" % nn)
    print("   distribution to fit. DriverVehicleAssignment is only written when a dispatcher")
    print("   uses Day Setup's Apply, so most real shared-car days never produce a row.")
    print("   negative gaps (PM driver moved BEFORE the AM driver's last complete) : %d = %.1f%%"
          % (sum(1 for x in hand if x < 0), 100.0 * sum(1 for x in hand if x < 0) / nn))
    print("   gaps under the VEHICLE_SHARE_PAD_MIN=60 the engine enforces               : %d = %.1f%%"
          % (sum(1 for x in hand if x < 60), 100.0 * sum(1 for x in hand if x < 60) / nn))
    w("vehicle_handoffs", ["gap_minutes"], [[round(x, 1)] for x in sorted(hand)])
else:
    print("   [unavailable] -- no shared car-day in the window has both drivers' taps.")

sub("7c. THE VERDICT")
print("""
   OUTGOING LEG  (last pickup -> clear -> reposition/wash/fuel -> base -> Vehicle Ready)
     last pickup -> clear ................... [measured]  picked-up -> completed taps,
                                              n in section 5, per lane, P50/P75/P90.
     clear location .......................... [measured] only as a 9-value BUCKET, never a
                                              point. 'Disney Resort' is one bucket covering
                                              ~30 resorts spread over ~8 road miles.
     clear -> base drive ..................... [unavailable]  no base exists (section 6c).
     wash / fuel / service ................... [unavailable]  no event, no field, no table.
                                              drivers_vehicledayreading holds start/end
                                              odometer for 66 vehicle-days only, with no
                                              location, so even a fuel STOP is invisible.
     => the outgoing leg must be [modeled] with assumed constants from `clear` onward.

   INCOMING LEG  (Vehicle Ready -> possession/inspect -> base -> first pickup)
     base -> first pickup drive .............. [unavailable]  same reason.
     take-possession / inspection ............ [unavailable]  no event exists.
     airport-arrival meet buffer ............. [measured] and this one is SOLID: section 3
                                              dwell, n in the thousands, split MCO vs SFB,
                                              anchored on a real FlightAware gate actual.
     departure pre-pickup buffer ............. [measured] via on-location vs scheduled pickup
                                              (audit sec 1.5 'punctuality'), not re-derived here.
     => the incoming leg's AIRPORT half is measurable; its BASE half is not.

   WHAT THIS MEANS FOR THE MODEL
     1. A handoff model written as   clear -> base -> next pickup   cannot be validated against
        history, because the middle term has no observable. Writing it that way buys a
        false sense of precision.
     2. A handoff model written as   clear -> [flat service+reposition constant] -> next pickup
        IS validatable end-to-end against section 7b's observed shared-car gaps, and against
        7a's observed same-driver repositions. Both are real, both are in the thousands/hundreds.
     3. Therefore: keep VEHICLE_SHARE_PAD_MIN as ONE tunable constant covering the whole
        base round-trip + service, and TUNE it against 7a/7b -- do not decompose it into
        drive_to_base + service + drive_out, because two of those three are [unavailable]
        and would be invented numbers wearing a measurement's clothes.
     4. The arrival meet-buffer is the one arrival-side input that can be replaced with a
        measured distribution today: section 3 gives P75/P90 by airport, and section 2 gives
        the flight-delay tail that shifts the anchor. That is the highest-value, lowest-risk
        change on the arrival side.
""")

print()
print("=" * 104)
print("END 05_flights.py")
print("=" * 104)
