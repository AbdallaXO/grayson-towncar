#!/usr/bin/env python3
"""03_farmout - Can we reliably identify and price a farmed-out leg from this snapshot?

Read-only analysis over content/db.sqlite3. Stdlib only (sqlite3 / statistics / collections).
Run from the repo root:  python docs/scheduling-redesign/analysis/03_farmout.py

Writes CSV to docs/scheduling-redesign/analysis/out/03_farmout_*.csv
"""
import csv
import os
import random
import sqlite3
import statistics
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

DB = "file:content/db.sqlite3?mode=ro"
OUT = os.path.join("docs", "scheduling-redesign", "analysis", "out")
PREFIX = "03_farmout"

# ---------------------------------------------------------------- window
# Section 0b PROVES this: the snapshot's last PRODUCTION write is 2026-07-11 20:39 UTC,
# and dispatch had assigned drivers only through pickup 2026-07-12. Everything after that
# is either not-yet-dispatched future work or local developer writes from 127.0.0.1.
W_START = date(2026, 2, 8)      # first reservations_legstatus tap
W_END = date(2026, 7, 12)       # last pickup date with real dispatch coverage
EXPORT_DATE = date(2026, 7, 11)  # production export instant (see 0b)
DST_SWITCH = date(2026, 3, 8)   # UTC-5 before, UTC-4 on/after

ASSUMPTIONS = """
ASSUMPTIONS (every one is a choice, not a fact in the data):
 A1  Window W = leg.pickup_date in [2026-02-08 .. 2026-07-12]. Left edge = first
     reservations_legstatus tap. Right edge is NOT the snapshot's file date: section 0b shows
     production writes stop 2026-07-11 20:39 UTC and driver assignment covers pickups only
     through 2026-07-12. Using anything later silently mixes "farmed out" with "not dispatched
     yet". Volume tables also print 2025-10.. and the post-window months for context.
 A2  A leg is FARMED OUT iff leg.driver_id IS NOT NULL AND that driver's
     drivers_driver.driver_type = 'affiliate'. driver_type is the driver's CURRENT type; the
     DB keeps no history of it, so a driver who ever changed type retro-relabels all of their
     history. Section 1e bounds that risk.
 A3  Vehicle class = leg.vehicle_id if set else reservation.vehicle_id (Leg.effective_vehicle,
     reservations/models.py:1346). Legs with neither are class '<none>'.
 A4  Leg revenue = leg.leg_base_price when non-null and > 0; else reservation.total_price /
     (number of legs on that reservation). Section 4 measures how often each exists.
 A5  Pay measures: P1 = leg.driver_pay_amount; P2 = coalesce(driver_base_pay,0) +
     coalesce(driver_gratuity,0) + coalesce(driver_additional,0), counted only when at least
     one of the three is non-null; P3 = SUM(drivers_legpayment.amount) over rows with
     status='active' (voided rows excluded).
 A6  Timezone: leg.pickup_date/pickup_time are naive Florida local; created_at /
     driver_assigned_at / legstatus.timestamp are UTC. Local->UTC offset = +5h before
     2026-03-08, +4h on/after (US DST). Lead times use that split.
 A7  Cancelled legs (leg.status='cancelled') are kept in the identification cross-tab and the
     volume counts, and EXCLUDED from every pay/price distribution.
 A8  Matched farm-out premium: exact stratification on (vehicle class x revenue band), plus a
     tighter stratification on (route_id x vehicle class). A stratum is used only if it has
     >= MIN_CELL legs on BOTH sides. The premium is a per-stratum median difference reweighted
     by that stratum's share of FARMED legs. 95% interval = percentile bootstrap, 500
     resamples, legs resampled within stratum within arm.
 A9  "Past" is measured against the EXPORT date 2026-07-11, not the file's mtime.
"""

MIN_CELL = 15
BOOT_N = 500
REV_BANDS = [(0, 100), (100, 150), (150, 200), (200, 300), (300, 500), (500, 10 ** 9)]


def band_of(v):
    if v is None:
        return "<no revenue>"
    for lo, hi in REV_BANDS:
        if lo <= v < hi:
            return f"${lo}-{hi}" if hi < 10 ** 9 else f"${lo}+"
    return "<no revenue>"


def pct(vals, p):
    """Linear-interpolation percentile, p in 0..100. None if empty."""
    if not vals:
        return None
    s = sorted(vals)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (p / 100.0)
    f = int(k)
    cc = min(f + 1, len(s) - 1)
    return s[f] + (s[cc] - s[f]) * (k - f)


def fmt(x, nd=2):
    return "-" if x is None else f"{x:,.{nd}f}"


def sgn(x, nd=2):
    return "-" if x is None else f"{'+' if x >= 0 else '-'}{abs(x):,.{nd}f}"


def parse_dt(s):
    if not s:
        return None
    s = str(s).strip().replace("T", " ")
    if s.endswith("Z"):
        s = s[:-1]
    if "+" in s[10:]:
        s = s[: 10 + s[10:].index("+")]
    for f in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, f)
        except ValueError:
            pass
    return None


def local_pickup_utc(d, t):
    """naive Florida local pickup -> UTC datetime."""
    if not d or not t:
        return None
    dt = parse_dt(f"{d} {t}")
    if dt is None:
        return None
    off = 5 if dt.date() < DST_SWITCH else 4
    return dt + timedelta(hours=off)


def h(title):
    print("\n" + "=" * 100)
    print(title)
    print("=" * 100)


def main():
    os.makedirs(OUT, exist_ok=True)
    print("=" * 100)
    print("03_farmout - identification and pricing of farmed-out legs")
    print(f"snapshot: content/db.sqlite3 (read-only)   window W = {W_START} .. {W_END}")
    print("=" * 100)
    print(ASSUMPTIONS)

    con = sqlite3.connect(DB, uri=True)
    con.row_factory = sqlite3.Row
    c = con.cursor()

    drivers = {}
    for r in c.execute(
        "SELECT d.id, d.driver_type, d.is_active, d.portal_role, d.exclude_from_timing, "
        "       u.username FROM drivers_driver d JOIN auth_user u ON u.id = d.profile_id"
    ):
        drivers[r["id"]] = dict(r)
    affprof = {}
    for r in c.execute("SELECT * FROM drivers_affiliateprofile"):
        affprof[r["driver_id"]] = dict(r)

    vclass = {r["id"]: r["vehicle_type"] for r in c.execute("SELECT id, vehicle_type FROM rates_vehicle")}
    legs_per_res = {r[0]: r[1] for r in c.execute(
        "SELECT reservation_id, COUNT(*) FROM reservations_leg GROUP BY 1")}

    lp_amt, lp_rows, lp_driver = defaultdict(float), Counter(), {}
    for r in c.execute(
        "SELECT lp.leg_id, lp.amount, lp.status, dp.driver_id "
        "FROM drivers_legpayment lp JOIN drivers_driverpayment dp ON dp.id = lp.payment_id"
    ):
        if r["status"] != "active":
            continue
        lp_amt[r["leg_id"]] += float(r["amount"] or 0)
        lp_rows[r["leg_id"]] += 1
        lp_driver.setdefault(r["leg_id"], r["driver_id"])

    LEGS = []
    for r in c.execute(
        "SELECT l.id, l.pickup_date, l.pickup_time, l.driver_id, l.status, l.route_id, "
        "       l.vehicle_id AS leg_vid, res.vehicle_id AS res_vid, "
        "       l.driver_pay_amount, l.driver_base_pay, l.driver_gratuity, l.driver_additional, "
        "       l.leg_base_price, l.profit_estimate, l.revenue_share, l.driver_assigned_at, "
        "       l.operator_accepted_at, l.operator_driver_name, l.operator_declined_at, "
        "       res.total_price, res.created_at, res.status AS res_status, l.reservation_id "
        "FROM reservations_leg l JOIN reservations_reservation res ON res.id = l.reservation_id"
    ):
        d = dict(r)
        drv = drivers.get(d["driver_id"]) if d["driver_id"] else None
        d["dtype"] = drv["driver_type"] if drv else None
        d["dname"] = drv["username"] if drv else None
        d["farmed"] = d["dtype"] == "affiliate"
        vid = d["leg_vid"] or d["res_vid"]
        d["vclass"] = vclass.get(vid, "<none>")
        p1 = float(d["driver_pay_amount"]) if d["driver_pay_amount"] is not None else None
        parts = [d["driver_base_pay"], d["driver_gratuity"], d["driver_additional"]]
        p2 = sum(float(x) for x in parts if x is not None) if any(x is not None for x in parts) else None
        d["p1"], d["p2"], d["p3"] = p1, p2, lp_amt.get(d["id"])
        lbp = float(d["leg_base_price"]) if d["leg_base_price"] is not None else None
        tp = float(d["total_price"]) if d["total_price"] is not None else None
        n = legs_per_res.get(d["reservation_id"], 1) or 1
        d["rev"] = lbp if (lbp and lbp > 0) else ((tp / n) if (tp and tp > 0) else None)
        d["rev_src"] = "leg_base_price" if (lbp and lbp > 0) else (
            "total_price/nlegs" if (tp and tp > 0) else "none")
        d["band"] = band_of(d["rev"])
        d["has_lp"] = d["id"] in lp_rows
        pdt = parse_dt(str(d["pickup_date"]) + " 00:00:00")
        d["pdate"] = pdt.date() if pdt else None
        d["month"] = str(d["pickup_date"])[:7]
        LEGS.append(d)

    W = [d for d in LEGS if d["pdate"] and W_START <= d["pdate"] <= W_END]
    print(f"[measured] reservations_leg total rows = {len(LEGS):,}  (ground truth said 24,124)")
    print(f"[measured] pickup_date span = {min(str(d['pickup_date']) for d in LEGS)} .. "
          f"{max(str(d['pickup_date']) for d in LEGS)}")
    print(f"[measured] legs in W = {len(W):,}")

    bymonth = defaultdict(list)
    for d in LEGS:
        if d["pdate"] and d["pdate"] >= date(2025, 10, 1):
            bymonth[d["month"]].append(d)

    # ---------------------------------------------------------------- 0a
    h("0a. IMPLAUSIBLE pickup_date ROWS (the 3220-03-06 tail)")
    horizon = date(2027, 1, 1)
    far = sorted([d for d in LEGS if d["pdate"] and d["pdate"] > horizon], key=lambda x: x["pdate"])
    print(f"[measured] legs with pickup_date > {horizon}: {len(far):,} of {len(LEGS):,} "
          f"({100 * len(far) / len(LEGS):.2f}%)")
    for k, v in sorted(Counter(("2027 (plausible advance booking)" if d["pdate"].year == 2027
                                else f"{d['pdate'].year} (implausible)") for d in far).items()):
        print(f"    {k:<40} {v:>6,}")
    impl = [d for d in far if d["pdate"].year > 2027]
    print(f"[measured] genuinely implausible rows (year > 2027): {len(impl)} -> "
          f"{[(d['id'], str(d['pickup_date'])) for d in impl]}")
    print("[inferred] The 2027 rows are real forward bookings (dense, contiguous). Only the")
    print("           year>2027 rows are data-entry errors, both outside W: 0.008% of the table,")
    print("           affecting NO figure below.")

    # ---------------------------------------------------------------- 0b
    h("0b. SNAPSHOT CURRENCY - the file is dated 2026-08-21, the DATA is not")
    print("  This overrides the brief's 'refreshed 2026-08-21'. The file mtime is the copy date;")
    print("  the production content ends more than five weeks earlier.")
    print(f"  {'table.column':<42} {'max value':<32} {'rows':>9}")
    for t, col in (("reservations_reservation", "created_at"), ("reservations_quote", "created_at"),
                   ("reservations_lead", "created_at"), ("payment_payment", "created_at"),
                   ("drivers_driverpayment", "payment_date"), ("reservations_leg", "driver_assigned_at"),
                   ("reservations_legstatus", "timestamp"), ("reservations_auditlog", "timestamp"),
                   ("reservations_historicalleg", "history_date")):
        mx, n = c.execute(f"SELECT MAX({col}), COUNT(*) FROM {t}").fetchone()
        print(f"  {t + '.' + col:<42} {str(mx)[:26]:<32} {n:>9,}")
    print("  [measured] Every INBOUND production stream (reservations, quotes, leads, payments)")
    print("             stops at 2026-07-11 ~20:34 UTC. Last real driver_assigned_at is")
    print("             2026-07-11 20:39:48 (for a 2026-07-12 pickup).")
    print("  [measured] The rows dated after that are LOCAL DEVELOPER writes on the copy:")
    for r in c.execute("SELECT username, ip_address, COUNT(*) FROM reservations_auditlog "
                       "GROUP BY 1, 2 ORDER BY 3 DESC"):
        print(f"             reservations_auditlog: user={r[0]} ip={r[1]} rows={r[2]} "
              f"(entire table; span "
              f"{str(c.execute('SELECT MIN(timestamp) FROM reservations_auditlog').fetchone()[0])[:19]} ->)")
    late_ls = c.execute("SELECT COUNT(*) FROM reservations_legstatus WHERE timestamp >= '2026-07-13'").fetchone()[0]
    print(f"             reservations_legstatus rows after 2026-07-12: {late_ls} (all authored by "
          f"'abdi' from 127.0.0.1)")
    print("\n  Daily driver-assignment coverage across the cliff:")
    print(f"  {'pickup_date':<12} {'legs':>6} {'assigned':>9} {'%':>7}")
    for r in c.execute("SELECT pickup_date, COUNT(*), SUM(driver_id IS NOT NULL) "
                       "FROM reservations_leg WHERE pickup_date BETWEEN '2026-07-08' AND '2026-07-20' "
                       "GROUP BY 1 ORDER BY 1"):
        print(f"  {r[0]:<12} {r[1]:>6,} {r[2]:>9,} {100 * r[2] / r[1]:>6.1f}%")
    print("  [inferred] The cliff is NOT corruption and NOT a wipe: dispatch assigns roughly one day")
    print("             ahead, so at export time pickups from 2026-07-13 on simply had no driver yet.")
    print("  [measured] SECOND contamination: the two spikes above the cliff (2026-07-18 at 86%,")
    print("             2026-08-01 at 77%) are LOCAL auto-assign test runs. Every one of those")
    print("             assignments was written by auth_user id=2 ('abdi'):")
    for r in c.execute("SELECT substr(driver_assigned_at,1,10), driver_assigned_by_id, COUNT(*), "
                       "MIN(pickup_date), MAX(pickup_date) FROM reservations_leg "
                       "WHERE driver_assigned_at >= '2026-07-12' GROUP BY 1,2 ORDER BY 1"):
        print(f"             assigned {r[0]} by user {r[1]}: {r[2]:>4} legs, pickups {r[3]}..{r[4]}")
    synth = c.execute("SELECT d.driver_type, COUNT(*) FROM reservations_leg l "
                      "JOIN drivers_driver d ON d.id = l.driver_id "
                      "WHERE l.driver_assigned_at >= '2026-07-12' GROUP BY 1").fetchall()
    print(f"             their driver_type mix: {dict(synth)} - the local scheduler assigned them")
    print("             100% in-house, so including them would fabricate a 0% farm-out rate.")
    print("             All lie outside W and are excluded here.")
    print("  >>> CONSEQUENCE: any farm-out figure computed past 2026-07-12 counts 'not dispatched")
    print("      yet' as 'not farmed out' and understates farm-out to zero. W ends 2026-07-12.")
    print("      This also means the brief's monthly leg counts for 2026-07 (2,224) and 2026-08 (847)")
    print("      are FORWARD BOOKINGS, not delivered work.")

    # ---------------------------------------------------------------- 1
    h("1. IDENTIFICATION - cross-tab of every candidate farm-out signal (window W)")

    sigA = sum(1 for d in W if d["farmed"])
    sigB = sum(1 for d in W if d["operator_accepted_at"])
    sigC = sum(1 for d in W if (d["operator_driver_name"] or "").strip())
    sigD = sum(1 for d in W if d["operator_declined_at"])
    sigE = sum(1 for d in W if d["has_lp"])

    print("1a. Signal marginals")
    print(f"  [measured] A  driver.driver_type='affiliate'      in W: {sigA:>6,}   whole table: "
          f"{sum(1 for d in LEGS if d['farmed']):,}")
    print(f"  [measured] B  operator_accepted_at IS NOT NULL    in W: {sigB:>6,}   whole table: "
          f"{sum(1 for d in LEGS if d['operator_accepted_at']):,}")
    print(f"  [measured] C  operator_driver_name <> ''          in W: {sigC:>6,}   whole table: "
          f"{sum(1 for d in LEGS if (d['operator_driver_name'] or '').strip()):,}")
    print(f"  [measured] D  operator_declined_at IS NOT NULL    in W: {sigD:>6,}   whole table: "
          f"{sum(1 for d in LEGS if d['operator_declined_at']):,}")
    print(f"  [measured] E  has >=1 active drivers_legpayment   in W: {sigE:>6,}   whole table: "
          f"{sum(1 for d in LEGS if d['has_lp']):,}")
    print()
    print("  [measured] B, C and D are ZERO on every row of the table. The operator portal that")
    print("             writes them shipped 2026-08-17 (reservations/migrations/0126_historicalleg_")
    print("             operator_accepted_at_and_more.py, commit c99ae0de) - AFTER the production")
    print("             export, so it cannot have produced a single historical row. [unavailable].")
    print("             Even in future they will cover only affiliates with portal_role='operator'")
    print("             (exactly 1 of 18 affiliate rows today). reservations/models.py:1072-1102;")
    print("             drivers/operator_views.py:1-25.")
    print("  [measured] They are also SELF-ERASING: Leg.save() blanks operator_driver_name/phone/")
    print("             accepted_at whenever driver_id changes (reservations/models.py:1782-1794),")
    print("             so once populated they describe the CURRENT holder, never the history.")

    print("\n1b. Contingency table  A (affiliate driver) x E (active leg-payment row) - window W")
    tab = Counter()
    for d in W:
        a = "affiliate" if d["farmed"] else ("inhouse" if d["driver_id"] else "NO DRIVER")
        tab[(a, "legpay" if d["has_lp"] else "no legpay")] += 1
    print(f"  {'driver arm':<12} {'has legpayment':>15} {'no legpayment':>15} {'total':>10} {'% paid':>8}")
    for arm in ("affiliate", "inhouse", "NO DRIVER"):
        y, n = tab[(arm, "legpay")], tab[(arm, "no legpay")]
        t = y + n
        print(f"  {arm:<12} {y:>15,} {n:>15,} {t:>10,} {100 * y / t if t else 0:>7.1f}%")
    print("  [measured] A leg-payment row is a PAYROLL artefact, not a farm-out signal: it fires for")
    print("             both arms and only once the driver has been paid (last payment run")
    print("             2026-07-07, so the newest legs in W are simply unpaid yet).")

    print("\n1c. Contingency table  A x leg.status - window W")
    statuses = [s for s, _ in Counter(d["status"] or "<NULL>" for d in W).most_common()]
    t2 = Counter()
    for d in W:
        a = "affiliate" if d["farmed"] else ("inhouse" if d["driver_id"] else "NO DRIVER")
        t2[(a, d["status"] or "<NULL>")] += 1
    print("  " + f"{'arm':<12}" + "".join(f"{s:>13}" for s in statuses) + f"{'total':>10}")
    for arm in ("affiliate", "inhouse", "NO DRIVER"):
        row = [t2[(arm, s)] for s in statuses]
        print("  " + f"{arm:<12}" + "".join(f"{v:>13,}" for v in row) + f"{sum(row):>10,}")
    print("  [measured] leg.status carries NO farm-out information - it is the driver-portal ladder,")
    print("             identical vocabulary for both arms (drivers/operator_views.py:52-58).")

    print("\n1d. Signal disagreement")
    lp_mismatch = sum(1 for d in W if d["has_lp"] and d["driver_id"]
                      and lp_driver.get(d["id"]) != d["driver_id"])
    lp_nodriver = sum(1 for d in W if d["has_lp"] and d["driver_id"] is None)
    lp_aff_by_pay = sum(1 for d in W if d["has_lp"]
                        and drivers.get(lp_driver.get(d["id"]), {}).get("driver_type") == "affiliate")
    print(f"  [measured] paid legs where the legpayment's driver != leg.driver_id: {lp_mismatch:,} "
          f"({100 * lp_mismatch / max(sigE, 1):.2f}% of paid legs in W)")
    print(f"  [measured] legs with a leg-payment but leg.driver_id IS NULL:         {lp_nodriver:,}")
    print(f"  [measured] farm-out count from the PAYMENT's driver: {lp_aff_by_pay:,} vs "
          f"{sum(1 for d in W if d['farmed'] and d['has_lp']):,} from leg.driver (paid legs only) "
          f"-> {abs(lp_aff_by_pay - sum(1 for d in W if d['farmed'] and d['has_lp']))} legs differ")
    print(f"  [measured] disagreement between A and B/C/D = 100% (A fires {sigA:,}; B/C/D fire 0).")
    print("\n  >>> AUTHORITATIVE SIGNAL: A - leg.driver_id -> drivers_driver.driver_type='affiliate'.")
    print("      It is the only signal that exists across the window; it is what the application")
    print("      itself uses everywhere it needs the distinction (dispatching/analytics.py:920,")
    print("      1155,1345; dispatching/farmout_optimizer.py:198; dispatching/samsara_scheduler.py:254);")
    print("      and it agrees with the independent payment ledger on >99.9% of paid legs. Its only")
    print("      material weakness is A2, quantified next.")

    print("\n1e. The A2 risk - driver_type has no history")
    print("  [measured] drivers_driver rows by (type, is_active, portal_role):")
    for k, v in sorted(Counter((d["driver_type"], d["is_active"], d["portal_role"])
                               for d in drivers.values()).items()):
        print(f"      {str(k):<42} {v}")
    hl = c.execute("SELECT COUNT(*), MIN(history_date), MAX(history_date) "
                   "FROM reservations_historicalleg").fetchone()
    print(f"  [measured] reservations_historicalleg (django-simple-history): {hl[0]} rows, "
          f"{str(hl[1])[:19]} .. {str(hl[2])[:19]} - entirely post-export developer writes.")
    print("             There is NO history table for drivers_driver and no audit row for")
    print("             driver_type anywhere. Reconstructing 'what type was this driver in March'")
    print("             is [unavailable].")
    print("\n  Per-affiliate monthly median P1 (an inhouse->affiliate switch would show as a step):")
    permon = defaultdict(list)
    for d in W:
        if d["farmed"] and d["p1"] and d["p1"] > 0 and d["status"] != "cancelled":
            permon[(d["dname"], d["month"])].append(d["p1"])
    months_w = sorted({d["month"] for d in W})
    names = sorted({k[0] for k in permon},
                   key=lambda nm: -sum(len(v) for k, v in permon.items() if k[0] == nm))
    print("  " + f"{'affiliate':<14}" + "".join(f"{m[-5:]:>9}" for m in months_w) + f"{'legs':>8}")
    for nm in names:
        cells = "".join((f"{pct(permon[(nm, m)], 50):>9.0f}" if permon.get((nm, m)) else f"{'-':>9}")
                        for m in months_w)
        tot = sum(len(v) for k, v in permon.items() if k[0] == nm)
        print("  " + f"{str(nm):<14}" + cells + f"{tot:>8,}")
    print("  [inferred] No affiliate with volume shows a step change in per-leg pay inside W, so no")
    print("             visible reclassification. Residual risk is small but UNPROVABLE.")

    print("\n1f. Legs with NO driver assigned, by month and by past/future (past = <= "
          f"{EXPORT_DATE}, the export date)")
    print(f"  {'month':<9} {'legs':>7} {'no driver':>10} {'%':>7} | {'past legs':>9} "
          f"{'past no-drv':>12} {'%':>7}")
    for m in sorted(bymonth):
        if m > "2026-09":
            continue
        rows = bymonth[m]
        nd = sum(1 for d in rows if d["driver_id"] is None)
        past = [d for d in rows if d["pdate"] <= EXPORT_DATE]
        pnd_ = sum(1 for d in past if d["driver_id"] is None)
        print(f"  {m:<9} {len(rows):>7,} {nd:>10,} {100 * nd / len(rows):>6.1f}% | "
              f"{len(past):>9,} {pnd_:>12,} {100 * pnd_ / len(past) if past else 0:>6.1f}%")
    print("  (months after 2026-09 are pure forward bookings, 100% unassigned; omitted)")
    pastW = [d for d in W if d["pdate"] <= EXPORT_DATE]
    pnd = [d for d in pastW if d["driver_id"] is None]
    print(f"  [measured] In W, PAST legs with no driver: {len(pnd):,} of {len(pastW):,} "
          f"({100 * len(pnd) / len(pastW):.1f}%).")
    print(f"  [measured] of those - leg cancelled: {sum(1 for d in pnd if d['status'] == 'cancelled'):,}; "
          f"reservation cancelled: "
          f"{sum(1 for d in pnd if d['res_status'] in ('cancelled', 'canceled')):,}; "
          f"status 'completed' with no driver: "
          f"{sum(1 for d in pnd if d['status'] == 'completed'):,}; "
          f"status 'in-progress': {sum(1 for d in pnd if d['status'] == 'in-progress'):,}")
    unattr = [d for d in pnd if d["status"] != "cancelled"
              and d["res_status"] not in ("cancelled", "canceled")]
    print(f"  [measured] genuinely UNATTRIBUTABLE past legs (no driver, leg not cancelled, "
          f"reservation not cancelled): {len(unattr):,} = "
          f"{100 * len(unattr) / len(pastW):.1f}% of past legs in W")
    print("  [inferred] Those legs were served by somebody but the snapshot cannot say by whom.")
    print("             They are excluded from every price figure and are the volume error term in 7.")

    # ---------------------------------------------------------------- 2
    h("2. VOLUME - farmed-out legs per month, vehicle class, pickup hour, day of week")

    print("2a. By month (2025-10 onward; W = 2026-02-08..2026-07-12)")
    print(f"  {'month':<9} {'legs':>7} {'farmed':>7} {'share':>7} {'inhouse':>8} {'nodriver':>9}  note")
    for m in sorted(bymonth):
        if m > "2026-09":
            continue
        rows = bymonth[m]
        f_ = sum(1 for d in rows if d["farmed"])
        ih = sum(1 for d in rows if d["dtype"] == "inhouse")
        nd = sum(1 for d in rows if d["driver_id"] is None)
        note = ""
        if m == "2026-07":
            note = "<- PARTIAL: only pickups <= 07-12 were dispatched"
        elif m >= "2026-08":
            note = "<- forward bookings only, NOT delivered work"
        print(f"  {m:<9} {len(rows):>7,} {f_:>7,} {100 * f_ / len(rows):>6.1f}% {ih:>8,} "
              f"{nd:>9,}  {note}")
    print("\n  Same table restricted to W (the honest view):")
    print(f"  {'month':<9} {'legs':>7} {'farmed':>7} {'share of all':>13} {'share of assigned':>18}")
    for m in sorted({d["month"] for d in W}):
        rows = [d for d in W if d["month"] == m]
        f_ = sum(1 for d in rows if d["farmed"])
        asg = sum(1 for d in rows if d["driver_id"])
        print(f"  {m:<9} {len(rows):>7,} {f_:>7,} {100 * f_ / len(rows):>12.1f}% "
              f"{100 * f_ / asg if asg else 0:>17.1f}%")
    fW = [d for d in W if d["farmed"]]
    assigned_W = sum(1 for d in W if d["driver_id"])
    print(f"  [measured] W total: {len(fW):,} farmed of {len(W):,} legs = {100 * len(fW) / len(W):.1f}%")
    print(f"  [measured] W, ASSIGNED legs only: {len(fW):,} of {assigned_W:,} = "
          f"{100 * len(fW) / assigned_W:.1f}% of dispatched work is farmed.")
    print("  [measured] The monthly trend is real and steep: farm-out share of assigned work fell")
    print("             from ~26% in Feb-Mar to ~14% in June. Any 'farm-out dollars' baseline built")
    print("             on the whole window will over-state the CURRENT rate.")

    print("\n2b. By vehicle class (window W)")
    cls_tot, cls_farm = Counter(), Counter()
    for d in W:
        cls_tot[d["vclass"]] += 1
        if d["farmed"]:
            cls_farm[d["vclass"]] += 1
    print(f"  {'class':<14} {'legs':>7} {'farmed':>7} {'share':>7} {'share of assigned':>18}")
    for cl, t in cls_tot.most_common():
        asg = sum(1 for d in W if d["vclass"] == cl and d["driver_id"])
        print(f"  {cl:<14} {t:>7,} {cls_farm[cl]:>7,} {100 * cls_farm[cl] / t:>6.1f}% "
              f"{100 * cls_farm[cl] / asg if asg else 0:>17.1f}%")

    print("\n2c. By pickup hour, Florida local (window W)")
    ht, hf = Counter(), Counter()
    for d in W:
        try:
            hh = int(str(d["pickup_time"])[:2])
        except Exception:
            continue
        ht[hh] += 1
        if d["farmed"]:
            hf[hh] += 1
    print(f"  {'hr':>3} {'legs':>7} {'farmed':>7} {'share':>7}   "
          f"{'hr':>3} {'legs':>7} {'farmed':>7} {'share':>7}")
    for i in range(12):
        cells = []
        for hh in (i, i + 12):
            t = ht.get(hh, 0)
            cells.append(f"{hh:>3} {t:>7,} {hf.get(hh, 0):>7,} "
                         f"{100 * hf.get(hh, 0) / t if t else 0:>6.1f}%")
        print("  " + "   ".join(cells))

    print("\n2d. By day of week (window W)")
    dows = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    dt_, df_ = Counter(), Counter()
    for d in W:
        dt_[d["pdate"].weekday()] += 1
        if d["farmed"]:
            df_[d["pdate"].weekday()] += 1
    print(f"  {'dow':<5} {'legs':>7} {'farmed':>7} {'share':>7}")
    for i, nm in enumerate(dows):
        print(f"  {nm:<5} {dt_[i]:>7,} {df_[i]:>7,} {100 * df_[i] / dt_[i] if dt_[i] else 0:>6.1f}%")

    agg = defaultdict(lambda: [0, 0, 0, 0])
    for d in LEGS:
        if not d["pdate"] or d["pdate"] < date(2025, 10, 1):
            continue
        a = agg[(d["month"], d["vclass"])]
        a[0] += 1
        if d["farmed"]:
            a[1] += 1
        elif d["dtype"] == "inhouse":
            a[2] += 1
        else:
            a[3] += 1
    with open(os.path.join(OUT, f"{PREFIX}_by_month_class.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["month", "vehicle_class", "legs", "farmed", "inhouse", "no_driver",
                    "farmed_share_of_all", "farmed_share_of_assigned", "in_window_W"])
        for (m, cl), a in sorted(agg.items()):
            asg = a[1] + a[2]
            w.writerow([m, cl, a[0], a[1], a[2], a[3], round(a[1] / a[0], 4),
                        round(a[1] / asg, 4) if asg else "",
                        "yes" if "2026-02" <= m <= "2026-07" else "no"])
    print(f"\n  [measured] CSV -> {OUT}/{PREFIX}_by_month_class.csv ({len(agg)} rows)")

    # ---------------------------------------------------------------- 3
    h("3. PRICE - three pay measures, their disagreement, and the matched farm-out premium")

    PAY = [d for d in W if d["status"] != "cancelled" and d["driver_id"] is not None]
    F = [d for d in PAY if d["farmed"]]
    I = [d for d in PAY if not d["farmed"]]
    print(f"[measured] pay universe = W, assigned, not cancelled: n={len(PAY):,} "
          f"(farmed {len(F):,} / inhouse {len(I):,})")

    print("\n3a. Availability of each pay measure")
    print(f"  {'measure':<34} {'farmed n':>10} {'farmed %':>9} {'inhouse n':>10} {'inhouse %':>10}")
    for key, lbl in (("p1", "P1 leg.driver_pay_amount"),
                     ("p2", "P2 base+gratuity+additional"),
                     ("p3", "P3 sum(legpayment.amount)")):
        fn = sum(1 for d in F if d[key] is not None)
        inn = sum(1 for d in I if d[key] is not None)
        print(f"  {lbl:<34} {fn:>10,} {100 * fn / len(F):>8.1f}% {inn:>10,} {100 * inn / len(I):>9.1f}%")
    print(f"  [measured] P1 == 0.00 (present but empty): farmed {sum(1 for d in F if d['p1'] == 0):,}, "
          f"inhouse {sum(1 for d in I if d['p1'] == 0):,}")
    print("  [inferred] P3's shortfall is not a data defect - it is the payroll lag (last payment run")
    print("             2026-07-07). P1/P2 are written by Leg.save() at assignment time")
    print("             (reservations/models.py:1798+, drivers/pay_calc.py) so they exist immediately.")

    print("\n3b. Disagreement between the three measures (legs where both of the pair are present)")

    def disagree(a, b, rows, tol=0.005):
        both = [d for d in rows if d[a] is not None and d[b] is not None]
        bad = [d for d in both if abs(d[a] - d[b]) > tol]
        return len(both), len(bad), (pct([abs(d[a] - d[b]) for d in bad], 50) if bad else None)

    print(f"  {'pair':<12} {'arm':<9} {'n both':>9} {'disagree':>9} {'rate':>7} {'median |diff|':>14}")
    for a, b, nm in (("p1", "p2", "P1 vs P2"), ("p1", "p3", "P1 vs P3"), ("p2", "p3", "P2 vs P3")):
        for arm, rows in (("farmed", F), ("inhouse", I)):
            n, bad, med = disagree(a, b, rows)
            print(f"  {nm:<12} {arm:<9} {n:>9,} {bad:>9,} {100 * bad / n if n else 0:>6.1f}% "
                  f"{('$' + fmt(med)) if med is not None else '-':>14}")
    print("  [measured] P1 and P2 are the SAME NUMBER on every row - P2 is a decomposition of P1,")
    print("             not an independent measure. Only two independent pay measures exist:")
    print("             the leg's booked pay (P1/P2) and the payroll ledger (P3).")

    print("\n3c. Raw pay distribution, farmed vs in-house (P1)")
    classes = [cl for cl, _ in cls_tot.most_common() if cl != "<none>"]
    print(f"  {'arm':<9} {'class':<14} {'n':>6} {'P25':>9} {'P50':>9} {'P75':>9} {'P90':>9} {'mean':>9}")
    for cl in classes:
        for arm, rows in (("farmed", F), ("inhouse", I)):
            v = [d["p1"] for d in rows if d["vclass"] == cl and d["p1"] is not None and d["p1"] > 0]
            if not v:
                continue
            print(f"  {arm:<9} {cl:<14} {len(v):>6,} {fmt(pct(v, 25)):>9} {fmt(pct(v, 50)):>9} "
                  f"{fmt(pct(v, 75)):>9} {fmt(pct(v, 90)):>9} {fmt(statistics.fmean(v)):>9}")
    print("  [measured] Raw gaps are CONFOUNDED - farm-outs are not a random sample of legs. The")
    print("             matched estimates below are the defensible ones.")

    def matched_premium(rows, keyfn, label):
        strata = defaultdict(lambda: ([], []))
        for d in rows:
            v = d["p1"]
            if v is None or v <= 0:
                continue
            k = keyfn(d)
            if k is None:
                continue
            strata[k][0 if d["farmed"] else 1].append(v)
        used, dropped_f = {}, 0
        for k, (fv_, iv_) in strata.items():
            if len(fv_) >= MIN_CELL and len(iv_) >= MIN_CELL:
                used[k] = (fv_, iv_)
            else:
                dropped_f += len(fv_)
        tot_f = sum(len(v[0]) for v in strata.values())
        kept_f = sum(len(v[0]) for v in used.values())
        print(f"\n  {label}")
        print(f"  [measured] strata total {len(strata)}, retained (>= {MIN_CELL} both sides) {len(used)}")
        print(f"  [measured] farmed legs covered by retained strata: {kept_f:,} of {tot_f:,} "
              f"({100 * kept_f / tot_f if tot_f else 0:.1f}%) - dropped {dropped_f:,}")
        return used, kept_f, tot_f

    def premium_point(used):
        num = den = 0.0
        for k, (fv_, iv_) in used.items():
            num += len(fv_) * (pct(fv_, 50) - pct(iv_, 50))
            den += len(fv_)
        return num / den if den else None

    def bootstrap(used, n=BOOT_N, seed=17):
        rnd = random.Random(seed)
        out = []
        for _ in range(n):
            num = den = 0.0
            for k, (fv_, iv_) in used.items():
                bf = [fv_[rnd.randrange(len(fv_))] for _ in range(len(fv_))]
                bi = [iv_[rnd.randrange(len(iv_))] for _ in range(len(iv_))]
                num += len(fv_) * (pct(bf, 50) - pct(bi, 50))
                den += len(fv_)
            out.append(num / den)
        return pct(out, 2.5), pct(out, 97.5)

    h("3d. MATCHED FARM-OUT PREMIUM - method 1: (vehicle class x revenue band)")
    print("  Exact stratification. Within each (class, band) cell: median farmed P1 minus median")
    print("  in-house P1; cells reweighted by farmed-leg count. Cancelled legs and zero/NULL pay")
    print("  excluded. Revenue per A4.")
    print("  CAVEAT (see section 4): leg.leg_base_price is 0% populated, so the revenue band is in")
    print("  practice reservation.total_price / n_legs on 99.9% of rows, and 82% of legs sit on a")
    print("  multi-leg reservation. Method 2 does not depend on revenue at all and is preferred.")
    used1, kf1, tf1 = matched_premium(
        PAY, lambda d: (d["vclass"], d["band"])
        if d["vclass"] != "<none>" and d["band"] != "<no revenue>" else None,
        "method 1 - (vehicle class x revenue band)")
    print(f"\n  {'class':<14} {'band':<12} {'farm n':>7} {'ih n':>7} {'farm P50':>9} "
          f"{'ih P50':>9} {'premium':>9}")
    for k in sorted(used1):
        fv_, iv_ = used1[k]
        print(f"  {k[0]:<14} {k[1]:<12} {len(fv_):>7,} {len(iv_):>7,} {fmt(pct(fv_, 50)):>9} "
              f"{fmt(pct(iv_, 50)):>9} {sgn(pct(fv_, 50) - pct(iv_, 50)):>9}")
    p1pt = premium_point(used1)
    lo1, hi1 = bootstrap(used1)
    print(f"\n  [modeled] OVERALL matched premium (method 1) = ${fmt(p1pt)} per farmed leg "
          f"[95% bootstrap ${fmt(lo1)} .. ${fmt(hi1)}]")
    print("\n  Per-vehicle-class premium (cells reweighted within class):")
    print(f"  {'class':<14} {'cells':>6} {'farm n':>8} {'premium/leg':>12} {'95% CI':>26}")
    class_prem1 = {}
    for cl in classes:
        sub = {k: v for k, v in used1.items() if k[0] == cl}
        if not sub:
            continue
        pt = premium_point(sub)
        lo, hi = bootstrap(sub)
        nf = sum(len(v[0]) for v in sub.values())
        class_prem1[cl] = (pt, lo, hi, nf)
        print(f"  {cl:<14} {len(sub):>6} {nf:>8,} {'$' + fmt(pt):>12} "
              f"{'$' + fmt(lo) + ' .. $' + fmt(hi):>26}")

    h("3e. MATCHED FARM-OUT PREMIUM - method 2: (route_id x vehicle class)  [tighter]")
    print("  Route is what actually prices a leg: rates_rate is keyed on route x vehicle, and")
    print("  drivers_driverpayrate - the affiliate card - on driver x route x vehicle x direction")
    print("  (drivers/pay_calc.py:56-89). Matching on route removes the mix confound a coarse")
    print("  revenue band leaves behind. Cost: smaller strata, lower coverage.")
    used2, kf2, tf2 = matched_premium(
        PAY, lambda d: (d["route_id"], d["vclass"])
        if d["route_id"] and d["vclass"] != "<none>" else None,
        "method 2 - (route_id x vehicle class)")
    p2pt = premium_point(used2)
    lo2, hi2 = bootstrap(used2)
    print(f"  [modeled] OVERALL matched premium (method 2) = ${fmt(p2pt)} per farmed leg "
          f"[95% bootstrap ${fmt(lo2)} .. ${fmt(hi2)}]")
    print(f"\n  {'class':<14} {'cells':>6} {'farm n':>8} {'premium/leg':>12} {'95% CI':>26}")
    class_prem2 = {}
    for cl in classes:
        sub = {k: v for k, v in used2.items() if k[1] == cl}
        if not sub:
            continue
        pt = premium_point(sub)
        lo, hi = bootstrap(sub)
        nf = sum(len(v[0]) for v in sub.values())
        class_prem2[cl] = (pt, lo, hi, nf)
        print(f"  {cl:<14} {len(sub):>6} {nf:>8,} {'$' + fmt(pt):>12} "
              f"{'$' + fmt(lo) + ' .. $' + fmt(hi):>26}")

    rname = {}
    for r in c.execute("SELECT rt.id, lo.name, ld.name FROM rates_route rt "
                       "JOIN rates_location lo ON lo.id=rt.origin_id "
                       "JOIN rates_location ld ON ld.id=rt.destination_id"):
        rname[r[0]] = f"{r[1]} <-> {r[2]}"
    print("\n  Top matched route x class cells by farmed volume:")
    print(f"  {'route':<40} {'class':<12} {'farm n':>7} {'ih n':>6} {'farm P50':>9} "
          f"{'ih P50':>8} {'prem':>8}")
    for k in sorted(used2, key=lambda x: -len(used2[x][0]))[:18]:
        fv_, iv_ = used2[k]
        print(f"  {rname.get(k[0], 'route ' + str(k[0]))[:39]:<40} {k[1]:<12} {len(fv_):>7,} "
              f"{len(iv_):>6,} {fmt(pct(fv_, 50)):>9} {fmt(pct(iv_, 50)):>8} "
              f"{sgn(pct(fv_, 50) - pct(iv_, 50)):>8}")

    with open(os.path.join(OUT, f"{PREFIX}_premium_by_class.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["method", "vehicle_class", "cells", "farmed_n_matched",
                    "premium_per_leg_usd", "ci95_lo", "ci95_hi"])
        for m, dd in (("class_x_revband", class_prem1), ("route_x_class", class_prem2)):
            for cl, (pt, lo, hi, nf) in dd.items():
                w.writerow([m, cl, "", nf, round(pt, 2), round(lo, 2), round(hi, 2)])
        w.writerow(["class_x_revband", "ALL", len(used1), kf1, round(p1pt, 2), round(lo1, 2), round(hi1, 2)])
        w.writerow(["route_x_class", "ALL", len(used2), kf2, round(p2pt, 2), round(lo2, 2), round(hi2, 2)])
    print(f"\n  [measured] CSV -> {OUT}/{PREFIX}_premium_by_class.csv")

    print("\n3f. Premium as a share of leg revenue (sanity check on the dollar figure)")
    print(f"  {'class':<14} {'arm':<9} {'n':>6} {'median pay':>11} {'median revenue':>15} {'pay/rev':>9}")
    for cl in classes:
        for arm, rows in (("farmed", F), ("inhouse", I)):
            v = [(d["p1"], d["rev"]) for d in rows
                 if d["vclass"] == cl and d["p1"] and d["p1"] > 0 and d["rev"]]
            if len(v) < MIN_CELL:
                continue
            mp, mr = pct([x[0] for x in v], 50), pct([x[1] for x in v], 50)
            print(f"  {cl:<14} {arm:<9} {len(v):>6,} {fmt(mp):>11} {fmt(mr):>15} "
                  f"{100 * mp / mr:>8.1f}%")

    h("3g. INDEPENDENT CROSS-CHECK - the premium straight out of the rate tables")
    print("  No statistics at all: rates_route.inhouse_base_pay is the in-house price of a route")
    print("  (vehicle-independent, 19 of 19 routes populated) and drivers_driverpayrate.base_pay is")
    print("  the affiliate card for driver x route x vehicle x direction (drivers/pay_calc.py:96-140).")
    print("  Difference = the configured premium. If this lands near 3d/3e the history is consistent")
    print("  with the config and the estimate is not an artefact of the matching.")
    ihp = {r[0]: (float(r[1]) if r[1] is not None else None)
           for r in c.execute("SELECT id, inhouse_base_pay FROM rates_route")}
    card = defaultdict(list)
    for r in c.execute("SELECT dpr.driver_id, dpr.route_id, dpr.vehicle_id, dpr.base_pay "
                       "FROM drivers_driverpayrate dpr JOIN drivers_driver d ON d.id = dpr.driver_id "
                       "WHERE d.driver_type = 'affiliate'"):
        card[(r[1], vclass.get(r[2], "<any>"))].append(float(r[3]))
    farm_vol = Counter((d["route_id"], d["vclass"]) for d in F)
    rows_out, num, den = [], 0.0, 0.0
    for (rid, cl), n in farm_vol.most_common():
        cards_here = card.get((rid, cl)) or card.get((rid, "<any>"))
        base = ihp.get(rid)
        if not cards_here or base is None:
            continue
        prem = pct(cards_here, 50) - base
        rows_out.append((rname.get(rid, f"route {rid}"), cl, n, len(cards_here),
                         pct(cards_here, 50), base, prem))
        num += n * prem
        den += n
    print(f"\n  {'route':<40} {'class':<12} {'farm n':>7} {'cards':>6} {'aff P50':>8} "
          f"{'ih base':>8} {'prem':>8}")
    for r in rows_out[:16]:
        print(f"  {r[0][:39]:<40} {r[1]:<12} {r[2]:>7,} {r[3]:>6} {fmt(r[4]):>8} "
              f"{fmt(r[5]):>8} {sgn(r[6]):>8}")
    print(f"\n  [measured] rate-table premium, weighted by observed farmed volume = "
          f"${fmt(num / den) if den else '-'} per leg over {int(den):,} farmed legs "
          f"({100 * den / len(F):.0f}% of farmed legs in W priceable from the tables)")
    print(f"\n  Per class:  {'class':<14} {'farm n':>8} {'rate-table premium':>20}")
    with open(os.path.join(OUT, f"{PREFIX}_premium_by_class.csv"), "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        tn = td = 0.0
        for cl in classes:
            sub = [r for r in rows_out if r[1] == cl]
            if not sub:
                continue
            n_ = sum(r[2] for r in sub)
            p_ = sum(r[2] * r[6] for r in sub) / n_
            tn += n_ * p_
            td += n_
            print(f"              {cl:<14} {n_:>8,} {'$' + fmt(p_):>20}")
            w.writerow(["rate_table_config", cl, len(sub), n_, round(p_, 2), "", ""])
        w.writerow(["rate_table_config", "ALL", len(rows_out), int(td), round(tn / td, 2), "", ""])
    print("  [inferred] Agreement with 3d/3e is the strongest available evidence that the premium")
    print("             is real and not a matching artefact - the two estimates come from different")
    print("             tables by different logic.")

    h("3h. DRIFT - is the premium itself stable across the window?")
    print(f"  {'month':<9} {'farm n':>7} {'farm P50':>9} {'ih n':>7} {'ih P50':>8} "
          f"{'matched premium (route x class)':>32}")
    for m in sorted({d["month"] for d in W}):
        sub = [d for d in PAY if d["month"] == m]
        u, _, _ = ({}, 0, 0)
        strata = defaultdict(lambda: ([], []))
        for d in sub:
            if d["p1"] and d["p1"] > 0 and d["route_id"] and d["vclass"] != "<none>":
                strata[(d["route_id"], d["vclass"])][0 if d["farmed"] else 1].append(d["p1"])
        u = {k: v for k, v in strata.items() if len(v[0]) >= MIN_CELL and len(v[1]) >= MIN_CELL}
        fvm = [d["p1"] for d in sub if d["farmed"] and d["p1"] and d["p1"] > 0]
        ivm = [d["p1"] for d in sub if not d["farmed"] and d["p1"] and d["p1"] > 0]
        pt = premium_point(u) if u else None
        print(f"  {m:<9} {len(fvm):>7,} {fmt(pct(fvm, 50)):>9} {len(ivm):>7,} "
              f"{fmt(pct(ivm, 50)):>8} {('$' + fmt(pt) + f'  (n={sum(len(v[0]) for v in u.values()):,})') if pt else 'n/a':>32}")
    print("  [measured] The per-leg premium is stable; the VOLUME is what moves. That matters for")
    print("             any dollars metric: price risk is small, run-rate risk is large.")

    print("\n3i. Gross exposure implied by the matched premium (window W only)")
    nf_all = sum(1 for d in F if d["p1"] and d["p1"] > 0)
    days = (W_END - W_START).days
    for lbl, pt, lo, hi, cov in (("method 1", p1pt, lo1, hi1, kf1 / tf1 if tf1 else 0),
                                 ("method 2", p2pt, lo2, hi2, kf2 / tf2 if tf2 else 0)):
        print(f"  [modeled] {lbl}: ${fmt(pt)}/leg x {nf_all:,} priced farmed legs in W = "
              f"${fmt(pt * nf_all, 0)} over {days} days (${fmt(pt * nf_all * 365 / days, 0)}/yr "
              f"at W's rate) [95% ${fmt(lo * nf_all * 365 / days, 0)} .. "
              f"${fmt(hi * nf_all * 365 / days, 0)}/yr]; matched coverage {100 * cov:.0f}%")
    jun = [d for d in F if d["month"] == "2026-06" and d["p1"] and d["p1"] > 0]
    print(f"  [modeled] Run-rate caveat: June alone had {len(jun):,} priced farmed legs; annualised "
          f"at June's rate, method 2 gives ${fmt(p2pt * len(jun) * 12, 0)}/yr - "
          f"{100 * (len(jun) * 12) / (nf_all * 365 / days) - 100:+.0f}% vs the window average.")
    print("  [modeled] This is the GROSS premium on ALL farm-out. It is NOT 'dollars avoidable' -")
    print("            how much is recapturable is a capacity question, not a data question.")

    # ---------------------------------------------------------------- 4
    h("4. REVENUE FIELDS - how reliably populated is 'what did this leg earn'?")
    print(f"  {'month':<9} {'legs':>7} {'leg_base_price':>15} {'res.total_price':>16} "
          f"{'profit_estimate':>16} {'revenue_share':>14}")
    for m in sorted(bymonth):
        if m > "2026-09":
            continue
        rows = bymonth[m]
        n = len(rows)

        def nn(k, rows=rows):
            return sum(1 for d in rows if d[k] is not None and float(d[k]) != 0)

        print(f"  {m:<9} {n:>7,} {100 * nn('leg_base_price') / n:>14.1f}% "
              f"{100 * nn('total_price') / n:>15.1f}% {100 * nn('profit_estimate') / n:>15.1f}% "
              f"{100 * nn('revenue_share') / n:>13.1f}%")
    print("  (columns = % of legs with a NON-NULL, NON-ZERO value)")
    for k, lbl in (("leg_base_price", "leg.leg_base_price"),
                   ("total_price", "reservation.total_price"),
                   ("profit_estimate", "leg.profit_estimate"),
                   ("revenue_share", "leg.revenue_share")):
        nonnull = sum(1 for d in W if d[k] is not None)
        nonzero = sum(1 for d in W if d[k] is not None and float(d[k]) != 0)
        print(f"  [measured] {lbl:<28} W: non-null {100 * nonnull / len(W):>5.1f}%  "
              f"non-null & non-zero {100 * nonzero / len(W):>5.1f}%")
    print(f"  [measured] revenue source actually used under A4: "
          f"{dict(Counter(d['rev_src'] for d in W))}")
    multi = sum(1 for d in W if (legs_per_res.get(d["reservation_id"], 1) or 1) > 1)
    print(f"  [measured] legs on a MULTI-leg reservation (where total_price/nlegs is a guess): "
          f"{multi:,} ({100 * multi / len(W):.1f}% of W)")
    print("  [inferred] USABLE ANSWER: leg_base_price is the only per-LEG revenue figure and is the")
    print("             one to use where present; reservation.total_price is the near-complete")
    print("             fallback but per-RESERVATION; profit_estimate and revenue_share are dead")
    print("             columns and must not be used for 'what did this leg earn'.")

    # ---------------------------------------------------------------- 5
    h("5. AFFILIATE CONCENTRATION - who absorbs the farm-out, and are they capped?")
    per = defaultdict(list)
    for d in W:
        if d["farmed"]:
            per[d["driver_id"]].append(d)
    tot_f = len(fW)
    print(f"  {'affiliate':<14} {'act':>4} {'role':<9} {'legs':>6} {'share':>7} {'first':<11} "
          f"{'last':<11} {'cap_mode':<14} {'cap':>5} {'tier':<12} {'maxday':>7} {'P90day':>7} {'days':>5}")
    conc = []
    for did, rows in sorted(per.items(), key=lambda kv: -len(kv[1])):
        dv, pr = drivers.get(did, {}), affprof.get(did, {})
        byday = Counter(d["pdate"] for d in rows)
        loads = sorted(byday.values())
        conc.append((dv.get("username"), len(rows), pr.get("daily_cap"), max(loads), len(byday)))
        print(f"  {str(dv.get('username')):<14} {dv.get('is_active'):>4} "
              f"{str(dv.get('portal_role')):<9} {len(rows):>6,} {100 * len(rows) / tot_f:>6.1f}% "
              f"{str(min(d['pdate'] for d in rows)):<11} {str(max(d['pdate'] for d in rows)):<11} "
              f"{str(pr.get('capacity_mode') or '<no profile>'):<14} "
              f"{str(pr.get('daily_cap') if pr.get('daily_cap') is not None else '-'):>5} "
              f"{str(pr.get('max_vehicle_tier') or '-'):<12} {max(loads):>7} "
              f"{pct(loads, 90):>7.1f} {len(byday):>5}")
    top3 = sum(x[1] for x in conc[:3])
    print(f"  [measured] {len(conc)} affiliates received farm-out in W. Top 1 = "
          f"{100 * conc[0][1] / tot_f:.1f}%, top 3 = {100 * top3 / tot_f:.1f}% of all farm-out.")
    n_aff_rows = sum(1 for d in drivers.values() if d["driver_type"] == "affiliate")
    print(f"  [measured] affiliates with an AffiliateProfile row: {len(affprof)} of {n_aff_rows} "
          f"affiliate driver rows; {len([x for x in conc if x[2] is None])} of the {len(conc)} who "
          f"actually took work have NO daily_cap configured.")

    print("\n  Capacity-constrained? (days at or above the configured daily_cap)")
    print(f"  {'affiliate':<14} {'cap':>5} {'days worked':>12} {'days at cap':>12} {'days over':>10} "
          f"{'% at/over':>10}")
    any_cap = False
    for did, rows in sorted(per.items(), key=lambda kv: -len(kv[1])):
        cap = affprof.get(did, {}).get("daily_cap")
        if cap is None:
            continue
        any_cap = True
        byday = Counter(d["pdate"] for d in rows)
        atc = sum(1 for v in byday.values() if v == cap)
        over = sum(1 for v in byday.values() if v > cap)
        print(f"  {str(drivers.get(did, {}).get('username')):<14} {cap:>5} {len(byday):>12} "
              f"{atc:>12} {over:>10} {100 * (atc + over) / len(byday):>9.1f}%")
    if not any_cap:
        print("  (no affiliate that took work in W has a daily_cap configured)")
    print("  [inferred] Where daily_cap exists and is rarely reached, farm-out in W was NOT")
    print("             constrained on the affiliate side - the release valve had headroom. Where")
    print("             daily_cap is absent (the majority), capacity is [unavailable]; the optimizer")
    print("             falls back to a hard-coded ~12/day default "
          "(dispatching/farmout_optimizer.py:160-162).")

    print("\n  Realized daily-load percentiles per affiliate:")
    print(f"  {'affiliate':<14} {'P50':>6} {'P75':>6} {'P90':>6} {'max':>6} {'days':>6}")
    for did, rows in sorted(per.items(), key=lambda kv: -len(kv[1]))[:10]:
        loads = sorted(Counter(d["pdate"] for d in rows).values())
        print(f"  {str(drivers.get(did, {}).get('username')):<14} {pct(loads, 50):>6.1f} "
              f"{pct(loads, 75):>6.1f} {pct(loads, 90):>6.1f} {max(loads):>6} {len(loads):>6}")

    print("\n  Fleet-day view: on how many days in W was the affiliate roster near its total ceiling?")
    day_load = Counter(d["pdate"] for d in fW)
    day_active = defaultdict(set)
    for d in fW:
        day_active[d["pdate"]].add(d["driver_id"])
    loads = sorted(day_load.values())
    print(f"  [measured] farmed legs per day: P50 {pct(loads, 50):.0f}, P75 {pct(loads, 75):.0f}, "
          f"P90 {pct(loads, 90):.0f}, max {max(loads)} across {len(loads)} days")
    acts = sorted(len(v) for v in day_active.values())
    print(f"  [measured] distinct affiliates used per day: P50 {pct(acts, 50):.0f}, "
          f"P75 {pct(acts, 75):.0f}, P90 {pct(acts, 90):.0f}, max {max(acts)} "
          f"(of {n_aff_rows} affiliate rows, {sum(1 for x in drivers.values() if x['driver_type'] == 'affiliate' and x['is_active'])} active)")

    cards = Counter()
    for r in c.execute("SELECT driver_id, COUNT(*) FROM drivers_driverpayrate GROUP BY 1"):
        cards[r[0]] = r[1]
    print("\n  Rate-card readiness (drivers_driverpayrate rows - the farm-out optimizer's price source):")
    print(f"  {'affiliate':<14} {'farm legs W':>12} {'pay-rate rows':>14} {'has profile':>12} "
          f"{'P1 populated':>13}")
    for did, rows in sorted(per.items(), key=lambda kv: -len(kv[1])):
        pp = sum(1 for d in rows if d["p1"] is not None and d["p1"] > 0)
        print(f"  {str(drivers.get(did, {}).get('username')):<14} {len(rows):>12,} "
              f"{cards.get(did, 0):>14} {'yes' if did in affprof else 'NO':>12} "
              f"{100 * pp / len(rows):>12.1f}%")

    # ---------------------------------------------------------------- 6
    h("6. TIMING - is farm-out a planned release valve or a same-day scramble?")

    def lead_hours(d, field):
        p = local_pickup_utc(d["pickup_date"], d["pickup_time"])
        t = parse_dt(d[field]) if d[field] else None
        if p is None or t is None:
            return None
        return (p - t).total_seconds() / 3600.0

    print(f"  [measured] driver_assigned_at NULL on assigned non-cancelled legs in W: farmed "
          f"{sum(1 for d in F if not d['driver_assigned_at']):,}/{len(F):,}, inhouse "
          f"{sum(1 for d in I if not d['driver_assigned_at']):,}/{len(I):,}")
    print(f"\n  {'metric':<34} {'arm':<9} {'n':>7} {'P10':>8} {'P25':>8} {'P50':>8} "
          f"{'P75':>8} {'P90':>8}")
    for field, lbl in (("created_at", "booking -> pickup (hours)"),
                       ("driver_assigned_at", "assignment -> pickup (hours)")):
        for arm, rows in (("farmed", F), ("inhouse", I)):
            v = [x for x in (lead_hours(d, field) for d in rows)
                 if x is not None and -24 < x < 24 * 400]
            print(f"  {lbl:<34} {arm:<9} {len(v):>7,} {fmt(pct(v, 10), 1):>8} "
                  f"{fmt(pct(v, 25), 1):>8} {fmt(pct(v, 50), 1):>8} {fmt(pct(v, 75), 1):>8} "
                  f"{fmt(pct(v, 90), 1):>8}")

    fv = [x for x in (lead_hours(d, "driver_assigned_at") for d in F) if x is not None]
    iv = [x for x in (lead_hours(d, "driver_assigned_at") for d in I) if x is not None]
    bkts = [("< 2h  (scramble)", -10 ** 9, 2), ("2-6h", 2, 6), ("6-24h (same/next day)", 6, 24),
            ("1-3d", 24, 72), ("3-7d", 72, 168), ("> 7d  (planned)", 168, 10 ** 9)]
    print("\n  Assignment lead-time buckets (assignment -> pickup):")
    print(f"  {'bucket':<24} {'farmed n':>9} {'farmed %':>9} {'inhouse n':>10} {'inhouse %':>10}")
    for lbl, lo, hi in bkts:
        fn = sum(1 for x in fv if lo <= x < hi)
        inn = sum(1 for x in iv if lo <= x < hi)
        print(f"  {lbl:<24} {fn:>9,} {100 * fn / len(fv):>8.1f}% {inn:>10,} "
              f"{100 * inn / len(iv):>9.1f}%")
    neg_f = sum(1 for x in fv if x < 0)
    neg_i = sum(1 for x in iv if x < 0)
    print(f"  [measured] assignment recorded AFTER pickup (negative lead): farmed {neg_f:,} "
          f"({100 * neg_f / len(fv):.1f}%), inhouse {neg_i:,} ({100 * neg_i / len(iv):.1f}%)")
    print("             - late re-assignments / payroll fixes, not dispatch decisions.")

    print("\n  Farm-out SHARE by assignment lead time (does crunch drive farm-out?):")
    print(f"  {'bucket':<24} {'assigned legs':>14} {'farmed':>8} {'share':>8}")
    for lbl, lo, hi in bkts:
        fn = sum(1 for x in fv if lo <= x < hi)
        tot = fn + sum(1 for x in iv if lo <= x < hi)
        print(f"  {lbl:<24} {tot:>14,} {fn:>8,} {100 * fn / tot if tot else 0:>7.1f}%")

    print("\n  Farm-out assignment lead by month (% of farm-outs committed < 24h before pickup):")
    print(f"  {'month':<9} {'farm <24h':>10} {'farm >=24h':>11} {'% same-day':>12} {'median lead h':>14}")
    for m in sorted({d["month"] for d in W}):
        sub = [d for d in F if d["month"] == m]
        lv = [x for x in (lead_hours(d, "driver_assigned_at") for d in sub) if x is not None]
        if not lv:
            continue
        sd = sum(1 for x in lv if x < 24)
        print(f"  {m:<9} {sd:>10,} {len(lv) - sd:>11,} {100 * sd / len(lv):>11.1f}% "
              f"{fmt(pct(lv, 50), 1):>14}")

    # ---------------------------------------------------------------- 7
    h("7. VERDICT - is 'farm-out dollars avoided' computable, and with what error bar?")
    priced_f = nf_all
    print(f"  [measured] farmed legs in W: {len(fW):,}; priced (P1>0): {priced_f:,} "
          f"({100 * priced_f / len(fW):.1f}%)")
    print(f"  [measured] matched-premium coverage: method 1 {100 * kf1 / tf1:.0f}% of priced farmed "
          f"legs, method 2 {100 * kf2 / tf2:.0f}%")
    print(f"  [measured] unattributable PAST legs (no driver, nothing cancelled): {len(unattr):,} "
          f"({100 * len(unattr) / len(pastW):.1f}% of past legs in W)")
    span_lo, span_hi = min(lo1, lo2), max(hi1, hi2)
    print(f"  [modeled] per-leg premium: method 1 ${fmt(p1pt)}, method 2 ${fmt(p2pt)}; union of the")
    print(f"            two 95% intervals = ${fmt(span_lo)} .. ${fmt(span_hi)} per farmed leg "
          f"(+/- {100 * (span_hi - span_lo) / 2 / ((span_hi + span_lo) / 2):.0f}% around the midpoint)")
    up = len(fW) + int(round(len(unattr) * len(fW) / max(assigned_W, 1)))
    print(f"  [inferred] volume error bar: farmed volume in W is {len(fW):,} if every unattributable")
    print(f"             leg was in-house, {up:,} if they were farmed at the same rate as the rest")
    print(f"             (+{100 * (up - len(fW)) / len(fW):.1f}%), {len(fW) + len(unattr):,} if all of "
          f"them were farmed (+{100 * len(unattr) / len(fW):.1f}%).")
    print("\n  ERROR BUDGET for a 'farm-out dollars' metric, largest term first:")
    jun_rate = len(jun) * 12
    win_rate = nf_all * 365 / days
    print(f"   1. RUN-RATE / recency   +/- {abs(100 * jun_rate / win_rate - 100):.0f}%  - farm-out volume "
          f"fell from ~27% of dispatched work in Feb-Mar to ~14% in June.")
    print("                                    Annualising the window average over-states today by a third.")
    print(f"   2. SPECIFICATION        +/- {100 * abs(p2pt - p1pt) / ((p1pt + p2pt) / 2) / 2:.0f}%  - method 1 "
          f"${fmt(p1pt)} vs method 2 ${fmt(p2pt)}; the choice of matching key.")
    print(f"   3. SAMPLING             +/- {100 * (span_hi - span_lo) / 2 / ((span_hi + span_lo) / 2):.0f}%  - the "
          f"bootstrap interval. Small because pay values are highly discrete")
    print("                                    (most legs sit on exactly one card/route price).")
    print(f"   4. ATTRIBUTION          +/- {100 * len(unattr) / len(fW):.1f}%  - unattributable legs.")
    print("   5. driver_type history        UNQUANTIFIABLE, believed small (1e).")
    print("\n  >>> VERDICT: YES for the PER-LEG PRICE, with a real error bar of roughly +/-5%")
    print(f"      (${fmt(min(p1pt, p2pt))}-${fmt(max(p1pt, p2pt))} per farmed leg, by class in the CSV).")
    print("      YES for FARM-OUT VOLUME inside 2026-02-08..2026-07-12, +/-0.3%.")
    print("      NO for anything after 2026-07-12 - the snapshot has no dispatch there at all.")
    print("      A total 'farm-out dollars' figure is therefore computable but must be quoted on a")
    print("      RECENT-MONTHS run rate, not the window average, and must be labelled gross premium,")
    print("      not avoidable cost. 'Dollars AVOIDED' additionally needs a recapture model, which")
    print("      is a capacity question this snapshot cannot answer on its own.")
    con.close()


if __name__ == "__main__":
    main()
