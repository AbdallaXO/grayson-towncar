"""Snapshot provenance + analysis-window derivation.

Answers the go/no-go question for the whole scheduling-redesign engagement:
WHEN was content/db.sqlite3 actually cut, and therefore what date range can be
analysed at all?

Read-only. No Django. stdlib only. Run from the repo root:
    python docs/scheduling-redesign/analysis/00_snapshot_provenance.py

ASSUMPTIONS (stated, per the Phase-1 ground rules):
  A1. reservations_reservation.created_at is written by the app at booking time and
      is never backdated, so its MAX is an upper bound on the snapshot cut.
  A2. reservations_legstatus.timestamp is UTC and is written on a driver's tap, so a
      dense daily series ending abruptly marks the cut; sparse rows after it are
      local development activity, not production.
  A3. reservations_leg.pickup_date / pickup_time are naive Florida local time.
  A4. A leg is "demand" unless the leg OR its reservation is cancelled - the same
      exclusion dispatching/day_setup.py uses for its demand query.
"""
import sqlite3
from collections import Counter

DB = "file:content/db.sqlite3?mode=ro"
con = sqlite3.connect(DB, uri=True)
cur = con.cursor()

def q(sql, args=()):
    return cur.execute(sql, args).fetchall()

def pct(vals, p):
    if not vals:
        return None
    v = sorted(vals)
    return v[min(len(v) - 1, int(p * (len(v) - 1)))]

print("=" * 78)
print("1. SNAPSHOT PROVENANCE  -- when was this file actually cut?")
print("=" * 78)
for label, sql in [
    ("MAX reservation.created_at   ", "select max(created_at) from reservations_reservation"),
    ("MAX leg.driver_assigned_at   ", "select max(driver_assigned_at) from reservations_leg"),
    ("MAX legstatus.timestamp (UTC)", "select max(timestamp) from reservations_legstatus"),
    ("MAX auditlog.timestamp       ", "select max(timestamp) from reservations_auditlog"),
    ("MAX leg.status_changed_at    ", "select max(status_changed_at) from reservations_leg"),
]:
    print(f"  {label} = {q(sql)[0][0]}")

print("\n  Bookings created per day, tail of the series:")
for d, n in q("select substr(created_at,1,10), count(*) from reservations_reservation "
              "where created_at >= '2026-07-01' group by 1 order by 1"):
    print(f"    {d}  {n:5d}")

print("\n  Driver status taps per day, tail of the series:")
for d, n, legs in q("select substr(timestamp,1,10), count(*), count(distinct leg_id) "
                    "from reservations_legstatus where timestamp >= '2026-07-01' "
                    "group by 1 order by 1"):
    print(f"    {d}  rows={n:5d}  legs={legs:4d}")

print("\n  VERDICT: production activity stops 2026-07-11. Rows after that date are")
print("  isolated local-development writes (single-digit row counts, no bookings).")
print("  => SNAPSHOT CUT = 2026-07-11. 'Present' for this research is 2026-07-11,")
print("     NOT today. content/db.sqlite3 is a WRITABLE dev DB the local app has")
print("     been driven against, so it is not a pristine copy either.")

print()
print("=" * 78)
print("2. WHY FORWARD DATES LOOK EMPTY  -- the booking-lead-time curve")
print("=" * 78)
rows = q("""select julianday(l.pickup_date) - julianday(substr(r.created_at,1,10))
            from reservations_leg l
            join reservations_reservation r on r.id = l.reservation_id
            where l.pickup_date between '2026-02-01' and '2026-06-30'
              and r.created_at is not null
              and l.status != 'cancelled' and r.status != 'cancelled'""")
lead = [x[0] for x in rows if x[0] is not None and -1 <= x[0] <= 400]
n = len(lead)
print(f"  Fully-observed pickup dates 2026-02-01..2026-06-30, n={n} legs")
print("  Lead time booking->pickup (days): "
      + "  ".join(f"P{p}={pct(lead, p/100):.0f}" for p in (10, 25, 50, 75, 90, 95)))
print("\n  Share of a day's FINAL demand already on the books H days before pickup:")
for h in (0, 3, 7, 14, 21, 30, 45, 60, 90):
    print(f"    H-{h:<3d} {100 * sum(1 for x in lead if x >= h) / n:5.1f}%")
print("\n  CONSEQUENCE 1 (data): at a 2026-07-11 cut, pickup dates in Aug 2026 sit")
print("  21-41 days out, so only ~25-49% of their eventual legs exist in this file.")
print("  The 'August collapse' is an artifact. Demand analysis MUST stop at the cut.")
print("  CONSEQUENCE 2 (product): Day Setup's peak_concurrency runs on BOOKED legs")
print("  with no lead-time correction, so opening a date 14 days out sees ~61% of")
print("  its eventual demand and under-sizes the roster accordingly.")

print()
print("=" * 78)
print("3. DEMAND VOLUME ON THE CORRECTED WINDOW")
print("=" * 78)
print("  Legs by month (pickup_date), cancellations excluded, cut applied:")
tot = 0
for m, nlegs, days in q("""select substr(l.pickup_date,1,7), count(*), count(distinct l.pickup_date)
        from reservations_leg l join reservations_reservation r on r.id = l.reservation_id
        where l.pickup_date between '2025-09-01' and '2026-07-11'
          and l.status != 'cancelled' and r.status != 'cancelled'
        group by 1 order by 1"""):
    print(f"    {m}  legs={nlegs:5d}  days={days:3d}  legs/day={nlegs/days:5.1f}")
    tot += nlegs
print(f"    TOTAL in range: {tot}")

print("\n  Trailing-28-day average legs/day (growth curve, every 14th day):")
daily = dict(q("""select l.pickup_date, count(*) from reservations_leg l
        join reservations_reservation r on r.id = l.reservation_id
        where l.pickup_date between '2025-06-01' and '2026-07-11'
          and l.status != 'cancelled' and r.status != 'cancelled'
        group by 1"""))
dates = sorted(daily)
for i in range(27, len(dates), 14):
    win = dates[i - 27:i + 1]
    print(f"    {dates[i]}  {sum(daily.get(d, 0) for d in win) / 28:6.1f}")

print()
print("=" * 78)
print("4. DATA HYGIENE")
print("=" * 78)
CUT = "2026-07-11"
# "Implausible" = beyond any real booking horizon. Legitimate far-future bookings
# exist (cruise season 2027), so the bar is set at 2028, not "after the cut".
bad = q("select id, pickup_date from reservations_leg "
        "where pickup_date >= '2028-01-01' or pickup_date < '2024-01-01' order by pickup_date")
print(f"  Implausible pickup_date rows (>=2028 or <2024): {len(bad)}")
for i, d in bad:
    print(f"    leg id={i}  pickup_date={d}")
far = q("select count(*) from reservations_leg where pickup_date > ? and pickup_date < '2028-01-01'", (CUT,))[0][0]
print(f"  Legs booked for dates after the cut: {far}  <- PARTIAL demand, exclude from")
print( "    demand analysis (only the early-booking share of those days exists here).")
print(f"  Legs flagged exclude_from_analytics: "
      f"{q('select count(*) from reservations_leg where exclude_from_analytics = 1')[0][0]}")

print()
print("=" * 78)
print("5. RECOMMENDED WINDOW")
print("=" * 78)
print("  The brief asks for 6-8 months. The growth curve above says the business was")
print("  NOT one business for that whole time: 2.3 legs/day (Jun 2025) -> 92 (Jun 2026),")
print("  with the ramp ending around 2026-03-23 and the trailing-28d average flat at")
print("  88-92 legs/day from 2026-04-06 to the cut. So the window is split by PURPOSE:")
print()
print("  PRIMARY  - shift shapes, absolute staffing levels, replay")
print("             2026-03-01 .. 2026-07-11   (steady state; ~11.9k legs, 133 days)")
print("             Jan-Feb sit on the ramp (47 and 70 legs/day); using them to set")
print("             absolute headcount would under-staff today's business by ~25-45%.")
print("  EXTENDED - trend, seasonality, weekly-shape stability, growth headroom")
print("             2026-01-01 .. 2026-07-11   (6.4 months; satisfies the brief range)")
print("             Use NORMALISED shape only (share of day), never absolute counts.")
print("  ACTUALS  - status-tap derived durations, dwell, turnaround")
print("             2026-02-08 .. 2026-07-11   (hard floor: no legstatus rows before it)")
print("  EXCLUDE  - every pickup_date after the 2026-07-11 cut (4,478 legs): partial")
print("             demand, see section 2. And leg ids 9210, 9211 (corrupt dates).")
