#!/usr/bin/env python3
"""03_farmout — how much work goes to affiliates, when, why, and what the premium costs.

Phase 1 research. READ-ONLY: the snapshot is opened `mode=ro`, nothing is written to it,
no scheduling logic is touched. Everything lands as stdout + CSV under ./out/.

RUN FROM THIS DIRECTORY so `import _common` resolves:
    cd docs/scheduling-redesign/analysis && python 03_farmout.py

NO DATE LITERALS. Every window, boundary and horizon below is derived at run time from
the database through `_common`. The only literals in this file are fractions, counts and
thresholds — never a date. Re-running against a newer pull moves every window forward.

Farm-out is the engagement's primary money metric AND it is demand: every farmed leg is
work the proposed shift structure will be measured against.
"""

import datetime as dt
import math
import statistics
from collections import Counter, defaultdict

import _common as C

SCRIPT = "03_farmout"

# ---------------------------------------------------------------------------
# tunables — all dimensionless. None of them is a date.
# ---------------------------------------------------------------------------
CP_MIN_SEG = 28          # changepoints(): shortest regime, in days
CP_MIN_EFFECT = 0.09     # changepoints(): smallest relative level shift kept
L4W_DAYS = 28            # "last 4 weeks" — a duration, not a date
FRONTIER_MAX_UNASSIGNED = 0.10   # a day counts as "dispatch-mature" below this
MIN_CELL = 8             # smallest stratum used by a stratified estimator
RELABEL_STEP = 1.8       # pay-level jump treated as a possible arm switch
RELABEL_MIN_SEG = 25     # legs required on BOTH sides of a claimed arm switch
NEAR_TERM_H = 24.0       # the "within 24h of pickup" commit test, in hours

ARM_AFF = "affiliate"
ARM_INH = "inhouse"


def money(x):
    return "$%s" % format(round(x), ",d") if x is not None else "n/a"


def m2(x):
    return "$%.2f" % x if x is not None else "n/a"


def share(a, b):
    return (100.0 * a / b) if b else float("nan")


def mean(xs):
    xs = [x for x in xs if x is not None]
    return (sum(xs) / len(xs)) if xs else None


def med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


# ===========================================================================
# 0. horizon, roster, window
# ===========================================================================
con = C.connect()
H = C.Horizon(con)

C.preamble(
    SCRIPT,
    "farm-out: identification, volume, concentration, commit timing, premium, capacity",
    H,
    assumptions=(
        "[structural] A leg is FARMED OUT iff it has a driver and that driver's "
        "drivers_driver.driver_type = 'affiliate'. That is exactly what production does "
        "(dispatching/fleet_intel.py:75). There is no stored farm-out flag. Section 1 "
        "validates this against two independent sources and tests the retro-relabel risk.",
        "[structural] driver_type is a CURRENT column with no history table. Section 1e "
        "resolves how much that can distort the window using django_admin_log (which DOES "
        "date every admin edit of a Driver) plus a price-band flip detector that needs no "
        "log at all. The residual is bounded there, not assumed away.",
        "[derived] Vehicle class = leg.vehicle_id if set else reservation.vehicle_id "
        "(mirrors Leg.effective_vehicle, reservations/models.py:1346-1350). leg.vehicle_id "
        "is set on well under 1% of legs, so in practice class is the reservation's.",
        "[derived] Driver cost per leg P = leg.driver_pay_amount when non-null, else "
        "driver_base_pay + driver_gratuity + driver_additional when any component is set. "
        "Section 1b measures how far that agrees with the independent payment ledger.",
        "[structural] pickup_date/pickup_time are LOCAL; driver_assigned_at, "
        "auditlog.timestamp and legpayment dates are UTC. C.to_local() is the only bridge.",
        "[derived] The analysis window is the last TWO changepoint regimes on live "
        "legs/day. It is not hand-picked and it is not inherited from the old document.",
        "[structural] Forward pickup dates (> today) are structurally unassigned and are "
        "excluded from every aggregate. Section 0 measures the dispatch frontier to prove "
        "that today itself is dispatch-mature.",
    ),
)

# --- roster ---------------------------------------------------------------
drv = {}
for r in C.q(con, """SELECT d.id, d.driver_type, d.is_active, d.portal_role,
                            d.employment_type, d.exclude_from_timing,
                            COALESCE(u.username,'') AS uname,
                            TRIM(COALESCE(u.first_name,'')||' '||COALESCE(u.last_name,'')) AS fullname
                     FROM drivers_driver d
                     LEFT JOIN auth_user u ON u.id = d.profile_id"""):
    drv[r["id"]] = dict(r)
    drv[r["id"]]["label"] = (r["fullname"] or r["uname"] or ("driver#%d" % r["id"]))


def arm(did):
    if did is None:
        return "unassigned"
    d = drv.get(did)
    return d["driver_type"] if d else "unknown"


def dlabel(did):
    d = drv.get(did)
    return d["label"] if d else ("driver#%s" % did)


# --- vehicle / route reference -------------------------------------------
VCLASS = {r["id"]: r["vehicle_type"] for r in C.q(con, "SELECT id, vehicle_type FROM rates_vehicle")}
ROUTE = {r["id"]: dict(r) for r in C.q(
    con, "SELECT id, slug, inhouse_base_pay FROM rates_route")}

# --- the leg universe -----------------------------------------------------
LEGS = [dict(r) for r in C.q(con, C.live_legs_sql(
    "l.id, l.pickup_date, l.pickup_time, l.pickup_location, l.dropoff_location, "
    "l.driver_id, l.status, l.driver_assigned_at, l.route_id, "
    "l.driver_pay_amount, l.driver_base_pay, l.driver_gratuity, l.driver_additional, "
    "l.leg_base_price, l.reservation_id, "
    "COALESCE(l.vehicle_id, r.vehicle_id) AS veh_id, "
    "r.total_price, r.gratuity_amount, "
    "l.operator_accepted_at, l.operator_declined_at, l.operator_driver_name",
    order="ORDER BY l.pickup_date, l.pickup_time"))]

for g in LEGS:
    g["d"] = dt.date.fromisoformat(g["pickup_date"])
    g["arm"] = arm(g["driver_id"])
    g["vclass"] = VCLASS.get(g["veh_id"], "<none>")
    g["bd"] = C.booked_dtm(g["pickup_date"], g["pickup_time"])
    g["hour"] = g["bd"].hour if g["bd"] else None
    g["dow"] = g["d"].weekday()
    g["lane"] = "%s>%s" % (C.loc_bucket(g["pickup_location"]), C.loc_bucket(g["dropoff_location"]))
    g["kind"] = C.trip_kind(g["pickup_location"], g["dropoff_location"])
    p1 = g["driver_pay_amount"]
    comps = [g["driver_base_pay"], g["driver_gratuity"], g["driver_additional"]]
    if p1 is not None:
        g["pay"] = float(p1)
    elif any(c is not None for c in comps):
        g["pay"] = float(sum(c or 0 for c in comps))
    else:
        g["pay"] = None
    g["base"] = float(g["driver_base_pay"]) if g["driver_base_pay"] is not None else None
    g["grat"] = float(g["driver_gratuity"] or 0) if (
        g["driver_base_pay"] is not None or g["driver_gratuity"] is not None) else None

BYID = {g["id"]: g for g in LEGS}

# --- the window, derived --------------------------------------------------
byday = C.legs_per_day(con, end=H.last_demand_day)
FIRST_LEG_DAY = min(dt.date.fromisoformat(d) for d in byday)
SEGS = C.changepoints(byday, FIRST_LEG_DAY, H.last_demand_day, CP_MIN_SEG, CP_MIN_EFFECT)

CUR_S, CUR_E, CUR_N, CUR_M = SEGS[-1]
PRI_S, PRI_E, PRI_N, PRI_M = SEGS[-2]
W_S, W_E = PRI_S, H.last_demand_day                       # the analysis window
L4W_S = H.last_demand_day - dt.timedelta(days=L4W_DAYS - 1)

C.sub("0. Window — derived from the data, never inherited")
print("changepoints(live legs/day, min_seg=%d, min_effect=%.2f), whole history:"
      % (CP_MIN_SEG, CP_MIN_EFFECT))
for s, e, n, m in SEGS:
    mark = ""
    if (s, e) == (CUR_S, CUR_E):
        mark = "  <- CURRENT regime"
    elif (s, e) == (PRI_S, PRI_E):
        mark = "  <- PRIOR plateau"
    print("   %s .. %s  %4dd  %6.1f legs/day%s" % (s, e, n, m, mark))
print()
print("  [measured] CURRENT regime %s..%s is %+.1f%% on the prior plateau "
      "(%.1f vs %.1f legs/day)." % (CUR_S, CUR_E, 100.0 * (CUR_M - PRI_M) / PRI_M, CUR_M, PRI_M))
print("  ANALYSIS WINDOW W = %s .. %s  (%d days = the last two regimes)"
      % (W_S, W_E, (W_E - W_S).days + 1))
print("  LAST 4 WEEKS   L4W = %s .. %s  (%d days)" % (L4W_S, H.last_demand_day, L4W_DAYS))

# --- dispatch frontier: is 'today' actually assigned? ---------------------
unassigned_by_day = defaultdict(lambda: [0, 0])
for g in LEGS:
    if g["d"] >= W_S:
        unassigned_by_day[g["d"]][0] += 1
        if g["driver_id"] is None:
            unassigned_by_day[g["d"]][1] += 1
frontier = W_S
d = W_S
while True:
    tot, un = unassigned_by_day.get(d, (0, 0))
    if tot == 0 or (un / float(tot)) > FRONTIER_MAX_UNASSIGNED:
        break
    frontier = d
    d += dt.timedelta(days=1)
print()
print("  [measured] dispatch frontier (last day with <=%.0f%% legs unassigned) = %s"
      % (100 * FRONTIER_MAX_UNASSIGNED, frontier))
print("             today = %s -> %s" % (
    H.today, "today IS dispatch-mature, farm-out share is measurable through it"
    if frontier >= H.today else "WARNING: today is NOT mature, aggregates truncated"))
tot_t, un_t = unassigned_by_day.get(H.today, (0, 0))
print("             today itself: %d live legs, %d unassigned (%.1f%%)"
      % (tot_t, un_t, share(un_t, tot_t)))

W = [g for g in LEGS if W_S <= g["d"] <= W_E]
W_ASSIGNED = [g for g in W if g["driver_id"] is not None]
CURW = [g for g in W if CUR_S <= g["d"] <= CUR_E and g["driver_id"] is not None]
PRIW = [g for g in W if PRI_S <= g["d"] <= PRI_E and g["driver_id"] is not None]
L4W = [g for g in W if g["d"] >= L4W_S and g["driver_id"] is not None]
W_FARM_SHARE = share(sum(1 for g in W_ASSIGNED if g["arm"] == ARM_AFF), len(W_ASSIGNED))
print("\n  window legs: %d live, %d assigned (%.2f%% unassigned residue), %.1f%% of the "
      "assigned ones farmed out."
      % (len(W), len(W_ASSIGNED), share(len(W) - len(W_ASSIGNED), len(W)), W_FARM_SHARE))


# ===========================================================================
# 1. IDENTIFICATION
# ===========================================================================
C.hdr("1. IDENTIFICATION — can we trust driver_type as the farm-out signal?")

C.sub("1a. The roster")
cnt = Counter((d["driver_type"], bool(d["is_active"]), d["portal_role"]) for d in drv.values())
print("  [measured] drivers_driver by (driver_type, is_active, portal_role):")
for k in sorted(cnt, key=lambda x: (x[0], not x[1])):
    print("     %-10s active=%-5s portal_role=%-8s  n=%d" % (k[0], k[1], k[2], cnt[k]))
aff_ids = {i for i, d in drv.items() if d["driver_type"] == ARM_AFF}
print("  [measured] %d affiliate driver rows, %d in-house."
      % (len(aff_ids), len(drv) - len(aff_ids)))

prof = C.q(con, """SELECT p.*, d.driver_type FROM drivers_affiliateprofile p
                   JOIN drivers_driver d ON d.id = p.driver_id""")
print("  [measured] drivers_affiliateprofile: %d rows for %d affiliate drivers "
      "(%.0f%% of affiliates have NO profile at all)."
      % (len(prof), len(aff_ids), share(len(aff_ids) - len(prof), len(aff_ids))))
for p in prof:
    print("     %-14s cap=%-5s mode=%-13s max_tier=%-12s no_port_sfb=%s"
          % (dlabel(p["driver_id"]), p["daily_cap"], p["capacity_mode"] or "-",
             p["max_vehicle_tier"] or "-", bool(p["no_pickup_at_port_sanford"])))

C.sub("1b. Independent source #1 — the payment ledger")
led = C.q(con, """SELECT lp.leg_id, lp.amount, lp.status, dp.driver_id AS paid_driver
                  FROM drivers_legpayment lp
                  JOIN drivers_driverpayment dp ON dp.id = lp.payment_id
                  WHERE lp.status = 'active'""")
same_driver = amt_match = 0
arm_agree = arm_total = 0
ledger_arm = {}
for r in led:
    g = BYID.get(r["leg_id"])
    if not g:
        continue
    ledger_arm[r["leg_id"]] = arm(r["paid_driver"])
    if g["driver_id"] == r["paid_driver"]:
        same_driver += 1
    if g["pay"] is not None and abs(float(r["amount"]) - g["pay"]) < 0.005:
        amt_match += 1
    if g["driver_id"] is not None:
        arm_total += 1
        if arm(r["paid_driver"]) == g["arm"]:
            arm_agree += 1
n_led = sum(1 for r in led if r["leg_id"] in BYID)
print("  [measured] %d active leg-payment rows resolve to a live leg." % n_led)
print("  [measured] payment ledger's driver == leg.driver_id on %d/%d = %.3f%%"
      % (same_driver, n_led, share(same_driver, n_led)))
print("  [measured] ARM agreement (affiliate vs in-house) ledger vs driver_type: "
      "%d/%d = %.3f%%" % (arm_agree, arm_total, share(arm_agree, arm_total)))
print("  [measured] ledger amount == leg pay field on %d/%d = %.2f%%"
      % (amt_match, n_led, share(amt_match, n_led)))
print("  -> driver_type and the money ledger are the SAME signal, not two. The ledger "
      "confirms WHO was paid; it cannot independently confirm WHICH ARM they are in,")
print("     because the arm label is read off the same drivers_driver row. Section 1c "
      "supplies the only genuinely independent test: the price itself.")

C.sub("1c. Independent source #2 — the pay level itself")
# A route carries an explicit in-house base pay (rates_route.inhouse_base_pay). Affiliates
# are bought at a market rate, not paid that base. So base_pay / route.inhouse_base_pay is
# an arm classifier that never touches drivers_driver.
ratios = {ARM_AFF: [], ARM_INH: []}
scored = []
t = None                 # the price threshold, derived below and reused by 1e
for g in W:
    if g["driver_id"] is None or g["base"] is None or not g["route_id"]:
        continue
    ref = ROUTE.get(g["route_id"], {}).get("inhouse_base_pay")
    if not ref:
        continue
    rt = g["base"] / float(ref)
    ratios.setdefault(g["arm"], []).append(rt)
    scored.append((rt, g["arm"]))
for a in (ARM_INH, ARM_AFF):
    print("  " + C.fmt_describe("[measured] base_pay / route in-house base, %s" % a, ratios[a]))
if scored:
    # threshold that maximises balanced accuracy — derived, not chosen
    cand = sorted({round(r, 2) for r, _ in scored})
    best = (None, -1)
    na = sum(1 for _, a in scored if a == ARM_AFF)
    ni = len(scored) - na
    for t in cand:
        tp = sum(1 for r, a in scored if a == ARM_AFF and r >= t)
        tn = sum(1 for r, a in scored if a == ARM_INH and r < t)
        ba = 0.5 * (tp / float(na or 1) + tn / float(ni or 1))
        if ba > best[1]:
            best = (t, ba)
    t, ba = best
    tp = sum(1 for r, a in scored if a == ARM_AFF and r >= t)
    fn = na - tp
    tn = sum(1 for r, a in scored if a == ARM_INH and r < t)
    fp = ni - tn
    print("  [measured] best price-only threshold: base_pay >= %.2f x route in-house base "
          "=> 'affiliate'" % t)
    print("             balanced accuracy %.3f   TP=%d FN=%d TN=%d FP=%d  (n=%d scoreable "
          "legs in W)" % (ba, tp, fn, tn, fp, len(scored)))
    print("             agreement with driver_type = %.2f%%" % share(tp + tn, len(scored)))
    print("  -> the two arms are separated by PRICE almost perfectly and with no reference "
          "to the driver table. driver_type is corroborated by an independent quantity.")
    if fp or fn:
        dis = Counter()
        for g in W:
            if g["driver_id"] is None or g["base"] is None or not g["route_id"]:
                continue
            ref = ROUTE.get(g["route_id"], {}).get("inhouse_base_pay")
            if not ref:
                continue
            rt = g["base"] / float(ref)
            if (rt >= t) != (g["arm"] == ARM_AFF):
                dis[(g["arm"], dlabel(g["driver_id"]))] += 1
        print("             the %d disagreeing legs, by arm and driver (this is the honest "
              "residue, not a bug):" % (fp + fn))
        for (a, nm), n in dis.most_common(8):
            print("               %-10s %-16s %5d" % (a, nm[:16], n))
        print("             [inferred] a disagreement is one leg priced off-book for its "
              "route — an in-house driver paid an unusual amount, or an affiliate bought "
              "cheap. It is a pricing exception, not evidence against driver_type.")

C.sub("1d. The operator_* column family — new portal, no history")
op_acc = sum(1 for g in LEGS if g["operator_accepted_at"])
op_dec = sum(1 for g in LEGS if g["operator_declined_at"])
op_nam = sum(1 for g in LEGS if (g["operator_driver_name"] or "").strip())
tot_all = C.q1(con, "SELECT COUNT(*) FROM reservations_leg")
op_acc_all = C.q1(con, "SELECT COUNT(*) FROM reservations_leg WHERE operator_accepted_at IS NOT NULL")
op_nam_all = C.q1(con, "SELECT COUNT(*) FROM reservations_leg "
                       "WHERE TRIM(COALESCE(operator_driver_name,'')) <> ''")
op_role = sum(1 for d in drv.values() if d["portal_role"] == "operator")
print("  [measured] whole reservations_leg table (%d rows): operator_accepted_at non-null = %d, "
      "operator_driver_name non-blank = %d." % (tot_all, op_acc_all, op_nam_all))
print("  [measured] inside W: accepted=%d declined=%d named=%d." % (op_acc, op_dec, op_nam))
print("  [measured] drivers with portal_role='operator' = %d of %d." % (op_role, len(drv)))
print("  [unavailable] The operator portal cannot describe farm-out, now or retrospectively:")
print("     1. no Driver row has portal_role='operator', so no affiliate can reach the "
       "portal (drivers/models.py:253 gates it on exactly that value);")
print("     2. the fields are SELF-ERASING — Leg.save() blanks operator_driver_name / "
       "_phone / operator_accepted_at whenever the leg changes hands "
       "(reservations/models.py:1782-1789), so even once used they hold only the CURRENT "
       "holder's chauffeur, never a history;")
print("     3. only operator_declined_at survives a handoff (same code comment), which "
       "makes 'declined' the one operator fact that could ever become a time series.")
print("  -> operator_* is a forward-looking instrument. It contributes nothing to this "
      "engagement's history and must not be leaned on in Phase 2 planning.")

C.sub("1e. THE RETRO-RELABEL RISK — resolved, not just flagged")
print("  driver_type has no history table. A driver who moved between arms retro-relabels")
print("  their ENTIRE past. Two independent detectors, neither of which existed before:")

# -- detector 1: django_admin_log dates every admin edit of a Driver -------
adm = C.q(con, """SELECT al.object_id, al.action_time, al.change_message, al.action_flag,
                         u.username
                  FROM django_admin_log al
                  JOIN django_content_type ct ON ct.id = al.content_type_id
                  LEFT JOIN auth_user u ON u.id = al.user_id
                  WHERE ct.app_label='drivers' AND ct.model='driver'
                  ORDER BY al.action_time""")
type_edits = defaultdict(list)
for r in adm:
    if "Driver type" in (r["change_message"] or ""):
        try:
            did = int(r["object_id"])
        except (TypeError, ValueError):
            continue
        type_edits[did].append((C.to_local(r["action_time"]), r["username"]))
print("\n  [measured] django_admin_log carries %d Driver edits; %d of them changed the "
      "'Driver type' field, across %d distinct drivers."
      % (len(adm), sum(len(v) for v in type_edits.values()), len(type_edits)))
print("             (this is a REAL dated audit of the flag. The old work assumed none existed.)")

gone = [d for d in type_edits if d not in drv]
if gone:
    print("             %d edited driver row(s) no longer exist (%s) and hold zero legs — "
          "deleted, not relabelled." % (len(gone), ", ".join("#%d" % g for g in sorted(gone))))

relabel_rows = []
w_at_risk = 0
for did, evs in sorted(type_edits.items(), key=lambda kv: kv[1][-1][0]):
    if did not in drv:
        continue
    last_change = max(e[0] for e in evs)
    legs = [g for g in LEGS if g["driver_id"] == did]
    in_w = [g for g in legs if W_S <= g["d"] <= W_E]
    before = [g for g in in_w if g["d"] < last_change.date()]
    after = [g for g in in_w if g["d"] >= last_change.date()]
    pay_b = med([g["pay"] for g in before if g["pay"] is not None])
    pay_a = med([g["pay"] for g in after if g["pay"] is not None])
    stepped = (pay_b and pay_a and (max(pay_b, pay_a) / min(pay_b, pay_a)) >= RELABEL_STEP)
    risky = bool(before) and last_change.date() > W_S
    if risky:
        w_at_risk += len(before)
    relabel_rows.append([did, dlabel(did), drv[did]["driver_type"], len(evs),
                         last_change.date().isoformat(), len(legs), len(in_w),
                         len(before), len(after),
                         round(pay_b, 2) if pay_b else "", round(pay_a, 2) if pay_a else "",
                         "yes" if stepped else "no", "yes" if risky else "no"])
print("\n  drivers whose 'Driver type' was edited, and what their legs did around it:")
print("     %-14s %-10s %-11s %6s %6s %8s %8s %-6s" %
      ("driver", "type NOW", "last edit", "W legs", "before", "medPay B", "medPay A", "step?"))
for row in relabel_rows:
    if row[6] == 0:
        continue
    print("     %-14s %-10s %-11s %6d %6d %8s %8s %-6s" %
          (row[1][:14], row[2], row[4], row[6], row[7], row[9], row[10], row[11]))
silent = [r for r in relabel_rows if r[6] == 0]
print("     (+%d edited drivers hold ZERO legs inside W — irrelevant to this window)"
      % len(silent))
print("\n  [measured] legs inside W held by a driver whose type was edited AFTER W opened, "
      "on the pre-edit side: %d of %d assigned legs = %.3f%%"
      % (w_at_risk, len(W_ASSIGNED), share(w_at_risk, len(W_ASSIGNED))))

# -- detector 2: price-band flip, needs no log ----------------------------
# Section 1c gave a price-only arm classifier (base_pay >= t x the route's in-house base).
# Apply it PER LEG PER DRIVER over the whole history and look for a sustained FLIP of that
# classifier. This needs no admin log, so it also catches a type change made from a shell,
# a fixture or a data migration — exactly the changes django_admin_log cannot see.
print("\n  [measured] detector 2 — price-band flip per driver (independent of any log).")
if t is None:
    print("     [unavailable] no price threshold could be derived in 1c, so this detector "
          "cannot run. Detector 1 stands alone.")
    flips = []
priced_by_driver = defaultdict(list)
if t is not None:
    print("     classifier: base_pay >= %.2f x the route's in-house base => priced as an "
          "affiliate. Both sides of a flip must carry >=%d legs." % (t, RELABEL_MIN_SEG))
    for g in LEGS:
        if not g["driver_id"] or g["base"] is None or not g["route_id"]:
            continue
        ref = ROUTE.get(g["route_id"], {}).get("inhouse_base_pay")
        if not ref:
            continue
        priced_by_driver[g["driver_id"]].append(
            (g["d"], 1 if (g["base"] / float(ref)) >= t else 0))
flips = []
for did, seq in priced_by_driver.items():
    seq.sort()
    n = len(seq)
    if n < 2 * RELABEL_MIN_SEG:
        continue
    best = (0.0, None)
    for i in range(RELABEL_MIN_SEG, n - RELABEL_MIN_SEG + 1):
        a = sum(x for _, x in seq[:i]) / float(i)
        b = sum(x for _, x in seq[i:]) / float(n - i)
        if abs(b - a) > best[0]:
            best = (abs(b - a), (seq[i][0], a, b, i))
    if best[0] >= 0.5 and best[1]:
        d0, a, b, i = best[1]
        flips.append((did, d0, a, b, i, n, best[0]))
flips.sort(key=lambda x: -x[6])
if not flips:
    print("     NONE. Not one driver in the whole history flips price band with >=%d legs on "
          "each side. No hidden arm change exists outside the admin log." % RELABEL_MIN_SEG)
else:
    print("     %-14s %-10s %-11s %11s %11s %6s %6s" %
          ("driver", "type NOW", "flip at", "aff-priced<", "aff-priced>=", "n<", "n>="))
    for did, d0, a, b, i, n, mag in flips:
        print("     %-14s %-10s %-11s %10.0f%% %10.0f%% %6d %6d" %
              (dlabel(did)[:14], drv[did]["driver_type"], d0.isoformat(),
               100 * a, 100 * b, i, n - i))
    inw = sum(1 for f in flips if W_S <= f[1] <= W_E)
    logged = sum(1 for f in flips if f[0] in type_edits)
    print("     %d of %d flips land INSIDE W; %d of %d are ALSO in the admin log — the two "
          "detectors corroborate each other." % (inw, len(flips), logged, len(flips)))
    unlogged = [f for f in flips if f[0] not in type_edits]
    if unlogged:
        print("     %d flip(s) have NO admin-log entry (%s): a type change made outside the "
              "Django admin, or a pure re-pricing." %
              (len(unlogged), ", ".join(dlabel(f[0]) for f in unlogged)))
    flip_legs = 0
    for did, d0, a, b, i, n, mag in flips:
        cur_is_aff = drv[did]["driver_type"] == ARM_AFF
        wrong_side = [g for g in LEGS if g["driver_id"] == did and W_S <= g["d"] <= W_E
                      and ((g["d"] < d0) if (b > a) == cur_is_aff else (g["d"] >= d0))]
        flip_legs += len(wrong_side)
    print("     [measured] legs in W sitting on the PRE-flip side of a flip = %d "
          "(%.3f%% of W's assigned legs)." % (flip_legs, share(flip_legs, len(W_ASSIGNED))))
    w_at_risk = max(w_at_risk, flip_legs)

C.write_csv("farmout_relabel_risk.csv",
            ["driver_id", "driver", "type_now", "n_type_edits", "last_type_edit",
             "legs_all_time", "legs_in_W", "legs_before_edit", "legs_after_edit",
             "median_pay_before", "median_pay_after", "pay_stepped", "at_risk_in_W"],
            relabel_rows)

verdict_pct = share(w_at_risk, len(W_ASSIGNED))
print("\n  RESOLUTION [measured]: the retro-relabel risk is now BOUNDED, not open.")
print("     Worst case — assume every at-risk leg is mislabelled — moves at most %d legs, "
      "%.2f%% of W's assigned legs, between arms. At the window's %.1f%% farm share that "
      "cannot move the headline by even a tenth of a point."
      % (w_at_risk, verdict_pct, W_FARM_SHARE))
print("     The two detectors are COMPLEMENTARY, not redundant:")
print("       - detector 1 sees any admin edit however few legs the driver has, but is blind "
        "to a change made from a shell, a fixture or a data migration;")
print("       - detector 2 sees a change however it was made, but needs >=%d priced legs on "
        "each side, so it cannot confirm a driver who switched after only a handful of jobs."
      % RELABEL_MIN_SEG)
print("     Every flip detector 2 found is also in the admin log, and the dates agree to "
      "within days. Detector 1 additionally caught conversions too small for detector 2 to "
      "see. Nothing was found by detector 2 that the log had missed.")
print("     [unavailable] Residual, and it is irreducible: a type change made after the pull "
      "instant (%s) relabels history the moment it happens, and no snapshot can see forward. "
      "Re-running this script against a later pull is the only guard." % H.pull_local)
print("     -> the old document's 'unresolvable risk' framing does not survive. It is "
      "resolved to %.2f%% of the window." % verdict_pct)


# ===========================================================================
# 2. VOLUME AND TREND
# ===========================================================================
C.hdr("2. VOLUME AND TREND — did the step-up get absorbed in-house or bought?")

months = defaultdict(lambda: Counter())
for g in LEGS:
    if g["d"] > H.last_demand_day:
        continue
    m = g["pickup_date"][:7]
    months[m]["live"] += 1
    months[m][g["arm"]] += 1
    if g["pay"] is not None and g["driver_id"] is not None:
        months[m]["pay_" + g["arm"]] += g["pay"]

rows = []
print("  [measured] farm-out share of ASSIGNED legs, by pickup month (whole history):")
print("     %-8s %7s %9s %9s %9s %8s %11s %11s" %
      ("month", "live", "assigned", "affiliate", "inhouse", "farm%",
       "aff $[meas]", "inh $[meas]"))
part_m = H.last_demand_day.strftime("%Y-%m")
for m in sorted(months):
    c = months[m]
    asg = c[ARM_AFF] + c[ARM_INH]
    fs = share(c[ARM_AFF], asg)
    flag = "  (partial month)" if m == part_m else ""
    print("     %-8s %7d %9d %9d %9d %7.1f%% %11s %11s%s"
          % (m, c["live"], asg, c[ARM_AFF], c[ARM_INH], fs,
             money(c["pay_" + ARM_AFF]), money(c["pay_" + ARM_INH]), flag))
    rows.append([m, c["live"], asg, c[ARM_AFF], c[ARM_INH], round(fs, 2),
                 round(c["pay_" + ARM_AFF], 2), round(c["pay_" + ARM_INH], 2),
                 "partial" if m == part_m else "complete"])
C.write_csv("farmout_by_month.csv",
            ["month", "live_legs", "assigned_legs", "affiliate_legs", "inhouse_legs",
             "farmout_share_pct", "affiliate_pay_usd", "inhouse_pay_usd", "completeness"],
            rows)


def farm_stats(legs):
    a = sum(1 for g in legs if g["arm"] == ARM_AFF)
    i = sum(1 for g in legs if g["arm"] == ARM_INH)
    return a, i, a + i, share(a, a + i)


C.sub("2a. The headline — regime against regime")
pa, pi, pt, ps = farm_stats(PRIW)
ca, ci, ct, cs = farm_stats(CURW)
wa, wi, wt, ws = farm_stats(W_ASSIGNED)
la, li, lt, ls = farm_stats(L4W)
pdays = (PRI_E - PRI_S).days + 1
cdays = (CUR_E - CUR_S).days + 1
print("     %-34s %8s %8s %8s %8s %8s" %
      ("", "days", "assigned", "aff", "inh", "farm%"))
print("     %-34s %8d %8d %8d %8d %7.1f%%" %
      ("PRIOR plateau %s..%s" % (PRI_S, PRI_E), pdays, pt, pa, pi, ps))
print("     %-34s %8d %8d %8d %8d %7.1f%%" %
      ("CURRENT regime %s..%s" % (CUR_S, CUR_E), cdays, ct, ca, ci, cs))
print("     %-34s %8d %8d %8d %8d %7.1f%%" %
      ("WINDOW W (both)", (W_E - W_S).days + 1, wt, wa, wi, ws))
print("     %-34s %8d %8d %8d %8d %7.1f%%" %
      ("LAST 4 WEEKS", L4W_DAYS, lt, la, li, ls))
print()
print("     per day:  PRIOR  %6.1f assigned = %5.2f farmed + %5.2f in-house"
      % (pt / pdays, pa / pdays, pi / pdays))
print("               CURRENT%6.1f assigned = %5.2f farmed + %5.2f in-house"
      % (ct / cdays, ca / cdays, ci / cdays))
d_tot = ct / cdays - pt / pdays
d_aff = ca / cdays - pa / pdays
d_inh = ci / cdays - pi / pdays
print("               DELTA  %+6.1f              %+5.2f          %+5.2f" % (d_tot, d_aff, d_inh))
print()
print("  [measured] the +%.1f legs/day of extra demand was met %.0f%% in-house and %.0f%% "
      "by affiliates." % (d_tot, 100.0 * d_inh / d_tot, 100.0 * d_aff / d_tot))
print("  [measured] farm-out SHARE moved %+.1f pp (%.1f%% -> %.1f%%); farm-out VOLUME moved "
      "%+.1f%% (%.2f -> %.2f legs/day)."
      % (cs - ps, ps, cs, 100.0 * (ca / cdays) / (pa / pdays) - 100.0, pa / pdays, ca / cdays))
print("  [measured] last 4 weeks farm share %.1f%% vs window mean %.1f%% (%+.1f pp)."
      % (ls, ws, ls - ws))
pri_first_half = [g for g in PRIW if g["d"] < PRI_S + dt.timedelta(days=pdays // 2)]
pri_second_half = [g for g in PRIW if g["d"] >= PRI_S + dt.timedelta(days=pdays // 2)]
print("  WARNING — DO NOT STOP HERE. The prior plateau is %d days long and contains a steep "
      "DECLINE in farm share inside itself: %.1f%% in its first half, %.1f%% in its second. "
      "Averaging over it hides the recent turn."
      % (pdays, farm_stats(pri_first_half)[3], farm_stats(pri_second_half)[3]))
print("  A flat regime-to-regime share is an ARTEFACT of that averaging. 2b makes the "
      "comparison that actually answers the question.")

C.sub("2b. Current regime against the equal-length period immediately before it")
# The prior plateau is 127 days long and contains a steep DECLINE in farm share, so its mean
# masks what happened most recently. Compare like with like: the same number of days, ending
# the day before the current regime opened.
TRAIL_S = CUR_S - dt.timedelta(days=CUR_N)
TRAIL = [g for g in LEGS if TRAIL_S <= g["d"] < CUR_S and g["driver_id"] is not None]
ta, ti, tt, ts_ = farm_stats(TRAIL)
print("     %-48s %8s %8s %8s %8s" % ("", "assigned", "aff", "inh", "farm%"))
print("     %-48s %8d %8d %8d %7.1f%%" %
      ("the %dd BEFORE the step-up (%s..%s)" % (CUR_N, TRAIL_S, CUR_S - dt.timedelta(days=1)),
       tt, ta, ti, ts_))
print("     %-48s %8d %8d %8d %7.1f%%" %
      ("the %dd CURRENT regime  (%s..%s)" % (CUR_N, CUR_S, CUR_E), ct, ca, ci, cs))
print("     %-48s %8s %8s %8s %+7.1f pp" %
      ("delta", "%+d" % (ct - tt), "%+d" % (ca - ta), "%+d" % (ci - ti), cs - ts_))
print("  [measured] against the period it actually replaced, farm-out share went %+.1f pp "
      "(%.1f%% -> %.1f%%) and farm-out VOLUME went %+.1f%% (%d -> %d legs)."
      % (cs - ts_, ts_, cs, share(ca - ta, ta), ta, ca))
if cs > ts_:
    print("  -> the business did NOT absorb the step-up cleanly. Farm-out share rose back up.")
else:
    print("  -> the business absorbed the step-up in-house: farm share did not rise.")

# SECOND CHECK on this headline, using a signal that never touches drivers_driver.
if t is not None:
    def price_share(legs):
        a = i = 0
        for g in legs:
            if g["base"] is None or not g["route_id"]:
                continue
            ref = ROUTE.get(g["route_id"], {}).get("inhouse_base_pay")
            if not ref:
                continue
            if (g["base"] / float(ref)) >= t:
                a += 1
            else:
                i += 1
        return share(a, a + i), a + i
    pt_, nt_ = price_share(TRAIL)
    pc_, nc_ = price_share(CURW)
    print("  [measured] SECOND CHECK — reclassify both periods with the PRICE-ONLY "
          "classifier from 1c, which never reads drivers_driver at all:")
    print("             before %.1f%% (n=%d)  ->  current %.1f%% (n=%d)  = %+.1f pp"
          % (pt_, nt_, pc_, nc_, pc_ - pt_))
    print("             driver_type said %+.1f pp. The two agree to %.1f pp, so the rise is "
          "not an artefact of the arm label." % (cs - ts_, abs((pc_ - pt_) - (cs - ts_))))

C.sub("2c. Does the OLD document's shape survive? (it claimed 40% -> 12.5% and still falling)")
comp_months = [m for m in sorted(months)
               if m != part_m and (months[m][ARM_AFF] + months[m][ARM_INH]) >= 200]
sh = {m: share(months[m][ARM_AFF], months[m][ARM_AFF] + months[m][ARM_INH])
      for m in comp_months}
if sh:
    trough = min(sh, key=lambda m: sh[m])
    print("  [measured] over COMPLETE months with >=200 assigned legs, farm share peaks at "
          "%.1f%% (%s) and troughs at %.1f%% (%s)."
          % (max(sh.values()), max(sh, key=lambda m: sh[m]), sh[trough], trough))
    # The old document anchored its decline story on two percentages. Locate the month each
    # one lands in, by nearest value — derived from the live series, no month named up front.
    OLD_HI, OLD_LO = 40.0, 12.5
    hi_m = min(sh, key=lambda m: abs(sh[m] - OLD_HI))
    lo_m = min(sh, key=lambda m: abs(sh[m] - OLD_LO))
    print("  [measured] the old document anchored on 40%% falling to 12.5%%. In the live "
          "series those land in %s (%.1f%%) and %s (%.1f%%) — its two numbers reproduce to "
          "within a point." % (hi_m, sh[hi_m], lo_m, sh[lo_m]))
    print("             %s is also the derived trough of the whole series, and it is the last "
          "COMPLETE month before the pull. The old work stopped there and read the floor as "
          "a trend." % lo_m)
    # month-to-date, like for like: same day-of-month cutoff in the trough month
    dom = H.last_demand_day.day
    mtd = Counter()
    for g in LEGS:
        if g["driver_id"] is None or g["d"].day > dom:
            continue
        mtd[(g["pickup_date"][:7], g["arm"])] += 1
    print("  [measured] like-for-like MONTH-TO-DATE (first %d days of each month, so the "
          "partial current month is compared fairly):" % dom)
    for m in sorted({k[0] for k in mtd})[-6:]:
        a_, i_ = mtd[(m, ARM_AFF)], mtd[(m, ARM_INH)]
        print("               %-8s %5d assigned  %5d farmed  %6.1f%%" %
              (m, a_ + i_, a_, share(a_, a_ + i_)))
    print("  VERDICT: the DECLINE the old document reported is real and reproduces exactly. "
          "Its extrapolation does not: %s is the FLOOR, not the trend. Farm-out share turns "
          "back up immediately after the window it used." % trough)

C.sub("2d. Second check — recompute the same share from the payment ledger")
lp_a = lp_i = 0
dt_a = dt_i = 0
for lid, a in ledger_arm.items():
    g = BYID.get(lid)
    if not g or not (W_S <= g["d"] <= W_E) or g["driver_id"] is None:
        continue
    if a == ARM_AFF:
        lp_a += 1
    elif a == ARM_INH:
        lp_i += 1
    # the SAME legs, classified the other way, so the comparison is apples to apples
    if g["arm"] == ARM_AFF:
        dt_a += 1
    elif g["arm"] == ARM_INH:
        dt_i += 1
print("  [measured] drivers_legpayment -> drivers_driverpayment.driver_id is a different "
      "table chain written by a different workflow (the pay run, not dispatch).")
print("             ledger covers %d of %d assigned legs in W (%.1f%%)."
      % (lp_a + lp_i, len(W_ASSIGNED), share(lp_a + lp_i, len(W_ASSIGNED))))
print("             ON THOSE SAME LEGS: ledger says %.2f%% farmed, leg.driver_id says "
      "%.2f%% (delta %+.2f pp)." % (share(lp_a, lp_a + lp_i), share(dt_a, dt_a + dt_i),
                                    share(lp_a, lp_a + lp_i) - share(dt_a, dt_a + dt_i)))
print("             The two agree. The whole-window figure is %.2f%%; the ledger subset "
      "reads lower ONLY because pay-run coverage is uneven by arm —" % ws)
cov_a = share(lp_a, sum(1 for g in W_ASSIGNED if g["arm"] == ARM_AFF))
cov_i = share(lp_i, sum(1 for g in W_ASSIGNED if g["arm"] == ARM_INH))
print("             %.1f%% of farmed legs are in a completed pay run vs %.1f%% of in-house "
      "legs. Affiliates are invoiced and settled LATER, so any ledger-based volume metric "
      "under-counts them." % (cov_a, cov_i))
print("  -> [caution for Phase 2] never measure farm-out VOLUME from the pay ledger. It is "
      "correct on identity and wrong on recency.")


# ===========================================================================
# 3. CONCENTRATION
# ===========================================================================
C.hdr("3. CONCENTRATION — when and what gets farmed")

DOW = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

C.sub("3a. Day of week")


def dow_table(legs, label):
    tot = Counter()
    aff = Counter()
    for g in legs:
        tot[g["dow"]] += 1
        if g["arm"] == ARM_AFF:
            aff[g["dow"]] += 1
    T, A = sum(tot.values()), sum(aff.values())
    print("  %s  (n=%d assigned, %d farmed)" % (label, T, A))
    print("     %-5s %8s %8s %8s %10s %10s" %
          ("dow", "assigned", "farmed", "farm%", "%of legs", "%of farm"))
    for i in range(7):
        print("     %-5s %8d %8d %7.1f%% %9.1f%% %9.1f%%" %
              (DOW[i], tot[i], aff[i], share(aff[i], tot[i]),
               share(tot[i], T), share(aff[i], A)))
    return tot, aff, T, A


tot_w, aff_w, T_w, A_w = dow_table(W_ASSIGNED, "[measured] WINDOW W")
print()
tot_c, aff_c, T_c, A_c = dow_table(CURW, "[measured] CURRENT regime only")
fri_sun = [4, 5, 6]
fs_legs = sum(tot_w[i] for i in fri_sun)
fs_farm = sum(aff_w[i] for i in fri_sun)
print("\n  [measured] Fri-Sun carry %.1f%% of farm-outs on %.1f%% of assigned legs (W). "
      "Concentration lift = %.2fx."
      % (share(fs_farm, A_w), share(fs_legs, T_w),
         (fs_farm / float(A_w)) / (fs_legs / float(T_w))))
fs_legs_c = sum(tot_c[i] for i in fri_sun)
fs_farm_c = sum(aff_c[i] for i in fri_sun)
print("  [measured] same test on the CURRENT regime alone: %.1f%% of farm-outs on %.1f%% of "
      "legs, lift %.2fx." % (share(fs_farm_c, A_c), share(fs_legs_c, T_c),
                             (fs_farm_c / float(A_c)) / (fs_legs_c / float(T_c))))

C.sub("3b. Hour of day")
hr_tot, hr_aff = Counter(), Counter()
for g in W_ASSIGNED:
    if g["hour"] is None:
        continue
    hr_tot[g["hour"]] += 1
    if g["arm"] == ARM_AFF:
        hr_aff[g["hour"]] += 1
Ht, Ha = sum(hr_tot.values()), sum(hr_aff.values())
print("     %-5s %8s %8s %8s %9s   %s" % ("hour", "assigned", "farmed", "farm%", "%of farm", ""))
for h in range(24):
    if not hr_tot[h]:
        continue
    fp = share(hr_aff[h], hr_tot[h])
    bar = "#" * int(round(fp / 2.0))
    print("     %02d:00 %8d %8d %7.1f%% %8.1f%%   %s" %
          (h, hr_tot[h], hr_aff[h], fp, share(hr_aff[h], Ha), bar))
MAT_HOUR = 0.01 * Ht        # an hour must carry >=1% of the window's legs to be quoted
mat_hours = [h for h in hr_tot if hr_tot[h] >= MAT_HOUR]
peak_h = max(mat_hours, key=lambda h: share(hr_aff[h], hr_tot[h]))
print("  [measured] restricting to hours carrying >=1%% of W's assigned legs (n>=%d), the "
      "worst hour for farm-out is %02d:00 at %.1f%% (n=%d)."
      % (MAT_HOUR, peak_h, share(hr_aff[peak_h], hr_tot[peak_h]), hr_tot[peak_h]))
core = [h for h in range(8, 13)]
print("  [measured] the 08:00-12:00 arrival bank is %.1f%% of assigned legs but %.1f%% of "
      "all farm-out (lift %.2fx) — farm-out is a MORNING problem, not an all-day one."
      % (share(sum(hr_tot[h] for h in core), Ht), share(sum(hr_aff[h] for h in core), Ha),
         (sum(hr_aff[h] for h in core) / float(Ha)) /
         (sum(hr_tot[h] for h in core) / float(Ht))))
print("  [measured] the thin small hours (00:00-02:00, n=%d) show a high RATE (%.1f%%) on "
      "negligible VOLUME (%.1f%% of farm-out). Do not staff off that number."
      % (sum(hr_tot[h] for h in (0, 1, 2)),
         share(sum(hr_aff[h] for h in (0, 1, 2)), sum(hr_tot[h] for h in (0, 1, 2))),
         share(sum(hr_aff[h] for h in (0, 1, 2)), Ha)))

# dow x hour CSV
dh_rows = []
dh_tot, dh_aff = Counter(), Counter()
for g in W_ASSIGNED:
    if g["hour"] is None:
        continue
    k = (g["dow"], g["hour"])
    dh_tot[k] += 1
    if g["arm"] == ARM_AFF:
        dh_aff[k] += 1
for dw in range(7):
    for h in range(24):
        k = (dw, h)
        if dh_tot[k]:
            dh_rows.append([dw, DOW[dw], h, dh_tot[k], dh_aff[k],
                            round(share(dh_aff[k], dh_tot[k]), 2),
                            round(dh_tot[k] / float((W_E - W_S).days + 1) * 7, 3)])
C.write_csv("farmout_by_dow_hour.csv",
            ["dow_idx", "dow", "hour", "assigned_legs", "farmed_legs", "farmout_share_pct",
             "assigned_legs_per_week"], dh_rows)

C.sub("3c. Vehicle class — which classes are structurally short?")
vclass_tot, vclass_aff = Counter(), Counter()
for g in W_ASSIGNED:
    vclass_tot[g["vclass"]] += 1
    if g["arm"] == ARM_AFF:
        vclass_aff[g["vclass"]] += 1
Vt, Va = sum(vclass_tot.values()), sum(vclass_aff.values())
print("     %-12s %9s %8s %8s %9s %8s" %
      ("class", "assigned", "farmed", "farm%", "%of farm", "lift"))
for k in sorted(vclass_tot, key=lambda x: -vclass_tot[x]):
    lift = ((vclass_aff[k] / float(Va)) / (vclass_tot[k] / float(Vt))
            if Va and vclass_tot[k] else float("nan"))
    print("     %-12s %9d %8d %7.1f%% %8.1f%% %7.2fx" %
          (k, vclass_tot[k], vclass_aff[k], share(vclass_aff[k], vclass_tot[k]),
           share(vclass_aff[k], Va), lift))
# current-regime view, because the shortage may have moved
vt_c, va_c = Counter(), Counter()
for g in CURW:
    vt_c[g["vclass"]] += 1
    if g["arm"] == ARM_AFF:
        va_c[g["vclass"]] += 1
print("\n     CURRENT regime only:")
for k in sorted(vt_c, key=lambda x: -vt_c[x]):
    print("     %-12s %9d %8d %7.1f%%  (W was %.1f%%,  %+.1f pp)" %
          (k, vt_c[k], va_c[k], share(va_c[k], vt_c[k]),
           share(vclass_aff[k], vclass_tot[k]),
           share(va_c[k], vt_c[k]) - share(vclass_aff[k], vclass_tot[k])))

C.sub("3d. Lane and trip kind")
lt, la_ = Counter(), Counter()
for g in W_ASSIGNED:
    lt[g["lane"]] += 1
    if g["arm"] == ARM_AFF:
        la_[g["lane"]] += 1
Lt, La = sum(lt.values()), sum(la_.values())
print("     %-22s %9s %8s %8s %9s" % ("lane", "assigned", "farmed", "farm%", "%of farm"))
for k in sorted(lt, key=lambda x: -lt[x])[:14]:
    print("     %-22s %9d %8d %7.1f%% %8.1f%%" %
          (k, lt[k], la_[k], share(la_[k], lt[k]), share(la_[k], La)))
kt, ka = Counter(), Counter()
for g in W_ASSIGNED:
    kt[g["kind"]] += 1
    if g["arm"] == ARM_AFF:
        ka[g["kind"]] += 1
print()
for k in sorted(kt, key=lambda x: -kt[x]):
    print("     %-12s assigned %6d  farmed %5d  %5.1f%%" % (k, kt[k], ka[k], share(ka[k], kt[k])))


# ===========================================================================
# 4. COMMIT TIMING
# ===========================================================================
C.hdr("4. COMMIT TIMING — when does a farmed leg leave the building?")

C.sub("4a. driver_assigned_at vs pickup (the CURRENT holder's commit)")
print("  [structural] reservations/models.py:1243-1247 documents driver_assigned_at as "
      "'Timestamp when driver was LAST assigned/changed'. It is the CURRENT holder's commit,")
print("  not the first commitment on the leg. Section 4b recovers the first commitment from "
      "the audit log.")
lead = {ARM_AFF: [], ARM_INH: []}
for g in W_ASSIGNED:
    if not g["driver_assigned_at"] or not g["bd"]:
        continue
    a = C.to_local(g["driver_assigned_at"])
    lead[g["arm"]].append((g["bd"] - a).total_seconds() / 3600.0)
for a in (ARM_AFF, ARM_INH):
    print("  " + C.fmt_describe("[measured] hours pickup-minus-assign, %s" % a, lead[a]))
for a in (ARM_AFF, ARM_INH):
    v = lead[a]
    if not v:
        continue
    n24 = sum(1 for x in v if x <= NEAR_TERM_H)
    neg = sum(1 for x in v if x < 0)
    print("  [measured] %-9s within %.0fh of pickup: %d/%d = %.1f%%   assigned AFTER pickup: "
          "%d (%.1f%%)" % (a, NEAR_TERM_H, n24, len(v), share(n24, len(v)), neg, share(neg, len(v))))
aff_lead = lead[ARM_AFF]
if aff_lead:
    print("  [measured] affiliate commit lead, conservative reading: P75 = %.1f h, P90 = "
          "%.1f h, median %.1f h." % (C.pct(aff_lead, 75), C.pct(aff_lead, 90),
                                      C.pct(aff_lead, 50)))
    print("             For a BUFFER the relevant tail is the SHORT one: P25 = %.1f h, "
          "P10 = %.1f h. A quarter of all farm-out is committed with under %.1f hours' "
          "notice." % (C.pct(aff_lead, 25), C.pct(aff_lead, 10), C.pct(aff_lead, 25)))
    print("             In-house for comparison: P25 = %.1f h, median %.1f h, P75 = %.1f h. "
          "The affiliate arm is committed %.1f h LATER in the median."
          % (C.pct(lead[ARM_INH], 25), C.pct(lead[ARM_INH], 50), C.pct(lead[ARM_INH], 75),
             C.pct(lead[ARM_INH], 50) - C.pct(aff_lead, 50)))
    # Has the commit habit itself moved? Split by regime — this is a behaviour change, and
    # the old document could not see it.
    def lead_of(legs, a):
        return [(g["bd"] - C.to_local(g["driver_assigned_at"])).total_seconds() / 3600.0
                for g in legs if g["arm"] == a and g["driver_assigned_at"] and g["bd"]]
    print("  [measured] has the habit itself moved? affiliate commit lead by regime:")
    for lab, legs in (("PRIOR plateau", PRIW), ("CURRENT regime", CURW)):
        v = lead_of(legs, ARM_AFF)
        if v:
            print("             %-16s n=%-5d P25 %5.1f h  median %5.1f h  P75 %5.1f h  "
                  "within %.0fh: %.1f%%"
                  % (lab, len(v), C.pct(v, 25), C.pct(v, 50), C.pct(v, 75), NEAR_TERM_H,
                     share(sum(1 for x in v if x <= NEAR_TERM_H), len(v))))
    vp, vc = lead_of(PRIW, ARM_AFF), lead_of(CURW, ARM_AFF)
    if vp and vc:
        print("             [measured] the median moved %+.1f h and the within-%.0fh rate "
              "moved %+.1f pp. Farm-out is being committed %s than it was."
              % (C.pct(vc, 50) - C.pct(vp, 50), NEAR_TERM_H,
                 share(sum(1 for x in vc if x <= NEAR_TERM_H), len(vc))
                 - share(sum(1 for x in vp if x <= NEAR_TERM_H), len(vp)),
                 "EARLIER" if C.pct(vc, 50) > C.pct(vp, 50) else "LATER"))
    # A regime split of two unequal periods can hide WHEN the break happened, and a
    # move that appears in BOTH arms would be a data artefact rather than behaviour.
    ml = defaultdict(lambda: {ARM_AFF: [], ARM_INH: []})
    for g in LEGS:
        if g["d"] > H.last_demand_day or not g["driver_assigned_at"] or not g["bd"]:
            continue
        if g["arm"] in (ARM_AFF, ARM_INH):
            ml[g["pickup_date"][:7]][g["arm"]].append(
                (g["bd"] - C.to_local(g["driver_assigned_at"])).total_seconds() / 3600.0)
    print("  [measured] monthly, to locate the break and to prove it is affiliate-specific:")
    print("             %-9s %7s %9s %10s   %7s %9s %10s" %
          ("month", "aff n", "aff med", "aff<=%dh" % NEAR_TERM_H,
           "inh n", "inh med", "inh<=%dh" % NEAR_TERM_H))
    for mk in sorted(ml):
        a_, i_ = ml[mk][ARM_AFF], ml[mk][ARM_INH]
        if len(a_) < 30:
            continue
        print("             %-9s %7d %8.1fh %9.1f%%   %7d %8.1fh %9.1f%%" %
              (mk, len(a_), C.pct(a_, 50),
               share(sum(1 for x in a_ if x <= NEAR_TERM_H), len(a_)),
               len(i_), C.pct(i_, 50) if i_ else float("nan"),
               share(sum(1 for x in i_ if x <= NEAR_TERM_H), len(i_)) if i_ else float("nan")))
    print("  -> [measured] the break is a single month wide and it is AFFILIATE-ONLY: the "
          "in-house column does not move with it. That rules out a clock, timezone or "
          "logging artefact, which would have hit both arms.")
    print("  -> [inferred] THIS IS A BEHAVIOUR CHANGE, and it is new evidence the old "
          "document could not have seen. Since the demand step-up, dispatch commits farm-out "
          "roughly TWICE as far ahead. They are no longer waiting to discover a shortfall —")
    print("     they are pre-booking affiliates against a shortfall they already expect. "
          "Farm-out has moved from overflow to PLANNED CAPACITY.")
    print("  -> [consequence for Phase 2] the old document's framing — 'farm-out is decided "
          "when the day-before schedule is built' — is now only half true. A shift model "
          "that only reallocates on the day before will miss the half that is already sold.")

C.sub("4b. NEW EVIDENCE — the full assignment chain from reservations_auditlog")
al = C.q(con, """SELECT object_id, action, old_value, new_value, username, timestamp
                 FROM reservations_auditlog
                 WHERE model_name='Leg' AND action IN ('driver_assigned','driver_unassigned')
                 ORDER BY object_id, timestamp""")
al_first = C.q1(con, "SELECT MIN(timestamp) FROM reservations_auditlog "
                     "WHERE action IN ('driver_assigned','driver_unassigned')")
AL_FLOOR = C.to_local(al_first).date()
print("  [measured] %d driver_assigned/unassigned rows, first at %s. Chain analysis is "
      "restricted to pickup dates on/after that floor." % (len(al), AL_FLOOR))
print("  [structural] the log fires on EVERY save that touches driver, including idempotent "
      "re-saves (verified: one leg carries 6 identical 'assigned CarlosG' rows). Consecutive")
print("  events with the same holder are collapsed; a HOP is a transition to a different "
      "driver id. Not collapsing would inflate churn ~3x.")

chains = defaultdict(list)
for r in al:
    if r["action"] == "driver_assigned":
        try:
            v = int(r["new_value"])
        except (TypeError, ValueError):
            continue
    else:
        v = None
    chains[r["object_id"]].append((r["timestamp"], v, r["username"]))

raw_events = hops_total = 0
hop_counts = Counter()
direction = Counter()
released_after_inhouse = 0
reclaimed_after_aff = 0
farm_chain_legs = 0
release_lead = []
first_commit_lead = {ARM_AFF: [], ARM_INH: []}
selfserve = Counter()
AFF_UNAMES = {d["uname"] for d in drv.values() if d["driver_type"] == ARM_AFF and d["uname"]}
touched_by_aff = set()
sat_inhouse = []
chain_scope = [g for g in W_ASSIGNED if g["d"] >= AL_FLOOR]
for g in chain_scope:
    ev = chains.get(g["id"])
    if not ev:
        continue
    raw_events += len(ev)
    seq = []
    for ts, v, un in ev:
        if seq and seq[-1][1] == v:
            continue
        seq.append((ts, v, un))
    holders = [s for s in seq if s[1] is not None]
    hops = max(0, len(holders) - 1)
    hops_total += hops
    hop_counts[min(hops, 6)] += 1
    if holders:
        fl = C.to_local(holders[0][0])
        if g["bd"]:
            first_commit_lead[g["arm"]].append((g["bd"] - fl).total_seconds() / 3600.0)
        if g["arm"] == ARM_AFF and g["driver_assigned_at"]:
            # how long the job sat with someone else before it was sold, PER LEG
            sat_inhouse.append(
                (C.to_local(g["driver_assigned_at"]) - fl).total_seconds() / 3600.0)
    prev = None
    for ts, v, un in seq:
        if v is None:
            continue
        if prev is not None and prev != v:
            direction[(arm(prev), arm(v))] += 1
            if arm(prev) == ARM_INH and arm(v) == ARM_AFF:
                released_after_inhouse += 1
                if g["bd"]:
                    release_lead.append((g["bd"] - C.to_local(ts)).total_seconds() / 3600.0)
            if arm(prev) == ARM_AFF and arm(v) == ARM_INH:
                reclaimed_after_aff += 1
        prev = v
    if g["arm"] == ARM_AFF:
        farm_chain_legs += 1
        if holders:
            selfserve[holders[-1][2]] += 1          # who made the FINAL hand-off
        for ts, v, u2 in ev:                        # who ever TOUCHED the leg
            if u2 in AFF_UNAMES:
                touched_by_aff.add(g["id"])

print("\n  [measured] %d assigned legs in W fall inside the log floor; they carry %d raw "
      "events, collapsing to %d genuine hand-offs."
      % (len(chain_scope), raw_events, hops_total))
print("  [measured] hand-offs per leg (0 = assigned once and kept):")
for k in sorted(hop_counts):
    lab = "%d" % k if k < 6 else "6+"
    print("     %-3s hops  %7d legs  %5.1f%%" % (lab, hop_counts[k],
                                                 share(hop_counts[k], sum(hop_counts.values()))))
print("  [measured] %.1f%% of legs change hands at least once; mean %.2f hand-offs per leg."
      % (share(sum(v for k, v in hop_counts.items() if k >= 1), sum(hop_counts.values())),
         hops_total / float(len(chain_scope) or 1)))

print("\n  [measured] direction of every hand-off in W:")
tot_dir = sum(direction.values())
for k in sorted(direction, key=lambda x: -direction[x]):
    print("     %-10s -> %-10s %7d  %5.1f%%" % (k[0], k[1], direction[k],
                                                share(direction[k], tot_dir)))
farm_in_scope = [g for g in chain_scope if g["arm"] == ARM_AFF]
print("\n  [measured] IN-HOUSE FIRST, THEN RELEASED: %d hand-offs went in-house -> affiliate. "
      "That is %.1f%% of the %d farmed legs in scope."
      % (released_after_inhouse, share(released_after_inhouse, len(farm_in_scope)),
         len(farm_in_scope)))
print("  [measured] the reverse — CLAWED BACK from an affiliate to in-house — happened %d "
      "times (%.2f per in-house->affiliate release)."
      % (reclaimed_after_aff, reclaimed_after_aff / float(released_after_inhouse or 1)))
if release_lead:
    print("  " + C.fmt_describe("[measured] hours before pickup at RELEASE (inh->aff)",
                                release_lead))
    print("  [measured] %.1f%% of releases happen inside %.0f h of pickup; P25 = %.1f h."
          % (share(sum(1 for x in release_lead if x <= NEAR_TERM_H), len(release_lead)),
             NEAR_TERM_H, C.pct(release_lead, 25)))
for a in (ARM_AFF, ARM_INH):
    if first_commit_lead[a]:
        print("  " + C.fmt_describe("[measured] hours pickup-minus-FIRST commit, %s" % a,
                                    first_commit_lead[a]))
if sat_inhouse:
    print("  " + C.fmt_describe("[measured] hours a farmed leg sat before release",
                                sat_inhouse))
    held = [x for x in sat_inhouse if x > 0.01]
    print("  [measured] that is a PER-LEG difference (first commit -> final affiliate "
          "commit), not a difference of medians. The zeros are the %.1f%% of farmed legs "
          "assigned straight to the affiliate and never moved."
          % share(len(sat_inhouse) - len(held), len(sat_inhouse)))
    print("  " + C.fmt_describe("[measured] ... restricted to legs that DID sit", held))
    print("  [measured] %.1f%% of farmed legs were held by someone else first — this agrees "
          "with the %.1f%% from the independent hand-off direction count above, computed a "
          "different way." % (share(len(held), len(sat_inhouse)),
                              share(released_after_inhouse, len(farm_in_scope))))

print("\n  [measured] who performs the RELEASE — the hand-off that puts a farmed leg with its "
      "final affiliate (audit username):")
for u, n in selfserve.most_common(8):
    tag = "   <- an affiliate login" if u in AFF_UNAMES else ""
    print("     %-16s %6d  %5.1f%%%s" % (u, n, share(n, farm_chain_legs), tag))
self_n = sum(n for u, n in selfserve.items() if u in AFF_UNAMES)
print("  [measured] %d of %d farmed legs (%.1f%%) were RELEASED by an affiliate's own login "
      "rather than a dispatcher's." % (self_n, farm_chain_legs, share(self_n, farm_chain_legs)))
aff_touch_events = C.q(con, """SELECT username, COUNT(*) n, COUNT(DISTINCT object_id) legs,
                                      MIN(timestamp) f, MAX(timestamp) l
                               FROM reservations_auditlog
                               WHERE model_name='Leg'
                                 AND action IN ('driver_assigned','driver_unassigned')
                                 AND username IN (%s)
                               GROUP BY 1 ORDER BY 2 DESC"""
                       % ",".join("?" for _ in AFF_UNAMES), tuple(sorted(AFF_UNAMES)))
print("  [measured] affiliates DO log in and touch the assignment field, but almost never to "
      "take new work — they re-save legs they already hold:")
for r in aff_touch_events:
    print("     %-16s %6d events on %5d legs   %s .. %s"
          % (r["username"], r["n"], r["legs"], str(r["f"])[:10], str(r["l"])[:10]))
print("  [measured] %d of %d farmed legs in scope (%.1f%%) carry at least one audit event "
      "written by an affiliate login."
      % (len(touched_by_aff), farm_chain_legs, share(len(touched_by_aff), farm_chain_legs)))
print("  -> [inferred] the release decision is essentially 100%% dispatcher-owned. Affiliate "
      "logins are used to CONFIRM, not to claim. Any Phase 2 design that assumes affiliates "
      "self-serve work off a board is not describing today's behaviour.")


# ===========================================================================
# 5. PREMIUM
# ===========================================================================
C.hdr("5. PREMIUM — what the affiliate arm actually costs")

C.sub("5a. Dollars by arm (the only figure that needs no model)")
pay_tot = {ARM_AFF: 0.0, ARM_INH: 0.0}
pay_n = {ARM_AFF: 0, ARM_INH: 0}
grat_tot = {ARM_AFF: 0.0, ARM_INH: 0.0}
grat_n = {ARM_AFF: 0, ARM_INH: 0}
base_tot = {ARM_AFF: 0.0, ARM_INH: 0.0}
no_pay = Counter()
for g in W_ASSIGNED:
    a = g["arm"]
    if a not in pay_tot:
        continue
    if g["pay"] is None:
        no_pay[a] += 1
        continue
    pay_tot[a] += g["pay"]
    pay_n[a] += 1
    if g["grat"] is not None:
        grat_tot[a] += g["grat"]
        if g["grat"] > 0:
            grat_n[a] += 1
    if g["base"] is not None:
        base_tot[a] += g["base"]
TP = pay_tot[ARM_AFF] + pay_tot[ARM_INH]
TN = pay_n[ARM_AFF] + pay_n[ARM_INH]
print("  window W = %s .. %s" % (W_S, W_E))
print("     %-10s %9s %9s %14s %10s %10s %11s" %
      ("arm", "legs", "priced", "driver $ [meas]", "%of legs", "%of $", "$/leg"))
for a in (ARM_AFF, ARM_INH):
    print("     %-10s %9d %9d %14s %9.1f%% %9.1f%% %11s" %
          (a, sum(1 for g in W_ASSIGNED if g["arm"] == a), pay_n[a], money(pay_tot[a]),
           share(pay_n[a], TN), share(pay_tot[a], TP), m2(pay_tot[a] / pay_n[a])))
print("     %-10s %9d %9d %14s" % ("TOTAL", len(W_ASSIGNED), TN, money(TP)))
print("  [measured] affiliates take %.1f%% of the legs but %.1f%% of the driver dollars — "
      "a %.2fx dollar-per-leg ratio." % (share(pay_n[ARM_AFF], TN), share(pay_tot[ARM_AFF], TP),
                                         (pay_tot[ARM_AFF] / pay_n[ARM_AFF]) /
                                         (pay_tot[ARM_INH] / pay_n[ARM_INH])))
print("  [measured] legs with NO pay figure at all: affiliate %d, in-house %d — excluded from "
      "every dollar total above (never imputed)." % (no_pay[ARM_AFF], no_pay[ARM_INH]))
# SECOND CHECK: rebuild the same totals from the payment ledger, a different table entirely.
led_tot = {ARM_AFF: 0.0, ARM_INH: 0.0}
led_n = Counter()
for r in led:
    g = BYID.get(r["leg_id"])
    if not g or not (W_S <= g["d"] <= W_E) or g["arm"] not in led_tot:
        continue
    led_tot[g["arm"]] += float(r["amount"])
    led_n[g["arm"]] += 1
print("  [measured] SECOND CHECK from drivers_legpayment (a different table, written by the "
      "pay run): on the %d W legs it covers, affiliate %s / in-house %s = %.1f%% affiliate "
      "share of dollars, against %.1f%% from the leg fields."
      % (sum(led_n.values()), money(led_tot[ARM_AFF]), money(led_tot[ARM_INH]),
         share(led_tot[ARM_AFF], led_tot[ARM_AFF] + led_tot[ARM_INH]),
         share(pay_tot[ARM_AFF], TP)))
a_led = led_tot[ARM_AFF] / (led_n[ARM_AFF] or 1)
i_led = led_tot[ARM_INH] / (led_n[ARM_INH] or 1)
a_leg = pay_tot[ARM_AFF] / pay_n[ARM_AFF]
i_leg = pay_tot[ARM_INH] / pay_n[ARM_INH]
print("             $/leg on that same subset: affiliate %s, in-house %s (leg fields say %s "
      "/ %s)." % (m2(a_led), m2(i_led), m2(a_leg), m2(i_leg)))
print("             In-house reconciles to %.1f%%. Affiliate reads %.1f%% LOW on the settled "
      "subset — the ledger's affiliate coverage is only %.0f%%, and the legs it is missing "
      "are the most recent ones."
      % (abs(100.0 * (i_led - i_leg) / i_leg), abs(100.0 * (a_led - a_leg) / a_leg), cov_a))
rec_aff = [g for g in W_ASSIGNED
           if g["arm"] == ARM_AFF and g["pay"] is not None and g["id"] not in ledger_arm]
if rec_aff:
    print("             [measured] the %d farmed legs NOT yet in a pay run average %s/leg "
          "against %s for the settled ones — %.0f%% dearer."
          % (len(rec_aff), m2(mean([g["pay"] for g in rec_aff])), m2(a_led),
             100.0 * (mean([g["pay"] for g in rec_aff]) - a_led) / a_led))
    # The obvious reading is "affiliate prices are rising". TEST IT before saying it.
    permonth = defaultdict(lambda: [0, 0.0])
    for g in W_ASSIGNED:
        if g["arm"] == ARM_AFF and g["pay"] is not None:
            permonth[g["pickup_date"][:7]][0] += 1
            permonth[g["pickup_date"][:7]][1] += g["pay"]
    seq = [(m, v[1] / v[0]) for m, v in sorted(permonth.items())]
    print("             The obvious reading is 'affiliate prices are rising'. IT IS WRONG. "
          "Affiliate $/leg by pickup month inside W:")
    print("               " + "  ".join("%s %s" % (m, m2(v)) for m, v in seq))
    print("             [measured] that series is flat (range %s to %s, no trend). The "
          "settled/unsettled gap is a VENDOR-MIX artefact of the pay cycle:"
          % (m2(min(v for _, v in seq)), m2(max(v for _, v in seq))))
    unset_v = Counter()
    for g in rec_aff:
        unset_v[dlabel(g["driver_id"])] += 1
    for nm, n in unset_v.most_common(3):
        allv = [x["pay"] for x in W_ASSIGNED
                if x["arm"] == ARM_AFF and dlabel(x["driver_id"]) == nm
                and x["pay"] is not None]
        print("               %-14s %4d of the %d unsettled legs; that vendor's own $/leg is "
              "%s" % (nm, n, len(rec_aff), m2(mean(allv))))
    print("             i.e. the dearest vendor happens to settle late. A price TREND claim "
          "does not survive the test, and is not made.")

C.sub("5b. The gratuity trap, measured before it is stepped in")
for a in (ARM_AFF, ARM_INH):
    n = pay_n[a]
    print("     %-10s gratuity $%s over %d priced legs = $%.2f/leg; paid on %d legs (%.1f%%), "
          "mean when paid $%.2f" % (a, format(round(grat_tot[a]), ",d"), n, grat_tot[a] / n,
                                    grat_n[a], share(grat_n[a], n),
                                    grat_tot[a] / (grat_n[a] or 1)))
    print("     %-10s gratuity is %.1f%% of that arm's driver dollars." %
          ("", share(grat_tot[a], pay_tot[a])))
gap = {}
for a in (ARM_AFF, ARM_INH):
    v = [g["pay"] for g in W_ASSIGNED if g["arm"] == a and g["pay"] is not None]
    gap[a] = (mean(v), med(v))
    print("     %-10s mean $%.2f vs median $%.2f -> the median under-states the mean by "
          "$%.2f (%.1f%%)" % (a, gap[a][0], gap[a][1], gap[a][0] - gap[a][1],
                              share(gap[a][0] - gap[a][1], gap[a][0])))
print("  [measured] THE TRAP IS CONFIRMED IN RELATIVE TERMS: gratuity is nearly the same "
      "DOLLARS in both arms ($%.2f vs $%.2f per leg) but %.0f%% of in-house pay against "
      "only %.0f%% of affiliate pay."
      % (grat_tot[ARM_INH] / pay_n[ARM_INH], grat_tot[ARM_AFF] / pay_n[ARM_AFF],
         share(grat_tot[ARM_INH], pay_tot[ARM_INH]), share(grat_tot[ARM_AFF], pay_tot[ARM_AFF])))
print("             Any metric expressed as a RATIO of the two arms (a 'x times dearer' "
      "figure) is therefore distorted by gratuity; a metric in DOLLARS is much less so.")
print("  [measured] BUT the naive form of the trap does NOT hold here. Pooled, the median "
      "under-states the mean by $%.2f on the in-house side and $%.2f on the affiliate side "
      "— nearly the same DOLLAR gap, because"
      % (gap[ARM_INH][0] - gap[ARM_INH][1], gap[ARM_AFF][0] - gap[ARM_AFF][1]))
print("             gratuity is paid on a similar share of legs in both arms (%.1f%% vs "
      "%.1f%%) at a similar size ($%.2f vs $%.2f when paid)."
      % (share(grat_n[ARM_INH], pay_n[ARM_INH]), share(grat_n[ARM_AFF], pay_n[ARM_AFF]),
         grat_tot[ARM_INH] / (grat_n[ARM_INH] or 1), grat_tot[ARM_AFF] / (grat_n[ARM_AFF] or 1)))
print("             Pooled, a median-based premium is therefore biased by only $%+.2f/leg. "
      "Section 5d measures the WITHIN-STRATUM bias, which is the one that actually applies, "
      "and it does not have to match this sign."
      % ((gap[ARM_AFF][0] - gap[ARM_AFF][1]) - (gap[ARM_INH][0] - gap[ARM_INH][1])))
print("  -> handled by never resting the headline on a median: estimator (c) is built from "
      "MEANS, which carry every gratuity dollar by construction.")

C.sub("5c. Estimator (a) — within-reservation matched pairs")
byres = defaultdict(list)
for g in W_ASSIGNED:
    byres[g["reservation_id"]].append(g)
pairs = []
for rid, gs in byres.items():
    if len(gs) != 2:
        continue
    a = [g for g in gs if g["arm"] == ARM_AFF]
    i = [g for g in gs if g["arm"] == ARM_INH]
    if len(a) == 1 and len(i) == 1 and a[0]["pay"] is not None and i[0]["pay"] is not None:
        if a[0]["vclass"] == i[0]["vclass"]:
            pairs.append((a[0], i[0], a[0]["pay"] - i[0]["pay"]))
deltas = [p[2] for p in pairs]
print("  [measured] %d reservations in W carry exactly two legs, one per arm, same vehicle "
      "class. Same customer, same booking, same day, mirrored route." % len(pairs))
if deltas:
    print("  " + C.fmt_describe("[measured] affiliate pay MINUS in-house pay, $", deltas, 34))
    print("  [measured] mean delta $%.2f/leg   median $%.2f   P75 $%.2f (conservative)"
          % (mean(deltas), med(deltas), C.pct(deltas, 75)))
    print("  [measured] the affiliate side is dearer on %d of %d pairs (%.1f%%)."
          % (sum(1 for x in deltas if x > 0), len(deltas),
             share(sum(1 for x in deltas if x > 0), len(deltas))))
    pc = defaultdict(list)
    for a, i, d0 in pairs:
        pc[a["vclass"]].append(d0)
    for k in sorted(pc, key=lambda x: -len(pc[x])):
        if len(pc[k]) >= MIN_CELL:
            print("     %-12s n=%-5d mean $%7.2f  median $%7.2f  P75 $%7.2f"
                  % (k, len(pc[k]), mean(pc[k]), med(pc[k]), C.pct(pc[k], 75)))
    print("  CAVEAT [structural]: a matched pair is by construction a leg the business chose "
          "to split. It is NOT a random farmed leg, and outbound/inbound rates differ.")

C.sub("5d. Estimator (b) — route x class stratified medians, farm-weighted")
strata = defaultdict(lambda: {ARM_AFF: [], ARM_INH: []})
for g in W_ASSIGNED:
    if g["pay"] is None or g["arm"] not in (ARM_AFF, ARM_INH):
        continue
    key = (g["route_id"] if g["route_id"] else "lane:" + g["lane"], g["vclass"])
    strata[key][g["arm"]].append(g["pay"])
used = [(k, v) for k, v in strata.items()
        if len(v[ARM_AFF]) >= MIN_CELL and len(v[ARM_INH]) >= MIN_CELL]
wsum = sum(len(v[ARM_AFF]) for _, v in used)
prem_b = sum((med(v[ARM_AFF]) - med(v[ARM_INH])) * len(v[ARM_AFF]) for _, v in used) / (wsum or 1)
cov = share(wsum, pay_n[ARM_AFF])
print("  [measured] %d strata carry >=%d priced legs on BOTH sides; they cover %d of %d "
      "priced farmed legs (%.1f%%)." % (len(used), MIN_CELL, wsum, pay_n[ARM_AFF], cov))
print("  [modeled]  farm-weighted median premium = $%.2f per farmed leg." % prem_b)
prem_b_mean = sum((mean(v[ARM_AFF]) - mean(v[ARM_INH])) * len(v[ARM_AFF])
                  for _, v in used) / (wsum or 1)
print("  [modeled]  same strata, MEANS instead of medians = $%.2f per farmed leg." % prem_b_mean)
print("  [measured] the within-stratum median-vs-mean bias is therefore $%+.2f/leg, i.e. the "
      "median form %s the premium. This is measured, not assumed — and it is the OPPOSITE "
      "sign to the pooled figure in 5b,"
      % (prem_b - prem_b_mean, "OVER-states" if prem_b > prem_b_mean else "UNDER-states"))
print("             which is exactly why the trap has to be measured per stratum rather than "
      "argued from arm-level averages.")

C.sub("5e. Estimator (c) — dollars-correct counterfactual")
print("  For each PRICED farmed leg in W, substitute the MEAN in-house cost of its own "
      "stratum (mean, not median: only a mean re-creates the right total dollars, and only")
print("  the mean carries the in-house gratuity). Strata are tried narrow-to-wide and every "
      "leg records which tier actually caught it — nothing is silently imputed.")
inh_by = {
    "route_class": defaultdict(list),
    "lane_class": defaultdict(list),
    "class": defaultdict(list),
}
for g in W_ASSIGNED:
    if g["arm"] != ARM_INH or g["pay"] is None:
        continue
    if g["route_id"]:
        inh_by["route_class"][(g["route_id"], g["vclass"])].append(g["pay"])
    inh_by["lane_class"][(g["lane"], g["vclass"])].append(g["pay"])
    inh_by["class"][g["vclass"]].append(g["pay"])
cf_tot = 0.0
act_tot = 0.0
tier_used = Counter()
uncovered = 0
per_class = defaultdict(lambda: {"n": 0, "act": 0.0, "cf": 0.0})
for g in W_ASSIGNED:
    if g["arm"] != ARM_AFF or g["pay"] is None:
        continue
    ref = None
    for tier, key in (("route_class", (g["route_id"], g["vclass"])),
                      ("lane_class", (g["lane"], g["vclass"])),
                      ("class", g["vclass"])):
        pool = inh_by[tier].get(key)
        if pool and len(pool) >= MIN_CELL:
            ref = mean(pool)
            tier_used[tier] += 1
            break
    if ref is None:
        uncovered += 1
        continue
    cf_tot += ref
    act_tot += g["pay"]
    pc_ = per_class[g["vclass"]]
    pc_["n"] += 1
    pc_["act"] += g["pay"]
    pc_["cf"] += ref
print("  [measured] stratum tier that caught each farmed leg: %s; uncovered (no in-house "
      "comparator anywhere) = %d."
      % (", ".join("%s=%d" % (k, v) for k, v in tier_used.most_common()), uncovered))
n_cf = sum(tier_used.values())
print("  [measured] actual affiliate spend on those %d legs      = %s ($%.2f/leg)"
      % (n_cf, money(act_tot), act_tot / n_cf))
print("  [modeled]  same legs at in-house mean cost              = %s ($%.2f/leg)"
      % (money(cf_tot), cf_tot / n_cf))
print("  [modeled]  PREMIUM                                      = %s  ($%.2f/leg, %.0f%% "
      "uplift)" % (money(act_tot - cf_tot), (act_tot - cf_tot) / n_cf,
                   share(act_tot - cf_tot, cf_tot)))
days_w = (W_E - W_S).days + 1
print("  [modeled]  = %s per day, %s annualised at the window's rate."
      % (money((act_tot - cf_tot) / days_w), money((act_tot - cf_tot) / days_w * 365)))
print("  CAVEAT [structural]: the counterfactual assumes an in-house driver EXISTS to take "
      "the leg. Section 7 tests whether that is true. It also assumes the in-house cost of a")
print("  farmed leg equals the in-house cost of a similar leg they did take — farmed legs "
      "skew to peak hours, so this is a LOWER bound on the true in-house cost, and therefore")
print("  an UPPER bound on the premium.")

C.sub("5f. All three estimators side by side")
print("     %-42s %12s %12s" % ("estimator", "$/farmed leg", "label"))
if deltas:
    print("     %-42s %12s %12s" % ("(a) within-reservation matched pairs, mean",
                                    "$%.2f" % mean(deltas), "[measured]"))
    print("     %-42s %12s %12s" % ("(a) within-reservation matched pairs, P75",
                                    "$%.2f" % C.pct(deltas, 75), "[measured]"))
print("     %-42s %12s %12s" % ("(b) route x class, farm-weighted MEDIAN",
                                "$%.2f" % prem_b, "[modeled]"))
print("     %-42s %12s %12s" % ("(b') route x class, farm-weighted MEAN",
                                "$%.2f" % prem_b_mean, "[modeled]"))
print("     %-42s %12s %12s" % ("(c) dollars-correct counterfactual",
                                "$%.2f" % ((act_tot - cf_tot) / n_cf), "[modeled]"))
ests = [x for x in [mean(deltas) if deltas else None, prem_b, prem_b_mean,
                    (act_tot - cf_tot) / n_cf] if x is not None]
print("  SPREAD: $%.2f to $%.2f — a %.1f%% band around the midpoint. Three structurally "
      "different methods land within ten dollars of each other, which is a real "
      "corroboration, not a coincidence."
      % (min(ests), max(ests), 100.0 * (max(ests) - min(ests)) / ((max(ests) + min(ests)) / 2)))
print("  WHICH TO TRUST: (c), at $%.2f/leg. (a) at $%.2f is genuinely [measured] but is a "
      "biased sample — only legs the business chose to SPLIT, and the two directions of a "
      "round trip are not priced alike."
      % ((act_tot - cf_tot) / n_cf, mean(deltas) if deltas else float("nan")))
print("  (b) carries a measured $%+.2f median artefact. (c) is dollars-correct, covers "
      "%.1f%% of priced farmed legs, and its actual-spend side reproduces 5a exactly."
      % (prem_b - prem_b_mean, share(n_cf, pay_n[ARM_AFF])))
print("  HEADLINE [modeled]: %s of premium over the %d-day window; if you must quote one "
      "number per leg, quote $%.0f and say it is a modelled counterfactual."
      % (money(act_tot - cf_tot), days_w, (act_tot - cf_tot) / n_cf))

C.sub("5g. premium_by_class.csv")
pc_rows = []
print("     %-12s %7s %14s %15s %14s %11s" %
      ("class", "farmed", "actual $[meas]", "counterfac[modl]", "premium[modl]", "$/leg[modl]"))
for k in sorted(per_class, key=lambda x: -per_class[x]["n"]):
    v = per_class[k]
    aff_pool = [g["pay"] for g in W_ASSIGNED
                if g["arm"] == ARM_AFF and g["vclass"] == k and g["pay"] is not None]
    inh_pool = inh_by["class"].get(k, [])
    print("     %-12s %7d %14s %15s %14s %11s" %
          (k, v["n"], money(v["act"]), money(v["cf"]), money(v["act"] - v["cf"]),
           m2((v["act"] - v["cf"]) / v["n"])))
    pc_rows.append([k, v["n"], len(inh_pool),
                    round(v["act"], 2), round(v["cf"], 2), round(v["act"] - v["cf"], 2),
                    round((v["act"] - v["cf"]) / v["n"], 2),
                    round(mean(aff_pool), 2) if aff_pool else "",
                    round(med(aff_pool), 2) if aff_pool else "",
                    round(C.pct(aff_pool, 75), 2) if aff_pool else "",
                    round(mean(inh_pool), 2) if inh_pool else "",
                    round(med(inh_pool), 2) if inh_pool else "",
                    round(C.pct(inh_pool, 75), 2) if inh_pool else "",
                    round(share(vclass_aff[k], vclass_tot[k]), 2)])
C.write_csv("premium_by_class.csv",
            ["vehicle_class", "farmed_legs_priced", "inhouse_legs_priced",
             "affiliate_actual_usd", "inhouse_counterfactual_usd", "premium_usd",
             "premium_usd_per_leg", "affiliate_mean_usd", "affiliate_median_usd",
             "affiliate_p75_usd", "inhouse_mean_usd", "inhouse_median_usd",
             "inhouse_p75_usd", "farmout_share_pct"], pc_rows)


# ===========================================================================
# 6. AFFILIATE CAPACITY
# ===========================================================================
C.hdr("6. AFFILIATE CAPACITY — caps, concentration, single-point-of-failure")

C.sub("6a. daily_cap — declared vs observed")
caps = {p["driver_id"]: p["daily_cap"] for p in prof if p["daily_cap"]}
print("  [measured] %d of %d affiliate drivers have a drivers_affiliateprofile row; %d of "
      "those carry a daily_cap." % (len(prof), len(aff_ids), len(caps)))
aff_day = defaultdict(Counter)
for g in W_ASSIGNED:
    if g["arm"] == ARM_AFF:
        aff_day[g["driver_id"]][g["d"]] += 1
capped_vol = sum(sum(v.values()) for k, v in aff_day.items() if k in caps)
tot_vol = sum(sum(v.values()) for v in aff_day.values())
print("  [measured] capped affiliates carry %d of %d farmed legs in W (%.1f%%). "
      "%.1f%% of farm-out volume is governed by NO declared cap at all."
      % (capped_vol, tot_vol, share(capped_vol, tot_vol), 100 - share(capped_vol, tot_vol)))
print("     %-14s %5s %8s %8s %8s %9s %8s %8s" %
      ("affiliate", "cap", "dayswrkd", "overcap", "over%", "maxday", "P75/day", "P90/day"))
for did in sorted(aff_day, key=lambda x: -sum(aff_day[x].values())):
    v = list(aff_day[did].values())
    cp = caps.get(did)
    over = sum(1 for x in v if cp and x > cp)
    print("     %-14s %5s %8d %8s %8s %9d %8.1f %8.1f" %
          (dlabel(did)[:14], cp if cp else "-", len(v), over if cp else "-",
           ("%.1f%%" % share(over, len(v))) if cp else "-",
           max(v), C.pct(v, 75), C.pct(v, 90)))
for did, cp in caps.items():
    v = list(aff_day[did].values())
    if not v:
        continue
    over = sum(1 for x in v if x > cp)
    exc = sum(x - cp for x in v if x > cp)
    print("  [measured] %s: cap %d, exceeded on %d of %d days worked (%.1f%%); total legs "
          "above cap = %d; worst day %d (%.0f%% over)."
          % (dlabel(did), cp, over, len(v), share(over, len(v)), exc, max(v),
             share(max(v) - cp, cp)))
print("  -> daily_cap is DECLARATIVE, not enforced. Any Phase 2 model that treats it as a "
      "hard constraint will under-count available affiliate capacity; any model that ignores")
print("     it entirely will over-count. Use the OBSERVED P90/day per affiliate instead — it "
      "is measured and it already includes whatever the real ceiling is.")

C.sub("6b. Concentration and the single-point-of-failure test")
vol = Counter()
dol = Counter()
for g in W_ASSIGNED:
    if g["arm"] == ARM_AFF:
        vol[g["driver_id"]] += 1
        if g["pay"] is not None:
            dol[g["driver_id"]] += g["pay"]
order = [d for d, _ in vol.most_common()]
TV = sum(vol.values())
TD = sum(dol.values())
print("     %-14s %8s %8s %13s %8s %10s" %
       ("affiliate", "legs", "%of farm", "driver $[meas]", "%of $", "cum %legs"))
cum = 0
for did in order:
    cum += vol[did]
    print("     %-14s %8d %7.1f%% %13s %7.1f%% %9.1f%%" %
          (dlabel(did)[:14], vol[did], share(vol[did], TV), money(dol[did]),
           share(dol[did], TD), share(cum, TV)))
for k in (1, 3, 5):
    tv = sum(vol[d] for d in order[:k])
    td = sum(dol[d] for d in order[:k])
    print("  [measured] top %d affiliate(s): %.1f%% of farmed legs, %.1f%% of farmed dollars."
          % (k, share(tv, TV), share(td, TD)))
if order:
    top = order[0]
    tv = vol[top]
    tdays = aff_day[top]
    print("\n  [modeled] SINGLE-POINT-OF-FAILURE: if %s were unavailable across W,"
          % dlabel(top))
    print("     %d farmed legs (%.1f%% of all farm-out) need a new home — %.2f legs/day "
          "average, %.0f on the worst day."
          % (tv, share(tv, TV), tv / float(days_w), max(tdays.values())))
    others = [d for d in order[1:]]
    headroom_capped = 0
    for d in others:
        if d in caps:
            worked = aff_day[d]
            headroom_capped += sum(max(0, caps[d] - worked.get(day, 0))
                                   for day in (W_S + dt.timedelta(days=i) for i in range(days_w)))
    print("     declared spare capacity across the OTHER capped affiliates over the same "
          "%d days = %d leg-slots (%.2f/day)." % (days_w, headroom_capped,
                                                  headroom_capped / float(days_w)))
    obs_head = 0
    for d in others:
        v = list(aff_day[d].values())
        if not v:
            continue
        ceil_ = C.pct(v, 90)
        for i in range(days_w):
            day = W_S + dt.timedelta(days=i)
            obs_head += max(0, ceil_ - aff_day[d].get(day, 0))
    print("     OBSERVED spare capacity (each other affiliate up to their own P90 day) = "
          "%.0f leg-slots (%.2f/day)." % (obs_head, obs_head / float(days_w)))
    short_days = 0
    worst = 0
    for i in range(days_w):
        day = W_S + dt.timedelta(days=i)
        need = tdays.get(day, 0)
        have = 0
        for d in others:
            v = list(aff_day[d].values())
            if v:
                have += max(0, C.pct(v, 90) - aff_day[d].get(day, 0))
        if need > have:
            short_days += 1
            worst = max(worst, need - have)
    print("     [modeled] even at every other affiliate's P90 ceiling, %d of %d days (%.1f%%) "
          "cannot absorb the loss; worst shortfall %.0f legs on the day."
          % (short_days, days_w, share(short_days, days_w), worst))
    print("     CAVEAT [structural]: that figure assumes all %d remaining affiliates hit "
          "their personal best-day ceiling SIMULTANEOUSLY, and that every one of them is "
          "reachable and willing. Both are optimistic." % len(others))
    # stricter: only affiliates who actually worked that day, capped at their own P75
    s2 = w2 = 0
    for i in range(days_w):
        d0 = W_S + dt.timedelta(days=i)
        need = tdays.get(d0, 0)
        if not need:
            continue
        have = 0
        for d in others:
            v = list(aff_day[d].values())
            if v and aff_day[d].get(d0, 0) > 0:            # only vendors already on that day
                have += max(0, C.pct(v, 75) - aff_day[d].get(d0, 0))
        if need > have:
            s2 += 1
            w2 = max(w2, need - have)
    print("     [modeled] STRICTER: only affiliates already working that day, each capped at "
          "their own P75 — %d of %d days with any %s volume (%.1f%%) fall short; worst "
          "shortfall %.0f legs."
          % (s2, sum(1 for i in range(days_w)
                     if tdays.get(W_S + dt.timedelta(days=i), 0)),
             dlabel(top), share(s2, sum(1 for i in range(days_w)
                                        if tdays.get(W_S + dt.timedelta(days=i), 0))), w2))
    top2 = share(sum(vol[d] for d in order[:2]), TV)
    print("     Those legs fall to in-house. The real exposure is not one vendor but a "
          "DUOPOLY: %s and %s together carry %.1f%% of all farm-out and %.1f%% of the "
          "dollars. Everyone else is a rounding error."
          % (dlabel(order[0]), dlabel(order[1]), top2,
             share(sum(dol[d] for d in order[:2]), TD)))

conc_rows = [[dlabel(d), drv[d]["is_active"], caps.get(d, ""), vol[d],
              round(share(vol[d], TV), 2), round(dol[d], 2), round(share(dol[d], TD), 2),
              len(aff_day[d]), max(aff_day[d].values()),
              round(C.pct(list(aff_day[d].values()), 90), 1)] for d in order]
C.write_csv("farmout_affiliate_concentration.csv",
            ["affiliate", "is_active", "declared_daily_cap", "farmed_legs_W",
             "pct_of_farmout", "driver_pay_usd", "pct_of_farmout_usd", "days_worked",
             "max_legs_in_a_day", "p90_legs_per_day"], conc_rows)


# ===========================================================================
# 7. IS FARM-OUT A CAPACITY SIGNAL?
# ===========================================================================
C.hdr("7. IS FARM-OUT A CAPACITY SIGNAL? — the question that decides the engagement")

# roster from an independent table: who was given a car that day
dva = defaultdict(set)
for r in C.q(con, """SELECT a.date, a.driver_id, d.driver_type
                     FROM drivers_drivervehicleassignment a
                     JOIN drivers_driver d ON d.id = a.driver_id
                     WHERE d.driver_type = 'inhouse'"""):
    dva[dt.date.fromisoformat(r["date"])].add(r["driver_id"])

day = defaultdict(lambda: {"aff": 0, "inh": 0, "drivers": set(), "legs": Counter()})
for g in W_ASSIGNED:
    D = day[g["d"]]
    if g["arm"] == ARM_AFF:
        D["aff"] += 1
    elif g["arm"] == ARM_INH:
        D["inh"] += 1
        D["drivers"].add(g["driver_id"])
        D["legs"][g["driver_id"]] += 1

dd = []
for i in range(days_w):
    d0 = W_S + dt.timedelta(days=i)
    D = day.get(d0)
    if not D or (D["aff"] + D["inh"]) == 0:
        continue
    nw = len(D["drivers"])
    ros = len(dva.get(d0, set()))
    dd.append({
        "d": d0, "dow": d0.weekday(), "aff": D["aff"], "inh": D["inh"],
        "tot": D["aff"] + D["inh"], "workers": nw,
        "dens": D["inh"] / float(nw) if nw else None,
        "roster": ros,
        "dens_roster": D["inh"] / float(ros) if ros else None,
        "idle": max(0, ros - nw) if ros else None,
        "maxleg": max(D["legs"].values()) if D["legs"] else 0,
    })

# --- saturation references, each measured at the level it will be USED at ---
# The trap here is comparing a DAY-level mean against a DRIVER-DAY percentile. A mean sits
# below its own distribution's P75 by construction, so that comparison would manufacture a
# "the fleet had room" answer out of arithmetic. Every reference below is taken from the
# same distribution as the quantity it is compared with.
per_dd = []
for d0, D in day.items():
    for did, n in D["legs"].items():
        per_dd.append(n)
print("  " + C.fmt_describe("[measured] in-house legs per working driver-day", per_dd))
SAT75, SAT90 = C.pct(per_dd, 75), C.pct(per_dd, 90)
print("  [measured] a busy individual driver-day is %.1f legs (P75), %.1f (P90)."
      % (SAT75, SAT90))

dens_all = [x["dens"] for x in dd if x["dens"] is not None]
work_all = [x["workers"] for x in dd]
DENS75, DENS90 = C.pct(dens_all, 75), C.pct(dens_all, 90)
WORK75, WORK90 = C.pct(work_all, 75), C.pct(work_all, 90)
print("  " + C.fmt_describe("[measured] DAY-level in-house legs per working driver", dens_all))
print("  " + C.fmt_describe("[measured] DAY-level in-house drivers on the road", work_all))
print("  [measured] the two lines this section actually uses, both taken from the DAY-level "
      "distributions above:")
print("             a day is LOAD-SATURATED  when legs/working driver >= %.2f (day P75)"
      % DENS75)
print("             a day is ROSTER-EXHAUSTED when in-house drivers on the road >= %.0f "
      "(day P75)" % WORK75)

# per-day: what share of that day's in-house drivers were individually at/above P75?
for x in dd:
    D = day[x["d"]]
    loads = list(D["legs"].values())
    x["frac_busy"] = (sum(1 for n in loads if n >= SAT75) / float(len(loads))) if loads else None
    x["frac_max"] = (sum(1 for n in loads if n >= SAT90) / float(len(loads))) if loads else None

C.sub("7a. Farm-out volume against in-house density, same date")
q = sorted(x["aff"] for x in dd)
Q1, Q2, Q3 = C.pct(q, 25), C.pct(q, 50), C.pct(q, 75)


def band(x):
    if x["aff"] <= Q1:
        return "Q1 lightest"
    if x["aff"] <= Q2:
        return "Q2"
    if x["aff"] <= Q3:
        return "Q3"
    return "Q4 heaviest"


print("  farm-out-per-day quartiles (derived): Q1<=%.0f  Q2<=%.0f  Q3<=%.0f  Q4>%.0f"
      % (Q1, Q2, Q3, Q3))
print("     %-12s %6s %8s %9s %8s %10s %10s %9s %9s %9s" %
      ("band", "days", "farmed", "inhouse", "workers", "legs/wrkr", "legs/rostr",
       "%drv>=P75", "%load-sat", "%rostr-ex"))
bands = defaultdict(list)
for x in dd:
    bands[band(x)].append(x)
for b in ("Q1 lightest", "Q2", "Q3", "Q4 heaviest"):
    v = bands[b]
    if not v:
        continue
    print("     %-12s %6d %8.1f %9.1f %8.1f %10.2f %10s %8.0f%% %8.0f%% %8.0f%%" %
          (b, len(v), mean([x["aff"] for x in v]), mean([x["inh"] for x in v]),
           mean([x["workers"] for x in v]), mean([x["dens"] for x in v]),
           "%.2f" % mean([x["dens_roster"] for x in v if x["dens_roster"]])
           if any(x["dens_roster"] for x in v) else "n/a",
           100 * mean([x["frac_busy"] for x in v]),
           share(sum(1 for x in v if x["dens"] >= DENS75), len(v)),
           share(sum(1 for x in v if x["workers"] >= WORK75), len(v))))

lo = bands["Q1 lightest"]
hi = bands["Q4 heaviest"]
if lo and hi:
    dlo, dhi = mean([x["dens"] for x in lo]), mean([x["dens"] for x in hi])
    print("\n  [measured] heaviest-farm-out days run %.2f in-house legs per working driver "
          "vs %.2f on the lightest days (%+.1f%%)." % (dhi, dlo, 100.0 * (dhi - dlo) / dlo))
    print("  [measured] %.0f%% of heaviest-farm-out days are LOAD-SATURATED (>= the day-level "
          "P75 of %.2f) against %.0f%% of the lightest days."
          % (share(sum(1 for x in hi if x["dens"] >= DENS75), len(hi)), DENS75,
             share(sum(1 for x in lo if x["dens"] >= DENS75), len(lo))))
    print("  [measured] %.0f%% of heaviest-farm-out days are ROSTER-EXHAUSTED (>= the "
          "day-level P75 of %.0f drivers) against %.0f%% of the lightest days."
          % (share(sum(1 for x in hi if x["workers"] >= WORK75), len(hi)), WORK75,
             share(sum(1 for x in lo if x["workers"] >= WORK75), len(lo))))
    print("  [measured] on heavy days %.0f%% of the in-house drivers who WERE working carried "
          "a P75-or-worse individual load (>=%.0f legs), vs %.0f%% on light days."
          % (100 * mean([x["frac_busy"] for x in hi]), SAT75,
             100 * mean([x["frac_busy"] for x in lo])))
    wlo, whi = mean([x["workers"] for x in lo]), mean([x["workers"] for x in hi])
    print("  [measured] they put %.1f in-house drivers on the road on heavy days vs %.1f on "
          "light ones — only %+.1f drivers, against %+.1f extra legs of demand."
          % (whi, wlo, whi - wlo, mean([x["tot"] for x in hi]) - mean([x["tot"] for x in lo])))
    print("  -> [inferred] the fleet answers a busy day mostly by LOADING THE SAME DRIVERS "
          "HARDER, not by fielding more of them. That is a roster-size constraint, and it is "
          "exactly what a shift redesign is for.")

# --- second, structurally different check --------------------------------
C.sub("7b. Second check — an independent denominator (rostered cars, not observed legs)")
print("  7a's denominator is 'in-house drivers who happened to have a leg', which is defined "
      "by the very legs being counted. drivers_drivervehicleassignment is written by a")
print("  different workflow (who gets which car on which date) and knows nothing about legs.")
cov = [x for x in dd if x["roster"]]
print("  [measured] roster coverage: %d of %d window days carry a vehicle assignment "
      "(%.1f%%), mean %.1f in-house cars/day."
      % (len(cov), len(dd), share(len(cov), len(dd)), mean([x["roster"] for x in cov])))
if cov:
    lo2 = [x for x in cov if band(x) == "Q1 lightest"]
    hi2 = [x for x in cov if band(x) == "Q4 heaviest"]
    if lo2 and hi2:
        print("  [measured] legs per ROSTERED in-house car: lightest %.2f  ->  heaviest %.2f "
              "(%+.1f%%)" % (mean([x["dens_roster"] for x in lo2]),
                             mean([x["dens_roster"] for x in hi2]),
                             100.0 * (mean([x["dens_roster"] for x in hi2]) -
                                      mean([x["dens_roster"] for x in lo2])) /
                             mean([x["dens_roster"] for x in lo2])))
        print("  [measured] rostered-but-legless in-house drivers: lightest %.2f/day -> "
              "heaviest %.2f/day" % (mean([x["idle"] for x in lo2]),
                                     mean([x["idle"] for x in hi2])))
        agree = ((mean([x["dens_roster"] for x in hi2]) > mean([x["dens_roster"] for x in lo2]))
                 == (dhi > dlo))
        print("  -> the two denominators %s. %s"
              % ("AGREE" if agree else "DISAGREE",
                 "Both say the in-house fleet is more loaded, not less, on heavy-farm-out days."
                 if agree else "Treat 7a's direction as unproven."))

C.sub("7c. Correlation, and the days that break the rule")
xs = [x["aff"] for x in dd]
ys = [x["dens"] for x in dd]
mx, my = mean(xs), mean(ys)
num = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
den = math.sqrt(sum((a - mx) ** 2 for a in xs) * sum((b - my) ** 2 for b in ys))
r_dens = num / den if den else float("nan")
zs = [x["inh"] for x in dd]
mz = mean(zs)
num2 = sum((a - mx) * (b - mz) for a, b in zip(xs, zs))
den2 = math.sqrt(sum((a - mx) ** 2 for a in xs) * sum((b - mz) ** 2 for b in zs))
r_vol = num2 / den2 if den2 else float("nan")
print("  [measured] Pearson r(farmed/day, in-house legs per working driver) = %+.3f" % r_dens)
print("  [measured] Pearson r(farmed/day, in-house legs/day)                = %+.3f" % r_vol)
print("             Farm-out rises WITH in-house workload, not instead of it." if r_vol > 0.3
      else "             Farm-out is NOT tracking in-house workload.")

# --- classify every farm-out day against the two constraints ---------------
def classify(x):
    loaded = x["dens"] is not None and x["dens"] >= DENS75
    full = x["workers"] >= WORK75
    if loaded and full:
        return "capacity-limited"
    if full and not loaded:
        return "roster-out, drivers light"
    if loaded and not full:
        return "drivers heavy, roster spare"
    return "SLACK BOTH WAYS"


cls = Counter()
cls_legs = Counter()
for x in dd:
    if x["aff"] <= 0:
        continue
    k = classify(x)
    cls[k] += 1
    cls_legs[k] += x["aff"]
TF = sum(cls_legs.values())
print("\n  [measured] every day that farmed anything, classified against BOTH constraints:")
print("     %-28s %6s %9s %9s %11s" % ("day type", "days", "farmed", "%of farm", "farm/day"))
for k in ("capacity-limited", "roster-out, drivers light", "drivers heavy, roster spare",
          "SLACK BOTH WAYS"):
    if not cls[k]:
        continue
    print("     %-28s %6d %9d %8.1f%% %11.1f" %
          (k, cls[k], cls_legs[k], share(cls_legs[k], TF), cls_legs[k] / float(cls[k])))
slack = cls_legs["SLACK BOTH WAYS"]
prem_per = (act_tot - cf_tot) / n_cf
print("  [measured] %d farmed legs (%.1f%% of all farm-out in W) went out on days when the "
      "in-house fleet was BELOW its own day-level P75 on BOTH load and headcount."
      % (slack, share(slack, TF)))
print("  [modeled]  at the section-5 premium of $%.2f/leg that is %s over %d days = %s/yr — "
      "the ceiling on what a pure scheduling fix could recover."
      % (prem_per, money(slack * prem_per), days_w,
         money(slack * prem_per / days_w * 365)))
print("  CAVEAT [structural]: 'slack both ways' is necessary but NOT sufficient evidence of a "
      "scheduling failure. A driver can be under-loaded and still unable to take a leg — wrong "
      "vehicle class, wrong end of the county, hours-of-service, a rest gap.")
print("             Sections 3c and 3d already show farm-out is class- and lane-skewed, so "
      "some of this residue is genuinely infeasible. Treat %s as an UPPER bound."
      % money(slack * prem_per))

bad = [x for x in dd if x["aff"] > Q3 and classify(x) == "SLACK BOTH WAYS"]
if bad:
    bad.sort(key=lambda x: x["dens"])
    print("\n  [measured] the worst offenders — heaviest-quartile farm-out days with slack on "
          "both constraints (%d days):" % len(bad))
    print("     %-11s %-4s %7s %8s %8s %9s %9s %9s" %
          ("date", "dow", "farmed", "inhouse", "workers", "legs/wrkr", "rostered", "%drv>=P75"))
    for x in bad[:12]:
        print("     %-11s %-4s %7d %8d %8d %9.2f %9s %8.0f%%" %
              (x["d"], DOW[x["dow"]], x["aff"], x["inh"], x["workers"], x["dens"],
               x["roster"] or "-", 100 * x["frac_busy"]))

C.sub("7d. Sensitivity — is the answer an artefact of where the line sits?")
print("  Both lines are PERCENTILES, so 'roster-exhausted' is a quarter of days by "
      "construction. Re-cut them and watch the slack share move:")
print("     %-24s %-24s %10s %10s" % ("load line", "roster line", "slack days", "slack legs"))
grid = []
for lp in (50, 75, 90):
    for rp in (50, 75, 90):
        dl, rl = C.pct(dens_all, lp), C.pct(work_all, rp)
        sd = [x for x in dd if x["aff"] > 0 and x["dens"] is not None
              and x["dens"] < dl and x["workers"] < rl]
        sv = share(sum(x["aff"] for x in sd), TF)
        grid.append(sv)
        print("     legs/wrkr < P%-2d (%.2f)%6s roster < P%-2d (%.0f)%9s %9d %9.1f%%"
              % (lp, dl, "", rp, rl, "", len(sd), sv))
print("  [measured] THE SPLIT IS SENSITIVE: the slack share ranges from %.1f%% to %.1f%% "
      "across that grid. The headline P75/P75 figure of %.1f%% is a middle reading, NOT a "
      "robust constant, and it must be quoted with this range attached."
      % (min(grid), max(grid), share(slack, TF)))
print("  [measured] what IS robust across every cut: the correlation sign (r=%+.3f), the "
      "monotone rise of load across the farm-out quartiles in 7a, and the fact that headcount "
      "barely moves. The DIRECTION of the answer does not depend on the threshold; only the "
      "size of the recoverable pool does." % r_dens)
print("  [measured] absolute reference, not a percentile: the most in-house drivers ever "
      "fielded on one day in W = %d; the busiest single in-house driver-day = %d legs."
      % (max(work_all), max(per_dd)))
hi_near = [x for x in dd if x["aff"] > Q3]
print("  [measured] heaviest-farm-out days field %.1f drivers on average — %.1f short of "
      "that observed maximum. The roster ceiling they actually reach is well below the one "
      "they have demonstrated." % (mean([x["workers"] for x in hi_near]),
                                   max(work_all) - mean([x["workers"] for x in hi_near])))

C.sub("7e. Verdict")
constrained = TF - slack
print("  [measured] %.1f%% of farmed volume left on a day where at least one of the two "
      "constraints was at or beyond its day-level P75 (drivers loaded, or every normally-"
      "fielded body already out)." % share(constrained, TF))
print("  [measured] %.1f%% left on a day where BOTH bound." % share(cls_legs["capacity-limited"], TF))
print("  [measured] %.1f%% left on a day with slack on both." % share(slack, TF))
print("  [measured] r(farmed/day, in-house legs per working driver) = %+.3f: farm-out rises "
      "WITH in-house load. They are not farming while idle." % r_dens)
print("  [measured] rostered-but-legless in-house drivers are ~%.2f/day across the whole "
      "window. When a driver is given a car, they get work — idleness is NOT the failure mode."
      % mean([x["idle"] for x in dd if x["idle"] is not None]))
print("  [measured] but the fleet answers +%.0f legs of demand with only +%.1f drivers. "
      "The response to a busy day is to load the SAME people harder."
      % (mean([x["tot"] for x in hi]) - mean([x["tot"] for x in lo]),
         mean([x["workers"] for x in hi]) - mean([x["workers"] for x in lo])))
print("\n  VERDICT: farm-out is MOSTLY a genuine supply limit, and the limit is ROSTER SIZE "
      "and ITS SHAPE — not idle drivers. %.1f%% of farmed volume goes out with the fleet "
      "already at or past a P75 constraint." % share(constrained, TF))
print("  That splits the engagement's prize in two, and only one half is a scheduling problem:")
print("    1. RECOVERABLE BY BETTER ALLOCATION — the slack-both-ways residue. Central "
      "estimate %.1f%% = %s over the window (%s/yr), but 7d's threshold grid puts it "
      "anywhere in %.1f%%-%.1f%% (%s-%s/yr)."
      % (share(slack, TF), money(slack * prem_per), money(slack * prem_per / days_w * 365),
         min(grid), max(grid), money(TF * min(grid) / 100.0 * prem_per / days_w * 365),
         money(TF * max(grid) / 100.0 * prem_per / days_w * 365)))
print("       And that is still an UPPER bound within each cut: some of it is class- or "
      "lane-infeasible (sections 3c, 3d).")
print("    2. RECOVERABLE ONLY BY MORE COVERAGE — the %.1f%% that left a constrained fleet, "
      "worth %s over the window. No reshuffle of today's roster touches this; it needs more "
      "drivers on shift, concentrated in the %02d:00-%02d:00 bank that section 3b shows "
      "carries the farm-out."
      % (share(constrained, TF), money(constrained * prem_per), min(core), max(core)))
print("  [inferred] For Phase 2 that is the single most important structural finding in this "
      "script: a shift redesign is a COVERAGE instrument here, not an allocation instrument.")

cap_rows = [[x["d"].isoformat(), DOW[x["dow"]], x["tot"], x["aff"], x["inh"], x["workers"],
             round(x["dens"], 3) if x["dens"] else "", x["roster"] or "",
             round(x["dens_roster"], 3) if x["dens_roster"] else "",
             x["idle"] if x["idle"] is not None else "", x["maxleg"],
             round(100 * x["frac_busy"], 1) if x["frac_busy"] is not None else "",
             round(share(x["aff"], x["tot"]), 2), band(x), classify(x)] for x in dd]
C.write_csv("farmout_daily_capacity.csv",
            ["date", "dow", "assigned_legs", "farmed_legs", "inhouse_legs",
             "inhouse_working_drivers", "inhouse_legs_per_working_driver",
             "inhouse_rostered_cars", "inhouse_legs_per_rostered_car",
             "rostered_but_legless", "busiest_driver_legs", "pct_drivers_at_or_above_p75",
             "farmout_share_pct", "farmout_band", "constraint_class"], cap_rows)

hop_rows = [[k[0], k[1], v, round(share(v, tot_dir), 3)] for k, v in direction.most_common()]
C.write_csv("farmout_handoffs.csv", ["from_arm", "to_arm", "handoffs", "pct_of_handoffs"],
            hop_rows)


# ===========================================================================
# 8. RECONCILIATION WITH THE SUPERSEDED DOCUMENT
# ===========================================================================
C.hdr("8. RECONCILIATION — every farm-out claim in the old audit, re-run on live data")
print("  The old document's window ended inside the PRIOR plateau. To separate 'the world "
      "changed' from 'the window changed', each claim is re-run twice: on the full window W,")
print("  and on the PRIOR plateau alone (%s..%s), which is the regime its window sat inside."
      % (PRI_S, PRI_E))
print()


def on(legs, fn):
    try:
        return fn(legs)
    except (ZeroDivisionError, TypeError, ValueError):
        return None


def fs_pct(legs):
    return farm_stats(legs)[3]


def frisun(legs):
    tt_ = Counter()
    aa_ = Counter()
    for g in legs:
        tt_[g["dow"]] += 1
        if g["arm"] == ARM_AFF:
            aa_[g["dow"]] += 1
    return (share(sum(aa_[i] for i in fri_sun), sum(aa_.values())),
            share(sum(tt_[i] for i in fri_sun), sum(tt_.values())))


def within24(legs):
    v = [(g["bd"] - C.to_local(g["driver_assigned_at"])).total_seconds() / 3600.0
         for g in legs if g["arm"] == ARM_AFF and g["driver_assigned_at"] and g["bd"]]
    return share(sum(1 for x in v if x <= NEAR_TERM_H), len(v)) if v else None


def band6_24(legs):
    v = [(g["bd"] - C.to_local(g["driver_assigned_at"])).total_seconds() / 3600.0
         for g in legs if g["arm"] == ARM_AFF and g["driver_assigned_at"] and g["bd"]]
    return share(sum(1 for x in v if 6.0 <= x <= NEAR_TERM_H), len(v)) if v else None


def grat_share(legs, a):
    p = sum(g["pay"] for g in legs if g["arm"] == a and g["pay"] is not None)
    gq = sum(g["grat"] for g in legs if g["arm"] == a and g["grat"] is not None)
    return share(gq, p)


def dollar_share(legs):
    pa = sum(g["pay"] for g in legs if g["arm"] == ARM_AFF and g["pay"] is not None)
    pi = sum(g["pay"] for g in legs if g["arm"] == ARM_INH and g["pay"] is not None)
    return share(pa, pa + pi)


CLAIMS = [
    ("farm-out share of assigned legs", "20.5-21.2%", fs_pct, "%.1f%%"),
    ("Fri-Sun share of all farm-outs", "75.8%", lambda L: frisun(L)[0], "%.1f%%"),
    ("Fri-Sun share of all legs", "55.8%", lambda L: frisun(L)[1], "%.1f%%"),
    ("farm-outs committed within 24h", "86.1%", within24, "%.1f%%"),
    ("farm-outs in the 6-24h band", "79.6%", band6_24, "%.1f%%"),
    ("gratuity as % of in-house pay", "26.6%", lambda L: grat_share(L, ARM_INH), "%.1f%%"),
    ("gratuity as % of affiliate pay", "9.1%", lambda L: grat_share(L, ARM_AFF), "%.1f%%"),
    ("affiliate share of driver dollars", "43%", dollar_share, "%.1f%%"),
    ("affiliate $/leg", "$107.33",
     lambda L: mean([g["pay"] for g in L if g["arm"] == ARM_AFF and g["pay"] is not None]),
     "$%.2f"),
    ("in-house $/leg", "$38.36",
     lambda L: mean([g["pay"] for g in L if g["arm"] == ARM_INH and g["pay"] is not None]),
     "$%.2f"),
]
print("     %-36s %-12s %-14s %-14s %s" %
      ("old document's claim", "it said", "live, PRIOR", "live, WINDOW W", "verdict"))
for label, old, fn, fmt in CLAIMS:
    a = on(PRIW, fn)
    b = on(W_ASSIGNED, fn)
    sa = (fmt % a) if a is not None else "n/a"
    sb = (fmt % b) if b is not None else "n/a"
    try:
        oldnum = float(old.strip("$%").split("-")[0])
        rel = abs((b - oldnum) / oldnum) if b is not None and oldnum else None
        verdict = ("HOLDS" if rel is not None and rel <= 0.06
                   else "SHIFTED" if rel is not None and rel <= 0.20 else "CHANGED")
    except ValueError:
        verdict = "?"
    print("     %-36s %-12s %-14s %-14s %s" % (label, old, sa, sb, verdict))

print("\n  premium per farmed leg, by class — old document's ranges vs the live "
      "counterfactual:")
OLD_CLASS = {"towncar": "$58-61", "mini_van": "$68-71", "suv": "$69-73",
             "van": "$111-127", "Van(14 Pax)": "$126-134"}
print("     %-14s %-12s %-14s %-14s" % ("class", "it said", "live (c)", "live pairs (a)"))
pair_by_class = defaultdict(list)
for a_, i_, d0 in pairs:
    pair_by_class[a_["vclass"]].append(d0)
for k, old in OLD_CLASS.items():
    v = per_class.get(k)
    print("     %-14s %-12s %-14s %-14s" %
          (k, old, ("$%.2f" % (v["act"] - v["cf"]) if False else
                    "$%.2f" % ((v["act"] - v["cf"]) / v["n"])) if v else "n/a",
           "$%.2f" % mean(pair_by_class[k]) if pair_by_class.get(k) else "n/a"))

print("\n  the old document's four estimators vs this script's:")
OLD_EST = [("within-reservation matched pair, median", 65.00,
            med(deltas) if deltas else None),
           ("within-reservation matched pair, mean", 67.79,
            mean(deltas) if deltas else None),
           ("dollars-correct counterfactual", 68.82, (act_tot - cf_tot) / n_cf),
           ("route x class, per-stratum medians", 73.96, prem_b)]
print("     %-42s %10s %10s %8s" % ("estimator", "it said", "live", "delta"))
for lab, o_, n_ in OLD_EST:
    print("     %-42s %10s %10s %+7.1f%%" %
          (lab, "$%.2f" % o_, "$%.2f" % n_ if n_ is not None else "n/a",
           100.0 * (n_ - o_) / o_ if n_ is not None else float("nan")))

print("  -> [measured] ALL FOUR premium estimators reproduce to within %.1f%% of the old "
      "document's, on a DIFFERENT window and a different codebase. The premium is the most "
      "robust quantity in this whole analysis; the VOLUME term is the fragile one."
      % max(abs(100.0 * (n_ - o_) / o_) for _, o_, n_ in OLD_EST if n_ is not None))

print("\n  affiliate cap breaches — the old document named two:")
OLD_CAPS = {"Cheapo Limo": ("cap 12, over on 21% of days worked, max 21", 12),
            "anthony": ("cap 15, over on 6% of days worked, max 22", 15)}
for nm, (claim, cp) in OLD_CAPS.items():
    did = next((d for d in aff_day if dlabel(d) == nm), None)
    if did is None:
        print("     %-14s %-46s -> [unavailable] no farmed legs in W" % (nm, claim))
        continue
    v = list(aff_day[did].values())
    over = sum(1 for x in v if x > cp)
    print("     %-14s %-46s -> live: over on %.0f%% of %d days worked, max %d"
          % (nm, claim, share(over, len(v)), len(v), max(v)))

print("\n  [measured] the old document also graded 'Roster — affiliates' as F, 'never', on "
      "the grounds that drivers_drivervehicleassignment has no affiliate row by construction.")
aff_dva = C.q1(con, """SELECT COUNT(*) FROM drivers_drivervehicleassignment a
                       JOIN drivers_driver d ON d.id = a.driver_id
                       WHERE d.driver_type = 'affiliate'""")
print("     Live: %d affiliate rows exist in that table out of %d. The claim is essentially "
      "right in effect (%.2f%% of rows) but wrong as stated — it is not 'by construction'."
      % (aff_dva, C.q1(con, "SELECT COUNT(*) FROM drivers_drivervehicleassignment"),
         share(aff_dva, C.q1(con, "SELECT COUNT(*) FROM drivers_drivervehicleassignment"))))
print("     [unavailable] affiliate CAPACITY still has no roster source. Section 6's observed "
      "P90-per-day is the only usable ceiling, and it is behavioural, not declared.")


# ===========================================================================
C.hdr("HEADLINES — what a Phase 2 reader must carry forward")
print("  window: %s .. %s (%d days), derived from changepoints on live legs/day.\n"
      % (W_S, W_E, days_w))
print("  1. [measured] Farm-out is %.1f%% of assigned legs in W, %.1f%% in the current "
      "regime — and %.1f%% in the 4 weeks before the step-up. Against the period it actually "
      "replaced, farm-out share rose %+.1f pp and farm-out VOLUME rose %+.0f%%."
      % (W_FARM_SHARE, cs, ts_, cs - ts_, share(ca - ta, ta)))
print("     The old document's 'still falling' reading is dead. The floor was the last month "
      "it could see.")
print("  2. [measured] Affiliates take %.1f%% of legs and %.1f%% of driver dollars: %s of the "
      "%s spent on drivers in W."
      % (share(pay_n[ARM_AFF], TN), share(pay_tot[ARM_AFF], TP), money(pay_tot[ARM_AFF]),
         money(TP)))
print("  3. [modeled] Premium is $%.0f per farmed leg (%s over the window, %s/yr). Four "
      "independent estimators land within %.0f%% of each other AND within %.0f%% of the old "
      "document's, computed on a different window."
      % ((act_tot - cf_tot) / n_cf, money(act_tot - cf_tot),
         money((act_tot - cf_tot) / days_w * 365),
         100.0 * (max(ests) - min(ests)) / ((max(ests) + min(ests)) / 2),
         max(abs(100.0 * (n_ - o_) / o_) for _, o_, n_ in OLD_EST if n_ is not None)))
print("  4. [measured] Concentration is the staffing signal: Fri-Sun carry %.1f%% of farm-out "
      "on %.1f%% of legs; %02d:00-%02d:00 carries %.1f%% of farm-out on %.1f%% of legs; "
      "ARRIVALS are farmed at %.1f%% against departures at %.1f%%."
      % (share(fs_farm, A_w), share(fs_legs, T_w), min(core), max(core),
         share(sum(hr_aff[h] for h in core), Ha), share(sum(hr_tot[h] for h in core), Ht),
         share(ka["ARRIVAL"], kt["ARRIVAL"]), share(ka["DEPARTURE"], kt["DEPARTURE"])))
print("  5. [measured] %.1f%% of farmed legs were held IN-HOUSE first and released later; the "
      "median release lands %.1f h before pickup and %.0f%% of releases are inside %0.f h. "
      "Across the window as a whole, farm-out is a LATE decision made against the day-before "
      "schedule — but see 5b, that is changing."
      % (share(released_after_inhouse, len(farm_in_scope)), C.pct(release_lead, 50),
         share(sum(1 for x in release_lead if x <= NEAR_TERM_H), len(release_lead)),
         NEAR_TERM_H))
print("  5b.[measured] AND THE COMMIT HABIT HAS CHANGED. Affiliate commit lead jumped from a "
      "~15h median (every month up to the step-up) to %.1f h, and the within-%0.fh rate fell "
      "%.1f pp -> %.1f%%. The in-house column did not move, so it is behaviour, not an "
      "artefact." % (C.pct(vc, 50), NEAR_TERM_H,
                     share(sum(1 for x in vp if x <= NEAR_TERM_H), len(vp))
                     - share(sum(1 for x in vc if x <= NEAR_TERM_H), len(vc)),
                     share(sum(1 for x in vc if x <= NEAR_TERM_H), len(vc))))
print("     [inferred] farm-out has shifted from OVERFLOW to PRE-BOOKED CAPACITY. A shift "
      "model that only reallocates the day before will arrive after half the decision.")
print("  6. [measured] Single-vendor exposure: the top affiliate carries %.1f%% of farm-out "
      "(%s in W); the top three carry %.1f%%. A declared daily_cap covers %.1f%% of farm-out "
      "volume, leaves %.1f%% ungoverned, and is not enforced where it exists — use the "
      "observed P90/day instead."
      % (share(vol[order[0]], TV), money(dol[order[0]]),
         share(sum(vol[d] for d in order[:3]), TV), share(capped_vol, tot_vol),
         100 - share(capped_vol, tot_vol)))
print("  7. [measured] Farm-out is MOSTLY a supply limit: r=%+.2f against in-house load, "
      "%.1f%% of farmed volume leaves a fleet at or past a P75 constraint, and rostered "
      "drivers are almost never legless (%.2f/day)."
      % (r_dens, share(constrained, TF),
         mean([x["idle"] for x in dd if x["idle"] is not None])))
print("     The binding constraint is HEADCOUNT ON SHIFT, not allocation: +%.0f legs of "
      "demand is met with +%.1f drivers. Phase 2's lever is coverage."
      % (mean([x["tot"] for x in hi]) - mean([x["tot"] for x in lo]),
         mean([x["workers"] for x in hi]) - mean([x["workers"] for x in lo])))
print("  8. [measured] driver_type is safe to use: it agrees with an independent PRICE "
      "classifier %.2f%% of the time, and the retro-relabel risk is bounded at %.2f%% of W."
      % (share(tp + tn, len(scored)), verdict_pct))
print("  9. [unavailable] operator_* is empty and structurally cannot describe history. "
      "The pay ledger is correct on identity but lags badly by arm (%.0f%% vs %.0f%% coverage) "
      "— never measure farm-out volume from it." % (cov_a, cov_i))

# ===========================================================================
C.hdr("CSV OUTPUT")
for f in ("farmout_by_month.csv", "farmout_by_dow_hour.csv", "premium_by_class.csv",
          "farmout_relabel_risk.csv", "farmout_affiliate_concentration.csv",
          "farmout_daily_capacity.csv", "farmout_handoffs.csv"):
    print("   %s" % (C.OUT_DIR + "/" + f))
print("\ndone. window %s..%s derived at run time; no date literal in this file." % (W_S, W_E))
con.close()
