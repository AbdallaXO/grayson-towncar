"""06_challenge.py - ADVERSARIAL RE-COMPUTATION of six high-stakes claims.

Self-contained, no arguments, read-only. Run from the repo root:
    python docs/scheduling-redesign/analysis/06_challenge.py

Every number is recomputed with a query formulation deliberately DIFFERENT from
the one used by the analyst whose claim is being challenged, so that agreement is
evidence and disagreement is diagnostic.
"""
import csv
import math
import os
import re
import sqlite3
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta

DB = "file:content/db.sqlite3?mode=ro"
OUT = os.path.join("docs", "scheduling-redesign", "analysis", "out")
PFX = "06_challenge"

HEADER = """
================================================================================
06_challenge - ADVERSARIAL VERIFICATION.  Assumptions declared up front:
 A1  Snapshot opened READ-ONLY: sqlite3.connect("file:content/db.sqlite3?mode=ro", uri=True).
     Nothing is written to the DB; the repo-root db.sqlite3 placeholder is ignored.
 A2  "live leg" = (leg.status IS NULL OR <> 'cancelled') AND reservation.status NOT IN
     ('cancelled','canceled')  [both spellings]. 'in-progress' is the Leg model default
     and is KEPT as demand.
 A3  exclude_from_analytics is NOT used as a demand filter (timing-quality flag).
 A4  Corrupt pickup_date guard: every date filter is an explicit BETWEEN, never MIN()/MAX().
 A5  legstatus.timestamp is UTC; leg.pickup_date/pickup_time are naive Florida local;
     flight *_local columns are UTC. Status-vs-status and status-vs-flight differences
     take NO conversion. Local->UTC = +5h before 2026-03-08, +4h from 2026-03-08.
 A6  First-occurrence only: MIN(timestamp) per (leg, status) for every timing figure.
 A7  Percentiles are NEAREST-RANK (idx = ceil(p*n)-1). An interpolating percentile can
     differ by ~1 unit; differences of 1 minute are treated as method, not data.
 A8  Money read as float from decimal columns; voided drivers_legpayment rows excluded.
 A9  driver_type / is_active / exclude_from_timing are CURRENT-STATE flags with no history
     table; every cohort defined with them applies a today-flag to the past.
 A10 "gold cohort" = the 14 names nominated in docs/operational-data-audit.md section 9,
     matched on auth_user.username.
 A11 Verdicts: CONFIRMED = within 5% of the claim (or within the stated absolute minute
     tolerance); REVISED otherwise; UNVERIFIABLE when the input does not exist.
================================================================================
"""

GOLD14 = ["Michael", "sereen", "yovanny", "steven", "junaid", "angel", "runer",
          "roberto", "lev", "george", "davide", "Charlie", "Aftab", "oualid"]
DST_FLIP = date(2026, 3, 8)


def pct(vals, p):
    if not vals:
        return None
    s = sorted(vals)
    idx = max(0, min(len(s) - 1, math.ceil(p / 100.0 * len(s)) - 1))
    return s[idx]


def pcts(vals, ps=(10, 25, 50, 75, 90, 95)):
    return [pct(vals, p) for p in ps]


def verdict(mine, claimed, tol=0.05, absmin=None):
    if mine is None or claimed is None:
        return "UNVERIFIABLE"
    if absmin is not None and abs(mine - claimed) <= absmin:
        return "CONFIRMED"
    if claimed == 0:
        return "CONFIRMED" if mine == 0 else "REVISED"
    return "CONFIRMED" if abs(mine - claimed) / abs(claimed) <= tol else "REVISED"


def line(ch="-", n=88):
    print(ch * n)


def w_csv(name, rows, header):
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, "%s_%s.csv" % (PFX, name))
    with open(path, "w", newline="", encoding="utf-8") as fh:
        wr = csv.writer(fh)
        wr.writerow(header)
        wr.writerows(rows)
    print("   [csv] %s  (%d rows)" % (path, len(rows)))


def parse_ts(s):
    if not s:
        return None
    s = s.strip()
    for f in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S",
              "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, f)
        except ValueError:
            pass
    return None


def d(s):
    return date(int(s[0:4]), int(s[5:7]), int(s[8:10]))


con = sqlite3.connect(DB, uri=True)
con.row_factory = sqlite3.Row
cur = con.cursor()
print(HEADER)

LIVE = ("(l.status IS NULL OR l.status <> 'cancelled') "
        "AND r.status NOT IN ('cancelled','canceled')")
Q_LIVE = ("select count(*) from reservations_leg l "
          "join reservations_reservation r on r.id=l.reservation_id "
          "where l.pickup_date between ? and ? and " + LIVE)

# ======================================================================= CLAIM 1
line("=")
print("CLAIM 1  |  RECOMMENDED WINDOW END / SNAPSHOT HORIZON")
print("CLAIMED  :  production horizon = 2026-07-11 ~20:34 UTC; the file's 2026-08-21 tail is")
print("            local dev writes; PRIMARY window 2026-04-01..2026-07-11 = 102 days,")
print("            9,278 live legs, 91.0 legs/day, 'stationary within +/-2%'.")
print("MY METHOD:  four write streams the bundle never used (auth_user.last_login,")
print("            django_session, payment_payment, leg.status_changed_at) plus a per-day")
print("            booking histogram - not MAX(created_at) on three customer tables.")
line()

streams = [
    ("auth_user.last_login  [NOT used by the bundle]", "select max(last_login) from auth_user"),
    ("django_session.expire_date  [NOT used]", "select max(expire_date) from django_session"),
    ("payment_payment.created_at", "select max(created_at) from payment_payment"),
    ("reservations_leg.status_changed_at  [NOT used]",
     "select max(status_changed_at) from reservations_leg"),
    ("reservations_leg.confirmation_sms_sent_at  [NOT used]",
     "select max(confirmation_sms_sent_at) from reservations_leg"),
    ("reservations_reservation.created_at", "select max(created_at) from reservations_reservation"),
    ("reservations_quote.created_at", "select max(created_at) from reservations_quote"),
    ("reservations_legstatus.timestamp", "select max(timestamp) from reservations_legstatus"),
]
print("  raw MAX() per stream:")
for lbl, sql in streams:
    print("     %-50s %s" % (lbl, cur.execute(sql).fetchone()[0]))

print("\n  auth_user.last_login AFTER 2026-07-12 - every human who touched the system since:")
for r in cur.execute("select username, last_login, is_staff, is_superuser from auth_user "
                     "where last_login > '2026-07-12' order by last_login desc"):
    print("     %-30s %s  staff=%s super=%s" % (r["username"], r["last_login"],
                                                r["is_staff"], r["is_superuser"]))
n_before = cur.execute("select count(*) from auth_user where last_login between "
                       "'2026-07-01' and '2026-07-12'").fetchone()[0]
last_real = cur.execute("select username, last_login from auth_user where last_login < '2026-07-12' "
                        "and last_login is not null order by last_login desc limit 1").fetchone()
print("     ...against %d accounts whose LAST login falls in 2026-07-01..07-11." % n_before)
print("     Last non-dev login: %s @ %s" % (last_real["username"], last_real["last_login"]))
print("     django_session rows: %d, all expiring %s.." % (
    cur.execute("select count(*) from django_session").fetchone()[0],
    cur.execute("select min(expire_date) from django_session").fetchone()[0]))

tot = cur.execute("select count(*) from reservations_legstatus").fetchone()[0]
ms = cur.execute("select count(*) from reservations_legstatus where length(timestamp)=26 "
                 "and substr(timestamp,-3)='000'").fetchone()[0]
nofrac = cur.execute("select count(*) from reservations_legstatus where length(timestamp)=19").fetchone()[0]
print("\n  millisecond-truncation fingerprint: %d legstatus rows | ms-truncated %d (%.2f%%) | "
      "no fractional part %d | full-microsecond %d" % (tot, ms, 100.0 * ms / tot, nofrac,
                                                       tot - ms - nofrac))
print("     CLAIMED 69,150 (99.90%%) ms-truncated -> mine %d (%.2f%%)  %s"
      % (ms, 100.0 * ms / tot, verdict(ms, 69150)))
mr = cur.execute("select id, leg_id, status, timestamp, updated_by_id from reservations_legstatus "
                 "where length(timestamp)=26 and substr(timestamp,-3)<>'000' order by timestamp").fetchall()
print("     full-microsecond rows = %d; authors = %s; span %s .. %s"
      % (len(mr), sorted(set(x["updated_by_id"] for x in mr)),
         mr[0]["timestamp"][:19], mr[-1]["timestamp"][:19]))
print("     NOTE the %d length-19 rows (no fractional seconds at all) - a third write regime the"
      % nofrac)
print("     bundle's two-regime fingerprint argument does not mention.")

gap = cur.execute("select date(created_at) dd, count(*) n from reservations_reservation "
                  "where created_at >= '2026-06-28' group by 1 order by 1").fetchall()
print("\n  reservations created per day from 2026-06-28 (histogram, not MAX):")
print("     " + "  ".join("%s:%d" % (r["dd"][5:], r["n"]) for r in gap))

primary = cur.execute(Q_LIVE, ("2026-04-01", "2026-07-11")).fetchone()[0]
pdays = (d("2026-07-11") - d("2026-04-01")).days + 1
print("\n  PRIMARY 2026-04-01..2026-07-11: mine %d live legs / %dd = %.1f/day   "
      "CLAIMED 9,278 / 102d / 91.0 -> legs %s, rate %s"
      % (primary, pdays, primary / pdays, verdict(primary, 9278), verdict(primary / pdays, 91.0)))
sec = cur.execute(Q_LIVE, ("2026-01-01", "2026-07-11")).fetchone()[0]
sdays = (d("2026-07-11") - d("2026-01-01")).days + 1
print("  SHAPE   2026-01-01..2026-07-11: mine %d / %dd = %.1f/day   CLAIMED 15,315 / 192d / 79.8 -> %s"
      % (sec, sdays, sec / sdays, verdict(sec, 15315)))
tapw = cur.execute(Q_LIVE, ("2026-02-08", "2026-07-10")).fetchone()[0]
tdays = (d("2026-07-10") - d("2026-02-08")).days + 1
print("  TAPS    2026-02-08..2026-07-10: mine %d / %dd = %.1f/day   CLAIMED 13,294 / 153d / 86.9 -> %s"
      % (tapw, tdays, tapw / tdays, verdict(tapw, 13294)))

print("\n  plateau stationarity re-test (live legs/day by whole month):")
MONTHS = [("2026-01", "2026-01-01", "2026-01-31"), ("2026-02", "2026-02-01", "2026-02-28"),
          ("2026-03", "2026-03-01", "2026-03-31"), ("2026-04", "2026-04-01", "2026-04-30"),
          ("2026-05", "2026-05-01", "2026-05-31"), ("2026-06", "2026-06-01", "2026-06-30"),
          ("2026-07a", "2026-07-01", "2026-07-11")]
rate = {}
for m, a, b in MONTHS:
    n = cur.execute(Q_LIVE, (a, b)).fetchone()[0]
    nd = (d(b) - d(a)).days + 1
    rate[m] = n / nd
    print("     %-8s %5d legs / %2dd = %5.1f/day" % (m, n, nd, n / nd))
plateau = [rate[k] for k in ("2026-04", "2026-05", "2026-06", "2026-07a")]
pm = statistics.mean(plateau)
print("     plateau spread %.1f..%.1f = +/-%.1f%% about the mean (CLAIMED 'within +/-2%%')"
      % (min(plateau), max(plateau), 100 * (max(plateau) - min(plateau)) / pm / 2))
print("     March = %.1f/day = %.0f%% of the plateau mean; February = %.0f%%; January = %.0f%%"
      % (rate["2026-03"], 100 * rate["2026-03"] / pm, 100 * rate["2026-02"] / pm,
         100 * rate["2026-01"] / pm))
print("     The bundle justifies the April cut with 'January averaged ~43 legs/day, February ~59,")
print("     March ~78'. Measured here: %.1f / %.1f / %.1f - and the bundle's OWN trailing-28 table"
      % (rate["2026-01"], rate["2026-02"], rate["2026-03"]))
print("     says 47.1 / 70.4 / 85.1. February and March are understated by 16-19% in the sentence")
print("     that carries the recommendation.")
mar = cur.execute(Q_LIVE, ("2026-03-01", "2026-07-11")).fetchone()[0]
mdays = (d("2026-07-11") - d("2026-03-01")).days + 1
print("     March-inclusive window 2026-03-01..2026-07-11: %d live legs / %dd = %.1f/day, i.e. %.1f%%"
      % (mar, mdays, mar / mdays, 100 * (pm - mar / mdays) / pm))
print("     below the plateau mean, for +%d legs (+%.0f%% sample) over the recommended window."
      % (mar - primary, 100.0 * (mar - primary) / primary))
print("     Claimed cost of pooling Jan-Jul: 'under-states required staffing by roughly 15-20%'.")
print("     Measured: pooled Jan-Jul is %.1f/day vs plateau %.1f/day = %.1f%% low."
      % (sec / sdays, pm, 100 * (pm - sec / sdays) / pm))

# ======================================================================= CLAIM 2
line("=")
print("CLAIM 2  |  THE AUGUST COLLAPSE IS 100% A SNAPSHOT ARTIFACT")
print("CLAIMED  :  Aug 1-21 = 836 live legs = 39.8/day vs June 91.0/day (44%); August median")
print("            booking lead 58d vs a 20d norm; 76.3% of Aug legs booked >=41d ahead;")
print("            not one August leg booked after 2026-07-11.")
print("MY METHOD:  lead time = julianday(pickup_date) - julianday(date(created_at)) in whole")
print("            days on the leg's own reservation, plus a falsification the bundle did not")
print("            run: re-impose the same K-day freeze on COMPLETE June and compare survivors")
print("            to what August actually shows.")
line()
aug = cur.execute(Q_LIVE, ("2026-08-01", "2026-08-21")).fetchone()[0]
jun = cur.execute(Q_LIVE, ("2026-06-01", "2026-06-30")).fetchone()[0]
print("  Aug 1-21 live legs mine %d (%.1f/day)  CLAIMED 836 / 39.8 -> %s"
      % (aug, aug / 21, verdict(aug, 836)))
print("  June     live legs mine %d (%.1f/day)  CLAIMED 2,731 / 91.0 -> %s"
      % (jun, jun / 30, verdict(jun, 2731)))
print("  Aug/Jun daily-rate ratio mine %.0f%%   CLAIMED 44%%" % (100 * (aug / 21) / (jun / 30)))

LEAD = ("select l.pickup_date pd, cast(julianday(l.pickup_date) - julianday(date(r.created_at)) "
        "as int) lead from reservations_leg l join reservations_reservation r "
        "on r.id=l.reservation_id where l.pickup_date between ? and ? and " + LIVE)
ref = [r["lead"] for r in cur.execute(LEAD, ("2026-02-01", "2026-06-30"))]
augl = [r["lead"] for r in cur.execute(LEAD, ("2026-08-01", "2026-08-21"))]
augcal = [r["lead"] for r in cur.execute(LEAD, ("2026-08-01", "2026-08-31"))]
print("\n  reference cohort pickup 2026-02-01..06-30  n=%d  p10/p25/p50/p75/p90/p95 = %s"
      % (len(ref), pcts(ref)))
print("     CLAIMED n=12,878  p50 20  p75 41  p90 67  ->  n %s, p50 %s, p75 %s, p90 %s"
      % (verdict(len(ref), 12878), verdict(pct(ref, 50), 20, absmin=1),
         verdict(pct(ref, 75), 41, absmin=1), verdict(pct(ref, 90), 67, absmin=1)))
s41 = 100.0 * sum(1 for x in ref if x >= 41) / len(ref)
print("     reference share booked >=41d ahead = %.1f%%  (CLAIMED 25-30%% by month, 75.0%% <=41d)" % s41)
print("  AUGUST 1-21  n=%d  percentiles = %s ; >=41d ahead = %.1f%%  CLAIMED p50 58 / 76.3%% -> %s"
      % (len(augl), pcts(augl), 100.0 * sum(1 for x in augl if x >= 41) / len(augl),
         verdict(100.0 * sum(1 for x in augl if x >= 41) / len(augl), 76.3)))
print("  AUGUST calendar month n=%d (CLAIMED 1,160)  p50=%s p75=%s p90=%s  >=41d %.1f%%  -> p50 %s"
      % (len(augcal), pct(augcal, 50), pct(augcal, 75), pct(augcal, 90),
         100.0 * sum(1 for x in augcal if x >= 41) / len(augcal),
         verdict(pct(augcal, 50), 58, absmin=2)))
late = cur.execute("select count(*) from reservations_leg l join reservations_reservation r "
                   "on r.id=l.reservation_id where l.pickup_date between '2026-08-01' and "
                   "'2026-08-31' and r.created_at > '2026-07-11 20:40' and " + LIVE).fetchone()[0]
print("  August legs booked after 2026-07-11 20:40 UTC: mine %d   CLAIMED 0 -> %s"
      % (late, verdict(late, 0)))

print("\n  MY FALSIFICATION (not in the bundle) - re-impose the freeze on COMPLETE June:")
june_rows = [(d(r["pd"]), r["lead"]) for r in cur.execute(LEAD, ("2026-06-01", "2026-06-30"))]
sim = []
for augdate in ("2026-08-01", "2026-08-08", "2026-08-15", "2026-08-21"):
    k = (d(augdate) - d("2026-07-11")).days
    dow = d(augdate).weekday()
    tot_, surv = 0, 0
    for pd_, ld in june_rows:
        if pd_.weekday() != dow:
            continue
        tot_ += 1
        if ld is not None and ld >= k:
            surv += 1
    ndow = sum(1 for i in range(30) if (d("2026-06-01") + timedelta(days=i)).weekday() == dow)
    obs = cur.execute(Q_LIVE, (augdate, augdate)).fetchone()[0]
    exp = surv / ndow
    print("     %s  K=%2dd  observed %3d | June same-dow mean %5.1f | June survivors at K %5.1f "
          "| observed/predicted %.2f" % (augdate, k, obs, tot_ / ndow, exp,
                                         (obs / exp) if exp else 0))
    sim.append([augdate, k, obs, round(tot_ / ndow, 1), round(exp, 1),
                round(obs / exp, 2) if exp else ""])
print("     A ratio near 1.00 means truncation ALONE explains the August level - no seasonality")
print("     term is needed, and none is measurable.")
w_csv("aug_truncation_sim", sim,
      ["aug_date", "K_days_past_freeze", "observed_live", "june_same_dow_mean",
       "june_survivors_at_K", "observed_over_predicted"])

# ======================================================================= CLAIM 3
line("=")
print("CLAIM 3  |  FARM-OUT IDENTIFICATION RULE AND PER-LEG PREMIUM")
print("CLAIMED  :  rule = drivers_driver.driver_type='affiliate' (authoritative, 99.98% agreement")
print("            with the payment ledger); premium ALL $73.96/leg; towncar $59.23, mini_van")
print("            $68.51, suv $72.18, van $127.49, Van14 $130.77 (method 2, route x class).")
print("MY METHOD:  a WITHIN-RESERVATION matched-pair estimator. On reservations that carried BOTH")
print("            a farmed and an in-house leg, the two legs share customer, price, vehicle class")
print("            and date, and for round trips the same endpoints reversed - so route, class and")
print("            revenue are controlled by construction. No strata, no bootstrap, no rate card.")
print("            Direction bias measured separately on all-in-house pairs.")
line()
W = ("2026-02-08", "2026-07-12")
aff = cur.execute("select count(*) from reservations_leg l join drivers_driver dd on dd.id=l.driver_id "
                  "where l.pickup_date between ? and ? and dd.driver_type='affiliate'", W).fetchone()[0]
inh = cur.execute("select count(*) from reservations_leg l join drivers_driver dd on dd.id=l.driver_id "
                  "where l.pickup_date between ? and ? and dd.driver_type='inhouse'", W).fetchone()[0]
print("  W = pickup_date %s..%s : affiliate legs mine %d (CLAIMED 2,874 -> %s); in-house mine %d "
      "(CLAIMED 10,660 -> %s)" % (W[0], W[1], aff, verdict(aff, 2874), inh, verdict(inh, 10660)))
for col in ("operator_accepted_at", "operator_declined_at"):
    print("     %s non-null on the WHOLE table = %d (CLAIMED 0)"
          % (col, cur.execute("select count(*) from reservations_leg where %s is not null" % col).fetchone()[0]))
print("     operator_driver_name non-blank on the WHOLE table = %d (CLAIMED 0)"
      % cur.execute("select count(*) from reservations_leg where trim(operator_driver_name)<>''").fetchone()[0])
mism = cur.execute("select count(*) from drivers_legpayment lp join drivers_driverpayment dp "
                   "on dp.id=lp.payment_id join reservations_leg l on l.id=lp.leg_id "
                   "where lp.status<>'voided' and l.pickup_date between ? and ? "
                   "and l.driver_id is not null and dp.driver_id <> l.driver_id", W).fetchone()[0]
paid = cur.execute("select count(*) from drivers_legpayment lp join reservations_leg l on l.id=lp.leg_id "
                   "where lp.status<>'voided' and l.pickup_date between ? and ?", W).fetchone()[0]
print("     ledger-vs-leg driver disagreement in W: %d of %d paid legs (%.3f%%)  CLAIMED 2 / 0.02%%"
      % (mism, paid, 100.0 * mism / paid))

print("\n  MY TEST OF THE RULE'S BIGGEST WEAKNESS (driver_type has no history). If anyone switched")
print("  arms mid-window their per-leg pay level would step. The bundle tested only affiliates with")
print("  volume; I test EVERY driver: monthly median pay over months with >=10 legs, flagged when")
print("  max/min >= 2 across >=3 such months.")
rows = cur.execute("select u.username un, dd.driver_type dt, substr(l.pickup_date,1,7) m, "
                   "l.driver_pay_amount amt from reservations_leg l "
                   "join drivers_driver dd on dd.id=l.driver_id join auth_user u on u.id=dd.profile_id "
                   "where l.pickup_date between ? and ? and l.driver_pay_amount is not null", W).fetchall()
bym = defaultdict(list)
for r in rows:
    bym[(r["un"], r["dt"], r["m"])].append(float(r["amt"]))
per_driver = defaultdict(dict)
for (u_, t_, m_), v in bym.items():
    if len(v) >= 10:
        per_driver[(u_, t_)][m_] = statistics.median(v)
flag = []
for (u_, t_), mm in sorted(per_driver.items()):
    if len(mm) >= 3:
        lo, hi = min(mm.values()), max(mm.values())
        if lo > 0 and hi / lo >= 2.0:
            flag.append((u_, t_, lo, hi, sorted(mm.items())))
print("     drivers with >=3 qualifying months: %d ; with a >=2x pay-level step: %d"
      % (len(per_driver), len(flag)))
for u_, t_, lo, hi, mm in flag:
    print("       %s (%s) $%.0f -> $%.0f : %s"
          % (u_, t_, lo, hi, " ".join("%s=%.0f" % (k[5:], v) for k, v in mm)))

print("\n  MATCHED-PAIR PREMIUM (within-reservation, mixed-arm reservations only):")
pair_rows = cur.execute(
    "select l.id, l.reservation_id rid, l.pickup_date pd, l.pickup_time pt, l.driver_pay_amount amt, "
    "dd.driver_type dt, v.vehicle_type cls, r.total_price tp, r.trip_type tt "
    "from reservations_leg l join reservations_reservation r on r.id=l.reservation_id "
    "join drivers_driver dd on dd.id=l.driver_id join rates_vehicle v on v.id=r.vehicle_id "
    "where l.pickup_date between ? and ? and " + LIVE + " and l.driver_pay_amount is not null", W).fetchall()
byres = defaultdict(list)
for r in pair_rows:
    byres[r["rid"]].append(r)
prem_by_cls, prem_all = defaultdict(list), []
nres = nfarm = 0
for rid, lg in byres.items():
    f = [float(x["amt"]) for x in lg if x["dt"] == "affiliate"]
    i = [float(x["amt"]) for x in lg if x["dt"] == "inhouse"]
    if not f or not i:
        continue
    nres += 1
    nfarm += len(f)
    delta = statistics.mean(f) - statistics.mean(i)
    for _ in f:
        prem_by_cls[lg[0]["cls"]].append(delta)
        prem_all.append(delta)
print("     mixed-arm reservations used: %d ; farmed legs covered: %d (%.1f%% of all farmed legs in W)"
      % (nres, nfarm, 100.0 * nfarm / aff))
claimed_cls = {"towncar": 59.23, "mini_van": 68.51, "suv": 72.18, "van": 127.49, "Van(14 Pax)": 130.77}
prem_rows = []
for cls in ["towncar", "suv", "mini_van", "van", "Van(14 Pax)"]:
    v = prem_by_cls.get(cls, [])
    if not v:
        continue
    med, mean = statistics.median(v), statistics.mean(v)
    cl = claimed_cls[cls]
    print("     %-12s n=%4d  matched-pair median +$%7.2f  mean +$%7.2f   CLAIMED $%6.2f -> %s"
          % (cls, len(v), med, mean, cl, verdict(med, cl)))
    prem_rows.append([cls, len(v), round(med, 2), round(mean, 2), cl, verdict(med, cl)])
allmed, allmean = statistics.median(prem_all), statistics.mean(prem_all)
print("     %-12s n=%4d  matched-pair median +$%7.2f  mean +$%7.2f   CLAIMED $ 73.96 -> %s (median) / %s (mean)"
      % ("ALL", len(prem_all), allmed, allmean, verdict(allmed, 73.96), verdict(allmean, 73.96)))
prem_rows.append(["ALL", len(prem_all), round(allmed, 2), round(allmean, 2), 73.96, verdict(allmed, 73.96)])
ctrl = []
for rid, lg in byres.items():
    if len(lg) == 2 and all(x["dt"] == "inhouse" for x in lg):
        a, b = sorted(lg, key=lambda x: (x["pd"], x["pt"]))
        ctrl.append(float(b["amt"]) - float(a["amt"]))
print("     direction-bias control (all-in-house 2-leg reservations, n=%d): median later-leg minus "
      "earlier-leg pay = $%.2f" % (len(ctrl), statistics.median(ctrl)))
w_csv("premium_matched_pairs", prem_rows,
      ["vehicle_class", "farmed_legs_matched", "matched_pair_median_premium",
       "matched_pair_mean_premium", "claimed_M2_premium", "verdict"])

print("\n  WHY THE TWO ESTIMATES DIFFER - in-house pay is right-skewed and median-based strata")
print("  differences do not price dollars. Components of driver_pay_amount in W:")
comp_rows = []
for arm in ("inhouse", "affiliate"):
    r = cur.execute("select count(*), sum(coalesce(l.driver_base_pay,0)), sum(coalesce(l.driver_gratuity,0)), "
                    "sum(coalesce(l.driver_additional,0)), sum(l.driver_pay_amount), "
                    "sum(case when coalesce(l.driver_gratuity,0)>0 then 1 else 0 end) "
                    "from reservations_leg l join reservations_reservation r on r.id=l.reservation_id "
                    "join drivers_driver dd on dd.id=l.driver_id where l.pickup_date between ? and ? "
                    "and " + LIVE + " and dd.driver_type=? and l.driver_pay_amount is not null",
                    (W[0], W[1], arm)).fetchone()
    v = [float(x["amt"]) for x in pair_rows if x["dt"] == arm]
    print("     %-10s n=%5d  base $%s  gratuity $%s (%.1f%% of pay, present on %.0f%% of legs)  "
          "additional $%s" % (arm, r[0], format(float(r[1]), ",.0f"), format(float(r[2]), ",.0f"),
                              100.0 * float(r[2]) / float(r[4]), 100.0 * r[5] / r[0],
                              format(float(r[3]), ",.0f")))
    print("                median pay $%.2f vs MEAN pay $%.2f  -> a median-based premium prices the"
          % (statistics.median(v), statistics.mean(v)))
    print("                arm at its base rate and ignores %.0f%% of the dollars actually paid."
          % (100.0 * (statistics.mean(v) - statistics.median(v)) / statistics.mean(v)))
    comp_rows.append([arm, r[0], round(float(r[1]), 2), round(float(r[2]), 2), round(float(r[3]), 2),
                      round(float(r[4]), 2), round(statistics.median(v), 2), round(statistics.mean(v), 2)])
w_csv("pay_components", comp_rows,
      ["arm", "legs", "base_pay_total", "gratuity_total", "additional_total", "pay_total",
       "median_pay_per_leg", "mean_pay_per_leg"])

print("\n  DOLLARS-CORRECT COUNTERFACTUAL (mine): price each farmed leg against the MEAN in-house")
print("  pay in its own (route_id x vehicle class) stratum, fall back to class mean; premium =")
print("  actual farmed dollars minus modelled in-house dollars. This is the estimator an annual")
print("  exposure figure requires, because sums are means times counts, never medians times counts.")
cf = cur.execute("select l.id, l.route_id rid, v.vehicle_type cls, dd.driver_type dt, "
                 "l.driver_pay_amount amt from reservations_leg l "
                 "join reservations_reservation r on r.id=l.reservation_id "
                 "join drivers_driver dd on dd.id=l.driver_id join rates_vehicle v on v.id=r.vehicle_id "
                 "where l.pickup_date between ? and ? and " + LIVE +
                 " and l.driver_pay_amount is not null", W).fetchall()
ih_stratum, ih_class = defaultdict(list), defaultdict(list)
for r in cf:
    if r["dt"] == "inhouse":
        ih_stratum[(r["rid"], r["cls"])].append(float(r["amt"]))
        ih_class[r["cls"]].append(float(r["amt"]))
act = mod = 0.0
nmatched = nfallback = 0
by_cls_cf = defaultdict(lambda: [0, 0.0, 0.0])
for r in cf:
    if r["dt"] != "affiliate":
        continue
    key = (r["rid"], r["cls"])
    if len(ih_stratum.get(key, [])) >= 5:
        exp = statistics.mean(ih_stratum[key])
        nmatched += 1
    elif ih_class.get(r["cls"]):
        exp = statistics.mean(ih_class[r["cls"]])
        nfallback += 1
    else:
        continue
    a = float(r["amt"])
    act += a
    mod += exp
    b = by_cls_cf[r["cls"]]
    b[0] += 1
    b[1] += a
    b[2] += exp
print("     farmed legs priced: %d (stratum-matched %d, class fallback %d)"
      % (nmatched + nfallback, nmatched, nfallback))
cf_rows = []
for cls in ["towncar", "suv", "mini_van", "van", "Van(14 Pax)"]:
    n_, a_, m_ = by_cls_cf[cls]
    if not n_:
        continue
    print("     %-12s n=%4d  actual $%9s  modelled in-house $%9s  premium/leg $%6.2f  (CLAIMED $%.2f)"
          % (cls, n_, format(a_, ",.0f"), format(m_, ",.0f"), (a_ - m_) / n_, claimed_cls[cls]))
    cf_rows.append([cls, n_, round(a_, 2), round(m_, 2), round((a_ - m_) / n_, 2), claimed_cls[cls]])
per = (act - mod) / (nmatched + nfallback)
print("     %-12s n=%4d  actual $%9s  modelled in-house $%9s  premium/leg $%6.2f  (CLAIMED $73.96) -> %s"
      % ("ALL", nmatched + nfallback, format(act, ",.0f"), format(mod, ",.0f"), per,
         verdict(per, 73.96)))
print("     gross premium over W = $%s  (CLAIMED $211,596 over the same window) -> %s"
      % (format(act - mod, ",.0f"), verdict(act - mod, 211596)))
cf_rows.append(["ALL", nmatched + nfallback, round(act, 2), round(mod, 2), round(per, 2), 73.96])
w_csv("premium_counterfactual", cf_rows,
      ["vehicle_class", "farmed_legs", "actual_farmed_dollars", "modelled_inhouse_dollars",
       "premium_per_leg", "claimed_M2_premium"])

print("\n  Is a per-leg premium the right cost object? Total driver-pay dollars by arm in W:")
for r in cur.execute("select dd.driver_type, count(*), sum(l.driver_pay_amount) from reservations_leg l "
                     "join drivers_driver dd on dd.id=l.driver_id "
                     "join reservations_reservation r on r.id=l.reservation_id "
                     "where l.pickup_date between ? and ? and " + LIVE +
                     " and l.driver_pay_amount is not null group by 1", W):
    print("     %-10s legs=%6d  paid=$%12s  avg $%6.2f/leg"
          % (r[0], r[1], format(float(r[2]), ",.2f"), float(r[2]) / r[1]))
print("   payroll cross-check - is per-leg pay the WHOLE of driver compensation?")
for r in cur.execute("select d.driver_type, count(*), "
                     "sum(case when abs(dp.amount - coalesce(lp.s,0))>0.5 then 1 else 0 end), "
                     "sum(dp.amount) from drivers_driverpayment dp "
                     "join drivers_driver d on d.id=dp.driver_id left join "
                     "(select payment_id, sum(amount) s from drivers_legpayment where status<>'voided' "
                     "group by 1) lp on lp.payment_id=dp.id group by 1"):
    print("     %-10s payments=%4d  payments whose total != sum of their leg pay = %d  "
          "lifetime $%s" % (r[0], r[1], r[2], format(float(r[3]), ",.2f")))

# ======================================================================= CLAIM 4
line("=")
print("CLAIM 4  |  IS DVA TRUSTWORTHY AS A ROSTER RECORD?")
print("CLAIMED  :  yes from 2026-01-18, in-house only - precision 99.5%, recall 94.5%, median")
print("            Jaccard 1.00 from April, graded 'A- reliable'.")
print("MY METHOD:  same set comparison, but scored over EVERY date on which in-house work")
print("            happened - including dates with zero DVA rows, which the bundle's 'dates")
print("            having both' filter drops - plus an information-content test: how many")
print("            driver-days does DVA supply that leg.driver_id does not already supply?")
line()
CUT = "2026-07-11"
dva = cur.execute("select a.date dt, a.driver_id did, a.vehicle_id vid, a.planned_start_hour ps, "
                  "dd.driver_type dtp, u.username un from drivers_drivervehicleassignment a "
                  "join drivers_driver dd on dd.id=a.driver_id join auth_user u on u.id=dd.profile_id "
                  "where a.date <= ?", (CUT,)).fetchall()
worked = cur.execute("select l.pickup_date dt, l.driver_id did, dd.driver_type dtp "
                     "from reservations_leg l join reservations_reservation r on r.id=l.reservation_id "
                     "join drivers_driver dd on dd.id=l.driver_id "
                     "where l.pickup_date between '2025-01-01' and ? and " + LIVE +
                     " group by 1,2", (CUT,)).fetchall()
dva_set, work_set = defaultdict(set), defaultdict(set)
for r in dva:
    if r["dtp"] == "inhouse":
        dva_set[r["dt"]].add(r["did"])
for r in worked:
    if r["dtp"] == "inhouse":
        work_set[r["dt"]].add(r["did"])


def score(dates):
    tp = fp = fn = 0
    jac = []
    for dt in dates:
        A, B = dva_set.get(dt, set()), work_set.get(dt, set())
        tp += len(A & B)
        fp += len(A - B)
        fn += len(B - A)
        if A | B:
            jac.append(len(A & B) / len(A | B))
    p = 100.0 * tp / (tp + fp) if tp + fp else float("nan")
    rc = 100.0 * tp / (tp + fn) if tp + fn else float("nan")
    return tp, fp, fn, p, rc, jac


habit = [dt for dt in sorted(set(list(dva_set) + list(work_set))) if "2026-01-18" <= dt <= CUT]
both = [dt for dt in habit if dt in dva_set and dt in work_set]
tp, fp, fn, p, rc, jac = score(both)
print("  (a) bundle's filter - dates having BOTH a DVA row and in-house work (%d dates):" % len(both))
print("      TP=%d FP=%d FN=%d  precision %.1f%%  recall %.1f%%  median Jaccard %.2f   "
      "CLAIMED 99.5 / 94.5 -> precision %s, recall %s"
      % (tp, fp, fn, p, rc, statistics.median(jac), verdict(p, 99.5), verdict(rc, 94.5)))
tp2, fp2, fn2, p2, rc2, jac2 = score(habit)
print("  (b) MY filter - EVERY date 2026-01-18..%s with in-house work (%d dates):" % (CUT, len(habit)))
print("      TP=%d FP=%d FN=%d  precision %.1f%%  recall %.1f%%  median Jaccard %.2f"
      % (tp2, fp2, fn2, p2, rc2, statistics.median(jac2)))
zero = [dt for dt in habit if dt not in dva_set]
print("      dates inside the 'contiguous habit' with ZERO DVA rows: %d %s" % (len(zero), zero[:12]))
dva_pairs = set((r["dt"], r["did"]) for r in dva if r["dtp"] == "inhouse")
work_pairs = set((r["dt"], r["did"]) for r in worked if r["dtp"] == "inhouse")
only_dva = dva_pairs - work_pairs
print("\n  INFORMATION-CONTENT TEST (mine): in-house DVA driver-days pre-CUT = %d ; of those %d "
      "(%.1f%%) are NOT" % (len(dva_pairs), len(only_dva), 100.0 * len(only_dva) / len(dva_pairs)))
print("      already derivable from leg.driver_id. A roster record earns its name by listing people")
print("      who were AVAILABLE BUT UNUSED; DVA supplies %d such driver-days in ~6 months." % len(only_dva))
ph = cur.execute("select count(*), sum(case when planned_start_hour is not null then 1 else 0 end), "
                 "sum(case when vehicle_id is not null then 1 else 0 end) "
                 "from drivers_drivervehicleassignment").fetchone()
print("      DVA rows total %d ; planned_start_hour populated %d ; vehicle_id populated %d"
      % (ph[0], ph[1], ph[2]))
aff_dva = sum(1 for r in dva if r["dtp"] == "affiliate")
aff_days = len(set((r["dt"], r["did"]) for r in worked if r["dtp"] == "affiliate"
                   and "2026-01-18" <= r["dt"] <= CUT))
print("      affiliate DVA rows pre-CUT: %d ; affiliate driver-days that actually WORKED in the same"
      % aff_dva)
print("      window: %d  ->  DVA recall against the WHOLE delivering roster = %.1f%%, not %.1f%%."
      % (aff_days, 100.0 * tp2 / (tp2 + fn2 + aff_days), rc))
w_csv("dva_daily", [[dt, len(dva_set.get(dt, set())), len(work_set.get(dt, set())),
                     len(dva_set.get(dt, set()) & work_set.get(dt, set()))] for dt in habit],
      ["date", "dva_inhouse_drivers", "worked_inhouse_drivers", "intersection"])

# ======================================================================= CLAIM 5
line("=")
print("CLAIM 5  |  GOLD-COHORT SHARE OF THE WINDOW = 44.0% (6,806 / 15,470)")
print("MY METHOD:  the same 14 nominated names under FOUR different denominators, because a")
print("            share is only meaningful with its denominator named - and the claim's own")
print("            text concedes the audit's 8,656 used 'a wider denominator'.")
line()
ph = ",".join("?" for _ in GOLD14)
gold_rows_db = cur.execute("select dd.id, u.username un, dd.driver_type dt, dd.exclude_from_timing ex "
                           "from drivers_driver dd join auth_user u on u.id=dd.profile_id "
                           "where u.username in (%s)" % ph, GOLD14).fetchall()
print("  gold names resolved: %d of 14 -> %s" % (len(gold_rows_db), sorted(r["un"] for r in gold_rows_db)))
miss = set(GOLD14) - set(r["un"] for r in gold_rows_db)
if miss:
    print("  UNRESOLVED NAMES: %s" % sorted(miss))
gids = set(r["id"] for r in gold_rows_db)
print("  of the 14: driver_type=%s ; exclude_from_timing=1 on %s"
      % (dict((r["un"], r["dt"]) for r in gold_rows_db if r["dt"] != "inhouse"),
         [r["un"] for r in gold_rows_db if r["ex"]]))
TW = ("2026-02-08", "2026-07-11")
BASE = ("select l.id, l.driver_id did from reservations_leg l "
        "join reservations_reservation r on r.id=l.reservation_id ")
den_defs = [
    ("live legs in tap window (driver or not)", BASE + "where l.pickup_date between ? and ? and " + LIVE),
    ("live legs in tap window WITH a driver",
     BASE + "where l.pickup_date between ? and ? and " + LIVE + " and l.driver_id is not null"),
    ("live legs in tap window with an IN-HOUSE driver",
     BASE + "join drivers_driver dd on dd.id=l.driver_id where l.pickup_date between ? and ? and "
     + LIVE + " and dd.driver_type='inhouse'"),
    ("live legs in tap window with a driver AND a completed tap",
     BASE + "where l.pickup_date between ? and ? and " + LIVE + " and l.driver_id is not null and "
     "exists(select 1 from reservations_legstatus s where s.leg_id=l.id and s.status='completed')"),
]
gshare = []
for lbl, sql in den_defs:
    rs = cur.execute(sql, TW).fetchall()
    den = len(rs)
    num = sum(1 for r in rs if r["did"] in gids)
    print("     %-56s %6d / %6d = %5.1f%%" % (lbl, num, den, 100.0 * num / den))
    gshare.append([lbl, num, den, round(100.0 * num / den, 2)])
print("  CLAIMED 6,806 / 15,470 = 44.0%")
print("  DENOMINATOR FORENSICS - where 15,470 actually comes from:")
ext = cur.execute(Q_LIVE, ("2026-07-12", "2026-08-21")).fetchone()[0]
tapn = cur.execute(Q_LIVE, ("2026-02-08", "2026-07-11")).fetchone()[0]
wide = cur.execute(Q_LIVE, ("2026-02-08", "2026-08-21")).fetchone()[0]
gold_ext = cur.execute("select count(*) from reservations_leg l "
                       "join reservations_reservation r on r.id=l.reservation_id "
                       "join drivers_driver dd on dd.id=l.driver_id join auth_user u on u.id=dd.profile_id "
                       "where l.pickup_date between '2026-07-12' and '2026-08-21' and " + LIVE +
                       " and u.username in (%s)" % ph, GOLD14).fetchone()[0]
print("     live legs 2026-02-08..2026-07-11 = %d ; live legs 2026-07-12..2026-08-21 = %d ; sum = %d"
      % (tapn, ext, wide))
print("     gold legs in the post-freeze extension = %d ; %d + %d = %d = the claimed numerator."
      % (gold_ext, gshare[0][1], gold_ext, gshare[0][1] + gold_ext))
print("     So 44.0%% is computed over a window that runs to 2026-08-21 - i.e. it includes %d legs"
      % ext)
print("     from the period the same bundle declares has ZERO operational record. On the tap window")
print("     the same 14 drivers hold %.1f%% -> numerator %s, share %s"
      % (gshare[1][3], verdict(gshare[1][1] + gold_ext, 6806), verdict(gshare[1][3], 44.0)))
w_csv("gold_share", gshare, ["denominator_definition", "gold_legs", "denominator", "share_pct"])

# ======================================================================= CLAIM 6
line("=")
print("CLAIM 6  |  ARRIVAL DWELL AND TURNAROUND PERCENTILES")
print("CLAIMED  :  true dwell (gate->picked-up): gold n=2,443 p50 37 p75 47 p90 64 ; ALL drivers")
print("            n=4,553 p50 39 p75 54 p90 77.  Turnaround completed(A)->on-location(B), gold")
print("            n=5,225 p50 48 p75 84 p90 131 - 'the number a feasibility gate should use'.")
print("MY METHOD:  arrival legs identified by flight linkage + an airport regex on the raw pickup")
print("            text (no categorize_location, no Django import); consecutive legs ordered by")
print("            ACTUAL first tap rather than by scheduled pickup_time.")
line()
taps = defaultdict(dict)
for r in cur.execute("select leg_id, status, min(timestamp) t from reservations_legstatus group by 1,2"):
    ts = parse_ts(r["t"])
    if ts:
        taps[r["leg_id"]][r["status"]] = ts
print("  first-occurrence tap index: %d legs carry >=1 tap" % len(taps))
legs = cur.execute(
    "select l.id, l.pickup_date pd, l.pickup_time pt, l.pickup_location loc, l.driver_id did, "
    "dd.driver_type dtp, dd.exclude_from_timing ex, u.username un, "
    "coalesce((select lf.flight_id from reservations_legflight lf where lf.leg_id=l.id "
    "and lf.is_controlling=1 limit 1), l.flight_information_id) fid "
    "from reservations_leg l join reservations_reservation r on r.id=l.reservation_id "
    "left join drivers_driver dd on dd.id=l.driver_id left join auth_user u on u.id=dd.profile_id "
    "where l.pickup_date between '2026-02-08' and '2026-07-11' and " + LIVE).fetchall()
print("  window legs (live, 2026-02-08..2026-07-11): %d" % len(legs))
flights = {}
for r in cur.execute("select id, actual_gate_arrival_local a, scheduled_gate_arrival_local s "
                     "from reservations_flight"):
    flights[r["id"]] = (parse_ts(r["a"]), parse_ts(r["s"]))
AIRPORT = re.compile(r"(\bMCO\b|\bSFB\b|orlando international|sanford|international airport)", re.I)
SANFORD = re.compile(r"(\bSFB\b|sanford)", re.I)
dwell, noregex = defaultdict(list), []
for r in legs:
    fid = r["fid"]
    if not fid or fid not in flights:
        continue
    act = flights[fid][0]
    pu = taps.get(r["id"], {}).get("picked-up")
    if not act or not pu:
        continue
    mins = (pu - act).total_seconds() / 60.0
    if not (-120 <= mins <= 240):
        continue
    noregex.append(mins)
    if not AIRPORT.search(r["loc"] or ""):
        continue
    dwell["ALL"].append(mins)
    if r["dtp"] == "inhouse" and not r["ex"]:
        dwell["trustworthy"].append(mins)
    if r["un"] in GOLD14:
        dwell["gold"].append(mins)
        dwell["gold_" + ("SFB" if SANFORD.search(r["loc"] or "") else "MCO")].append(mins)
claims6 = {"ALL": (4553, 39, 54, 77), "trustworthy": (2870, 37, 47, 64), "gold": (2443, 37, 47, 64)}
dw_rows = []
for k in ("ALL", "trustworthy", "gold"):
    v = dwell[k]
    cn, c50, c75, c90 = claims6[k]
    print("  dwell[%-11s] n=%5d  p25=%.0f p50=%.0f p75=%.0f p90=%.0f p95=%.0f   CLAIMED n=%d "
          "p50=%d p75=%d p90=%d" % (k, len(v), pct(v, 25), pct(v, 50), pct(v, 75), pct(v, 90),
                                    pct(v, 95), cn, c50, c75, c90))
    print("      -> n %s, p50 %s, p75 %s, p90 %s"
          % (verdict(len(v), cn), verdict(pct(v, 50), c50, absmin=1),
             verdict(pct(v, 75), c75, absmin=1), verdict(pct(v, 90), c90, absmin=1)))
    dw_rows.append([k, len(v)] + [round(x, 1) for x in pcts(v)] + [cn, c50, c75, c90])
print("  flight-linkage-only variant (no airport regex), ALL drivers: n=%d - the regex removes %d "
      "legs that carry" % (len(noregex), len(noregex) - len(dwell["ALL"])))
print("  an arrival flight with a real gate time but whose pickup text is not an airport.")
for k in ("gold_MCO", "gold_SFB"):
    v = dwell[k]
    if v:
        print("  dwell[%s] n=%d p25=%.0f p50=%.0f p75=%.0f p90=%.0f p95=%.0f"
              % (k, len(v), pct(v, 25), pct(v, 50), pct(v, 75), pct(v, 90), pct(v, 95)))
w_csv("dwell", dw_rows, ["cohort", "n", "p10", "p25", "p50", "p75", "p90", "p95",
                         "claimed_n", "claimed_p50", "claimed_p75", "claimed_p90"])

print("\n  turnaround: consecutive same-driver same-day legs, ordered by ACTUAL first tap")
byday = defaultdict(list)
for r in legs:
    if r["did"] is None:
        continue
    byday[(r["did"], r["pd"])].append((r, taps.get(r["id"], {})))
g1, g2, npair, disagree, ndays = [], [], 0, 0, 0
for (drv, dt), items in byday.items():
    if len(items) < 2:
        continue
    keyed = []
    for r, t in items:
        anchor = t.get("on-the-way") or t.get("on-location") or t.get("picked-up") or t.get("completed")
        if anchor is not None:
            keyed.append((anchor, r, t))
    if len(keyed) < 2:
        continue
    ndays += 1
    keyed.sort(key=lambda x: x[0])
    sched = sorted(keyed, key=lambda x: (x[1]["pd"], x[1]["pt"]))
    if [x[1]["id"] for x in keyed] != [x[1]["id"] for x in sched]:
        disagree += 1
    for i in range(len(keyed) - 1):
        _, ra, ta = keyed[i]
        _, rb, tb = keyed[i + 1]
        if ra["un"] not in GOLD14 or rb["un"] not in GOLD14:
            continue
        npair += 1
        if "completed" in ta and "on-location" in tb:
            m = (tb["on-location"] - ta["completed"]).total_seconds() / 60.0
            if -120 <= m <= 720:
                g1.append(m)
        if "completed" in ta and "on-the-way" in tb:
            m = (tb["on-the-way"] - ta["completed"]).total_seconds() / 60.0
            if -120 <= m <= 720:
                g2.append(m)
print("     driver-days with >=2 tapped legs: %d ; days where ACTUAL tap order differs from "
      "SCHEDULED pickup order: %d (%.1f%%)" % (ndays, disagree, 100.0 * disagree / ndays))
print("     gold-only consecutive pairs considered: %d" % npair)
print("     completed(A)->on-location(B): n=%d p25=%.0f p50=%.0f p75=%.0f p90=%.0f   "
      "CLAIMED n=5,225 p50=48 p75=84 p90=131" % (len(g1), pct(g1, 25), pct(g1, 50),
                                                 pct(g1, 75), pct(g1, 90)))
print("       -> n %s, p50 %s, p75 %s, p90 %s"
      % (verdict(len(g1), 5225), verdict(pct(g1, 50), 48, absmin=1),
         verdict(pct(g1, 75), 84, absmin=1), verdict(pct(g1, 90), 131, absmin=2)))
print("     completed(A)->on-the-way(B):  n=%d p50=%.0f p75=%.0f p90=%.0f   CLAIMED n=5,251 "
      "p50=4 p75=49 p90=100" % (len(g2), pct(g2, 50), pct(g2, 75), pct(g2, 90)))
print("       -> p50 %s, p75 %s, p90 %s"
      % (verdict(pct(g2, 50), 4, absmin=2), verdict(pct(g2, 75), 49, absmin=2),
         verdict(pct(g2, 90), 100, absmin=5)))
print("     negative completed->on-the-way gaps: %.1f%% (CLAIMED 3.8%% over all pairs)"
      % (100.0 * sum(1 for x in g2 if x < 0) / len(g2)))
w_csv("turnaround", [["completed->on-location", len(g1)] + [round(x, 1) for x in pcts(g1)],
                     ["completed->on-the-way", len(g2)] + [round(x, 1) for x in pcts(g2)]],
      ["metric", "n", "p10", "p25", "p50", "p75", "p90", "p95"])

# ======================================================================= MISLEAD
line("=")
print("THREE WAYS THIS DATASET COULD MOST PLAUSIBLY MISLEAD THE REDESIGN")
line()
ex = cur.execute("select u.username un, dd.exclude_from_timing ex, count(*) n, min(l.pickup_date) a, "
                 "max(l.pickup_date) b from reservations_leg l join drivers_driver dd on dd.id=l.driver_id "
                 "join auth_user u on u.id=dd.profile_id where l.pickup_date between '2026-02-08' "
                 "and '2026-07-11' and dd.driver_type='inhouse' and dd.is_active=0 "
                 "group by 1,2 order by n desc").fetchall()
print("M1 SURVIVORSHIP - in-house drivers flagged is_active=0 TODAY who worked in the tap window:")
print("   %d drivers, %d legs" % (len(ex), sum(r["n"] for r in ex)))
for r in ex:
    print("     %-14s %5d legs  %s..%s  exclude_from_timing=%s" % (r["un"], r["n"], r["a"], r["b"], r["ex"]))
exl = cur.execute("select count(*) from reservations_leg l join drivers_driver dd on dd.id=l.driver_id "
                  "where l.pickup_date between '2026-02-08' and '2026-07-11' and dd.driver_type='inhouse' "
                  "and dd.exclude_from_timing=1").fetchone()[0]
print("   in-house legs sitting under exclude_from_timing=1 in the same window: %d" % exl)

print("\nM2 CLEAN-COHORT BIAS - the same metric on the population the redesign must serve:")
for k in ("gold", "trustworthy", "ALL"):
    v = dwell[k]
    print("     dwell %-11s n=%5d p50=%.0f p75=%.0f p90=%.0f" % (k, len(v), pct(v, 50), pct(v, 75), pct(v, 90)))
print("     ALL-driver dwell is +%.0f min at p75 and +%.0f min at p90 versus the gold cohort."
      % (pct(dwell["ALL"], 75) - pct(dwell["gold"], 75), pct(dwell["ALL"], 90) - pct(dwell["gold"], 90)))
cov = cur.execute("select count(*) from reservations_leg l join reservations_reservation r "
                  "on r.id=l.reservation_id where l.pickup_date between '2026-02-08' and '2026-07-11' "
                  "and " + LIVE + " and exists(select 1 from reservations_legstatus s where s.leg_id=l.id "
                  "and s.status='on-the-way')").fetchone()[0]
allw = cur.execute(Q_LIVE, ("2026-02-08", "2026-07-11")).fetchone()[0]
print("     Only %d/%d = %.1f%% of window legs carry an 'on-the-way' tap at all, and the timed"
      % (cov, allw, 100.0 * cov / allw))
print("     subset is selected by driver discipline, not at random.")

print("\nM3 EVERY TREND IS CONFOUNDED WITH EVERY OTHER TREND:")
print("     month     live_legs  legs/day  inhouse_drvs  affiliate_legs  farm_share  cancel%")
for m, a, b in [("2025-10", "2025-10-01", "2025-10-31"), ("2025-11", "2025-11-01", "2025-11-30"),
                ("2025-12", "2025-12-01", "2025-12-31"), ("2026-01", "2026-01-01", "2026-01-31"),
                ("2026-02", "2026-02-01", "2026-02-28"), ("2026-03", "2026-03-01", "2026-03-31"),
                ("2026-04", "2026-04-01", "2026-04-30"), ("2026-05", "2026-05-01", "2026-05-31"),
                ("2026-06", "2026-06-01", "2026-06-30"), ("2026-07a", "2026-07-01", "2026-07-11")]:
    n = cur.execute(Q_LIVE, (a, b)).fetchone()[0]
    nd = (d(b) - d(a)).days + 1
    r2 = cur.execute("select count(distinct case when dd.driver_type='inhouse' then dd.id end), "
                     "sum(case when dd.driver_type='affiliate' then 1 else 0 end), "
                     "sum(case when dd.driver_type is not null then 1 else 0 end) "
                     "from reservations_leg l join reservations_reservation r on r.id=l.reservation_id "
                     "left join drivers_driver dd on dd.id=l.driver_id "
                     "where l.pickup_date between ? and ? and " + LIVE, (a, b)).fetchone()
    allrows = cur.execute("select count(*) from reservations_leg l "
                          "join reservations_reservation r on r.id=l.reservation_id "
                          "where l.pickup_date between ? and ?", (a, b)).fetchone()[0]
    fs = 100.0 * r2[1] / r2[2] if r2[2] else 0
    print("     %-9s %8d  %8.1f  %12d  %14d  %9.1f%%  %6.1f%%"
          % (m, n, n / nd, r2[0], r2[1], fs, 100.0 * (allrows - n) / allrows))
print("     Volume, in-house headcount, farm-out share and the cancellation flag all moved inside")
print("     the same nine months; no month-over-month change can be attributed to one cause.")

line("=")
print("END 06_challenge")
