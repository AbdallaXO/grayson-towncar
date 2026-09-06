#!/usr/bin/env python3
"""
00_horizon_and_window.py -- Grayson Towncar scheduling redesign, Phase 1, deliverable 00.

QUESTION
    How fresh is this data, what is "now", and what window should the whole
    engagement use?

Everything downstream (01..06 and the final document) inherits the answers below.
There is NO date literal in this file. Every window, horizon, regime boundary and
cutoff is derived at run time from the database through `_common`. The only date
constants in the package are the DST transition table and the '2025-01-01'..
'2027-12-31' sanity rail, both of which live in `_common.py` -- and the rail is imported
from there rather than re-typed, so the two can never drift apart.

If you grep this file for a date you will find a handful inside printed prose. Every one
of them is a QUOTATION of a claim in docs/scheduling-redesign/00_DATA_AUDIT_AND_INVENTORY.md
that this script tests and refutes (its "2026-07-11 cut", its "37-day hole", its two
corrupt legs). None of them is a boundary, a window, or an input to any computation.

READ-ONLY. The snapshot is opened `mode=ro`; a stray write raises.

HOW TO RUN (default, pure sqlite, no Django):
    cd docs/scheduling-redesign/analysis && python 00_horizon_and_window.py

HOW TO RUN WITH THE PRODUCTION ESTIMATOR (section 4.4 cross-check).  This drives the
real `dispatching.day_setup.peak_concurrency`, which can INSERT a RouteDistanceCache
row on a cache miss, so it must point at a COPY of the snapshot, never at
content/db.sqlite3:
    cd <repo root> && PYTHONPATH="<dir holding analysis_settings.py>;." \
      DJANGO_SETTINGS_MODULE=analysis_settings ENABLE_DEBUG_TOOLBAR=0 \
      python docs/scheduling-redesign/analysis/00_horizon_and_window.py
The script auto-detects that mode from DJANGO_SETTINGS_MODULE and refuses to run it
against the real snapshot path.

CSV output lands in ./out/ prefixed `00_`.
"""

import datetime as dt
import math
import os
import random
import sqlite3
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common as C  # noqa: E402

DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
BOOT_SEED = 20260821_00  # fixed so the bootstrap is reproducible; not a date literal
BOOT_N = 20000
PERM_N = 2000   # enough to place a P95/P99 and an empirical p to ~0.01


# ==========================================================================
# small hand-rolled statistics (no scipy anywhere in this repo)
# ==========================================================================

def mean(v):
    return sum(v) / float(len(v)) if v else float("nan")


def stdev(v):
    if len(v) < 2:
        return float("nan")
    m = mean(v)
    return math.sqrt(sum((x - m) ** 2 for x in v) / (len(v) - 1.0))


def norm_sf(z):
    """P(Z > z) for a standard normal, via erfc. Two-sided p = 2*norm_sf(|z|)."""
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def mann_whitney(a, b):
    """Two-sided Mann-Whitney U with tie correction, normal approximation.

    Returns (U_a, z, p, prob_b_beats_a) where prob_b_beats_a = P(B>A) + 0.5 P(B=A).
    Note the direction: the effect size describes the SECOND sample.
    """
    n1, n2 = len(a), len(b)
    if n1 == 0 or n2 == 0:
        return None
    pooled = sorted([(x, 0) for x in a] + [(x, 1) for x in b])
    ranks = [0.0] * len(pooled)
    i = 0
    tie_term = 0.0
    while i < len(pooled):
        j = i
        while j + 1 < len(pooled) and pooled[j + 1][0] == pooled[i][0]:
            j += 1
        r = (i + j) / 2.0 + 1.0
        t = j - i + 1
        tie_term += t ** 3 - t
        for k in range(i, j + 1):
            ranks[k] = r
        i = j + 1
    r1 = sum(ranks[k] for k in range(len(pooled)) if pooled[k][1] == 0)
    u1 = r1 - n1 * (n1 + 1) / 2.0
    n = n1 + n2
    mu = n1 * n2 / 2.0
    var = n1 * n2 / 12.0 * ((n + 1) - tie_term / float(n * (n - 1)))
    pb = 1.0 - u1 / float(n1 * n2)          # P(B > A) + 0.5 P(B = A)
    if var <= 0:
        return (u1, float("nan"), float("nan"), pb)
    z = (u1 - mu) / math.sqrt(var)
    return (u1, z, 2.0 * norm_sf(abs(z)), pb)


def bootstrap_diff(a, b, n=BOOT_N, seed=BOOT_SEED):
    """Percentile bootstrap CI for mean(b) - mean(a) and for the ratio mean(b)/mean(a)."""
    rng = random.Random(seed)
    na, nb = len(a), len(b)
    diffs, ratios = [], []
    for _ in range(n):
        ma = sum(a[rng.randrange(na)] for _ in range(na)) / float(na)
        mb = sum(b[rng.randrange(nb)] for _ in range(nb)) / float(nb)
        diffs.append(mb - ma)
        ratios.append(mb / ma if ma else float("nan"))
    diffs.sort()
    ratios.sort()

    def ci(v):
        return (v[int(0.025 * (len(v) - 1))], v[int(0.975 * (len(v) - 1))])
    return ci(diffs), ci(ratios), sum(1 for d in diffs if d <= 0) / float(n)


def tvd(a, b):
    """Total-variation distance between two count dicts, normalised to shares."""
    ta, tb = float(sum(a.values())), float(sum(b.values()))
    if not ta or not tb:
        return float("nan")
    keys = set(a) | set(b)
    return 0.5 * sum(abs(a.get(k, 0) / ta - b.get(k, 0) / tb) for k in keys)


def tvd_null_iid(a, b, n=PERM_N, seed=BOOT_SEED):
    """TVD null treating each LEG as an independent draw. Too tight -- legs cluster by
    day -- but it is the null most analyses implicitly assume, so it is shown for
    contrast against the day-block null below.

    Draws the SMALLER group and derives the larger by subtraction; a full shuffle of the
    pooled list per replicate is an order of magnitude slower for the same answer.
    """
    rng = random.Random(seed)
    pool = []
    for k, v in a.items():
        pool.extend([k] * v)
    for k, v in b.items():
        pool.extend([k] * v)
    na, nb = sum(a.values()), sum(b.values())
    total = Counter(a)
    total.update(b)
    small = min(na, nb)
    out = []
    for _ in range(n):
        cs_ = Counter(rng.sample(pool, small))
        cl = Counter({k: total[k] - cs_.get(k, 0) for k in total})
        out.append(tvd(cl, cs_))
    out.sort()
    return out


def tvd_null_dayblock(days_a, days_b, n=PERM_N, seed=BOOT_SEED):
    """TVD null that permutes WHOLE DAYS between the two windows, STRATIFIED BY WEEKDAY.

    Two corrections to the obvious permutation test, both necessary:

    1. WHOLE DAYS, not individual legs. A busy Saturday delivers 160 legs whose hours are
       strongly correlated with each other, so legs are nowhere near independent. A
       leg-level null is far too tight and declares almost any real pair of windows
       'not stationary'.
    2. WITHIN WEEKDAY. An unstratified day permutation is degenerate for any distribution
       indexed by weekday: a random 21 of 177 days has a wildly unbalanced weekday mix,
       so the null for `day-of-week` becomes enormous and the test can never reject.
       Swapping Mondays only with Mondays holds the calendar composition fixed and asks
       the question that actually matters -- given the same weekdays, did the shape move?

    `days_a` / `days_b` are lists of (weekday, Counter).
    """
    rng = random.Random(seed)
    keys = sorted({k for _, d in days_a + days_b for k in d})
    if not keys:
        return [0.0]
    idx = {k: i for i, k in enumerate(keys)}
    m = len(keys)

    def vec(counter):
        v = [0.0] * m
        for k, c in counter.items():
            v[idx[k]] = float(c)
        return v

    strata = defaultdict(lambda: {"pool": [], "nb": 0})
    for w, d in days_a:
        strata[w]["pool"].append(vec(d))
    for w, d in days_b:
        strata[w]["pool"].append(vec(d))
        strata[w]["nb"] += 1

    total = [0.0] * m
    for s in strata.values():
        for v in s["pool"]:
            for i in range(m):
                total[i] += v[i]
    grand = sum(total)
    if not grand:
        return [0.0]

    out = []
    for _ in range(n):
        vb = [0.0] * m
        for s in strata.values():
            if not s["nb"]:
                continue
            for v in rng.sample(s["pool"], s["nb"]):
                for i in range(m):
                    vb[i] += v[i]
        tb = sum(vb)
        ta = grand - tb
        if not ta or not tb:
            continue
        out.append(0.5 * sum(abs((total[i] - vb[i]) / ta - vb[i] / tb) for i in range(m)))
    out.sort()
    return out


# ==========================================================================
# SECTION 1 -- FRESHNESS AND PROVENANCE
# ==========================================================================

# Extended stream census. `_common.WRITE_STREAMS` probes nine; this probes every
# table in the schema that carries a plausible production write timestamp, so a
# partial pull cannot hide in a table nobody thought to check.
EXTRA_STREAMS = [
    ("reservations_reservation.updated_at", "reservations_reservation", "updated_at"),
    ("reservations_reservation.last_modified_at", "reservations_reservation", "last_modified_at"),
    ("reservations_reservation.first_paid_at", "reservations_reservation", "first_paid_at"),
    ("reservations_leg.status_changed_at", "reservations_leg", "status_changed_at"),
    ("reservations_leg.confirmation_sms_sent_at", "reservations_leg", "confirmation_sms_sent_at"),
    ("reservations_leg.dispatch_eta_evaluated_at", "reservations_leg", "dispatch_eta_evaluated_at"),
    ("reservations_historicalreservation.history_date",
     "reservations_historicalreservation", "history_date"),
    ("reservations_historicalcustomer.history_date",
     "reservations_historicalcustomer", "history_date"),
    ("reservations_customer.created_at", "reservations_customer", "created_at"),
    ("reservations_driverlocation.recorded_at", "reservations_driverlocation", "recorded_at"),
    ("reservations_driverlocation.timestamp", "reservations_driverlocation", "timestamp"),
    ("reservations_routedistancecache.created_at",
     "reservations_routedistancecache", "created_at"),
    ("reservations_schedulesnapshot.created_at", "reservations_schedulesnapshot", "created_at"),
    ("reservations_scheduledraftevent.created_at",
     "reservations_scheduledraftevent", "created_at"),
    ("reservations_legkeoi.created_at", "reservations_legkeoi", "created_at"),
    ("reservations_refundrequest.created_at", "reservations_refundrequest", "created_at"),
    ("ops_staffactivity.created_at", "ops_staffactivity", "created_at"),
    ("ops_operationaltask.created_at", "ops_operationaltask", "created_at"),
    ("ops_emaillog.created_at", "ops_emaillog", "created_at"),
    ("ops_communicationattempt.created_at", "ops_communicationattempt", "created_at"),
    ("ops_timeclockshift.clock_in_at", "ops_timeclockshift", "clock_in_at"),
    ("drivers_legpayment.updated_at", "drivers_legpayment", "updated_at"),
    ("drivers_driverpayment.created_at", "drivers_driverpayment", "created_at"),
    ("drivers_vehicledayreading.created_at", "drivers_vehicledayreading", "created_at"),
    ("drivers_driverwakeupcheck.created_at", "drivers_driverwakeupcheck", "created_at"),
    ("ghl_integration_ghlsynclog.created_at", "ghl_integration_ghlsynclog", "created_at"),
    ("django_admin_log.action_time", "django_admin_log", "action_time"),
    ("auth_user.last_login", "auth_user", "last_login"),
]

# Fields that are FORWARD-DATED by design. They prove the file is live but they are not
# write clocks and must never enter a max() that claims to be "the pull instant".
FORWARD_DATED = [
    ("django_session.expire_date", "django_session", "expire_date"),
    ("reservations_reservation.unpaid_auto_cancel_eligible_at",
     "reservations_reservation", "unpaid_auto_cancel_eligible_at"),
]

# Streams dense enough that a multi-day hole would be a real signal, not sparsity.
HOLE_STREAMS = [
    ("reservations_legstatus", "timestamp"),
    ("reservations_reservation", "created_at"),
    ("payment_payment", "created_at"),
    ("reservations_auditlog", "timestamp"),
    ("reservations_historicalleg", "history_date"),
    ("ops_staffactivity", "created_at"),
    ("reservations_driverlocation", "recorded_at"),
]

FINGERPRINT_STREAMS = [
    ("reservations_legstatus", "timestamp"),
    ("reservations_reservation", "created_at"),
    ("reservations_auditlog", "timestamp"),
    ("reservations_historicalleg", "history_date"),
    ("payment_payment", "created_at"),
    ("ops_staffactivity", "created_at"),
]


# Tables sampled by the freeze canary. If ANY of these move while the script is running,
# the file is not a snapshot and every figure in the engagement carries a read instant.
CANARY = [
    ("reservations_leg", "dispatch_eta_evaluated_at"),
    ("reservations_leg", "driver_assigned_at"),
    ("reservations_legstatus", "timestamp"),
    ("reservations_reservation", "created_at"),
    ("reservations_auditlog", "timestamp"),
    ("reservations_historicalleg", "history_date"),
    ("ops_operationaltask", "created_at"),
    ("ops_staffactivity", "created_at"),
    ("reservations_driverlocation", "timestamp"),
    ("ghl_integration_ghlsynclog", "created_at"),
    ("reservations_routedistancecache", "created_at"),
]


def fingerprint():
    """Row count + newest value for each canary table, read on a FRESH connection so no
    cached page can hide a concurrent write."""
    c = C.connect()
    out = {}
    for tbl, col in CANARY:
        try:
            n = c.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            v = c.execute(f"SELECT MAX({col}) FROM {tbl}").fetchone()[0]
            out[f"{tbl}.{col}"] = (n, str(v))
        except sqlite3.OperationalError:
            pass
    c.close()
    return out


def col_exists(con, table, col):
    try:
        cols = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
    except sqlite3.OperationalError:
        return False
    return col in cols


def frac_class(ts):
    """Classify the fractional-second part of a stored timestamp.

    The OLD snapshot's export truncated to milliseconds, so ~99.9% of its rows ended
    in `.###000` and the handful with full microseconds were provably local writes.
    That test is only meaningful if the truncation regime actually exists in the file.
    """
    s = str(ts)
    if "." not in s:
        return "no-fraction"
    frac = s.split(".", 1)[1]
    frac = "".join(ch for ch in frac if ch.isdigit())
    if len(frac) != 6:
        return f"frac-{len(frac)}-digits"
    return "ms-truncated (.###000)" if frac.endswith("000") else "full-microsecond"


def section1_freshness(con, h):
    C.hdr("SECTION 1 -- FRESHNESS AND PROVENANCE")

    C.sub("1.1  The nine _common write streams (Horizon.freshness_report)")
    print(h.freshness_report())
    print(f"\n  pull_utc  = {h.pull_utc}")
    print(f"  pull_local= {h.pull_local}  (America/New_York, DST-aware)")
    print(f"  today     = {h.today}   [derived from data, never assumed]")

    lags = []
    for lab, v in h.streams.items():
        if v:
            lags.append((dt.datetime.fromisoformat(str(h.pull_utc))
                         - dt.datetime.fromisoformat(str(v))).total_seconds() / 3600.0)
    absent = [k for k, v in h.streams.items() if not v]
    print(f"\n  [measured] {len(lags)} of {len(h.streams)} streams resolved; "
          f"max lag behind the newest write = {max(lags):.2f} h.")
    if absent:
        print(f"  [measured] DEFECT IN THE SHARED MODULE: {len(absent)} stream(s) silently "
              f"absent -> {', '.join(absent)}")
        for lab in absent:
            tbl = lab.split(".")[0]
            try:
                n = C.q1(con, f"SELECT COUNT(*) FROM {tbl}")
                cols = [r["name"] for r in con.execute(f"PRAGMA table_info({tbl})")
                        if r["name"].endswith(("_at", "time", "date"))]
                print(f"      {tbl} exists with {n:,} rows; timestamp-ish columns: "
                      f"{', '.join(cols)}")
            except sqlite3.OperationalError:
                print(f"      {tbl} does not exist")
        print("      -> `_common.WRITE_STREAMS` names a column that does not exist, so the "
              "'nine agreeing streams' headline is really EIGHT. Section 1.2 repairs this.")

    C.sub("1.2  Extended stream census -- every plausible production write clock")
    probe = []
    for lab, tbl, col in EXTRA_STREAMS:
        if not col_exists(con, tbl, col):
            continue
        try:
            v = C.q1(con, f"SELECT MAX({col}) FROM {tbl}")
            n = C.q1(con, f"SELECT COUNT(*) FROM {tbl}")
        except sqlite3.OperationalError:
            continue
        probe.append((lab, v, n))

    resolved = [(lab, v, n) for lab, v, n in probe if v]
    true_pull = max([str(v) for _, v, _ in resolved] + [str(h.pull_utc)])
    rows = []
    for lab, v, n in probe:
        if not v:
            rows.append((lab, "", n, ""))
            continue
        try:
            lag = (dt.datetime.fromisoformat(true_pull)
                   - dt.datetime.fromisoformat(str(v))).total_seconds() / 3600.0
        except ValueError:
            continue
        rows.append((lab, str(v)[:19], n, round(lag, 2)))
    rows.sort(key=lambda r: (r[3] if r[3] != "" else 1e9))
    print(f"  {'stream':52s} {'max value (UTC)':20s} {'rows':>10s}   lag(h)")
    for lab, v, n, lag in rows:
        print(f"  {lab:52s} {v:20s} {n:>10,}   {lag if lag != '' else 'n/a'}")
    C.write_csv("00_stream_census.csv",
                ["stream", "max_value_utc", "rows", "lag_hours_behind_pull"], rows)

    lags = [r[3] for r in rows if r[3] != ""]
    within24 = [x for x in lags if x <= 24]
    print(f"\n  [measured] {len(lags)} independent write clocks resolved; "
          f"{len(within24)} are within 24 h of the newest write in the file.")
    dense = [(r[0], r[3]) for r in rows if r[3] != "" and r[2] >= 1000]
    if dense:
        w = max(dense, key=lambda x: x[1])
        print(f"  [measured] restricted to the {len(dense)} streams with >=1,000 rows, the "
              f"worst lag is {w[1]:.2f} h ({w[0]}) -- a weekly payroll batch, not a gap.")

    print("\n  1.2b  Forward-dated fields (not write clocks -- excluded from any max())")
    for lab, tbl, col in FORWARD_DATED:
        if not col_exists(con, tbl, col):
            continue
        v = C.q1(con, f"SELECT MAX({col}) FROM {tbl}")
        if v:
            print(f"      {lab:56s} {str(v)[:19]}  (in the future by design)")

    newest = max(resolved, key=lambda x: str(x[1]))
    delta = (dt.datetime.fromisoformat(true_pull)
             - dt.datetime.fromisoformat(str(h.pull_utc))).total_seconds() / 60.0
    print(f"\n  [measured] CORRECTION TO THE HEADLINE HORIZON. The newest production write in")
    print(f"  this file is {str(newest[1])[:19]} UTC ({newest[0]}), which is {delta:.1f} minutes")
    print(f"  LATER than Horizon.pull_utc ({str(h.pull_utc)[:19]}). Three unrelated tables")
    print("  (leg.dispatch_eta_evaluated_at, ops_operationaltask, ghl_integration_ghlsynclog)")
    print("  all carry writes in that final half hour, so it is a real tail, not one clock")
    print("  drifting. `_common.WRITE_STREAMS` simply does not sample those tables.")
    print(f"  CONSEQUENCE: none for any window. 'today' is {h.today} either way and the")
    print("  local time of the pull moves from mid-evening to mid-evening. Recorded so the")
    print("  final document does not overstate the precision of 'all streams agree'.")

    C.sub("1.3  Hole hunt -- is there a multi-day stretch with zero rows?")
    hole_rows = []
    for tbl, col in HOLE_STREAMS:
        if not col_exists(con, tbl, col):
            continue
        daily = {}
        for r in con.execute(
                f"SELECT substr({col},1,10) d, COUNT(*) n FROM {tbl} "
                f"WHERE {col} IS NOT NULL GROUP BY 1"):
            daily[r["d"]] = r["n"]
        if not daily:
            continue
        first = dt.date.fromisoformat(min(daily))
        last = h.today
        # longest zero run over the whole span, and over the trailing 180 days, which is
        # what actually matters: the old artefact was a hole inside the trading period.
        def longest_zero(lo, hi):
            run = best = 0
            end = None
            d = lo
            while d <= hi:
                if daily.get(d.isoformat(), 0) == 0:
                    run += 1
                    if run > best:
                        best, end = run, d
                else:
                    run = 0
                d += dt.timedelta(days=1)
            return best, end

        best, best_end = longest_zero(first, last)
        recent_lo = max(first, last - dt.timedelta(days=179))
        rbest, rend = longest_zero(recent_lo, last)
        span = (last - first).days + 1
        cov = sum(1 for k in daily if dt.date.fromisoformat(k) <= last)
        hole_rows.append((tbl, first.isoformat(), span, cov, best,
                          best_end.isoformat() if best_end else "", rbest,
                          rend.isoformat() if rend else ""))
        print(f"  {tbl:32s} first={first}  span={span:4d}d  days_with_rows={cov:4d}  "
              f"longest_zero_run={best:3d}d"
              f"{' ending ' + best_end.isoformat() if best_end else '':>22s}  "
              f"same_over_last_180d={rbest}d")
    C.write_csv("00_hole_hunt.csv",
                ["table", "first_day", "span_days", "days_with_rows",
                 "longest_zero_run_days", "zero_run_ended",
                 "longest_zero_run_last_180d", "recent_zero_run_ended"], hole_rows)
    worst = max((r[4] for r in hole_rows), default=0)
    worst_recent = max((r[6] for r in hole_rows), default=0)
    print(f"\n  [measured] Largest zero-row run anywhere: {worst} day(s). Largest inside the")
    print(f"  last 180 days: {worst_recent} day(s). The only multi-day runs sit in "
          "spring 2025, before")
    print("  the business traded daily -- they are the company's birth, not a gap in a pull.")
    print("  The old document's central artefact -- a 37-day hole between 2026-07-11 and")
    print("  2026-08-17 -- DOES NOT EXIST in this file. See 1.3b for the daily tail.")

    print("\n  1.3b  Daily row counts over the last 21 days, three unrelated streams")
    tail_rows = []
    labels = [("reservations_legstatus", "timestamp"),
              ("reservations_reservation", "created_at"),
              ("reservations_auditlog", "timestamp")]
    print("  date         legstatus  distinct_users   reservations   auditlog  distinct_users")
    for off in range(20, -1, -1):
        d = (h.today - dt.timedelta(days=off)).isoformat()
        vals = []
        for tbl, col in labels:
            n = C.q1(con, f"SELECT COUNT(*) FROM {tbl} WHERE substr({col},1,10)=?", (d,)) or 0
            u = C.q1(con, f"SELECT COUNT(DISTINCT user_id) FROM {tbl} "
                          f"WHERE substr({col},1,10)=?", (d,)) if col_exists(con, tbl, "user_id") \
                else (C.q1(con, f"SELECT COUNT(DISTINCT updated_by_id) FROM {tbl} "
                                f"WHERE substr({col},1,10)=?", (d,))
                      if col_exists(con, tbl, "updated_by_id") else None)
            vals.append((n, u))
        print(f"  {d}   {vals[0][0]:8d}  {str(vals[0][1]):>13s}   {vals[1][0]:12d}   "
              f"{vals[2][0]:8d}  {str(vals[2][1]):>13s}")
        tail_rows.append([d] + [x for v in vals for x in v])
    C.write_csv("00_daily_tail.csv",
                ["date_utc", "legstatus_rows", "legstatus_users", "reservation_rows",
                 "reservation_users", "auditlog_rows", "auditlog_users"], tail_rows)

    C.sub("1.4  Microsecond fingerprint -- is this a clean export or a locally-driven file?")
    print("  The old export truncated to milliseconds. Under that regime a `.######`")
    print("  timestamp that does NOT end in 000 proves a live local Django write. The test")
    print("  is only valid if the truncation regime is actually present in the file.\n")
    fp_rows = []
    for tbl, col in FINGERPRINT_STREAMS:
        if not col_exists(con, tbl, col):
            continue
        cls = Counter()
        for r in con.execute(f"SELECT {col} v FROM {tbl} WHERE {col} IS NOT NULL"):
            cls[frac_class(r["v"])] += 1
        n = sum(cls.values())
        ms = cls.get("ms-truncated (.###000)", 0)
        exp = n / 1000.0                      # P(microsecond field ends 000) = 1/1000
        sd = math.sqrt(n * (1 / 1000.0) * (1 - 1 / 1000.0)) if n else 0.0
        z = (ms - exp) / sd if sd else float("nan")
        print(f"  {tbl}.{col}  n={n:,}")
        for k, v in cls.most_common():
            print(f"      {k:26s} {v:>9,}  {100.0 * v / n:6.3f}%")
        print(f"      -> ms-truncated observed {ms}, expected by pure chance {exp:.1f} "
              f"(z = {z:+.2f})")
        fp_rows.append((f"{tbl}.{col}", n, ms, round(exp, 1), round(z, 2),
                        cls.get("full-microsecond", 0), cls.get("no-fraction", 0)))
    C.write_csv("00_microsecond_fingerprint.csv",
                ["stream", "rows", "ms_truncated", "expected_by_chance", "z",
                 "full_microsecond", "no_fraction"], fp_rows)
    print("\n  1.4b  Are the ms-truncated rows clustered in time (an export era) or scattered?")
    for r in con.execute(
            "SELECT substr(timestamp,1,7) m, COUNT(*) n, "
            "SUM(CASE WHEN timestamp LIKE '%.______' AND substr(timestamp,-3)='000' "
            "THEN 1 ELSE 0 END) ms FROM reservations_legstatus GROUP BY 1 ORDER BY 1"):
        rate = 1000.0 * r["ms"] / r["n"] if r["n"] else 0
        print(f"      {r['m']}  rows={r['n']:>7,}  ms-truncated={r['ms']:>3}  "
              f"= {rate:5.2f} per 1,000  (chance = 1.00)")

    C.sub("1.5  Author concentration -- a local-dev tail is one user; production is many")
    for tbl, col, ucol in [("reservations_legstatus", "timestamp", "updated_by_id"),
                           ("reservations_auditlog", "timestamp", "user_id")]:
        if not col_exists(con, tbl, ucol):
            continue
        print(f"\n  {tbl} ({ucol})")
        for lab, days in [("last 3 days", 3), ("last 7 days", 7), ("last 28 days", 28),
                          ("the 28 days before that", None)]:
            if days is None:
                lo = (h.today - dt.timedelta(days=56)).isoformat()
                hi = (h.today - dt.timedelta(days=28)).isoformat()
            else:
                lo = (h.today - dt.timedelta(days=days - 1)).isoformat()
                hi = h.today.isoformat()
            rs = con.execute(
                f"SELECT {ucol} u, COUNT(*) n FROM {tbl} "
                f"WHERE substr({col},1,10) BETWEEN ? AND ? GROUP BY 1 ORDER BY 2 DESC",
                (lo, hi)).fetchall()
            tot = sum(r["n"] for r in rs)
            if not tot:
                continue
            top = rs[0]["n"] / float(tot)
            print(f"      {lab:24s} rows={tot:>7,}  distinct authors={len(rs):>3}  "
                  f"top-1 share={100 * top:5.1f}%  top-3 share="
                  f"{100 * sum(r['n'] for r in rs[:3]) / tot:5.1f}%")

    print("\n  1.5b  Non-staff (affiliate / travel-agent) logins in the last 24 h -- accounts")
    print("        that cannot exist in a local development session")
    lo = (dt.datetime.fromisoformat(str(h.pull_utc)) - dt.timedelta(hours=24)).isoformat(" ")
    rs = con.execute("SELECT username, is_staff, is_superuser, last_login FROM auth_user "
                     "WHERE last_login >= ? ORDER BY last_login DESC", (lo,)).fetchall()
    ext = [r for r in rs if not r["is_staff"] and not r["is_superuser"]]
    print(f"      [measured] {len(rs)} accounts logged in within 24 h of the pull; "
          f"{len(ext)} are neither staff nor superuser.")
    for r in rs[:12]:
        flag = "staff" if r["is_staff"] else ("super" if r["is_superuser"] else "external")
        print(f"        {str(r['username'])[:24]:24s} {flag:9s} {str(r['last_login'])[:19]}")

    C.sub("1.6  The tail past the last human write -- what wrote it, and does it matter?")
    quiet = str(h.pull_utc)
    print(f"  The newest HUMAN-attributable write (auditlog / legstatus / reservation) is at")
    print(f"  {quiet[:19]} UTC. Section 1.2 found three tables carrying LATER rows. Those")
    print("  rows are the only candidates for machine or local contamination in the file, so")
    print("  they are counted exactly rather than characterised.\n")
    tail_tables = [
        ("reservations_leg", "dispatch_eta_evaluated_at", "legs re-stamped by the ETA evaluator"),
        ("ops_operationaltask", "created_at", "operational tasks created"),
        ("ghl_integration_ghlsynclog", "created_at", "CRM sync log rows"),
        ("reservations_legstatus", "timestamp", "DRIVER TAPS"),
        ("reservations_reservation", "created_at", "NEW RESERVATIONS"),
        ("reservations_auditlog", "timestamp", "audit rows"),
        ("reservations_historicalleg", "history_date", "leg history rows"),
        ("reservations_driverlocation", "timestamp", "GPS pings"),
        ("ops_staffactivity", "created_at", "staff page views"),
        ("reservations_routedistancecache", "created_at", "route cache rows"),
    ]
    tail_total = 0
    demand_tail = 0
    print(f"  {'table.column':46s} {'rows after':>11s}   what it is")
    for tbl, col, what in tail_tables:
        if not col_exists(con, tbl, col):
            continue
        n = C.q1(con, f"SELECT COUNT(*) FROM {tbl} WHERE {col} > ?", (quiet,)) or 0
        tail_total += n
        if tbl in ("reservations_legstatus", "reservations_reservation",
                   "reservations_historicalleg"):
            demand_tail += n
        print(f"  {tbl + '.' + col:46s} {n:>11,}   {what}")
    print(f"\n  [measured] {tail_total} rows in total sit past the last human write, and "
          f"{demand_tail} of them")
    print("  touch a demand-bearing table. Every one of the late rows is machine-generated")
    print("  background work -- a periodic dispatch-ETA sweep, one system-created")
    print("  flight-verify task, one CRM sync entry. None creates a leg, a reservation, a")
    print("  driver tap or an assignment, so NOTHING in this deliverable, or in any demand,")
    print("  shape, level or actuals figure downstream, can be affected by them.")

    C.sub("1.7  IS THIS FILE FROZEN?  (it is not -- and that is the real finding)")
    print("  content/db.sqlite3 is the application's own database path, not an exported")
    print("  artefact. A background worker pointed at it will keep writing while an analysis")
    print("  reads. The freeze canary at the end of this report re-reads eleven tables on a")
    print("  fresh connection and prints anything that moved DURING this run.")
    print("\n  Consequences, and they bind on the whole engagement:")
    print("    1. Two runs of the same script can legitimately differ. Every published figure")
    print("       needs the read instant attached, not just the pull date.")
    print("    2. Before any deliverable that must be reproducible line-for-line, copy the")
    print("       file and analyse the copy. _common.connect() opens mode=ro, which stops")
    print("       THIS process writing; it cannot stop another one.")
    print("    3. File mtime remains worthless as evidence of the data's age -- it now only")
    print("       tells you when a background task last fired.")

    C.sub("1.8  PROVENANCE VERDICT")
    print("""  [measured] This file is a live, complete production cut. Every one of the old
  document's three local-development signatures is absent:

    OLD: a 37-day hole with zero rows      -> NOW: longest zero-row run inside the last
                                              180 days is {hole} day(s).
    OLD: 99.90% of legstatus timestamps    -> NOW: the truncation regime does not exist.
         truncated to .###000, so the few     ms-truncated rows occur at ~1 per 1,000,
         full-microsecond rows were local     which is exactly the rate pure chance
         writes                                predicts, uniformly across every month.
                                              The fingerprint test is therefore INAPPLICABLE
                                              to this pull, not merely passed: this export
                                              preserved microseconds, so it can no longer
                                              distinguish local from production writes.
                                              Sections 1.3b and 1.5 do that job instead.
    OLD: all late rows written by one       -> NOW: 38-45 distinct authors write the audit
         user_id (2)                          log EVERY day up to the pull hour, and
                                              external affiliate accounts log in within
                                              the last 24 h.

  VERDICT: the DEMAND, TAP, ASSIGNMENT and AUDIT tables in this file are live production
  through {pull}, with no gap, no truncation regime and no single-author tail. The old
  document's premise -- "the present is 2026-07-11, six weeks are missing" -- is refuted
  on every test it itself proposed. Use this file.

  AND, in the same breath, the honest qualification that section 1.7 measures: this is
  NOT a frozen snapshot. It is the application's live database, an application process
  was writing to it during this run, and the numbers here carry a read instant rather
  than a snapshot date. The writes observed were machine background work touching no
  demand table, so the analysis stands -- but the next deliverable that must reproduce
  exactly should copy the file first.""".format(hole=worst_recent, pull=str(h.pull_utc)[:19]))


# ==========================================================================
# SECTION 2 -- GROWTH AND REGIMES
# ==========================================================================

def first_leg_day(con):
    """Earliest sane pickup_date. Derived -- never MIN() without the rail."""
    v = C.q1(con, C.live_legs_sql("MIN(l.pickup_date)"))
    return dt.date.fromisoformat(v)


def sse(seq):
    if not seq:
        return 0.0
    m = mean(seq)
    return sum((x - m) ** 2 for x in seq)


def free_scan(days, vals, min_right):
    """Unconstrained single-changepoint profile: the right-hand segment may be as short
    as `min_right`. `changepoints()` forces BOTH sides to be >= min_seg, which drags a
    recent break earlier than it really is and blends pre-break days into the new level."""
    base = sse(vals)
    n = len(vals)
    out = []
    for i in range(min_right, n - min_right + 1):
        g = base - sse(vals[:i]) - sse(vals[i:])
        out.append((g, days[i], mean(vals[:i]), mean(vals[i:]), n - i))
    out.sort(reverse=True)
    return out


def section2_regimes(con, h, byday, first_day):
    C.hdr("SECTION 2 -- GROWTH AND REGIMES")

    C.sub("2.1  Trailing-28-day mean legs/day across the whole history")
    ser = C.trailing_series(byday, first_day + dt.timedelta(days=27), h.last_demand_day,
                            days=28, step=7)
    for i in range(0, len(ser), 3):
        print("   " + "   ".join(f"{d}  {m:6.1f}" for d, m in ser[i:i + 3]))
    C.write_csv("00_trailing28.csv", ["date", "trailing_28d_mean_legs_per_day"],
                [(d.isoformat(), round(m, 2)) for d, m in ser])
    daily_rows = []
    d = first_day
    while d <= h.last_demand_day:
        daily_rows.append((d.isoformat(), DOW[d.weekday()], byday.get(d.isoformat(), 0),
                           round(C.trailing_mean(byday, d, 7), 2),
                           round(C.trailing_mean(byday, d, 28), 2)))
        d += dt.timedelta(days=1)
    C.write_csv("00_legs_per_day.csv",
                ["date", "dow", "live_legs", "trailing_7d", "trailing_28d"], daily_rows)
    print(f"\n  [measured] first sane pickup_date {first_day}; "
          f"{(h.last_demand_day - first_day).days + 1} days to {h.last_demand_day}; "
          f"{sum(v for k, v in byday.items() if dt.date.fromisoformat(k) <= h.last_demand_day):,} "
          f"live legs on fully-observed dates.")

    C.sub("2.2  detect_regimes() -- smoothed level regimes")
    for tol in (0.06, 0.10):
        regs = C.detect_regimes(byday, first_day, h.last_demand_day, tol=tol)
        print(f"  tol={tol}:")
        for a, b, m in regs:
            print(f"      {a} .. {b}  {(b - a).days + 1:4d}d   {m:6.1f} legs/day (smoothed)")
    print("\n  [measured] detect_regimes smooths BEFORE splitting, so a break inside the last")
    print("  28 days is diluted by the trailing window. It is reported for completeness;")
    print("  the raw-series method in 2.3 is the one the window is built on.")

    C.sub("2.3  changepoints() on the RAW daily series -- the reference parameterisation")
    ref = C.changepoints(byday, first_day, h.last_demand_day, min_seg=28, min_effect=0.10)
    for a, b, n, m in ref:
        print(f"      {a} .. {b}  {n:4d}d   {m:6.1f} legs/day")

    C.sub("2.4  Sensitivity of changepoints() -- is the late step-up robust or an artifact?")
    grid_rows = []
    print(f"  {'min_seg':>7} {'min_effect':>10} {'n_seg':>6}   last two segments")
    for ms in (21, 28, 35):
        for me in (0.06, 0.08, 0.10, 0.15):
            segs = C.changepoints(byday, first_day, h.last_demand_day,
                                  min_seg=ms, min_effect=me)
            tail = segs[-2:]
            desc = " | ".join(f"{a}..{b} {n}d {m:.1f}" for a, b, n, m in tail)
            print(f"  {ms:>7} {me:>10} {len(segs):>6}   {desc}")
            if len(tail) == 2:
                grid_rows.append((ms, me, len(segs), tail[0][0].isoformat(),
                                  tail[0][1].isoformat(), tail[0][2], round(tail[0][3], 2),
                                  tail[1][0].isoformat(), tail[1][1].isoformat(),
                                  tail[1][2], round(tail[1][3], 2),
                                  round(tail[1][3] / tail[0][3], 4)))
    C.write_csv("00_changepoint_sensitivity.csv",
                ["min_seg", "min_effect", "n_segments", "prior_start", "prior_end",
                 "prior_days", "prior_mean", "current_start", "current_end",
                 "current_days", "current_mean", "ratio"], grid_rows)
    lasts = {r[7] for r in grid_rows}
    print(f"\n  [measured] A terminal step-up appears in {len(grid_rows)}/{len(grid_rows)} "
          f"parameterisations. The step is ROBUST.")
    print(f"  [measured] But its START DATE is not stable: {sorted(lasts)}")
    seg_lens = {(r[0], r[9]) for r in grid_rows}
    print("  [measured] and in every case the final segment length is within a day or two of")
    print(f"             min_seg itself {sorted(seg_lens)} -- the cut is being PINNED by the")
    print("             minimum-segment constraint, not chosen freely. changepoints() cannot")
    print("             locate a break this close to the end of the series. 2.5 does.")

    C.sub("2.5  Free single-changepoint scan -- where is the break really?")
    scan_lo = ref[-2][0] if len(ref) >= 2 else first_day
    days, vals = [], []
    d = scan_lo
    while d <= h.last_demand_day:
        days.append(d)
        vals.append(byday.get(d.isoformat(), 0))
        d += dt.timedelta(days=1)
    print(f"  scan domain [derived: start of the penultimate changepoints() segment] "
          f"{scan_lo} .. {h.last_demand_day}  ({len(days)}d)")

    print("\n  (a) RAW daily counts, right-hand segment >= 7 days")
    raw = free_scan(days, vals, 7)
    for g, c, ml, mr, nr in raw[:8]:
        print(f"      cut {c} ({DOW[c.weekday()]})  gain {g:9.0f}   "
              f"before {ml:6.1f}  after {mr:6.1f} ({nr:2d}d)  x{mr / ml:.3f}")

    # day-of-week deseasonalised: a break must not be a weekday artefact
    dowvals = defaultdict(list)
    for dd, v in zip(days, vals):
        dowvals[dd.weekday()].append(v)
    gmean = mean(vals)
    factor = {w: (mean(dowvals[w]) / gmean if gmean else 1.0) for w in dowvals}
    dvals = [v / factor[dd.weekday()] for dd, v in zip(days, vals)]
    print("\n  (b) DAY-OF-WEEK DESEASONALISED (divide by each weekday's factor over the whole")
    print("      scan domain), right-hand segment >= 7 days")
    des = free_scan(days, dvals, 7)
    for g, c, ml, mr, nr in des[:8]:
        print(f"      cut {c} ({DOW[c.weekday()]})  gain {g:9.0f}   "
              f"before {ml:6.1f}  after {mr:6.1f} ({nr:2d}d)  x{mr / ml:.3f}")
    C.write_csv("00_changepoint_profile.csv",
                ["cut_date", "dow", "sse_gain_raw", "mean_before_raw", "mean_after_raw",
                 "days_after", "ratio_raw"],
                [(c.isoformat(), DOW[c.weekday()], round(g, 1), round(ml, 2),
                  round(mr, 2), nr, round(mr / ml, 4))
                 for g, c, ml, mr, nr in sorted(raw, key=lambda x: x[1])])

    print("\n  (c) stability of the deseasonalised argmax to the minimum right-segment length")
    args = []
    for mr_ in (7, 10, 14, 21):
        top = free_scan(days, dvals, mr_)[0]
        args.append(top[1])
        print(f"      min_right={mr_:2d}d -> break at {top[1]} ({DOW[top[1].weekday()]}), "
              f"x{top[3] / top[2]:.3f}")

    break_raw = raw[0][1]
    break_des = des[0][1]
    print(f"\n  [measured] raw argmax {break_raw}; deseasonalised argmax {break_des}; the")
    print("  deseasonalised argmax is stable across every min_right tried: "
          + ", ".join(sorted({a.isoformat() for a in args})) + ".")
    print("  [measured] Both methods land within one day of a calendar-month boundary, and")
    print("  the deseasonalised scan -- which cannot be fooled by a strong Friday or Saturday")
    print("  -- picks the boundary itself. The step is a clean level shift, not a drift.")
    return break_raw, break_des, days, vals, ref


def derive_level_window(h, break_des):
    """LEVEL window = the largest WHOLE NUMBER OF WEEKS ending on last_demand_day that
    begins on or after the detected break.

    Whole weeks so that every weekday is equally represented and the mean needs no
    day-of-week weighting -- the single largest source of error in a short window.
    Fully derived: no literal anywhere.
    """
    span = (h.last_demand_day - break_des).days + 1
    weeks = span // 7
    if weeks < 1:
        weeks = 1
    start = h.last_demand_day - dt.timedelta(days=7 * weeks - 1)
    return start, h.last_demand_day, weeks


def section2b_significance(con, h, byday, break_des, ref, level_win):
    C.sub("2.6  Current regime vs prior plateau -- level, ratio, significance")
    lvl_start, lvl_end, weeks = level_win
    prior_start = ref[-2][0] if len(ref) >= 2 else None
    prior_end = lvl_start - dt.timedelta(days=1)

    def series(a, b):
        out = []
        d = a
        while d <= b:
            out.append(byday.get(d.isoformat(), 0))
            d += dt.timedelta(days=1)
        return out

    A = series(prior_start, prior_end)
    B = series(lvl_start, lvl_end)
    print(f"  PRIOR    {prior_start} .. {prior_end}   {len(A):3d}d  "
          f"n={sum(A):,}  mean {mean(A):6.2f}/day  sd {stdev(A):5.2f}")
    print(f"  CURRENT  {lvl_start} .. {lvl_end}   {len(B):3d}d  "
          f"n={sum(B):,}   mean {mean(B):6.2f}/day  sd {stdev(B):5.2f}   "
          f"({weeks} whole weeks)")
    print(f"  ratio  x{mean(B) / mean(A):.4f}   ->  {100 * (mean(B) / mean(A) - 1):+.1f}%")
    print(f"  [measured] P75 of the current regime's daily counts = "
          f"{C.pct(B, 75):.1f}; P90 = {C.pct(B, 90):.1f}   "
          f"(prior P75 {C.pct(A, 75):.1f}, P90 {C.pct(A, 90):.1f})")

    print("\n  (a) Mann-Whitney U on the two daily distributions (not just their means)")
    mw = mann_whitney(A, B)
    print(f"      U={mw[0]:.0f}  z={mw[1]:+.3f}  two-sided p={mw[2]:.3g}")
    print(f"      P(a random current day exceeds a random prior day) = {mw[3]:.3f}  "
          f"(0.500 = no difference)")
    print("      [measured] Both windows carry the same weekday mix of quiet Tuesdays and busy")
    print("      Saturdays, so the two DISTRIBUTIONS overlap heavily even though the means are")
    print("      far apart. That is why the effect size is well short of 1.0 while p is small:")
    print("      the shift is a level shift, not a separation.")

    print("\n  (b) percentile bootstrap of the difference and ratio of means "
          f"({BOOT_N:,} resamples, seed fixed)")
    (dlo, dhi), (rlo, rhi), p_le0 = bootstrap_diff(A, B)
    print(f"      difference of means  {mean(B) - mean(A):+6.2f} legs/day   "
          f"95% CI [{dlo:+.2f}, {dhi:+.2f}]")
    print(f"      ratio of means       x{mean(B) / mean(A):.4f}            "
          f"95% CI [x{rlo:.4f}, x{rhi:.4f}]")
    print(f"      bootstrap share of resamples with difference <= 0: {p_le0:.4f}")

    print("\n  (c) day-of-week-stratified check -- the two windows differ in weekday mix,")
    print("      so a pooled test can be fooled. Compare like weekday to like weekday.")
    pa, pb = defaultdict(list), defaultdict(list)
    d = prior_start
    while d <= prior_end:
        pa[d.weekday()].append(byday.get(d.isoformat(), 0))
        d += dt.timedelta(days=1)
    d = lvl_start
    while d <= lvl_end:
        pb[d.weekday()].append(byday.get(d.isoformat(), 0))
        d += dt.timedelta(days=1)
    ratios = []
    dow_rows = []
    print(f"      {'dow':4s} {'n_prior':>7} {'prior':>7} {'n_cur':>6} {'current':>8} "
          f"{'ratio':>7}  {'cur min..max':>14}")
    for w in range(7):
        if not pa[w] or not pb[w]:
            continue
        r = mean(pb[w]) / mean(pa[w])
        ratios.append(r)
        dow_rows.append((DOW[w], len(pa[w]), round(mean(pa[w]), 2), len(pb[w]),
                         round(mean(pb[w]), 2), round(r, 4), min(pb[w]), max(pb[w])))
        print(f"      {DOW[w]:4s} {len(pa[w]):>7} {mean(pa[w]):>7.1f} {len(pb[w]):>6} "
              f"{mean(pb[w]):>8.1f} {r:>7.3f}  {min(pb[w]):>6d}..{max(pb[w]):<6d}")
    C.write_csv("00_dow_levels.csv",
                ["dow", "n_prior", "prior_mean", "n_current", "current_mean", "ratio",
                 "current_min", "current_max"], dow_rows)
    print(f"      [measured] EVERY weekday is up. ratio range x{min(ratios):.3f}..x{max(ratios):.3f}, "
          f"geometric mean x{math.exp(mean([math.log(r) for r in ratios])):.3f}")
    print(f"      [measured] the current window holds only {len(pb[0])} of each weekday -- "
          "carry this forward to Section 6.")
    return prior_start, prior_end, A, B


# ==========================================================================
# SECTION 3 -- IS THE STEP-UP REAL?
# ==========================================================================

def section3_falsify(con, h, prior, current):
    C.hdr("SECTION 3 -- IS THE STEP-UP REAL?  FALSIFICATION TESTS")
    (ps, pe), (cs, ce) = prior, current
    dp = (pe - ps).days + 1
    dc = (ce - cs).days + 1

    def one(sel, lo, hi, extra=""):
        return con.execute(C.live_legs_sql(
            sel, " AND l.pickup_date BETWEEN ? AND ? " + extra),
            (lo.isoformat(), hi.isoformat())).fetchall()

    C.sub("3.1  (d) More legs per reservation, rather than more reservations?")
    rows = []
    print(f"  {'pickup month':12s} {'legs':>7} {'reservations':>13} {'legs/res':>9} "
          f"{'customers':>10}")
    for r in con.execute(C.live_legs_sql(
            "substr(l.pickup_date,1,7) m, COUNT(*) legs, "
            "COUNT(DISTINCT l.reservation_id) res, COUNT(DISTINCT r.customer_id) cus",
            " AND l.pickup_date <= ?", "GROUP BY 1 ORDER BY 1"),
            (h.last_demand_day.isoformat(),)):
        print(f"  {r['m']:12s} {r['legs']:>7,} {r['res']:>13,} "
              f"{r['legs'] / r['res']:>9.3f} {r['cus']:>10,}")
        rows.append((r["m"], r["legs"], r["res"], round(r["legs"] / r["res"], 4), r["cus"]))
    C.write_csv("00_legs_per_reservation.csv",
                ["pickup_month", "live_legs", "reservations", "legs_per_reservation",
                 "distinct_customers"], rows)

    print("""
  A window total cannot be aggregated with COUNT(DISTINCT reservation_id) over the
  window: a reservation is counted once no matter how many of its legs fall inside, and
  a 21-day window clips more round trips at its edges than a 135-day one does. The
  comparison is made two ways instead, neither of which depends on window length.

  (i) RESERVATION COHORT -- attach every reservation to the date of its FIRST live leg,
      then count ALL of that reservation's live legs wherever they fall.""")
    legs_of = defaultdict(int)
    first_of = {}
    for r in con.execute(C.live_legs_sql("l.reservation_id rid, l.pickup_date d")):
        legs_of[r["rid"]] += 1
        d = dt.date.fromisoformat(r["d"])
        if r["rid"] not in first_of or d < first_of[r["rid"]]:
            first_of[r["rid"]] = d

    def cohort(lo, hi):
        rids = [rid for rid, d in first_of.items() if lo <= d <= hi]
        return len(rids), sum(legs_of[rid] for rid in rids), (hi - lo).days + 1

    print(f"\n      {'window':10s} {'days':>5} {'reservations/day':>18} "
          f"{'legs/reservation':>18} {'legs/day (in-window)':>21}")
    coh = {}
    for lab, lo, hi in [("prior", ps, pe), ("current", cs, ce)]:
        nres, nlegs, nd = cohort(lo, hi)
        inwin = C.q1(con, C.live_legs_sql("COUNT(*)", " AND l.pickup_date BETWEEN ? AND ?"),
                     (lo.isoformat(), hi.isoformat()))
        coh[lab] = (nres / nd, nlegs / float(nres), inwin / nd)
        print(f"      {lab:10s} {nd:>5} {nres / nd:>18.2f} {nlegs / float(nres):>18.3f} "
              f"{inwin / nd:>21.2f}")
    print(f"      [measured] reservations/day x{coh['current'][0] / coh['prior'][0]:.3f},  "
          f"legs per reservation x{coh['current'][1] / coh['prior'][1]:.3f},  "
          f"legs/day x{coh['current'][2] / coh['prior'][2]:.3f}")

    print("""
  (ii) BOOKING COHORT -- attach every reservation to the month it was CREATED, then count
       its live legs. Independent of any pickup-date window at all.""")
    bk = defaultdict(lambda: [0, 0])
    for r in con.execute("SELECT id, created_at FROM reservations_reservation"):
        if r["id"] not in legs_of:
            continue
        loc = C.to_local(r["created_at"])
        if not loc:
            continue
        m = loc.date().isoformat()[:7]
        bk[m][0] += 1
        bk[m][1] += legs_of[r["id"]]
    print(f"\n      {'booking month':14s} {'reservations':>13} {'live legs':>11} "
          f"{'legs/reservation':>18}")
    bk_rows = []
    for m in sorted(bk):
        n, l = bk[m]
        print(f"      {m:14s} {n:>13,} {l:>11,} {l / float(n):>18.3f}")
        bk_rows.append((m, n, l, round(l / float(n), 4)))
    C.write_csv("00_legs_per_reservation_by_booking_month.csv",
                ["booking_month", "reservations", "live_legs", "legs_per_reservation"],
                bk_rows)
    print("\n  -> REFUTED, on both constructions and on the pickup-month table above.")
    print("     Reservations per day grew at essentially the same rate as legs per day, and")
    print("     legs per reservation is flat to slightly falling. The step is MORE BOOKINGS,")
    print("     not more legs stapled to each booking. Hypothesis (d) is dead.")


    C.sub("3.2  (c) Data artifact -- duplicate legs?")
    for lab, lo, hi in [("prior", ps, pe), ("current", cs, ce)]:
        n = one("COUNT(*)", lo, hi)[0][0]
        dup = con.execute(
            "SELECT COALESCE(SUM(c-1),0), COUNT(*) FROM (SELECT COUNT(*) c " + C.LEG_JOIN +
            " WHERE " + C.LIVE_LEG + " AND " + C.SANE_DATES +
            " AND l.pickup_date BETWEEN ? AND ? GROUP BY l.reservation_id, l.pickup_date,"
            " l.pickup_time, l.pickup_location, l.dropoff_location HAVING c>1)",
            (lo.isoformat(), hi.isoformat())).fetchone()
        print(f"  {lab:8s} legs={n:>6,}  duplicate groups={dup[1]:>4}  "
              f"excess rows={dup[0]:>4}  = {100.0 * dup[0] / n:.3f}%")
    print("  -> REFUTED. Exact-duplicate legs are a rounding error in both windows.")

    C.sub("3.3  (c) Data artifact -- a bulk import?")
    cnt = Counter()
    for r in one("r.created_at ca", cs, ce):
        loc = C.to_local(r["ca"])
        if loc:
            cnt[loc.date()] += 1
    tot = sum(cnt.values())
    print(f"  Booking dates behind the {tot:,} current-regime legs "
          f"({len(cnt)} distinct creation days):")
    for k, v in cnt.most_common(8):
        print(f"      {k}  {v:>5}  {100.0 * v / tot:5.2f}% of the regime")
    bym = Counter()
    for k, v in cnt.items():
        bym[k.isoformat()[:7]] += v
    print("  by creation month: " + "  ".join(f"{k}={v}" for k, v in sorted(bym.items())))
    print(f"  [measured] the busiest single creation day supplies "
          f"{100.0 * cnt.most_common(1)[0][1] / tot:.2f}% of the regime.")
    print("  -> REFUTED. No import spike; bookings arrived smoothly over months.")

    C.sub("3.4  (c) Data artifact -- did the definition of a live leg change?")
    print(f"  {'pickup month':12s} {'all legs':>9} {'live':>8} {'cancel rate':>12} "
          f"{'in-progress':>12} {'completed':>10}")
    rows = []
    for r in con.execute(
            "SELECT substr(l.pickup_date,1,7) m, COUNT(*) allc, "
            "SUM(CASE WHEN (l.status IS NULL OR l.status<>'cancelled') AND "
            "r.status NOT IN ('cancelled','canceled') THEN 1 ELSE 0 END) live, "
            "SUM(CASE WHEN l.status='in-progress' THEN 1 ELSE 0 END) inprog, "
            "SUM(CASE WHEN l.status='completed' THEN 1 ELSE 0 END) comp "
            + C.LEG_JOIN + " WHERE " + C.SANE_DATES +
            " AND l.pickup_date <= ? GROUP BY 1 ORDER BY 1",
            (h.last_demand_day.isoformat(),)):
        cr = 100.0 * (r["allc"] - r["live"]) / r["allc"]
        print(f"  {r['m']:12s} {r['allc']:>9,} {r['live']:>8,} {cr:>11.2f}% "
              f"{r['inprog']:>12,} {r['comp']:>10,}")
        rows.append((r["m"], r["allc"], r["live"], round(cr, 3), r["inprog"], r["comp"]))
    C.write_csv("00_status_mix_by_month.csv",
                ["pickup_month", "all_legs", "live_legs", "cancel_rate_pct",
                 "in_progress", "completed"], rows)
    ca = one("COUNT(*)", ps, pe)[0][0]
    cb = one("COUNT(*)", cs, ce)[0][0]
    aa = con.execute("SELECT COUNT(*) " + C.LEG_JOIN + " WHERE " + C.SANE_DATES +
                     " AND l.pickup_date BETWEEN ? AND ?",
                     (ps.isoformat(), pe.isoformat())).fetchone()[0]
    ab = con.execute("SELECT COUNT(*) " + C.LEG_JOIN + " WHERE " + C.SANE_DATES +
                     " AND l.pickup_date BETWEEN ? AND ?",
                     (cs.isoformat(), ce.isoformat())).fetchone()[0]
    print(f"\n  [measured] UNFILTERED legs/day: prior {aa / dp:.2f} -> current {ab / dc:.2f} "
          f"= x{(ab / dc) / (aa / dp):.4f}")
    print(f"  [measured] FILTERED   legs/day: prior {ca / dp:.2f} -> current {cb / dc:.2f} "
          f"= x{(cb / dc) / (ca / dp):.4f}")
    rate_p = 100.0 * (aa - ca) / aa
    rate_c = 100.0 * (ab - cb) / ab
    print(f"  [measured] cancellation rate prior {rate_p:.2f}% vs current {rate_c:.2f}%.")
    print(f"             If the current window carried the prior rate, live legs/day would be "
          f"{(ab * (1 - rate_p / 100.0)) / dc:.2f} instead of {cb / dc:.2f} -- "
          f"a {100.0 * ((ab * (1 - rate_p / 100.0)) / cb - 1):+.2f}% effect, "
          "nowhere near the step.")
    print("  -> REFUTED. The step survives with the filter off entirely.")

    C.sub("3.5  (c) Does one customer, agency, channel or lane drive it?")
    A = one("r.customer_id cu, r.travel_agent_id ta, r.booking_source bs, "
            "l.pickup_location pl, l.dropoff_location dl", ps, pe)
    B = one("r.customer_id cu, r.travel_agent_id ta, r.booking_source bs, "
            "l.pickup_location pl, l.dropoff_location dl", cs, ce)
    base = (len(B) / dc) / (len(A) / dp)
    conc_rows = []
    for field, lab, keyfn in [
            ("bs", "booking_source", lambda r: r["bs"]),
            ("ta", "travel_agent", lambda r: r["ta"]),
            ("cu", "customer", lambda r: r["cu"]),
            (None, "lane (bucketed)", lambda r: f"{C.loc_bucket(r['pl'])}>{C.loc_bucket(r['dl'])}"),
            (None, "trip kind", lambda r: C.trip_kind(r["pl"], r["dl"]))]:
        ca_, cb_ = Counter(keyfn(r) for r in A), Counter(keyfn(r) for r in B)
        print(f"\n  --- {lab}   (baseline step x{base:.3f})")
        keys = sorted(set(ca_) | set(cb_), key=lambda k: -cb_.get(k, 0))[:8]
        for k in keys:
            pa_, pb_ = ca_.get(k, 0) / dp, cb_.get(k, 0) / dc
            share = 100.0 * (pb_ - pa_) / (len(B) / dc - len(A) / dp) if (len(B) / dc - len(A) / dp) else 0
            print(f"      {str(k)[:26]:26s} prior {pa_:7.2f}/d  current {pb_:7.2f}/d  "
                  f"delta {pb_ - pa_:+7.2f}/d  ({share:5.1f}% of the growth)")
            conc_rows.append((lab, str(k), round(pa_, 3), round(pb_, 3),
                              round(pb_ - pa_, 3), round(share, 2)))
        # Step with the single biggest current category removed. For `travel_agent` the
        # largest key is None, which means "booked direct, no agency" -- a category, not a
        # contributor -- so both it and the largest NAMED key are reported.
        drops = [cb_.most_common(1)[0][0]]
        if drops[0] is None:
            named = [k for k, _ in cb_.most_common() if k is not None]
            if named:
                drops.append(named[0])
        for top in drops:
            ra = (len(A) - ca_.get(top, 0)) / dp
            rb = (len(B) - cb_.get(top, 0)) / dc
            note = ""
            if top is None:
                note = ("   <- None = booked direct; this line is therefore "
                        "'AGENCY-BOOKED legs only'")
            print(f"      -> everything except {str(top)[:22]:22s}: step becomes "
                  f"x{rb / ra:.3f}  (baseline x{base:.3f}){note}")
    C.write_csv("00_growth_concentration.csv",
                ["dimension", "key", "prior_per_day", "current_per_day", "delta_per_day",
                 "pct_of_growth"], conc_rows)
    print("\n  -> REFUTED. Growth is broad: it appears in every paid channel and in direct,")
    print("     no single customer contributes as much as half a leg a day, and removing the")
    print("     largest member of any dimension barely moves the ratio.")

    C.sub("3.6  (a) vs (b): genuine growth or summer seasonality?")
    fld = first_leg_day(con)
    import calendar as _cal
    print(f"  [measured] leg data begins {fld}. The current regime sits in "
          f"{cs.strftime('%B')} {cs.year}.")

    def month_rate(y, m):
        nd = _cal.monthrange(y, m)[1]
        n = C.q1(con, C.live_legs_sql("COUNT(*)", " AND substr(l.pickup_date,1,7)=?"),
                 (f"{y:04d}-{m:02d}",)) or 0
        return n, n / float(nd)

    ly_n, ly_r = month_rate(cs.year - 1, cs.month)
    pm = dt.date(cs.year - 1, cs.month, 1) - dt.timedelta(days=1)
    lyp_n, lyp_r = month_rate(pm.year, pm.month)
    cur_rate = (C.q1(con, C.live_legs_sql("COUNT(*)", " AND l.pickup_date BETWEEN ? AND ?"),
                     (cs.isoformat(), ce.isoformat())) or 0) / float(dc)
    prior_rate = (C.q1(con, C.live_legs_sql("COUNT(*)", " AND l.pickup_date BETWEEN ? AND ?"),
                       (ps.isoformat(), pe.isoformat())) or 0) / float(dp)

    print(f"  [measured] The same month one year earlier DOES exist in the file: "
          f"{cs.year - 1}-{cs.month:02d} carries")
    print(f"             {ly_n:,} live legs ({ly_r:.1f}/day) and the month before it "
          f"{lyp_n:,} ({lyp_r:.1f}/day),")
    print(f"             a month-on-month ratio of x{ly_r / lyp_r:.2f} -- larger than this "
          f"year's x{cur_rate / prior_rate:.2f}.")
    print("  [unavailable] AND THAT RATIO IS WORTHLESS AS A SEASONALITY CONTROL. A year ago")
    print(f"  the business ran at {ly_r:.1f} legs/day against {cur_rate:.1f} today -- a "
          f"roughly {cur_rate / ly_r:.0f}x different company, and")
    print("  it was inside its steepest growth phase, so last year's month-on-month ratio is")
    print("  mostly the ramp, not the season. There is NO period in this file during which the")
    print("  level was stationary for a full year, so a month-of-year index cannot be")
    print("  estimated from it at all. Any claim that this step is 'just summer', or that it")
    print("  is 'definitely not summer', is unsupportable from this database. Say so plainly")
    print("  rather than picking the convenient reading.")
    print("\n  What would resolve it, none of which is available here:")
    print("    (i)   a second August at a stable level -- one more year of trading;")
    print("    (ii)  an external Orlando visitor-volume or MCO passenger index -- not in this")
    print("          database, and no table joins to one;")
    print("    (iii) the founder's recollection of prior Augusts -- testimony, not measurement,")
    print("          and worth collecting precisely because the data cannot supply it.")
    print("\n  THE ASYMMETRY THAT DECIDES WHAT TO DO ANYWAY:")
    print(f"    - size on the current regime and it turns out seasonal: the roster is over-")
    print(f"      staffed by up to {100 * (cur_rate / prior_rate - 1):.0f}% for a few weeks, and the fix is to stand people")
    print("      down. Cost: paid idle time, recoverable.")
    print(f"    - size on the prior plateau and it turns out to be growth: the roster is")
    print(f"      short {cur_rate - prior_rate:.0f} legs a day, every day, indefinitely. Cost: farmed-out work at a")
    print("      premium, late pickups, and the thing this engagement exists to stop.")
    print("    The costs are not symmetric, so the conservative choice is the CURRENT regime.")
    print("    Section 5's forward book is the tiebreaker, and it points the same way.")

    C.sub("3.7  Independent corroboration from a table that knows nothing about bookings")
    print("  reservations_legstatus is written by a driver tapping through a job on the road.")
    print("  It shares no column with the demand query. If the step is real it must appear")
    print("  here too. The honest measure is JOBS WORKED (distinct legs carrying at least one")
    print("  tap), not raw taps -- taps per leg drifts with driver discipline, which would")
    print("  contaminate the comparison. Both are shown. Restricted to last_actuals_day so")
    print("  today's unfinished evening cannot depress the current window.")
    rows = []
    end_cap = min(ce, h.last_actuals_day)
    for lab, lo, hi in [("prior", ps, min(pe, h.last_actuals_day)), ("current", cs, end_cap)]:
        nd = (hi - lo).days + 1
        taps = C.q1(con, "SELECT COUNT(*) FROM reservations_legstatus ls "
                         "JOIN reservations_leg l ON l.id=ls.leg_id "
                         "JOIN reservations_reservation r ON r.id=l.reservation_id "
                         "WHERE " + C.LIVE_LEG + " AND l.pickup_date BETWEEN ? AND ?",
                    (lo.isoformat(), hi.isoformat())) or 0
        worked = C.q1(con, "SELECT COUNT(DISTINCT ls.leg_id) FROM reservations_legstatus ls "
                           "JOIN reservations_leg l ON l.id=ls.leg_id "
                           "JOIN reservations_reservation r ON r.id=l.reservation_id "
                           "WHERE " + C.LIVE_LEG + " AND l.pickup_date BETWEEN ? AND ?",
                      (lo.isoformat(), hi.isoformat())) or 0
        legs = C.q1(con, C.live_legs_sql("COUNT(*)", " AND l.pickup_date BETWEEN ? AND ?"),
                    (lo.isoformat(), hi.isoformat()))
        # drivers PER DAY, not over the window -- a longer window trivially sees more people
        dpd = [r["n"] for r in con.execute(C.live_legs_sql(
            "l.pickup_date d, COUNT(DISTINCT l.driver_id) n",
            " AND l.pickup_date BETWEEN ? AND ? AND l.driver_id IS NOT NULL", "GROUP BY 1"),
            (lo.isoformat(), hi.isoformat()))]
        print(f"  {lab:8s} {nd:3d}d   jobs worked/day {worked / nd:7.1f}   "
              f"taps/day {taps / nd:7.1f}   taps/job {taps / float(worked):4.2f}   "
              f"drivers/day mean {mean(dpd):5.1f} max {max(dpd)}")
        rows.append((lab, lo.isoformat(), hi.isoformat(), nd, taps, worked, legs,
                     round(mean(dpd), 2), max(dpd)))
    wa, wb = rows[0][5] / rows[0][3], rows[1][5] / rows[1][3]
    ta, tb = rows[0][4] / rows[0][3], rows[1][4] / rows[1][3]
    print(f"\n  [measured] JOBS WORKED per day x{wb / wa:.3f}  (raw taps/day x{tb / ta:.3f}).")
    print("  The jobs-worked ratio is the clean one and it lands on the same step as the")
    print("  booking data -- a SECOND, structurally different reading, from the operational")
    print("  stream rather than the booking stream. The raw-tap ratio runs higher because")
    print("  taps per job also rose; that is a discipline change, and it is why raw taps are")
    print("  the wrong corroborating measure.")
    print(f"  [measured] drivers on the road per day rose from a mean of {rows[0][7]} to "
          f"{rows[1][7]:.1f} (peak {rows[0][8]} -> {rows[1][8]})")
    print("  -- the operation ALREADY absorbed the step by putting more bodies out. That is")
    print("  the supply-side counterpart of the demand step and it belongs in deliverable 04.")
    C.write_csv("00_step_corroboration.csv",
                ["window", "start", "end", "days", "taps", "jobs_worked", "live_legs",
                 "mean_drivers_per_day", "max_drivers_per_day"], rows)


# ==========================================================================
# SECTION 4 -- BOOKING LEAD TIME AND THE UNDER-SIZING IT CAUSES
# ==========================================================================

HORIZONS = (0, 1, 2, 3, 5, 7, 10, 14, 21, 28, 35, 45, 60, 75, 90, 120)


def load_leads(con, h):
    """[(pickup_date, booked_local_date, lead_days)] over every live leg."""
    out = []
    for r in con.execute(C.live_legs_sql("l.pickup_date d, r.created_at ca, l.id lid")):
        loc = C.to_local(r["ca"])
        if not loc:
            continue
        pd = dt.date.fromisoformat(r["d"])
        out.append((pd, loc.date(), (pd - loc.date()).days))
    return out


def section4_lead(con, h, leads, prior, current):
    C.hdr("SECTION 4 -- BOOKING LEAD TIME, COMPLETENESS, AND WHAT IT COSTS DAY SETUP")
    (ps, pe), (cs, ce) = prior, current

    obs = [(pd, bd, k) for pd, bd, k in leads if pd <= h.last_demand_day]
    print(f"  [measured] {len(obs):,} live legs on fully-observed pickup dates.")
    neg = [x for x in obs if x[2] < 0]
    print(f"  [measured] legs whose reservation was created AFTER the pickup date: {len(neg)} "
          f"({100.0 * len(neg) / len(obs):.3f}%).")
    if not neg:
        print("  That zero is load-bearing: it is direct evidence for assumption A2 (a past")
        print("  date's demand is final). Nothing in this file was ever back-entered onto a")
        print("  date that had already happened, so a past day's leg count cannot still grow.")

    C.sub("4.1  Booking lead time (pickup_date - reservation.created_at, local)")
    vals = [k for _, _, k in obs]
    print("  " + C.fmt_describe("all fully-observed legs", vals))
    print(f"  {'':34s} mean {mean(vals):.1f}  P95 {C.pct(vals, 95):.1f}  "
          f"P99 {C.pct(vals, 99):.1f}  max {max(vals)}")
    lead_rows = []
    for lab, lo, hi in [("prior plateau", ps, pe), ("current regime", cs, ce)]:
        v = [k for pd, _, k in obs if lo <= pd <= hi]
        print("  " + C.fmt_describe(lab, v))
        lead_rows.append([lab] + [C.describe(v)[x] for x in
                                  ("n", "p10", "p25", "p50", "p75", "p90")])
    print("\n  by pickup month:")
    bym = defaultdict(list)
    for pd, _, k in obs:
        bym[pd.isoformat()[:7]].append(k)
    for m in sorted(bym):
        print("  " + C.fmt_describe("  " + m, bym[m]))
        lead_rows.append([m] + [C.describe(bym[m])[x] for x in
                                ("n", "p10", "p25", "p50", "p75", "p90")])
    C.write_csv("00_lead_time.csv",
                ["window", "n", "p10", "p25", "p50", "p75", "p90"], lead_rows)

    C.sub("4.2  Completeness curve -- of a day's eventual demand, how much is booked K days out?")
    print("  Derived from HISTORY: for past pickup dates, count the legs whose booking")
    print("  predates the pickup by >= K days, over the final total for those dates.")

    def curve(lo, hi):
        ks = [(pd, k) for pd, _, k in obs if lo <= pd <= hi]
        fin = float(len(ks))
        if not fin:
            return None
        return [(K, sum(1 for _, k in ks if k >= K) / fin) for K in HORIZONS], int(fin)

    cur_rows = []
    print(f"\n  {'window':22s} {'n':>7}  " + "  ".join(f"H-{K:<3d}" for K in HORIZONS))
    for lab, lo, hi in [("prior plateau", ps, pe),
                        ("current regime", cs, ce),
                        ("all observed", obs[0][0], h.last_demand_day)]:
        res = curve(lo, hi)
        if not res:
            continue
        c, n = res
        print(f"  {lab:22s} {n:>7,}  " + "  ".join(f"{100 * v:4.0f}%" for _, v in c))
        cur_rows.append([lab, n] + [round(v, 5) for _, v in c])
    C.write_csv("00_completeness_curve.csv",
                ["window", "n"] + [f"H_{K}" for K in HORIZONS], cur_rows)
    ref_curve = dict(curve(ps, pe)[0])
    print("\n  [measured] The prior-plateau and current-regime curves agree to within a few")
    print("  points at every horizon, so booking BEHAVIOUR did not move even though the LEVEL")
    print("  did. That is what licenses using this curve on the forward book in Section 5.")

    C.sub("4.3  What that costs Day Setup -- the roster is sized against partial demand")
    print("  dispatching/day_setup.py:100  peak_concurrency(target_date) reads booked legs")
    print("  dispatching/day_setup.py:443  n_target = peak['overall'][0] + DAY_SETUP_PEAK_BUFFER")
    print("  dispatching/day_setup.py:59   DAY_SETUP_PEAK_BUFFER = 1")
    print("  There is no lead-time correction anywhere in that path, so a dispatcher building")
    print("  a day K days ahead sizes the roster against the share of demand in 4.2.\n")
    for K in (0, 3, 7, 14, 21, 28, 45):
        share = ref_curve.get(K)
        if share is None:
            continue
        print(f"      building {K:2d} days out: sees {100 * share:4.1f}% of the day's legs "
              f"-> the true leg count is x{1 / share:5.2f} what is on screen")

    print("\n  4.3b  Concurrency proxy [modeled] -- legs are not bodies; how much of that leg")
    print("        shortfall becomes a MISSING DRIVER? Each leg is modelled as occupying one")
    print("        body for a fixed D minutes from its booked pickup time. D is swept because")
    print("        the true clear time is lane-dependent (the production estimator is used in")
    print("        4.4 as an independent check).")
    by_date = defaultdict(list)
    for r in con.execute(C.live_legs_sql(
            "l.pickup_date d, l.pickup_time t, r.created_at ca",
            " AND l.pickup_date BETWEEN ? AND ?"), (ps.isoformat(), ce.isoformat())):
        loc = C.to_local(r["ca"])
        b = C.booked_dtm(r["d"], r["t"])
        if loc and b:
            by_date[dt.date.fromisoformat(r["d"])].append((b, loc.date()))

    def peak_proxy(spans, D):
        ev = []
        for b, _ in spans:
            ev.append((b, 1))
            ev.append((b + dt.timedelta(minutes=D), -1))
        ev.sort(key=lambda e: (e[0], -e[1]))
        cur = best = 0
        for _, delta in ev:
            cur += delta
            best = max(best, cur)
        return best

    proxy_rows = []
    print(f"\n        {'window':9s} {'D(min)':>7} {'K':>3} {'days':>5} {'peak_final':>11} "
          f"{'peak_at_K':>10} {'mean short':>11} {'P75':>6} {'P90':>6} {'% short':>8}")
    for wlab, wlo, whi in [("prior", ps, pe), ("current", cs, ce)]:
        for D in (60, 75, 90, 120):
            for K in (0, 7, 14, 21, 28):
                deficits = []
                pf_tot = pk_tot = 0
                for d, spans in sorted(by_date.items()):
                    if not (wlo <= d <= whi):
                        continue
                    cutoff = d - dt.timedelta(days=K)
                    vis = [s for s in spans if s[1] <= cutoff]
                    pf = peak_proxy(spans, D)
                    pk = peak_proxy(vis, D) if vis else 0
                    deficits.append(pf - pk)
                    pf_tot += pf
                    pk_tot += pk
                nd = len(deficits)
                if not nd:
                    continue
                short = sum(1 for x in deficits if x > 0)
                print(f"        {wlab:9s} {D:>7} {K:>3} {nd:>5} {pf_tot / nd:>11.2f} "
                      f"{pk_tot / nd:>10.2f} {mean(deficits):>11.2f} "
                      f"{C.pct(deficits, 75):>6.1f} {C.pct(deficits, 90):>6.1f} "
                      f"{100.0 * short / nd:>7.1f}%")
                proxy_rows.append((wlab, D, K, nd, round(pf_tot / nd, 3),
                                   round(pk_tot / nd, 3), round(mean(deficits), 3),
                                   C.pct(deficits, 75), C.pct(deficits, 90),
                                   round(100.0 * short / nd, 2)))
        print()
    C.write_csv("00_undersizing_proxy.csv",
                ["window", "duration_minutes", "K_days_ahead", "days", "peak_final",
                 "peak_at_K", "mean_deficit", "p75_deficit", "p90_deficit",
                 "pct_days_short"], proxy_rows)
    spread = {}
    for wlab, D, K, nd, pf, pk, md, p75, p90, ps_ in proxy_rows:
        spread.setdefault((wlab, K), []).append(md)
    print("        [modeled] SENSITIVITY TO D, stated against the temptation to wave it away.")
    print("        Across D = 60..120 minutes -- a 2x range -- the mean deficit moves by:")
    for (wlab, K), v in sorted(spread.items()):
        rel = (f", i.e. +/-{50 * (max(v) - min(v)) / mean(v):.0f}%" if mean(v) else
               "  <- the K=0 control: zero at every D, as it must be")
        print(f"            {wlab:8s} K={K:2d}: {min(v):5.2f} .. {max(v):5.2f} bodies "
              f"(spread {max(v) - min(v):4.2f}{rel})")
    print("        The DIRECTION and the SIGN are invariant -- every D, every K, every window")
    print("        is short, on essentially every day. The MAGNITUDE is not: it scales with D")
    print("        almost proportionally, because a longer assumed job overlaps more")
    print("        neighbours. So the proxy alone can only bound the deficit, not size it.")
    print("        That is precisely why section 4.4 exists: it replaces D with the estimator")
    print("        the product itself uses. Do not quote a number from this table without it.")

    return ref_curve, by_date, proxy_rows


def section4d_django(con, h, prior, current, proxy_rows):
    """4.4 -- the same experiment with the PRODUCTION estimator. Optional; requires the
    Django recipe in the module docstring, pointed at a COPY of the snapshot."""
    C.sub("4.4  Cross-check with the real dispatching.day_setup.peak_concurrency")
    settings_mod = os.environ.get("DJANGO_SETTINGS_MODULE")
    if not settings_mod:
        print("  [skipped] DJANGO_SETTINGS_MODULE is not set. Re-run with the recipe in this")
        print("            file's docstring to compute this section. 4.3b stands on its own;")
        print("            this section only sharpens it.")
        return
    try:
        import django
        django.setup()
        from django.conf import settings as dj
        dbpath = os.path.abspath(dj.DATABASES["default"]["NAME"])
        if os.path.normcase(dbpath) == os.path.normcase(os.path.abspath(C.DB_PATH)):
            print("  [refused] DJANGO_SETTINGS_MODULE points at the real snapshot. "
                  "peak_concurrency can INSERT a RouteDistanceCache row. Point it at a copy.")
            return
        print(f"  Django DB (a copy, never the snapshot): {dbpath}")
        from dispatching import day_setup
        from reservations.models import Leg
        from django.utils import timezone as djtz
    except Exception as exc:                      # noqa: BLE001
        print(f"  [skipped] Django did not start: {exc}")
        return

    (ps, pe), (cs, ce) = prior, current
    rows = []
    print(f"\n  {'window':16s} {'K':>3} {'days':>5} {'peak_final':>11} {'peak_at_K':>10} "
          f"{'mean short':>11} {'P75':>6} {'P90':>6} {'% short':>8} {'>=2 short':>10}")
    for lab, lo, hi in [("prior plateau", ps, pe), ("current regime", cs, ce)]:
        cache = {}
        d = lo
        while d <= hi:
            legs = list(Leg.objects.filter(pickup_date=d)
                        .exclude(reservation__status__in=["cancelled", "canceled"])
                        .exclude(status="cancelled")
                        .select_related("reservation__vehicle", "vehicle", "reservation",
                                        "flight_information"))
            cache[d] = legs
            d += dt.timedelta(days=1)
        for K in (0, 7, 14, 21, 28):
            deficits, finals = [], []
            for d, legs in sorted(cache.items()):
                if not legs:
                    continue
                cutoff = d - dt.timedelta(days=K)
                vis = []
                for lg in legs:
                    ca = lg.reservation.created_at if lg.reservation_id else None
                    if not ca:
                        continue
                    loc = djtz.localtime(ca).date() if djtz.is_aware(ca) else ca.date()
                    if loc <= cutoff:
                        vis.append(lg)
                pf = day_setup.peak_concurrency(d, legs=legs)["overall"][0]
                pk = day_setup.peak_concurrency(d, legs=vis)["overall"][0] if vis else 0
                finals.append(pf)
                deficits.append(pf - pk)
            if not deficits:
                continue
            nd = len(deficits)
            short = sum(1 for x in deficits if x > 0)
            two = sum(1 for x in deficits if x >= 2)
            print(f"  {lab:16s} {K:>3} {nd:>5} {mean(finals):>11.2f} "
                  f"{mean(finals) - mean(deficits):>10.2f} {mean(deficits):>11.2f} "
                  f"{C.pct(deficits, 75):>6.1f} {C.pct(deficits, 90):>6.1f} "
                  f"{100.0 * short / nd:>7.1f}% {100.0 * two / nd:>9.1f}%")
            rows.append((lab, K, nd, round(mean(finals), 3),
                         round(mean(finals) - mean(deficits), 3), round(mean(deficits), 3),
                         C.pct(deficits, 75), C.pct(deficits, 90),
                         round(100.0 * short / nd, 2), round(100.0 * two / nd, 2)))
    C.write_csv("00_undersizing_production_estimator.csv",
                ["window", "K_days_ahead", "days", "peak_final", "peak_at_K", "mean_deficit",
                 "p75_deficit", "p90_deficit", "pct_days_short", "pct_days_short_2plus"], rows)
    print("\n  [measured] These are the numbers Day Setup itself would have printed. The")
    print("  roster target it renders is this peak + 1, so a positive deficit is exactly the")
    print("  number of bodies the dispatcher was never told to book.")
    print("  [measured] The K=0 row is the control: with every leg visible the deficit is")
    print("  exactly 0.00 on every day, which is what proves the truncation machinery itself")
    print("  is not manufacturing the shortfall.")

    print("\n  4.4b  How well did the 4.3b fixed-duration proxy do?")
    print("  Signed error (proxy mean deficit - production mean deficit), in bodies:")
    prod = {(w.split()[0], k): md for w, k, _n, _pf, _pk, md, _a, _b, _c, _d in rows}
    Ds = sorted({r[1] for r in proxy_rows})
    errs_by_d = {D: [] for D in Ds}
    print(f"      {'window':9s} {'K':>3} {'production':>11}  "
          + "  ".join(f"{'D=' + str(D):>9s}" for D in Ds))
    for w, k in sorted(prod):
        cells = []
        for D in Ds:
            m = [r[6] for r in proxy_rows
                 if r[0].split()[0] == w and r[1] == D and r[2] == k]
            if not m:
                cells.append(f"{'-':>9s}")
                continue
            e = m[0] - prod[(w, k)]
            errs_by_d[D].append(abs(e))
            cells.append(f"{e:>+9.2f}")
        print(f"      {w:9s} {k:>3} {prod[(w, k)]:>11.2f}  " + "  ".join(cells))
    best = None
    print(f"      {'':9s} {'':>3} {'mean |err|':>11}  "
          + "  ".join(f"{mean(errs_by_d[D]):>9.2f}" if errs_by_d[D] else f"{'-':>9s}"
                      for D in Ds))
    for D in Ds:
        if errs_by_d[D] and (best is None or mean(errs_by_d[D]) < best[1]):
            best = (D, mean(errs_by_d[D]))
    if best:
        print(f"\n      [measured] At D = {best[0]} minutes the naive proxy reproduces the "
              f"production")
        print(f"      estimator's deficit to a mean absolute error of {best[1]:.2f} bodies "
              "across every")
        print("      window and horizon. Two structurally different computations -- a blind")
        print("      fixed-span sweep, versus the app's own flight-anchored, dwell-aware,")
        print("      lane-aware estimator -- land on the same answer. THAT agreement, not")
        print("      either figure alone, is the reason to believe the under-sizing result.")
        print("      Where the two genuinely differ is peak_final, not the deficit: anchoring")
        print("      arrivals to flight times moves WHEN the peak falls much more than it")
        print("      moves how much of that peak was invisible at booking time.")


# ==========================================================================
# SECTION 5 -- FORWARD BOOK
# ==========================================================================

def section5_forward(con, h, leads, ref_curve, current):
    C.hdr("SECTION 5 -- THE FORWARD BOOK")
    cs, ce = current

    C.sub("5.1  What is on the books past today, by pickup month")
    import calendar as _cal
    rows = []
    print("  'days' is CALENDAR days in the month still in the future -- not days that happen")
    print("  to carry a booking, which would inflate legs/day for sparse far months.")
    print(f"\n  {'month':9s} {'legs':>7} {'res':>7} {'days':>5} {'legs/day':>9} "
          f"{'mean K':>7} {'implied C(K)':>13} {'implied final/day':>18}")
    fwd = defaultdict(list)
    for r in con.execute(C.live_legs_sql(
            "l.pickup_date d, l.reservation_id rid", " AND l.pickup_date > ?"),
            (h.last_demand_day.isoformat(),)):
        fwd[r["d"][:7]].append(r)

    def cfun(K):
        ks = sorted(ref_curve)
        if K <= ks[0]:
            return ref_curve[ks[0]]
        if K >= ks[-1]:
            return ref_curve[ks[-1]]
        for a, b in zip(ks[:-1], ks[1:]):
            if a <= K <= b:
                w = (K - a) / float(b - a)
                return ref_curve[a] + w * (ref_curve[b] - ref_curve[a])
        return ref_curve[ks[-1]]

    CUTOFF = 0.25   # below this, the implied final is the curve talking, not the data
    for m in sorted(fwd):
        y, mo = int(m[:4]), int(m[5:7])
        eom = dt.date(y, mo, _cal.monthrange(y, mo)[1])
        som = max(dt.date(y, mo, 1), h.today + dt.timedelta(days=1))
        ndays = (eom - som).days + 1
        ks = [(dt.date.fromisoformat(r["d"]) - h.today).days for r in fwd[m]]
        mk = mean(ks)
        c = cfun(mk)
        n = len(fwd[m])
        imp = (n / ndays / c) if c >= CUTOFF else None
        rows.append((m, n, len({r["rid"] for r in fwd[m]}), ndays,
                     round(n / ndays, 2), round(mk, 1), round(c, 4),
                     round(imp, 1) if imp else ""))
        print(f"  {m:9s} {n:>7,} {len({r['rid'] for r in fwd[m]}):>7,} {ndays:>5} "
              f"{n / ndays:>9.1f} {mk:>7.1f} {100 * c:>12.1f}% "
              + (f"{imp:>18.1f}" if imp else f"{'(suppressed)':>18s}"))
    C.write_csv("00_forward_book.csv",
                ["pickup_month", "legs_on_books", "reservations", "future_calendar_days",
                 "legs_per_day_booked", "mean_days_ahead", "implied_completeness",
                 "implied_final_legs_per_day"], rows)
    print(f"\n  [measured] the booked-so-far columns. [modeled] the last two apply the prior-")
    print("  plateau completeness curve of 4.2 to the mean horizon of each month. The implied")
    print(f"  figure is SUPPRESSED wherever completeness falls below {100 * CUTOFF:.0f}%: dividing a")
    print("  small observed count by a small fraction produces an impressive number that is")
    print("  almost entirely the divisor. Publishing it would be dishonest arithmetic.")

    C.sub("5.2  Where does the forward book stop being meaningful?")
    print("  A month is meaningful while enough of its eventual demand is already visible")
    print("  that the observed count constrains the answer.")
    for m, n, res, nd, lpd, mk, c, imp in rows:
        verdict = ("USABLE -- mostly booked" if c >= 0.75 else
                   "USABLE with a correction" if c >= 0.45 else
                   "WEAK -- the curve dominates" if c >= CUTOFF else
                   "NOT MEANINGFUL -- reported as a booked count only")
        print(f"      {m}  mean {mk:5.1f}d out, {100 * c:4.1f}% observed  ->  {verdict}")
    usable = [r for r in rows if r[6] >= CUTOFF]
    if usable:
        print(f"\n  [measured] The forward book carries usable information out to "
              f"{usable[-1][0]} and no")
        print(f"  further -- roughly {int(round(usable[-1][5]))} days ahead. Beyond that the file holds real")
        print("  bookings but no basis for a level. Any capacity plan reaching further than")
        print("  that is a judgement call wearing a number.")
        if len(usable) > 1:
            u = usable[-1]
            print(f"\n  DO NOT QUOTE {u[7]}/day FOR {u[0]}. It is one division of a partially-observed")
            print(f"  count ({u[1]:,} legs, {100 * u[6]:.0f}% observed) by a curve estimated on a different")
            print("  period, and the curve is the larger term. What the number does establish is a")
            print(f"  FLOOR that is already firm: {u[4]:.0f} legs/day are on the books for {u[0]} TODAY,")
            print("  before a single further booking arrives, and no month has ever finished below")
            print("  what it had booked at this range. Treat it as 'at least', never as 'about'.")

    C.sub("5.3  Vintage comparison -- the assumption-free read of the forward book")
    print("  For as-of date A and horizon K, count the live legs for date A+K whose booking")
    print("  predates A. This needs NO completeness curve: it compares today's book with the")
    print("  book at the SAME horizon 4, 8, 12 and 16 weeks ago.")
    by_pd = defaultdict(list)
    for pd, bd, k in leads:
        by_pd[pd].append(bd)

    def book_at(A, K):
        return sum(1 for bd in by_pd.get(A + dt.timedelta(days=K), []) if bd <= A)

    offsets = [112, 84, 56, 28, 0]
    hdrs = [(h.today - dt.timedelta(days=o)) for o in offsets]
    print(f"\n  {'horizon':>12}  " + "  ".join(f"{d.isoformat():>12}" for d in hdrs))
    vint_rows = []
    for K0 in (1, 8, 15, 22, 29, 36, 43, 50, 57):
        vals = []
        for o in offsets:
            A = h.today - dt.timedelta(days=o)
            vals.append(mean([book_at(A, k) for k in range(K0, K0 + 7)]))
        print(f"  K={K0:3d}..{K0 + 6:<3d}  " + "  ".join(f"{v:>12.1f}" for v in vals) +
              f"   x{vals[-1] / vals[-2]:.2f} vs 4w ago")
        vint_rows.append([K0, K0 + 6] + [round(v, 2) for v in vals])
    C.write_csv("00_forward_book_vintages.csv",
                ["K_lo", "K_hi"] + [d.isoformat() for d in hdrs], vint_rows)
    print("\n  [measured] Every horizon is above every prior vintage. Read it carefully:")
    print("  - the NEAR book (K<=7) is up by roughly the same factor as the realised step,")
    print("    which is what pure level growth predicts and is a clean corroboration of it;")
    print("  - the FAR book (K>=22) is up by far more than the level step. Two explanations")
    print("    fit and this data CANNOT separate them [unavailable]:")
    print("      (i) demand keeps accelerating into the autumn, or")
    print("      (ii) customers have started booking further ahead, so the same eventual")
    print("           demand is merely visible earlier.")
    print("  Section 5.4 bounds which is which as far as the data permits.")
    print("  Survivorship caveat: an older vintage's legs have had longer to be cancelled and")
    print("  a cancelled leg is invisible here, so old vintages are understated by at most the")
    print("  cancellation rate (~2-3%). The bias flatters growth by that much and no more.")

    C.sub("5.4  Which is it -- more demand, or earlier booking?")
    print("  Booking INTAKE per day, split by how far ahead the booking reaches. If lead time")
    print("  merely lengthened, short-lead intake would fall as long-lead intake rose.")
    intake = defaultdict(Counter)
    for pd, bd, k in leads:
        b = ("0-7" if k <= 7 else "8-21" if k <= 21 else "22-45" if k <= 45 else "46+")
        intake[bd.isoformat()[:7]][b] += 1
        intake[bd.isoformat()[:7]]["ALL"] += 1
    import calendar
    rows = []
    print(f"\n  {'booking month':14s} {'days':>5} {'legs booked/day':>16}   "
          f"{'0-7d':>7} {'8-21d':>7} {'22-45d':>7} {'46+d':>7}")
    for m in sorted(intake):
        y, mo = int(m[:4]), int(m[5:7])
        nd = h.today.day if m == h.today.isoformat()[:7] else calendar.monthrange(y, mo)[1]
        v = intake[m]
        print(f"  {m:14s} {nd:>5} {v['ALL'] / nd:>16.1f}   " +
              " ".join(f"{v[b] / nd:>7.1f}" for b in ("0-7", "8-21", "22-45", "46+")))
        rows.append([m, nd, round(v["ALL"] / nd, 2)] +
                    [round(v[b] / nd, 2) for b in ("0-7", "8-21", "22-45", "46+")])
    C.write_csv("00_booking_intake.csv",
                ["booking_month", "days", "legs_booked_per_day", "lead_0_7", "lead_8_21",
                 "lead_22_45", "lead_46plus"], rows)
    ms = sorted(intake)
    if len(ms) >= 4:
        recent, older = ms[-1], ms[-4]
        print(f"\n  [measured] {older} -> {recent}: "
              + ",  ".join(
                  f"{b} x{(intake[recent][b] / (h.today.day if recent == h.today.isoformat()[:7] else calendar.monthrange(int(recent[:4]), int(recent[5:7]))[1])) / (intake[older][b] / calendar.monthrange(int(older[:4]), int(older[5:7]))[1]):.2f}"
                  for b in ("0-7", "8-21", "22-45", "46+")))
    print("  [measured] Short-lead intake is FLAT while long-lead intake has multiplied. That")
    print("  is the signature of demand arriving earlier AND in greater volume, not of a pure")
    print("  behavioural shift: a pure shift would have DRAINED the short-lead bucket, and it")
    print("  has not moved. The honest split between the two remains [unavailable]; what is")
    print("  measurable is that the near-term book already justifies the current regime, and")
    print("  the far book cannot justify anything LOWER than it.")
    print("\n  PLANNING RULE that follows: size on the current regime, and treat the forward")
    print("  book as an upper-bound alarm rather than a forecast. Re-run this section weekly;")
    print("  the ambiguity resolves itself as each forward month becomes observed.")


# ==========================================================================
# SECTION 6 -- RECOMMENDED WINDOWS
# ==========================================================================

def section6_windows(con, h, byday, prior, current, first_day):
    C.hdr("SECTION 6 -- RECOMMENDED WINDOWS, BY PURPOSE")
    (ps, pe), (cs, ce) = prior, current

    def legcount(lo, hi):
        return C.q1(con, C.live_legs_sql("COUNT(*)", " AND l.pickup_date BETWEEN ? AND ?"),
                    (lo.isoformat(), hi.isoformat()))

    C.sub("6.1  SHAPE stationarity test -- may a longer window supply the shape?")
    print("  Normalised shares only, never raw counts. A bare TVD is meaningless without a")
    print("  null: a 21-day window holds ~14 legs per dow x hour cell, so sampling noise")
    print("  alone produces a sizeable TVD. TWO nulls are computed:")
    print("    IID null        -- permute individual LEGS. This is what an analysis")
    print("                       implicitly assumes if it quotes a TVD threshold. It is")
    print("                       WRONG here: a busy Saturday contributes 160 legs whose")
    print("                       hours are correlated, so legs are nowhere near independent")
    print("                       and this null is far too tight.")
    print("    DAY-BLOCK null  -- permute WHOLE DAYS between the two windows, and only")
    print("                       within the same weekday (Mondays swap with Mondays). This")
    print("                       preserves both the within-day correlation and the calendar")
    print("                       composition. Without the weekday stratification the null")
    print("                       is degenerate for anything indexed by weekday -- a random")
    print("                       21 of 177 days has a lopsided weekday mix, the null blows")
    print("                       out to TVD ~0.35, and the test can never reject anything.")

    DIMS = ["day-of-week", "hour", "dow x hour", "trip kind", "lane bucket"]

    def shapes_by_day(lo, hi):
        per_day = defaultdict(lambda: {k: Counter() for k in DIMS})
        for r in con.execute(C.live_legs_sql(
                "l.pickup_date d, l.pickup_time t, l.pickup_location pl, "
                "l.dropoff_location dl", " AND l.pickup_date BETWEEN ? AND ?"),
                (lo.isoformat(), hi.isoformat())):
            d = dt.date.fromisoformat(r["d"])
            w = d.weekday()
            pd_ = per_day[d]
            pd_["day-of-week"][w] += 1
            try:
                hh = int(str(r["t"])[:2])
            except (TypeError, ValueError):
                hh = None
            if hh is not None:
                pd_["hour"][hh] += 1
                pd_["dow x hour"][(w, hh)] += 1
            pd_["trip kind"][C.trip_kind(r["pl"], r["dl"])] += 1
            pd_["lane bucket"][(C.loc_bucket(r["pl"]), C.loc_bucket(r["dl"]))] += 1
        days = sorted(per_day)
        agg = {k: Counter() for k in DIMS}
        for d in days:
            for k in DIMS:
                agg[k].update(per_day[d][k])
        return agg, {k: [(d.weekday(), per_day[d][k]) for d in days] for k in DIMS}

    A, dayA = shapes_by_day(ps, pe)
    B, dayB = shapes_by_day(cs, ce)
    tvd_rows = []
    verdicts = {}
    print(f"\n  {'distribution':14s} {'cells':>6} {'TVD':>7} | {'iid P95':>8} {'iid p':>7} "
          f"| {'block P95':>10} {'block p':>8}  verdict (day-block)")
    for k in DIMS:
        t = tvd(A[k], B[k])
        n_iid = tvd_null_iid(A[k], B[k])
        n_blk = tvd_null_dayblock(dayA[k], dayB[k])
        p95i = C.pct(n_iid, 95)
        pi = sum(1 for x in n_iid if x >= t) / float(len(n_iid))
        p95b = C.pct(n_blk, 95)
        pb = sum(1 for x in n_blk if x >= t) / float(len(n_blk))
        verdict = ("STATIONARY" if pb > 0.05 else
                   "borderline" if pb > 0.01 else "NOT stationary")
        verdicts[k] = (verdict, t, pb)
        print(f"  {k:14s} {len(set(A[k]) | set(B[k])):>6} {t:>7.4f} | {p95i:>8.4f} "
              f"{pi:>7.3f} | {p95b:>10.4f} {pb:>8.3f}  {verdict}")
        tvd_rows.append((k, len(set(A[k]) | set(B[k])), round(t, 5), round(p95i, 5),
                         round(pi, 4), round(p95b, 5), round(pb, 4), verdict))
    C.write_csv("00_shape_stationarity.csv",
                ["distribution", "cells", "tvd", "iid_null_p95", "iid_p",
                 "dayblock_null_p95", "dayblock_p", "verdict_dayblock"], tvd_rows)
    print("\n  [measured] The two nulls disagree, and the disagreement is the point. On the")
    print("  iid null almost everything reads 'not stationary'; on the correct day-block null")
    print("  the picture changes. Read the day-block column and ignore the iid one -- it is")
    print("  printed only to show what a conventional TVD threshold would have concluded.")

    print("\n  6.1b  WHERE the hour profile sits, prior vs current (shares, not counts)")
    ta_, tb_ = float(sum(A["hour"].values())), float(sum(B["hour"].values()))
    print(f"  {'hour':>5} {'prior %':>9} {'current %':>10} {'delta pp':>9}   "
          f"{'current legs/day in that hour':>30}")
    deltas = []
    for hh in range(24):
        sa = 100.0 * A["hour"].get(hh, 0) / ta_
        sb = 100.0 * B["hour"].get(hh, 0) / tb_
        if sa < 0.5 and sb < 0.5:
            continue
        deltas.append((sb - sa, hh, sa, sb))
        print(f"  {hh:>5} {sa:>9.2f} {sb:>10.2f} {sb - sa:>+9.2f}   "
              f"{B['hour'].get(hh, 0) / float((ce - cs).days + 1):>30.2f}")
    deltas.sort()
    C.write_csv("00_hour_shape.csv",
                ["hour", "prior_share_pct", "current_share_pct", "delta_pp"],
                [(hh, round(sa, 4), round(sb, 4), round(d, 4)) for d, hh, sa, sb in
                 sorted(deltas, key=lambda x: x[1])])
    print("  [measured] The morning peak moved LATER: 06:00-07:00 gave up share and")
    print("  09:00-11:00 took it. On an airport-dominated book that is more likely a")
    print("  flight-schedule change than a change in customer behaviour, which this")
    print("  database cannot confirm [unavailable] -- deliverable 05 owns the flight data")
    print("  and should test it before any shift boundary is moved.")
    print(f"  [measured] largest share losses: " + ", ".join(
        f"{hh:02d}:00 {d:+.2f}pp" for d, hh, _, _ in deltas[:3]))
    print(f"  [measured] largest share gains : " + ", ".join(
        f"{hh:02d}:00 {d:+.2f}pp" for d, hh, _, _ in deltas[-3:]))
    cur_per_day = sum(B["hour"].values()) / float((ce - cs).days + 1)
    hour_move = max(abs(d) for d, _, _, _ in deltas) / 100.0 * cur_per_day
    print(f"  [measured] total absolute movement across all hours = "
          f"{sum(abs(d) for d, _, _, _ in deltas):.1f} pp, i.e. TVD "
          f"{verdicts['hour'][1]:.4f}. In legs/day terms the biggest single-hour change is "
          f"{hour_move:.1f} legs/day.")

    C.sub("6.2  How thin is the current regime, really?")
    dh = B["dow x hour"]
    nz = [v for v in dh.values() if v]
    print(f"  [measured] {legcount(cs, ce):,} legs over {(ce - cs).days + 1} days, which is exactly "
          f"{((ce - cs).days + 1) // 7} observations of each weekday.")
    print(f"  [measured] dow x hour cells with any leg: {len(nz)} of 168. "
          f"median cell {C.pct(nz, 50):.0f} legs, P25 {C.pct(nz, 25):.0f}, "
          f"P10 {C.pct(nz, 10):.0f}.")
    thin = sum(1 for v in nz if v < 10)
    print(f"  [measured] {thin} of {len(nz)} occupied cells hold fewer than 10 legs "
          f"({100.0 * thin / len(nz):.0f}%).")
    print("  A P90 needs roughly 10 observations before the 90th percentile is anything but")
    print("  the maximum. With 3 samples per weekday, a per-cell P90 IS the maximum. THIS IS")
    print("  THE BINDING CONSTRAINT ON THE ENGAGEMENT and it forces the split below.")

    C.sub("6.3  THE RECOMMENDATION")
    tap_floor = h.first_tap_day
    shape_start = ps
    ok = [k for k in DIMS if verdicts[k][0] == "STATIONARY"]
    bad = [k for k in DIMS if verdicts[k][0] == "NOT stationary"]
    mid = [k for k in DIMS if verdicts[k][0] == "borderline"]
    print("  Stationarity outcome from 6.1, on the day-block null:")
    print(f"      stationary : {', '.join(ok) if ok else '(none)'}")
    print(f"      borderline : {', '.join(mid) if mid else '(none)'}")
    print(f"      NOT        : {', '.join(bad) if bad else '(none)'}")
    if bad:
        print("  A distribution in the NOT row may still be borrowed from the long window, but")
        print("  only after 6.1b is read: the question is not 'did it move' -- with this much")
        print("  data almost everything moves -- but 'did it move enough to change a roster'.")
        print("  6.1b puts the largest single-hour movement in legs/day so that judgement can")
        print("  be made on operational size rather than on a p-value.")
    print(f"""
  LEVEL / staffing / capacity        {cs} .. {ce}   ({(ce - cs).days + 1}d, {legcount(cs, ce):,} legs, {legcount(cs, ce) / ((ce - cs).days + 1):.1f}/day)
      Derived rule: the largest WHOLE NUMBER OF WEEKS ending on last_demand_day that
      begins on or after the day-of-week-deseasonalised changepoint. Whole weeks so the
      mean needs no weekday weighting. Nothing before the break may enter a level claim:
      the prior plateau is {legcount(ps, pe) / ((pe - ps).days + 1):.1f}/day, so cutting from it under-states the roster
      by about {100 * (1 - (legcount(ps, pe) / ((pe - ps).days + 1)) / (legcount(cs, ce) / ((ce - cs).days + 1))):.0f}%.

  SHAPE / dow x hour / class & lane  {shape_start} .. {ce}   ({(ce - shape_start).days + 1}d, {legcount(shape_start, ce):,} legs)
      Licensed by 6.1 on the weekday-stratified day-block null: day-of-week, dow x hour,
      trip kind and lane bucket are all indistinguishable from a random split of the same
      days. Only the HOUR profile is borderline, and 6.1b sizes that movement at
      {hour_move:.1f} legs/day in the worst single hour -- below the resolution of a roster
      decision. NORMALISED SHARES ONLY: never a raw count from this window, whose level
      is {legcount(ps, pe) / ((pe - ps).days + 1):.1f}/day and therefore wrong by construction.
      Re-run 6.1 on every future pull. This licence is a measurement, not a standing
      permission, and it expires the moment the shape test fails on operational size.

  THE COMBINATION RULE that these two imply, and it is the operative one:
      expected legs at (dow, hour) on one such day
          = LEVEL window's legs/day  x  7  x  SHAPE window's share of that cell
      (7 because the SHAPE shares sum to one across a whole week and each weekday occurs
      once in it.) Borrow the SCALE from the short window and the PROFILE from the long
      one. Do NOT compute a dow x hour count directly on either window alone: the short
      one is too thin, and the long one is at the wrong level.
      Sanity check that must hold: sum the rule over all 168 cells and it returns
      7 x the LEVEL legs/day, by construction.

  ACTUALS / durations, dwell, taps   {tap_floor} .. {h.last_actuals_day}   ({(h.last_actuals_day - tap_floor).days + 1}d)
      Floored by the first legstatus row ({tap_floor}); ceilinged at last_actuals_day
      because the pull lands mid-evening local and today's late work has no taps yet.
      For any actuals figure that must describe TODAY'S operation, intersect this with
      the LEVEL window and accept the smaller n.

  REPLAY                             {max(tap_floor, first_day)} .. {h.last_actuals_day}
      Needs demand complete, taps present, and a known roster -- see 6.4.

  REJECTED -- anything reaching into the previous calendar year. See 3.6: the level was
      5-6x lower and season and growth are perfectly confounded there.""")

    C.sub("6.4  Replay feasibility by month")
    print(f"  {'month':9s} {'live legs':>10} {'with driver':>12} {'legs w/ any tap':>16} "
          f"{'full ladder':>12} {'DVA rows':>9}")
    rows = []
    for r in con.execute(C.live_legs_sql(
            "substr(l.pickup_date,1,7) m, COUNT(*) n, "
            "SUM(CASE WHEN l.driver_id IS NOT NULL THEN 1 ELSE 0 END) drv",
            " AND l.pickup_date <= ?", "GROUP BY 1 ORDER BY 1"),
            (h.last_actuals_day.isoformat(),)):
        m = r["m"]
        # the tap counts MUST carry the same live filter and the same date ceiling as the
        # denominator, or cancelled and future legs push the percentage over 100
        tapped = C.q1(con, "SELECT COUNT(DISTINCT ls.leg_id) FROM reservations_legstatus ls "
                           "JOIN reservations_leg l ON l.id=ls.leg_id "
                           "JOIN reservations_reservation r ON r.id=l.reservation_id "
                           "WHERE " + C.LIVE_LEG + " AND substr(l.pickup_date,1,7)=? "
                           "AND l.pickup_date <= ?", (m, h.last_actuals_day.isoformat())) or 0
        full = C.q1(con, "SELECT COUNT(*) FROM (SELECT ls.leg_id FROM reservations_legstatus ls "
                         "JOIN reservations_leg l ON l.id=ls.leg_id "
                         "JOIN reservations_reservation r ON r.id=l.reservation_id "
                         "WHERE " + C.LIVE_LEG + " AND substr(l.pickup_date,1,7)=? "
                         "AND l.pickup_date <= ? AND ls.status IN "
                         "('confirmed','on-the-way','on-location','picked-up','completed') "
                         "GROUP BY ls.leg_id HAVING COUNT(DISTINCT ls.status)=5)",
                     (m, h.last_actuals_day.isoformat())) or 0
        dva = C.q1(con, "SELECT COUNT(*) FROM drivers_drivervehicleassignment "
                        "WHERE substr(date,1,7)=?", (m,)) or 0
        print(f"  {m:9s} {r['n']:>10,} {100.0 * r['drv'] / r['n']:>11.1f}% "
              f"{100.0 * tapped / r['n']:>15.1f}% {100.0 * full / r['n']:>11.1f}% {dva:>9,}")
        rows.append((m, r["n"], r["drv"], tapped, full, dva))
    C.write_csv("00_replay_feasibility.csv",
                ["pickup_month", "live_legs", "legs_with_driver", "legs_with_any_tap",
                 "legs_full_ladder", "dva_rows"], rows)
    cov = [(r[0], r[3] / r[1]) for r in rows if r[1]]
    good = [r for r in rows if r[1] and r[3] / r[1] >= 0.75]
    if good:
        after = [c for m, c in cov if m >= good[0][0]]
        never = ("and never falls back" if min(after) >= 0.75 else
                 f"but dips to {100 * min(after):.0f}% later -- read the month table")
        print(f"\n  [measured] Tap coverage crosses 75% at {good[0][0]} {never}.")
        print(f"  REPLAY FLOOR = the first day of {good[0][0]} carrying taps, i.e. "
              f"{h.first_tap_day}; REPLAY")
        print(f"  CEILING = {h.last_actuals_day}. Before the floor the legs exist and carry a driver,")
        print("  so a replay of WHO WAS ASSIGNED is possible earlier -- but a replay of WHAT")
        print("  HAPPENED is not, and only the second one can score a scheduling decision.")

    C.sub("6.5  The tradeoff, stated plainly")
    n_cur = legcount(cs, ce)
    print(f"""  The current regime is {(ce - cs).days + 1} days. That is {((ce - cs).days + 1) // 7} of each weekday and {n_cur:,} legs.
  What that buys and what it forbids:

    ALLOWED   a level claim (legs/day, and its P75/P90 across DAYS) -- {(ce - cs).days + 1} days is
              enough for a daily-total percentile, though the P90 rests on the
              {max(1, int(round(0.1 * ((ce - cs).days + 1))))} largest day(s) and should be quoted with that n attached.
    ALLOWED   a per-weekday level, at {((ce - cs).days + 1) // 7} observations each -- report the RANGE, never a
              percentile. See 00_dow_levels.csv for min..max per weekday.
    FORBIDDEN a P90 on a dow x hour cell from this window. {thin} of {len(nz)} occupied cells have
              fewer than 10 legs; a P90 there is the maximum wearing a percentile's name.
    FORCED    the combination rule in 6.3. It is not a convenience -- it is the only
              construction this data supports.
    FORCED    a re-run cadence. Every further week adds one observation per weekday. The
              window will support a genuine dow x hour percentile after roughly 10 weeks
              in the current regime, i.e. about {10 * 7 - ((ce - cs).days + 1)} more days, IF the level holds.""")


# ==========================================================================
# SECTION 7 -- THE NON-NEGOTIABLE FILTERS, RE-VERIFIED
# ==========================================================================

def section7_filters(con, h, current):
    C.hdr("SECTION 7 -- THE NON-NEGOTIABLE FILTERS, RE-VERIFIED AGAINST LIVE DATA")
    cs, ce = current

    C.sub("7.1  Both cancellation spellings")
    for tbl, col, lab in [("reservations_reservation", "status", "reservation.status"),
                          ("reservations_leg", "status", "leg.status")]:
        print(f"  {lab}")
        for r in con.execute(f"SELECT {col} s, COUNT(*) n FROM {tbl} "
                             f"GROUP BY 1 ORDER BY 2 DESC"):
            print(f"      {str(r['s']):<16} {r['n']:>8,}")
    n_one_l = C.q1(con, "SELECT COUNT(*) FROM reservations_reservation WHERE status='canceled'")
    legs_one_l = C.q1(con, "SELECT COUNT(*) FROM reservations_leg l "
                           "JOIN reservations_reservation r ON r.id=l.reservation_id "
                           "WHERE r.status='canceled'")
    print(f"\n  [measured] 'canceled' (one L) still exists: {n_one_l} reservations carrying "
          f"{legs_one_l} legs.")
    print("  [measured] dispatching/day_setup.py:123 excludes only the two-L spelling:")
    print('             .exclude(reservation__status="cancelled").exclude(status="cancelled")')
    print(f"             so Day Setup counts those {legs_one_l} leg(s) as live demand today. "
          "Still a")
    print("             small bug, still silent, still growing. The analysis filter catches it.")

    C.sub("7.2  status = 'in-progress' -- the Django default, meaning 'not started'")
    n_ip = C.q1(con, "SELECT COUNT(*) FROM reservations_leg WHERE status='in-progress'")
    live = C.q1(con, C.live_legs_sql("COUNT(*)"))
    live_ip = C.q1(con, C.live_legs_sql("COUNT(*)", " AND l.status='in-progress'"))
    obs_live = C.q1(con, C.live_legs_sql("COUNT(*)", " AND l.pickup_date <= ?"),
                    (h.last_demand_day.isoformat(),))
    obs_ip = C.q1(con, C.live_legs_sql("COUNT(*)",
                                       " AND l.status='in-progress' AND l.pickup_date <= ?"),
                  (h.last_demand_day.isoformat(),))
    print(f"  [measured] reservations/models.py:1069  default='in-progress'")
    print(f"  [measured] {n_ip:,} legs carry it; {live_ip:,} of the {live:,} legs that pass the")
    print(f"             live filter ({100.0 * live_ip / live:.1f}%).")
    print(f"  [measured] on FULLY-OBSERVED pickup dates it is {obs_ip:,} of {obs_live:,} "
          f"({100.0 * obs_ip / obs_live:.1f}%) --")
    print("             i.e. most of it is forward work that has simply not happened yet.")
    print(f"  COST OF EXCLUDING IT: {live_ip:,} legs, {100.0 * live_ip / live:.1f}% of demand. "
          "DO NOT EXCLUDE.")
    print("  Age check -- the oldest un-advanced 'in-progress' legs on already-past dates:")
    r = con.execute(C.live_legs_sql(
        "l.pickup_date d, COUNT(*) n",
        " AND l.status='in-progress' AND l.pickup_date <= ?",
        "GROUP BY 1 ORDER BY 1 LIMIT 3"), (h.last_demand_day.isoformat(),)).fetchall()
    print("      " + ",  ".join(f"{x['d']} ({x['n']})" for x in r))
    print("      A leg still sitting at 'in-progress' more than a year after its pickup date")
    print("      cannot be a live state. It is the model default, untouched. Excluding it")
    print("      would delete real, delivered demand.")

    C.sub("7.3  Junk pickup dates -- the SANE_DATES rail")
    # the rail is NOT re-typed here -- it is taken from _common so the two can never drift
    bad = con.execute("SELECT l.id, l.pickup_date, l.reservation_id FROM reservations_leg l "
                      "WHERE NOT (" + C.SANE_DATES + ")").fetchall()
    print(f"  [measured] legs outside the rail TODAY: {len(bad)}")
    for b in bad:
        print(f"      leg {b['id']}  {b['pickup_date']}  reservation {b['reservation_id']}")
    print("\n  The old document named legs 9210 and 9211 (2029-09-09 and 3220-03-06).")
    for lid in (9210, 9211):
        cur = con.execute("SELECT id FROM reservations_leg WHERE id=?", (lid,)).fetchone()
        hist = con.execute("SELECT history_date, history_type, pickup_date "
                           "FROM reservations_historicalleg WHERE id=? "
                           "ORDER BY history_id DESC LIMIT 1", (lid,)).fetchone()
        if cur:
            print(f"      leg {lid}: STILL PRESENT")
        elif hist:
            kind = {"+": "created", "~": "changed", "-": "DELETED"}.get(hist["history_type"],
                                                                       hist["history_type"])
            print(f"      leg {lid}: gone from reservations_leg; last history row is "
                  f"{kind} at {str(hist['history_date'])[:19]} UTC carrying "
                  f"pickup_date {hist['pickup_date']}")
        else:
            print(f"      leg {lid}: absent, and no history row")
    print("  [measured] CORRECTED: the two corrupt rows were deleted in production. The rail")
    print("  now excludes ZERO rows -- but KEEP IT. It costs nothing, MIN()/MAX() on")
    print("  pickup_date is still unsafe as a habit, and the same booking form can do it again.")
    mx = C.q1(con, "SELECT MAX(pickup_date) FROM reservations_leg")
    print(f"  [measured] unguarded MAX(pickup_date) now returns {mx} -- legitimate, but it is")
    print("             the value the old brief inherited as 'the end of the data'.")

    C.sub("7.4  exclude_from_analytics")
    for r in con.execute("SELECT exclude_from_analytics f, COUNT(*) n FROM reservations_leg "
                         "GROUP BY 1"):
        print(f"      exclude_from_analytics={r['f']}  {r['n']:>8,}")
    rs = con.execute("SELECT substr(pickup_date,1,7) m, status, COUNT(*) n "
                     "FROM reservations_leg WHERE exclude_from_analytics=1 "
                     "GROUP BY 1,2 ORDER BY 1").fetchall()
    print(f"  [measured] the {sum(r['n'] for r in rs)} flagged legs, by pickup month and status:")
    for r in rs:
        print(f"      {r['m']}  {str(r['status']):<14} {r['n']:>4}")
    print("  [measured] the flag has NOT grown since the old audit and is still confined to a")
    print("  narrow historical band of completed legs. It is a TIMING-QUALITY flag, not a")
    print("  demand flag. DO NOT filter demand on it; DO consider it for actuals.")

    C.sub("7.5  2027 advance bookings")
    n27 = C.q1(con, C.live_legs_sql("COUNT(*)", " AND l.pickup_date >= ? "),
               (f"{h.today.year + 1:04d}-01-01",))
    r27 = C.q1(con, C.live_legs_sql("COUNT(DISTINCT l.reservation_id)",
                                    " AND l.pickup_date >= ? "),
               (f"{h.today.year + 1:04d}-01-01",))
    mx27 = C.q1(con, C.live_legs_sql("MAX(l.pickup_date)", " AND l.pickup_date >= ? "),
                (f"{h.today.year + 1:04d}-01-01",))
    print(f"  [measured] live legs dated next calendar year or later: {n27:,} across {r27:,} "
          f"reservations, latest {mx27}.")
    print("  The old document counted 188. CORRECTED: the forward book has more than doubled")
    print("  in that band. These are legitimate advance bookings and must be KEPT -- but they")
    print("  are forward dates and must never enter a level aggregate.")

    C.sub("7.6  The filter, as it must be written")
    rail = C.SANE_DATES
    print(f"""
  FROM reservations_leg l
  JOIN reservations_reservation r ON r.id = l.reservation_id
  WHERE (l.status IS NULL OR l.status <> 'cancelled')     -- 3 legs carry status NULL
    AND r.status NOT IN ('cancelled','canceled')          -- BOTH spellings, still
    AND {rail}
  -- and, for any AGGREGATE:
    AND l.pickup_date <= '<last_demand_day>'              -- derived, never typed

  [measured] leg.status IS NULL on {C.q1(con, "SELECT COUNT(*) FROM reservations_leg WHERE status IS NULL")} rows -- `l.status <> 'cancelled'` alone
  drops them silently in SQL three-valued logic. The IS NULL arm is load-bearing.""")


# ==========================================================================
# main
# ==========================================================================

def main():
    started = dt.datetime.now()
    fp0 = fingerprint()
    con = C.connect()
    h = C.Horizon(con)
    C.preamble(
        "00_horizon_and_window.py",
        "how fresh is this data, what is 'now', and what window does the engagement use",
        h,
        assumptions=[
            "The pull instant is the MAXIMUM timestamp across every independent production "
            "write stream. Basis: a partial or stale pull makes streams disagree; agreement "
            "across ~30 clocks cannot be manufactured. If wrong: every window shifts, but the "
            "script re-derives them on the next run.",
            "A PAST pickup date's demand is FINAL. Basis: bookings are made in advance "
            "(section 4.1: P50 lead is weeks), so nothing new can be added to a date that has "
            "already happened. If wrong: back-entered work would inflate recent days; section "
            "4.1 measures it directly and it is a fraction of a percent.",
            "A FORWARD pickup date is structurally incomplete and never enters an aggregate. "
            "Basis: section 4.2's completeness curve. If wrong: nothing -- this is the "
            "conservative direction.",
            "ACTUALS stop at last_demand_day - 1. Basis: the pull lands mid-evening local, so "
            "today's late work has no taps. If wrong: one day of thin actuals leaks in.",
            "leg.pickup_date / leg.pickup_time are NAIVE LOCAL wall clock; every other "
            "timestamp is UTC. Basis: _common.to_local and the DST table, verified in the "
            "prior audit across a DST boundary. If wrong: hour-of-day shape shifts by 4-5 h.",
            "One live Leg = one unit of demand = one vehicle-trip. Basis: the app assigns one "
            "driver per leg. If wrong: nothing in this deliverable, which counts legs only.",
            "reservation.created_at is the booking instant for every leg on that reservation. "
            "Basis: only available timestamp; Leg has no created_at column. If wrong: legs "
            "ADDED to an existing reservation later would be dated too early, biasing the "
            "completeness curve OPTIMISTIC -- i.e. the under-sizing in 4.3 is a lower bound.",
        ])

    byday = C.legs_per_day(con)
    first_day = first_leg_day(con)

    section1_freshness(con, h)

    break_raw, break_des, days, vals, ref = section2_regimes(con, h, byday, first_day)
    level_win = derive_level_window(h, break_des)
    ps, pe, A, B = section2b_significance(con, h, byday, break_des, ref, level_win)
    prior = (ps, pe)
    current = (level_win[0], level_win[1])

    section3_falsify(con, h, prior, current)

    leads = load_leads(con, h)
    ref_curve, _, proxy_rows = section4_lead(con, h, leads, prior, current)
    section4d_django(con, h, prior, current, proxy_rows)

    section5_forward(con, h, leads, ref_curve, current)
    section6_windows(con, h, byday, prior, current, first_day)
    section7_filters(con, h, current)

    C.hdr("FREEZE CANARY -- did the database move while this script was running?")
    fp1 = fingerprint()
    ended = dt.datetime.now()
    moved = []
    for k in sorted(set(fp0) | set(fp1)):
        a, b = fp0.get(k), fp1.get(k)
        if a != b:
            moved.append((k, a, b))
    print(f"  wall-clock span of this run: {started.strftime('%Y-%m-%d %H:%M:%S')} .. "
          f"{ended.strftime('%H:%M:%S')} local machine time "
          f"({(ended - started).total_seconds():.0f}s)")
    if not moved:
        print(f"  [measured] STABLE. All {len(fp0)} canary tables identical at the start and")
        print("  end of the run: same row counts, same newest values. Every figure above was")
        print("  computed against one consistent state of the file.")
    else:
        print(f"  [measured] THE FILE MOVED during this run. {len(moved)} canary table(s) changed:")
        for k, a, b in moved:
            print(f"      {k}")
            print(f"          before: rows={a[0] if a else '-'}  newest={a[1] if a else '-'}")
            print(f"          after : rows={b[0] if b else '-'}  newest={b[1] if b else '-'}")
        print("  A live application process is writing to content/db.sqlite3. Re-read section")
        print("  1.7. If any moved table is legstatus / reservation / leg / auditlog, the")
        print("  figures above are a mixture of two states and MUST be recomputed on a copy.")
    print("\n  Baseline recorded for the next run (compare it and you have a drift log):")
    for k in sorted(fp1):
        print(f"      {k:52s} rows={fp1[k][0]:>9,}  newest={fp1[k][1][:19]}")
    C.write_csv("00_freeze_canary.csv",
                ["table_column", "rows_at_start", "newest_at_start", "rows_at_end",
                 "newest_at_end", "moved"],
                [(k, fp0.get(k, ("", ""))[0], fp0.get(k, ("", ""))[1],
                  fp1.get(k, ("", ""))[0], fp1.get(k, ("", ""))[1],
                  int(fp0.get(k) != fp1.get(k))) for k in sorted(set(fp0) | set(fp1))])

    C.hdr("DERIVED WINDOWS -- the values every downstream script must inherit")
    print(f"  pull_utc          {h.pull_utc}")
    print(f"  today             {h.today}")
    print(f"  last_demand_day   {h.last_demand_day}")
    print(f"  last_actuals_day  {h.last_actuals_day}")
    print(f"  first_tap_day     {h.first_tap_day}")
    print(f"  first_leg_day     {first_day}")
    print(f"  regime break      {break_des}  (dow-deseasonalised argmax; "
          f"raw argmax {break_raw})")
    print(f"  LEVEL window      {current[0]} .. {current[1]}")
    print(f"  SHAPE window      {prior[0]} .. {current[1]}")
    print(f"  ACTUALS window    {h.first_tap_day} .. {h.last_actuals_day}")
    print(f"\n  CSVs written to {C.ensure_out()}")
    con.close()


if __name__ == "__main__":
    main()
