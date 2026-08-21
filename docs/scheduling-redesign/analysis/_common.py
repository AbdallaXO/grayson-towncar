"""Shared foundation for the scheduling-redesign analysis scripts.

Every window, horizon and cutoff used by 00..06 is DERIVED HERE FROM THE DATABASE
at run time. There is no hardcoded "present", no snapshot cut, and no analysis date
literal anywhere in this package. Re-running the scripts against a newer pull moves
every window forward on its own.

Read-only by construction: `connect()` opens the file with `mode=ro`, so a stray
write raises rather than corrupting the snapshot.

Timezone: the database stores every timestamp in UTC. Booked `pickup_date` /
`pickup_time` are LOCAL (America/New_York). `to_local()` is the only bridge between
the two, and it is DST-aware — see `US_DST_TRANSITIONS`.
"""

import datetime as dt
import os
import sqlite3

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
DB_PATH = os.path.join(REPO_ROOT, "content", "db.sqlite3")
OUT_DIR = os.path.join(os.path.dirname(__file__), "out")


# --------------------------------------------------------------------------
# connection
# --------------------------------------------------------------------------

def connect(path=None):
    """Open the snapshot read-only. Any write attempt raises OperationalError."""
    p = path or DB_PATH
    if not os.path.exists(p):
        raise SystemExit(f"snapshot not found: {p}")
    con = sqlite3.connect(f"file:{p.replace(os.sep, '/')}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def q(con, sql, params=()):
    return con.execute(sql, params).fetchall()


def q1(con, sql, params=()):
    r = con.execute(sql, params).fetchone()
    return r[0] if r else None


# --------------------------------------------------------------------------
# the non-negotiable demand filter
# --------------------------------------------------------------------------

# Both cancellation spellings exist in production ('cancelled' and 'canceled').
# `status = 'in-progress'` is the Django model default for a NEW leg and means
# "not started" — excluding it would delete a third of the table.
LIVE_LEG = ("l.status IS NOT DISTINCT FROM l.status "  # no-op, keeps the clause composable
            "AND (l.status IS NULL OR l.status <> 'cancelled') "
            "AND r.status NOT IN ('cancelled', 'canceled')")

# SQLite has no IS NOT DISTINCT FROM; use the plain form.
LIVE_LEG = ("(l.status IS NULL OR l.status <> 'cancelled') "
            "AND r.status NOT IN ('cancelled', 'canceled')")

# Two legs carry junk pickup dates (year 2029 and 3220) from one bad booking.
# Never bound a window with MIN()/MAX() on pickup_date — always an explicit BETWEEN.
SANE_DATES = "l.pickup_date BETWEEN '2025-01-01' AND '2027-12-31'"

LEG_JOIN = ("FROM reservations_leg l "
            "JOIN reservations_reservation r ON r.id = l.reservation_id")


def live_legs_sql(select, extra="", order=""):
    return f"SELECT {select} {LEG_JOIN} WHERE {LIVE_LEG} AND {SANE_DATES} {extra} {order}"


# --------------------------------------------------------------------------
# timezone
# --------------------------------------------------------------------------

# America/New_York DST transitions, as UTC instants. Second Sunday in March 07:00 UTC
# (02:00 EST -> 03:00 EDT); first Sunday in November 06:00 UTC (02:00 EDT -> 01:00 EST).
US_DST_TRANSITIONS = [
    (dt.datetime(2025, 3, 9, 7), dt.datetime(2025, 11, 2, 6)),
    (dt.datetime(2026, 3, 8, 7), dt.datetime(2026, 11, 1, 6)),
    (dt.datetime(2027, 3, 14, 7), dt.datetime(2027, 11, 7, 6)),
]


def utc_offset_hours(utc_dtm):
    """Hours to SUBTRACT from a UTC instant to get America/New_York local time."""
    for start, end in US_DST_TRANSITIONS:
        if start <= utc_dtm < end:
            return 4
    return 5


def to_local(ts):
    """'YYYY-MM-DD HH:MM:SS[.ffffff]' in UTC -> naive local datetime. None-safe."""
    if not ts:
        return None
    if isinstance(ts, dt.datetime):
        d = ts
    else:
        d = dt.datetime.fromisoformat(str(ts).replace("T", " "))
    return d - dt.timedelta(hours=utc_offset_hours(d))


def booked_dtm(pickup_date, pickup_time):
    """Booked local pickup instant. None if either half is missing/unparseable."""
    if not pickup_date or not pickup_time:
        return None
    try:
        return dt.datetime.fromisoformat(f"{pickup_date} {str(pickup_time)[:8]}")
    except ValueError:
        return None


# --------------------------------------------------------------------------
# horizon — derived, never assumed
# --------------------------------------------------------------------------

# Independent production write streams. If the pull were partial or stale, these
# would disagree; agreement across all of them is what establishes the horizon.
WRITE_STREAMS = [
    ("reservations_reservation.created_at", "SELECT MAX(created_at) FROM reservations_reservation"),
    ("reservations_quote.created_at", "SELECT MAX(created_at) FROM reservations_quote"),
    ("reservations_lead.created_at", "SELECT MAX(created_at) FROM reservations_lead"),
    ("payment_payment.created_at", "SELECT MAX(created_at) FROM payment_payment"),
    ("reservations_legstatus.timestamp", "SELECT MAX(timestamp) FROM reservations_legstatus"),
    ("reservations_auditlog.timestamp", "SELECT MAX(timestamp) FROM reservations_auditlog"),
    ("reservations_leg.driver_assigned_at",
     "SELECT MAX(driver_assigned_at) FROM reservations_leg"),
    ("reservations_historicalleg.history_date",
     "SELECT MAX(history_date) FROM reservations_historicalleg"),
    ("ops_staffactivity.timestamp", "SELECT MAX(timestamp) FROM ops_staffactivity"),
]


class Horizon:
    """Everything downstream needs to know about 'when is now', derived from data.

    pull_utc          latest write across every independent production stream
    pull_local        the same instant in America/New_York
    today             pull_local.date() — the real 'present' for this analysis
    last_demand_day   last pickup_date whose DEMAND is fully known. Bookings are made
                      in advance, so a past date's leg count is final the moment the
                      date passes; `today` itself is final too (its legs were booked
                      earlier). Forward dates are structurally incomplete.
    last_actuals_day  last pickup_date whose OPERATIONAL RECORD is complete. The pull
                      lands mid-evening, so `today`'s late work has no taps yet —
                      actuals must stop the day before.
    """

    def __init__(self, con):
        self.streams = {}
        for label, sql in WRITE_STREAMS:
            try:
                self.streams[label] = q1(con, sql)
            except sqlite3.OperationalError:
                self.streams[label] = None
        vals = [v for v in self.streams.values() if v]
        if not vals:
            raise SystemExit("no production write stream carries a timestamp")
        self.pull_utc = max(vals)
        self.pull_local = to_local(self.pull_utc)
        self.today = self.pull_local.date()
        self.last_demand_day = self.today
        self.last_actuals_day = self.today - dt.timedelta(days=1)
        # first legstatus tap — the hard floor for anything measuring what happened
        first_tap = q1(con, "SELECT MIN(timestamp) FROM reservations_legstatus")
        self.first_tap_day = to_local(first_tap).date() if first_tap else None

    def freshness_report(self):
        lines = []
        for label, v in self.streams.items():
            if not v:
                lines.append(f"  {label:44s}  (absent)")
                continue
            lag = (dt.datetime.fromisoformat(str(self.pull_utc))
                   - dt.datetime.fromisoformat(str(v))).total_seconds() / 3600.0
            lines.append(f"  {label:44s}  {str(v)[:19]}  ({lag:5.1f} h behind newest)")
        return "\n".join(lines)


# --------------------------------------------------------------------------
# demand regime detection — where the window comes from
# --------------------------------------------------------------------------

def legs_per_day(con, start=None, end=None):
    """{'YYYY-MM-DD': live leg count}, demand-filtered."""
    extra = ""
    params = []
    if start:
        extra += " AND l.pickup_date >= ?"
        params.append(str(start))
    if end:
        extra += " AND l.pickup_date <= ?"
        params.append(str(end))
    rows = q(con, live_legs_sql("l.pickup_date AS d, COUNT(*) AS n", extra,
                                "GROUP BY 1 ORDER BY 1"), tuple(params))
    return {r["d"]: r["n"] for r in rows}


def trailing_mean(byday, end_date, days=28):
    """Mean legs/day over the `days` window ENDING on end_date inclusive."""
    tot = sum(byday.get((end_date - dt.timedelta(days=i)).isoformat(), 0)
              for i in range(days))
    return tot / float(days)


def trailing_series(byday, start, end, days=28, step=7):
    out = []
    d = start
    while d <= end:
        out.append((d, trailing_mean(byday, d, days)))
        d += dt.timedelta(days=step)
    return out


def detect_regimes(byday, first_day, last_day, window=28, tol=0.06, min_len=42):
    """Split the trailing-mean curve into level regimes.

    Walks backwards from `last_day`. A regime continues while the trailing mean stays
    within `tol` of the regime's running mean; a sustained departure opens a new one.
    Returns [(start_date, end_date, mean_legs_per_day)] oldest-first. Purely
    data-driven — this is what replaces a hand-picked window.
    """
    days = []
    d = first_day + dt.timedelta(days=window)
    while d <= last_day:
        days.append((d, trailing_mean(byday, d, window)))
        d += dt.timedelta(days=1)
    if not days:
        return []
    regimes = []
    cur = [days[-1]]
    for d, m in reversed(days[:-1]):
        ref = sum(x[1] for x in cur) / len(cur)
        if ref > 0 and abs(m - ref) / ref > tol and len(cur) >= min_len:
            regimes.append(cur)
            cur = [(d, m)]
        else:
            cur.append((d, m))
    regimes.append(cur)
    out = []
    for r in regimes:
        r = sorted(r)
        out.append((r[0][0], r[-1][0], sum(x[1] for x in r) / len(r)))
    return sorted(out)


def changepoints(byday, first_day, last_day, min_seg=28, min_effect=0.10):
    """Binary-segmentation mean-shift changepoints on the RAW daily series.

    `detect_regimes` smooths first, so a late step-up can hide inside a long plateau
    whose running mean drifts to meet it. This does not smooth: it scans every legal
    split, takes the one that most reduces total squared error, and keeps it only if
    the two sides differ by more than `min_effect` in relative terms. Then recurses.
    Segments shorter than `min_seg` days are never created.

    Returns [(start, end, n_days, mean)] oldest-first.
    """
    days = []
    d = first_day
    while d <= last_day:
        days.append(byday.get(d.isoformat(), 0))
        d += dt.timedelta(days=1)
    n = len(days)
    if n < 2 * min_seg:
        return [(first_day, last_day, n, sum(days) / float(n or 1))]

    def sse(seq):
        if not seq:
            return 0.0
        m = sum(seq) / float(len(seq))
        return sum((x - m) ** 2 for x in seq)

    cuts = []

    def rec(lo, hi):
        seg = days[lo:hi]
        if len(seg) < 2 * min_seg:
            return
        base = sse(seg)
        best, best_i = None, None
        for i in range(lo + min_seg, hi - min_seg + 1):
            gain = base - sse(days[lo:i]) - sse(days[i:hi])
            if best is None or gain > best:
                best, best_i = gain, i
        if best_i is None:
            return
        left = days[lo:best_i]
        right = days[best_i:hi]
        ml = sum(left) / float(len(left))
        mr = sum(right) / float(len(right))
        if ml <= 0 or abs(mr - ml) / ml < min_effect:
            return
        cuts.append(best_i)
        rec(lo, best_i)
        rec(best_i, hi)

    rec(0, n)
    bounds = [0] + sorted(cuts) + [n]
    out = []
    for a, b in zip(bounds[:-1], bounds[1:]):
        seg = days[a:b]
        out.append((first_day + dt.timedelta(days=a),
                    first_day + dt.timedelta(days=b - 1),
                    b - a, sum(seg) / float(len(seg))))
    return out


# --------------------------------------------------------------------------
# status taps
# --------------------------------------------------------------------------

LADDER = ("confirmed", "on-the-way", "on-location", "picked-up", "completed")


def first_taps(con, statuses=LADDER):
    """{leg_id: {status: local datetime}} using MIN(timestamp) per (leg, status).

    MIN, never `.first()`: duplicate taps are common (auto-resets on driver unassign),
    and Django's default ordering makes `.first()` return the LATEST row. This mirrors
    `analytics.first_status_times`, which is the one production reader that gets it right.
    """
    marks = ",".join("?" for _ in statuses)
    rows = q(con, f"""SELECT leg_id, status, MIN(timestamp) AS ts
                      FROM reservations_legstatus
                      WHERE status IN ({marks})
                      GROUP BY leg_id, status""", tuple(statuses))
    out = {}
    for r in rows:
        out.setdefault(r["leg_id"], {})[r["status"]] = to_local(r["ts"])
    return out


# --------------------------------------------------------------------------
# location classification
# --------------------------------------------------------------------------

def loc_bucket(text):
    """Coarse location class. Deliberately conservative — 'other' is honest."""
    s = (text or "").lower()
    if "mco" in s or "orlando international" in s:
        return "MCO"
    if "sfb" in s or "sanford" in s:
        return "SFB"
    if "port canaveral" in s or "cruise" in s or "terminal " in s and "port" in s:
        return "PORT"
    if "disney" in s or "walt disney" in s:
        return "DISNEY"
    if "universal" in s:
        return "UNIVERSAL"
    return "OTHER"


def trip_kind(pickup_location, dropoff_location):
    """ARRIVAL (airport pickup) / DEPARTURE (airport dropoff) / OTHER.

    Airport-to-airport is counted as ARRIVAL: the leg starts at a gate, so it inherits
    arrival dwell. `get_trip_type()` in the app has a hole here; this does not.
    """
    p, d = loc_bucket(pickup_location), loc_bucket(dropoff_location)
    if p in ("MCO", "SFB"):
        return "ARRIVAL"
    if d in ("MCO", "SFB"):
        return "DEPARTURE"
    return "OTHER"


# --------------------------------------------------------------------------
# stats
# --------------------------------------------------------------------------

def pct(values, p):
    """Linear-interpolated percentile. None on an empty list."""
    v = sorted(x for x in values if x is not None)
    if not v:
        return None
    if len(v) == 1:
        return float(v[0])
    k = (len(v) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(v) - 1)
    return float(v[f] + (v[c] - v[f]) * (k - f))


def describe(values, places=1):
    v = [x for x in values if x is not None]
    if not v:
        return {"n": 0}
    return {"n": len(v),
            "p10": round(pct(v, 10), places), "p25": round(pct(v, 25), places),
            "p50": round(pct(v, 50), places), "p75": round(pct(v, 75), places),
            "p90": round(pct(v, 90), places), "mean": round(sum(v) / len(v), places)}


def fmt_describe(label, values, width=34):
    d = describe(values)
    if not d["n"]:
        return f"{label:<{width}} n=0"
    return (f"{label:<{width}} n={d['n']:>6}  P10 {d['p10']:>7}  P25 {d['p25']:>7}  "
            f"P50 {d['p50']:>7}  P75 {d['p75']:>7}  P90 {d['p90']:>7}")


# --------------------------------------------------------------------------
# output
# --------------------------------------------------------------------------

def ensure_out():
    os.makedirs(OUT_DIR, exist_ok=True)
    return OUT_DIR


def write_csv(name, header, rows):
    import csv
    ensure_out()
    path = os.path.join(OUT_DIR, name)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)
    return path


def hdr(text, ch="="):
    print("\n" + ch * 78)
    print(text)
    print(ch * 78)


def sub(text):
    print("\n" + "-" * 78)
    print(text)
    print("-" * 78)


def preamble(script, purpose, horizon, assumptions=()):
    """Standard header. Every script prints its derived horizon and its assumptions."""
    hdr(f"{script} — {purpose}")
    print(f"snapshot     : {DB_PATH}")
    print(f"opened       : read-only (mode=ro)")
    print(f"data horizon : {horizon.pull_utc} UTC  =  {horizon.pull_local} local")
    print(f"'today'      : {horizon.today}   (derived, not assumed)")
    print(f"demand thru  : {horizon.last_demand_day}   actuals thru: {horizon.last_actuals_day}")
    print(f"first tap    : {horizon.first_tap_day}")
    if assumptions:
        print("\nassumptions:")
        for i, a in enumerate(assumptions, 1):
            print(f"  A{i}. {a}")
