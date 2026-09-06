#!/usr/bin/env python
"""10 — The standby pool and the FIXED-STRICT cap + mint replay.

THE QUESTION THIS SCRIPT EXISTS TO ANSWER
-----------------------------------------
If the 13.5 h span cap (D4) were enforced, could standby drivers minted onto second
shifts of EXISTING cars absorb the shed work plus some of today's farm-out — and at
what settings, under real physical constraints?

Three parts:
  1. STANDBY POOL — per day: in-house drivers with zero legs, zero car assignment,
     and no approved time off (available that day — no activity-history filter,
     founder rule 2026-08-23). Split into the worked-yesterday core
     and the PM-feasible-and-rested core; same-day pull-in rate measured from the
     assignment stream (historicalleg walk — NEVER auditlog, 00 §A4.6).
  2. FIXED-STRICT REPLAY — enforce the cap, shed edge legs, refill rostered drivers
     first, then mint standby second shifts on cars that already roll. The strict
     car-sharing constraints are NON-NEGOTIABLE (an earlier lenient run booked one
     car in two places at once; the adversarial verifier cut +4.0/day to +2.4):
       * co-driver occupancy blocks on a shared car may not overlap;
       * no interleaving — the incoming driver's pickups sit entirely >= GAP before
         the co-driver's first pickup or >= GAP after their last;
       * out-of-service cars are excluded from minting;
       * <= 2 drivers per vehicle-day, waterfall capacity consumption;
       * 510-min rest on BOTH sides against the driver's actual adjacent boards
         (previous day = the REPLAYED board, so mints count against tomorrow).
     Sweep gap x buffer, run the cap-only control (same machinery, mint lever off),
     and the unlimited-standby limit case (the car ceiling).
  3. MINT POLICY — the founder's ">=2 jobs per second shift" preference (D6):
     unrestricted vs soft packing vs a hard 2-leg floor.

NO HARDCODED DATES. The current regime is derived at run time via _common.changepoints
on the daily leg series; the replay window is that segment clipped to last_actuals_day.

State B (who actually ran each leg) is read from reservations_leg directly — never
replayed from events. The assignment-event stream (part 1's pull-in rate) is the
historicalleg walk per leg ordered by (id, history_date, history_id).
"""

import datetime as dt
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common as C  # noqa: E402

# The pool rule + per-day mint engine are the SHARED core in
# dispatching/standby_mints.py (Build 2c: extracted from this script so script
# and product cannot drift). Pure module — no Django needed to import it.
sys.path.insert(0, C.REPO_ROOT)
from dispatching.standby_mints import (  # noqa: E402
    FARMOUT_PREMIUM_PER_LEG as PREMIUM,
    MintLeg as Leg,
    SPAN_CAP_H as CAP_H,
    VEHICLE_TIER as TIER,
    VEHICLE_TIER_DEFAULT as TIER_DEFAULT,
    best_window,
    buffer_ok,
    replay_one_day,
    span_h,
    standby_pool_ids,
)

# --------------------------------------------------------------------------
# parameters — every knob, with its source. These are parameters, not dates.
# (Occupancy lead/tail, the 13.5h cap, the tier ladder and the $70.99 premium
# now come from the shared core above — one home, imported here.)
# --------------------------------------------------------------------------

SPAN_WARN_H = 15.0    # "tolerable on crunch days" line — D4
REST_MIN = 510.0      # min inter-shift rest, minutes — live SchedulerSettings
                      # rest_min_gap_minutes (00, shipped scheduler constants)

# gap = min pickup-to-pickup separation on a shared car (both the mint anchor and the
# strict no-interleave rule). Grid brackets the founder's base handoff chain — D7:
# drop -> wash (14-17 + 15-20) -> fuel -> base -> incoming driver waits >= 1 h — against
# the 32 MEASURED same-car handoffs (P25 182 / P50 220 min, script 08 family).
GAPS = [90, 120, 180]
# buf = min occupancy-interval clearance vs board neighbours when inserting a leg.
# 5 = shipped MIN_TURN_BUFFER (SchedulerSettings); 45 = conservative envelope.
BUFS = [5, 30, 45]
CENTRAL = (120, 30)   # the setting the plan's central row uses
TRIPLE = (("conservative", (180, 45)), ("central", (120, 30)), ("generous", (90, 5)))

# FOUNDER RULE (2026-08-23, supersedes the draft behavioural definition): standby =
# available that day — ACTIVE driver, zero live legs, zero DVA row, no approved time
# off. NO activity-history filter: "imagine a new driver just hired? that will be
# forgotten." POOL_W = None applies the adopted rule; integer values reproduce the
# struck +/-W behavioural variants, printed for comparison (the replay evidence was
# first derived under +/-7, so the adopted superset can only widen the pool).
POOL_W = None
POOL_W_COMPARE = (3, 7, 10)  # struck behavioural variants, printed alongside
PM_END = "23:00"      # founder's PM half-shift is 16:30-23:00 [founder-supplied];
PM_NEXT_OK = "07:30"  # 23:00 + 510 min rest = 07:30 — earliest feasible D+1 pickup
POLICIES = ("free", "soft", "hard2")  # D6: unrestricted / soft >=2 packing / hard floor

ASSUMPTIONS = (
    "driver_type and is_active are CURRENT-STATE flags — every historical in-house/"
    "affiliate split below inherits that caveat.",
    "Occupancy = [pickup_time - lead(kind), pickup_time + tail(kind)] at the 00 §A3.5 "
    "fitted P50 constants. Every span, buffer, rest and car-share check runs on it.",
    "Standby pool = AVAILABLE THAT DAY (active, not deployed, not marked off) — the "
    "founder's adopted rule, 2026-08-23; the struck +/-W behavioural variants print as "
    "comparison. Willingness to answer the phone is unrecorded; the same-day pull-in "
    "rate is the only measured floor on reachability.",
    "The replay holds demand fixed at state B: no leg is re-timed, no booking refused. "
    "Mint results are [modeled] on measured boards.",
    "Approved DriverDateOverride exception_type='off' rows (identically: is_available=0) "
    "are full-day unavailability; partial-day windows are ignored (pool err on the "
    "generous side; the pull-in rate bounds the real answer).",
)


def d(s):
    return dt.date.fromisoformat(str(s)[:10])


# --------------------------------------------------------------------------
# data loading  (leg model + board geometry come from dispatching.standby_mints)
# --------------------------------------------------------------------------

def load(con, cur_a, cur_b):
    """Everything the replay needs. Legs come from a window PADDED +/-8 days around
    the regime because both rest checks and the +/-7d activity window look across
    the boundary."""
    dtyp, dname, dactive = {}, {}, {}
    for r in C.q(con, """SELECT dr.id, dr.driver_type, dr.is_active,
                                u.first_name, u.last_name, u.username
                         FROM drivers_driver dr
                         LEFT JOIN auth_user u ON u.id = dr.profile_id"""):
        dtyp[r["id"]] = r["driver_type"]
        dname[r["id"]] = ((f"{r['first_name']} {r['last_name']}").strip()
                          or r["username"] or f"#{r['id']}")
        dactive[r["id"]] = bool(r["is_active"])

    lo = (cur_a - dt.timedelta(days=8)).isoformat()
    hi = (cur_b + dt.timedelta(days=8)).isoformat()
    legs_all = []
    rows = C.q(con, f"""SELECT l.id, l.pickup_date, l.pickup_time, l.pickup_location pu,
                               l.dropoff_location do_, l.driver_id,
                               COALESCE(lv.vehicle_type, rvv.vehicle_type) vt
                        {C.LEG_JOIN}
                        LEFT JOIN rates_vehicle lv ON lv.id = l.vehicle_id
                        LEFT JOIN rates_vehicle rvv ON rvv.id = r.vehicle_id
                        WHERE {C.LIVE_LEG} AND {C.SANE_DATES}
                          AND l.pickup_date BETWEEN ? AND ?""", (lo, hi))
    for r in rows:
        bp = C.booked_dtm(r["pickup_date"], r["pickup_time"])
        if bp is None:
            continue
        legs_all.append(Leg(r["id"], d(r["pickup_date"]), bp,
                            C.trip_kind(r["pu"], r["do_"]), r["driver_id"],
                            TIER.get(r["vt"], TIER_DEFAULT)))

    # driver -> vehicle roster rows (the day's car plan)
    dva = defaultdict(dict)
    for r in C.q(con, "SELECT date, driver_id, vehicle_id FROM "
                      "drivers_drivervehicleassignment WHERE date BETWEEN ? AND ?",
                 (cur_a.isoformat(), cur_b.isoformat())):
        dva[d(r["date"])][r["driver_id"]] = r["vehicle_id"]

    fleet = {}
    for r in C.q(con, """SELECT f.id, f.is_active, v.vehicle_type vt
                         FROM drivers_fleetvehicle f
                         LEFT JOIN rates_vehicle v ON v.id = f.vehicle_type_id"""):
        fleet[r["id"]] = {"active": bool(r["is_active"]),
                          "tier": TIER.get(r["vt"], TIER_DEFAULT)}

    # out-of-service windows — an OOS car may not be minted on (strict fix 4)
    oos = {}
    for r in C.q(con, "SELECT id, out_of_service_from f, out_of_service_until u "
                      "FROM drivers_fleetvehicle WHERE out_of_service_from IS NOT NULL"):
        a = d(r["f"])
        b = d(r["u"]) if r["u"] else dt.date(2099, 1, 1)
        oos[r["id"]] = (a, b)

    # approved full-day time off (exception_type='off' == is_available=0 on approved rows)
    off = set()
    for r in C.q(con, """SELECT driver_id, date, end_date FROM drivers_driverdateoverride
                         WHERE status='approved' AND exception_type='off'"""):
        a = d(r["date"])
        b = d(r["end_date"]) if r["end_date"] else a
        if b < a or (b - a).days > 90:   # guard one malformed open-ended row
            b = a
        x = a
        while x <= b:
            off.add((x, r["driver_id"]))
            x += dt.timedelta(days=1)
    return {"legs_all": legs_all, "dtyp": dtyp, "names": dname, "active": dactive,
            "dva": dva, "fleet": fleet, "oos": oos, "off": off}


def build_state(legs_all):
    """(driver_id, date) -> [Leg] sorted by pickup — state B boards, all driver types."""
    by = defaultdict(list)
    for l in legs_all:
        if l.did is not None:
            by[(l.did, l.day)].append(l)
    for v in by.values():
        v.sort(key=lambda l: l.pick)
    return by


def car_is_oos(D, v, day):
    w = D["oos"].get(v)
    return bool(w and w[0] <= day <= w[1])


# --------------------------------------------------------------------------
# the replay engine — fixed-strict, policy-parameterized
# --------------------------------------------------------------------------

def run_setting(GAP, BUF, D, by_drv_day, cur_a, cur_b, policy="free",
                limit_mode=None, no_mint=False):
    """One full replay over the regime window.

    policy      free | soft (prefer packing a 2nd leg onto 1-leg mints, prefer mint
                sites that could capture a 2nd pool leg) | hard2 (soft + end-of-day
                cancellation of 1-leg mints) — D6.
    limit_mode  None (real standby pool) | 'dva' (unlimited synthetic standby, cars
                with a roster row only) | 'all' (also idle active cars) — the ceiling.
    no_mint     control: cap + roster-refill only, the standby lever disabled.

    Rest checks are chronologically stateful: the PREVIOUS day is the REPLAYED board
    (mints included), the NEXT day is the baseline; a morning extension tomorrow
    re-validates against the replayed today, so every pair is checked in final form.

    The per-day engine (cap-shed, waterfall refill, strict-share minting, the D6
    policies) is dispatching.standby_mints.replay_one_day — the SHARED core the
    Day Setup panel runs on, extracted from this script (Build 2c). This wrapper
    keeps the chronology: it feeds each day's state in, commits the result to
    `final`, and hands the engine rest callbacks that read `final` for yesterday.
    """
    dtyp, dva, fleet, off = D["dtyp"], D["dva"], D["fleet"], D["off"]
    ndays = (cur_b - cur_a).days + 1
    final = {}          # (did, day) -> [Leg] committed after replay
    all_mints = []      # (day, [mint dict]) — for the independent conflict counter

    def prev_bound(did, day):
        k = (did, day - dt.timedelta(days=1))
        ls = final.get(k, by_drv_day.get(k))
        return max(l.end for l in ls) if ls else None

    def next_bound(did, day):
        ls = by_drv_day.get((did, day + dt.timedelta(days=1)))
        return min(l.start for l in ls) if ls else None

    def rest_ok_first(did, day, new_first_start):
        b = prev_bound(did, day)
        return b is None or (new_first_start - b).total_seconds() / 60.0 >= REST_MIN

    def rest_ok_last(did, day, new_last_end):
        b = next_bound(did, day)
        return b is None or (b - new_last_end).total_seconds() / 60.0 >= REST_MIN

    tot_shed = tot_refill_shed = tot_refill_farm = 0
    tot_capped_days = 0
    mints_per_day, pool_per_day = [], []
    mint_shapes = []    # (day, side, first_pick, last_pick, span_h, vehicle_tier, n_legs)
    capped_info = []    # (day, did, orig_span, new_span, n_shed)
    fail_reasons = Counter()
    per_day = []        # (day, base_ih, after_ih, total, n_mints)
    residual_farm_hours = Counter()
    post_viol = 0
    used_standby = Counter()
    roster_refill = 0

    day = cur_a
    while day <= cur_b:
        # ---- state-B boards for this day ----
        boards, farmed = {}, []
        for (did, dy), ls in by_drv_day.items():
            if dy != day:
                continue
            if dtyp.get(did) == "inhouse":
                boards[did] = list(ls)
            elif dtyp.get(did) == "affiliate":
                farmed.extend(ls)
        dva_day = dva.get(day, {})

        # ---- the day's standby pool (real bodies only when limit_mode None).
        # The ADOPTED rule is the shared standby_pool_ids (03 §1); the struck
        # +/-W behavioural variants remain reproducible via POOL_W. This pool
        # ADDITIONALLY requires is_active (a mint is a real call-out); part 1's
        # behavioural pool does not — the delta is reported there. ----
        if limit_mode is None:
            cand = [did for did, t in dtyp.items()
                    if t == "inhouse" and D["active"].get(did)]
            works_today = {did for did in cand if by_drv_day.get((did, day))}
            off_today = {did for did in cand if (day, did) in off}
            standby = standby_pool_ids(cand, works_today, dva_day, off_today)
            if POOL_W is not None:
                standby = [did for did in standby if any(
                    (did, day + dt.timedelta(days=k)) in by_drv_day
                    for k in range(-POOL_W, POOL_W + 1) if k != 0)]
            pool_per_day.append(len(standby))
        else:
            standby = None
            pool_per_day.append(-1)

        base_ih_day = sum(len(ls) for (dd, dy), ls in by_drv_day.items()
                          if dy == day and dtyp.get(dd) == "inhouse")

        r1 = replay_one_day(
            day, boards, farmed, dva_day, fleet, standby,
            gap=GAP, buf=BUF, cap_h=CAP_H, policy=policy,
            limit_mode=limit_mode, no_mint=no_mint,
            rest_ok_first=rest_ok_first, rest_ok_last=rest_ok_last,
            is_oos=lambda v, _d=day: car_is_oos(D, v, _d))
        boards, mints = r1["boards"], r1["mints"]
        tot_capped_days += r1["capped_days"]
        capped_info.extend(r1["capped_info"])
        tot_shed += r1["shed"]
        tot_refill_shed += r1["refill_shed"]
        tot_refill_farm += r1["refill_farm"]
        roster_refill += r1["roster_refill"]
        post_viol += r1["post_viol"]
        fail_reasons.update(r1["fail_reasons"])
        residual_farm_hours.update(r1["residual_farm_hours"])
        used_standby.update(r1["used_standby"])
        mint_shapes.extend(r1["mint_shapes"])

        mints_per_day.append(len(mints))
        all_mints.append((day, [{"veh": m["veh"], "driver": m["driver"],
                                 "legs": list(m["legs"])} for m in mints]))
        # ---- commit the day (stateful rest reads this tomorrow) ----
        for did2, ls in boards.items():
            assert span_h(ls) <= CAP_H + 1e-9, f"final board over cap {did2} {day}"
            final[(did2, day)] = sorted(ls, key=lambda l: l.pick)
        for m in mints:
            assert span_h(m["legs"]) <= CAP_H + 1e-9, f"mint over cap {day}"
            final[(m["driver"], day)] = sorted(m["legs"], key=lambda l: l.pick)
        after_ih_day = (sum(len(ls) for ls in boards.values())
                        + sum(len(m["legs"]) for m in mints))
        per_day.append((day, base_ih_day, after_ih_day,
                        base_ih_day + len(farmed), len(mints)))
        day += dt.timedelta(days=1)

    # ---- post-hoc rest audit over the whole window: NEW breaches must be zero ----
    def bounds(src, did, dy):
        ls = src.get((did, dy))
        return (min(l.start for l in ls), max(l.end for l in ls)) if ls else None

    replay = dict(by_drv_day)
    replay.update(final)
    dids_all = {k[0] for k in final} | {k[0] for k in by_drv_day}
    new_breach = old_breach = rep_breach = 0
    for did in dids_all:
        dy = cur_a - dt.timedelta(days=1)
        while dy <= cur_b:
            br = bounds(replay, did, dy)
            nr = bounds(replay, did, dy + dt.timedelta(days=1))
            bb = bounds(by_drv_day, did, dy)
            nb = bounds(by_drv_day, did, dy + dt.timedelta(days=1))
            r_breach = (br and nr and (nr[0] - br[1]).total_seconds() / 60.0 < REST_MIN)
            b_breach = (bb and nb and (nb[0] - bb[1]).total_seconds() / 60.0 < REST_MIN)
            rep_breach += bool(r_breach)
            old_breach += bool(b_breach)
            new_breach += bool(r_breach and not b_breach)
            dy += dt.timedelta(days=1)

    base_ih = sum(len(ls) for (did, dy), ls in by_drv_day.items()
                  if cur_a <= dy <= cur_b and dtyp.get(did) == "inhouse")
    total = sum(1 for l in D["legs_all"] if cur_a <= l.day <= cur_b)
    after_ih = base_ih - tot_shed + tot_refill_shed + tot_refill_farm
    return {"gap": GAP, "buf": BUF, "policy": policy, "limit": limit_mode,
            "no_mint": no_mint, "ndays": ndays,
            "base_ih": base_ih, "total": total, "shed": tot_shed,
            "refill_shed": tot_refill_shed, "refill_farm": tot_refill_farm,
            "after_ih": after_ih, "net": after_ih - base_ih,
            "coverage_after": 100.0 * after_ih / total,
            "capped_days": tot_capped_days, "mints": mints_per_day,
            "pool": pool_per_day, "shapes": mint_shapes, "capped_info": capped_info,
            "fails": dict(fail_reasons), "post_viol": post_viol,
            "roster_refill": roster_refill, "standby_used": used_standby,
            "per_day": per_day, "residual_farm_hours": dict(residual_farm_hours),
            "rest_new": new_breach, "rest_old": old_breach, "rest_replay": rep_breach,
            "final": final, "all_mints": all_mints}


def count_conflicts(res, D, by_drv_day, cur_a, cur_b):
    """INDEPENDENT verification: walk every replayed vehicle-day and count (a) pairs of
    occupancy-overlapping legs held by DIFFERENT drivers on the SAME car, and (b)
    vehicle-days where the two drivers' legs interleave (more than one hand-over in
    pickup order). Both must be zero under the strict rules."""
    final, mints_by_day = res["final"], dict(res["all_mints"])
    conf = inter = 0
    day = cur_a
    while day <= cur_b:
        dva_day = D["dva"].get(day, {})
        mset = {m["driver"]: m for m in mints_by_day.get(day, [])}
        veh_legs = defaultdict(list)
        for (did, dy), ls in final.items():
            if dy != day:
                continue
            v = mset[did]["veh"] if did in mset else dva_day.get(did)
            if v is None:
                continue
            for l in ls:
                veh_legs[v].append((did, l))
        for v, items in veh_legs.items():
            for i in range(len(items)):
                for j in range(i + 1, len(items)):
                    if (items[i][0] != items[j][0]
                            and items[i][1].start < items[j][1].end
                            and items[j][1].start < items[i][1].end):
                        conf += 1
            if len({x for x, _ in items}) >= 2:
                seq = sorted(items, key=lambda x: x[1].pick)
                if sum(1 for i in range(len(seq) - 1) if seq[i][0] != seq[i + 1][0]) != 1:
                    inter += 1
        day += dt.timedelta(days=1)
    return conf, inter


# --------------------------------------------------------------------------
# part 1 — the behavioural standby pool
# --------------------------------------------------------------------------

def pool_section(con, cur_a, cur_b, plateau, D):
    """Pool per day + PM feasibility. The ADOPTED rule (available that day) requires
    is_active — a real call-out needs a real active driver; the struck behavioural
    variants do not (the flag is current-state), and both print for comparison."""
    C.hdr("1. THE STANDBY POOL — who could plausibly take a phone call [measured]")
    inhouse = {i for i, t in D["dtyp"].items() if t == "inhouse"}
    n_inact = sum(1 for i in inhouse if not D["active"].get(i))
    print(f"in-house driver rows: {len(inhouse)} ({n_inact} with is_active=0 — "
          f"CURRENT-STATE flag, see assumptions)")
    print("pool(D) = in-house AND ACTIVE, zero live legs on D, no DriverVehicleAssignment")
    print("          row on D, no approved 'off' override. [ADOPTED, founder 2026-08-23 —")
    print("          no activity-history filter, so a new hire is visible from day one]")

    # operated days + first pickup per driver-day (one forward day for D+1 rest checks)
    worked, firstpk = {}, {}
    sql = C.live_legs_sql("l.driver_id AS dd, l.pickup_date AS pd, "
                          "MIN(l.pickup_time) AS ft, COUNT(*) AS n",
                          "AND l.driver_id IS NOT NULL AND l.pickup_date <= ? GROUP BY 1,2")
    for r in C.q(con, sql, ((cur_b + dt.timedelta(days=1)).isoformat(),)):
        if r["dd"] in inhouse:
            day = d(r["pd"])
            worked.setdefault(r["dd"], set()).add(day)
            firstpk[(r["dd"], day)] = str(r["ft"])[:5]
    dva_rows = {(r["driver_id"], d(r["date"])) for r in
                C.q(con, "SELECT driver_id, date FROM drivers_drivervehicleassignment")}

    def in_pool(x, day, w):
        if day in worked.get(x, set()) or (x, day) in dva_rows:
            return False
        if (day, x) in D["off"]:
            return False
        if w is None:
            # adopted rule: available that day, full stop (is_active is a
            # current-state flag — the caveat prints in the header)
            return bool(D["active"].get(x))
        wd = worked.get(x, set())
        # activity clamped to operated days on or before cur_b (no forward leakage)
        return any((day + dt.timedelta(days=k)) in wd
                   and (day + dt.timedelta(days=k)) <= cur_b
                   for k in range(-w, w + 1))

    def dist(sizes):
        s = sorted(sizes)
        return (f"min {s[0]}  P25 {C.pct(s, 25):.1f}  P50 {C.pct(s, 50):.1f}  "
                f"P75 {C.pct(s, 75):.1f}  max {s[-1]}  mean {sum(s) / len(s):.2f}")

    def series(win, w):
        out, day = [], win[0]
        while day <= win[1]:
            out.append((day, [x for x in inhouse if in_pool(x, day, w)]))
            day += dt.timedelta(days=1)
        return out

    ser_h = series((cur_a, cur_b), POOL_W)
    print(f"  regime pool, ADOPTED rule (available that day): "
          f"{dist([len(p) for _, p in ser_h])} <- headline")
    for w in POOL_W_COMPARE:
        ser = series((cur_a, cur_b), w)
        print(f"  regime pool, struck +/-{w:>2}d behavioural variant: "
              f"{dist([len(p) for _, p in ser])}")
    pser = series(plateau, POOL_W)
    print(f"  plateau {plateau[0]}..{plateau[1]} (adopted rule): "
          f"{dist([len(p) for _, p in pser])}")

    ser = series((cur_a, cur_b), POOL_W)
    per_date = {}
    worked_prev, rested_l, pm_l, pm_rest_l = [], [], [], []
    freq = Counter()
    for day, p in ser:
        wp = [x for x in p if (day - dt.timedelta(days=1)) in worked.get(x, set())]
        nxt = day + dt.timedelta(days=1)
        # PM half 16:30-23:00 [founder]: feasible iff a 23:00 finish keeps REST_MIN vs
        # the driver's D+1 first pickup (23:00 + 510 min = 07:30 next day)
        pm_ok = [x for x in p if nxt not in worked.get(x, set())
                 or firstpk.get((x, nxt), "23:59") >= PM_NEXT_OK]
        pm_rest = [x for x in pm_ok if (day - dt.timedelta(days=1)) not in worked.get(x, set())]
        worked_prev.append(len(wp))
        rested_l.append(len(p) - len(wp))
        pm_l.append(len(pm_ok))
        pm_rest_l.append(len(pm_rest))
        for x in p:
            freq[x] += 1
        per_date[day] = {"pool": len(p), "worked_d1": len(wp),
                         "rested": len(p) - len(wp), "pm_ok": len(pm_ok),
                         "pm_ok_rested": len(pm_rest)}
    print("\n  of the adopted pool:")
    print(f"    worked the previous day (the D4 second-shift core): {dist(worked_prev)}")
    print(f"    fully rested (no D-1 work)                        : {dist(rested_l)}")
    print(f"    PM-half feasible (23:00 finish keeps {REST_MIN:.0f}-min rest): {dist(pm_l)}")
    print(f"    PM-feasible AND rested                            : {dist(pm_rest_l)}")
    sizes = Counter(len(p) for _, p in ser)
    n14 = sum(1 for _, p in ser if 1 <= len(p) <= 4)
    print(f"  days by pool size: {dict(sorted(sizes.items()))}")
    print(f"  founder 1-4 envelope check: {n14}/{len(ser)} days in 1-4; "
          f"{sum(1 for _, p in ser if len(p) == 0)} days at 0; "
          f"{sum(1 for _, p in ser if len(p) > 4)} days above 4 "
          f"(the FULL behavioural pool exceeds it; the reachable core below matches)")
    by_dow = defaultdict(list)
    for day, p in ser:
        by_dow[day.strftime("%a")].append(len(p))
    print("  mean pool by weekday:",
          {k: round(sum(v) / len(v), 1) for k, v in
           sorted(by_dow.items(), key=lambda kv: "MonTueWedThuFriSatSun".find(kv[0]))})
    inact = [i for i in freq if not D["active"].get(i)]
    print(f"  distinct pool members: {len(freq)}; carrying is_active=0 today: {sorted(inact)}")
    print("  member -> pool-days:",
          {i: n for i, n in sorted(freq.items(), key=lambda kv: -kv[1])})
    return per_date


def pullin_section(con, cur_a, cur_b, D, per_date):
    """Same-day pull-in rate — the measured floor on standby reachability. Stream =
    historicalleg walked per leg ordered by (id, history_date, history_id); a worked
    in-house driver-day (X, D) is SAME-DAY CALLED if the earliest transition-to-X
    among X's state-B legs of D lands on local date D. NEVER auditlog (00 §A4.6)."""
    C.hdr("2. SAME-DAY PULL-INS — how often standby actually answers today [measured]")
    inhouse = {i for i, t in D["dtyp"].items() if t == "inhouse"}
    sb = {}
    sql = C.live_legs_sql("l.id AS lid, l.driver_id AS dd, l.pickup_date AS pd",
                          "AND l.driver_id IS NOT NULL AND l.pickup_date BETWEEN ? AND ?")
    for r in C.q(con, sql, (cur_a.isoformat(), cur_b.isoformat())):
        if r["dd"] in inhouse:
            sb.setdefault((r["dd"], d(r["pd"])), []).append(r["lid"])
    legids = sorted({lid for v in sb.values() for lid in v})
    print(f"in-house worked driver-days: {len(sb)}, legs: {len(legids)}")

    first = {}   # (leg, driver) -> local dt of first transition to that driver
    for i in range(0, len(legids), 800):
        chunk = legids[i:i + 800]
        marks = ",".join("?" for _ in chunk)
        prev_leg, prev_drv = None, object()
        for r in C.q(con, f"""SELECT id, driver_id, history_date, history_id
                              FROM reservations_historicalleg WHERE id IN ({marks})
                              ORDER BY id, history_date, history_id""", tuple(chunk)):
            if r["id"] != prev_leg:
                prev_leg, prev_drv = r["id"], None
            if r["driver_id"] != prev_drv:
                if r["driver_id"] is not None and (r["id"], r["driver_id"]) not in first:
                    first[(r["id"], r["driver_id"])] = C.to_local(r["history_date"])
                prev_drv = r["driver_id"]

    same, before, unknown = [], 0, 0
    per_day_same = Counter()
    per_driver_same = Counter()
    for (dd, day), lids in sb.items():
        ts = [t for t in (first.get((lid, dd)) for lid in lids) if t is not None]
        if not ts:
            unknown += 1
            continue
        first_t = min(ts)
        if first_t.date() == day:
            same.append((dd, day, first_t))
            per_day_same[day] += 1
            per_driver_same[dd] += 1
        elif first_t.date() > day:
            unknown += 1   # trail rebuilt after the fact (Reset Schedule) — not a call-in
        else:
            before += 1
    n = len(sb)
    ndays = (cur_b - cur_a).days + 1
    print(f"  assigned BEFORE the day : {before} ({100 * before / n:.1f}%)")
    print(f"  SAME-DAY first assigned : {len(same)} ({100 * len(same) / n:.1f}%)  "
          f"-> {len(same) / ndays:.2f} pull-ins/day across {len(per_day_same)}/{ndays} days")
    print(f"  unknown (no pre-day event; Reset-Schedule rebuilds): {unknown} "
          f"({100 * unknown / n:.1f}%)")
    hrs = sorted(t.hour for _, _, t in same)
    if hrs:
        print(f"  call local hour: P10 {C.pct(hrs, 10):.0f}  P25 {C.pct(hrs, 25):.0f}  "
              f"P50 {C.pct(hrs, 50):.0f}  P75 {C.pct(hrs, 75):.0f}  P90 {C.pct(hrs, 90):.0f}")
    print("  same-day pull-ins by driver:", dict(per_driver_same.most_common()))
    for day, info in per_date.items():
        info["same_day_pullins"] = per_day_same.get(day, 0)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main():
    t0 = dt.datetime.now()
    con = C.connect()
    h = C.Horizon(con)
    C.preamble("10_standby_and_mints.py",
               "the standby pool and the fixed-strict cap + mint replay",
               h, ASSUMPTIONS)

    # ---- derive the regime window (never a literal) ----
    byday = C.legs_per_day(con)
    scan_from = dt.date.fromisoformat(min(byday))
    segs = C.changepoints(byday, scan_from, h.today, min_seg=28, min_effect=0.08)
    current = segs[-1]
    prior = segs[-2] if len(segs) > 1 else segs[-1]
    cur_a = current[0]
    cur_b = min(current[1], h.last_actuals_day)   # actuals stop the day before the pull
    plateau = (prior[0], min(prior[1], cur_a - dt.timedelta(days=1)))
    ndays = (cur_b - cur_a).days + 1
    print(f"\nderived regimes  : prior {prior[0]}..{prior[1]} ({prior[3]:.1f} legs/day), "
          f"current {current[0]}..{current[1]} ({current[3]:.1f} legs/day)")
    print(f"replay window    : {cur_a} .. {cur_b}  ({ndays} days, current regime "
          f"clipped to last_actuals_day)")
    print(f"parameters       : cap {CAP_H}h (D4), rest {REST_MIN:.0f}min, "
          f"premium ${PREMIUM}/leg, gaps {GAPS} x buffers {BUFS} (D7 chain envelope), "
          f"central {CENTRAL}")

    D = load(con, cur_a, cur_b)
    by_drv_day = build_state(D["legs_all"])

    # ---- parts 1 + 2: the pool and the pull-in rate ----
    per_date = pool_section(con, cur_a, cur_b, plateau, D)
    pullin_section(con, cur_a, cur_b, D, per_date)
    C.write_csv("10_standby_pool_per_day.csv",
                ["date", "pool", "worked_d1", "rested", "pm_feasible",
                 "pm_feasible_rested", "same_day_pullins"],
                [[day, i["pool"], i["worked_d1"], i["rested"], i["pm_ok"],
                  i["pm_ok_rested"], i.get("same_day_pullins", 0)]
                 for day, i in sorted(per_date.items())])

    # ---- part 3: state-B benchmark the replay stands on ----
    C.hdr("3. THE STATE-B BASELINE the replay stands on [measured]")
    ih_days = [(k, ls) for k, ls in by_drv_day.items()
               if cur_a <= k[1] <= cur_b and D["dtyp"].get(k[0]) == "inhouse"]
    spans = [span_h(ls) for _, ls in ih_days]
    ih_legs = sum(len(ls) for _, ls in ih_days)
    farm_legs = sum(len(ls) for k, ls in by_drv_day.items()
                    if cur_a <= k[1] <= cur_b and D["dtyp"].get(k[0]) == "affiliate")
    total = sum(1 for l in D["legs_all"] if cur_a <= l.day <= cur_b)
    print(f"  legs {total} ({total / ndays:.1f}/day): in-house {ih_legs} "
          f"({100 * ih_legs / total:.1f}%), farmed {farm_legs} "
          f"({farm_legs / ndays:.2f}/day)")
    print(f"  in-house driver-days {len(ih_days)}; span >{CAP_H}h on "
          f"{sum(1 for s in spans if s > CAP_H)} ({sum(1 for s in spans if s > CAP_H) / ndays:.2f}/day), "
          f">{SPAN_WARN_H}h on {sum(1 for s in spans if s > SPAN_WARN_H)} "
          f"({sum(1 for s in spans if s > SPAN_WARN_H) / ndays:.2f}/day), max {max(spans):.2f}h")

    # ---- part 4: the fixed-strict sweep ----
    C.hdr("4. FIXED-STRICT CAP+MINT REPLAY — gap x buffer sweep [modeled]")
    print("strict co-driver rules ON everywhere: no occupancy overlap on a shared car, no")
    print("interleaving (>= gap pickup separation), OOS cars excluded, <=2 drivers/car-day.")
    print("conf/intl = independent recount of violations on the final boards (must be 0).\n")
    print(f"{'gap':>4} {'buf':>4} | {'shed':>5} {'re_shed':>7} {'re_farm':>7} {'net':>6} "
          f"{'net/day':>8} {'cov%':>6} {'mints/d':>8} {'maxM':>5} {'conf':>5} {'intl':>5} "
          f"{'newRest':>8} {'$/yr':>10}")
    results = {}
    csv_rows = []

    def csv_add(scenario, r):
        for (day, base, after, tot, nm), pl in zip(r["per_day"], r["pool"]):
            csv_rows.append([scenario, r["gap"], r["buf"], r["policy"],
                             int(r["no_mint"]), r["limit"] or "", day, base, after,
                             tot, nm, pl])

    for GAP in GAPS:
        for BUF in BUFS:
            r = run_setting(GAP, BUF, D, by_drv_day, cur_a, cur_b, policy="free")
            results[(GAP, BUF)] = r
            conf, inter = count_conflicts(r, D, by_drv_day, cur_a, cur_b)
            assert r["post_viol"] == 0, "post-cap span violation"
            assert conf == 0 and inter == 0, f"car-share violation at {GAP}/{BUF}"
            mm = r["mints"]
            print(f"{GAP:>4} {BUF:>4} | {r['shed']:>5} {r['refill_shed']:>7} "
                  f"{r['refill_farm']:>7} {r['net']:>+6} {r['net'] / ndays:>+8.2f} "
                  f"{r['coverage_after']:>6.1f} {sum(mm) / ndays:>8.2f} {max(mm):>5} "
                  f"{conf:>5} {inter:>5} {r['rest_new']:>8} "
                  f"{r['net'] / ndays * PREMIUM * 365:>10,.0f}")
            csv_add(f"sweep_g{GAP}_b{BUF}", r)

    print("\nCONTROL — cap only, mint lever OFF (same fixed-strict machinery):")
    for lab, key in TRIPLE:
        rc = run_setting(key[0], key[1], D, by_drv_day, cur_a, cur_b,
                         policy="free", no_mint=True)
        rf = results[key]
        print(f"  {lab:<13} gap={key[0]:>3} buf={key[1]:>2}: cap-only net "
              f"{rc['net'] / ndays:+.2f}/day (cov {rc['coverage_after']:.1f}%)  ->  "
              f"with mints {rf['net'] / ndays:+.2f}/day (cov {rf['coverage_after']:.1f}%)  "
              f"mint increment {(rf['net'] - rc['net']) / ndays:+.2f}/day = "
              f"${(rf['net'] - rc['net']) / ndays * PREMIUM * 365:,.0f}/yr")
        csv_add(f"control_g{key[0]}_b{key[1]}", rc)

    # ---- central detail ----
    r = results[CENTRAL]
    C.hdr(f"5. CENTRAL SETTING DETAIL (gap={CENTRAL[0]}, buf={CENTRAL[1]}) [modeled]")
    print(f"  capped driver-days: {r['capped_days']} ({r['capped_days'] / ndays:.2f}/day)")
    ci = r["capped_info"]
    print("  " + C.fmt_describe("legs shed per capped day", [float(x[4]) for x in ci]))
    print("  " + C.fmt_describe("orig span h of capped days", [x[2] for x in ci]))
    print("  " + C.fmt_describe("post-cap span h", [x[3] for x in ci]))
    print(f"  fill failures: {r['fails']}  (bodies bind no_standby_body; "
          f"cars bind no_car_side)")
    print(f"  refills onto rostered drivers: {r['roster_refill']}, onto mints: "
          f"{r['refill_shed'] + r['refill_farm'] - r['roster_refill']}")
    print(f"  REST AUDIT: baseline breach-pairs {r['rest_old']} -> replay "
          f"{r['rest_replay']} (capping HEALS), NEW breaches {r['rest_new']} (must be 0)")
    print(f"  replay call-out pool (is_active-filtered): "
          + C.fmt_describe("", [float(x) for x in r["pool"]], width=0))
    print(f"  mints/day distribution: {dict(sorted(Counter(r['mints']).items()))}")
    n_env = sum(1 for m in r["mints"] if 1 <= m <= 4)
    print(f"  founder 1-4 mint envelope: {n_env}/{ndays} days inside")
    print(f"  distinct standby drivers minted: {len(r['standby_used'])}  "
          f"-> {dict(r['standby_used'].most_common())}")
    sh = r["shapes"]
    print(f"  minted shifts: {len(sh)}; sides {dict(Counter(x[1] for x in sh))}")
    print("  " + C.fmt_describe("mint span h", [x[4] for x in sh]))
    print("  " + C.fmt_describe("mint legs per shift", [float(x[6]) for x in sh]))
    rf = r["residual_farm_hours"]
    morn = sum(v for k, v in rf.items() if 8 <= k <= 12)
    tot_rf = sum(rf.values())
    print(f"  residual farm-out by pickup hour: {dict(sorted(rf.items()))}")
    if tot_rf:
        print(f"  -> {100.0 * morn / tot_rf:.0f}% of unreachable farm-out picks up "
              f"08:00-12:59, where no rolling car has a free side")

    # ---- limit case: the car ceiling ----
    C.hdr("6. THE CEILING — unlimited standby bodies, central setting [modeled]")
    for mode, lab in (("dva", "cars with a roster row"), ("all", "plus idle active cars")):
        rl = run_setting(CENTRAL[0], CENTRAL[1], D, by_drv_day, cur_a, cur_b,
                         policy="free", limit_mode=mode)
        mm = rl["mints"]
        rfh = rl["residual_farm_hours"]
        morn_l = sum(v for k, v in rfh.items() if 8 <= k <= 12)
        print(f"  {lab:<26} net {rl['net']:+d} ({rl['net'] / ndays:+.2f}/day)  "
              f"SATURATION cov {rl['coverage_after']:.1f}%  mints/day {sum(mm) / ndays:.2f} "
              f"max {max(mm)}  fails {rl['fails']}  "
              f"residual 08:00-12:59 {100.0 * morn_l / sum(rfh.values()):.0f}%")
        csv_add(f"limit_{mode}", rl)
    print("  cars run out before bodies do — with unlimited bodies the residual is purely")
    print("  car-bound, and its morning share is the ceiling's signature.")

    # ---- part 7: mint policy (D6) ----
    C.hdr(f"7. MINT POLICY — the >=2-jobs preference (D6), central setting [modeled]")
    print("  free = unrestricted; soft = prefer packing onto 1-leg mints + prefer mint")
    print("  sites able to capture a 2nd pool leg; hard2 = soft + cancel 1-leg mints.\n")
    print(f"{'policy':<7} | {'net/day':>8} {'cov%':>6} {'$/yr':>10} | {'callouts':>8} "
          f"{'1-leg':>6} {'1leg%':>6} {'2+':>4} | {'conf':>5} {'intl':>5} {'newRest':>8}")
    pol_results = {}
    pol_csv = []
    for pol in POLICIES:
        rp = (results[CENTRAL] if pol == "free"
              else run_setting(CENTRAL[0], CENTRAL[1], D, by_drv_day, cur_a, cur_b,
                               policy=pol))
        pol_results[pol] = rp
        conf, inter = count_conflicts(rp, D, by_drv_day, cur_a, cur_b)
        assert rp["post_viol"] == 0 and conf == 0 and inter == 0
        sh = rp["shapes"]
        nlegs = [x[6] for x in sh]
        n1 = sum(1 for x in nlegs if x == 1)
        n2 = sum(1 for x in nlegs if x >= 2)
        netd = rp["net"] / ndays
        print(f"{pol:<7} | {netd:>+8.2f} {rp['coverage_after']:>6.1f} "
              f"{netd * PREMIUM * 365:>10,.0f} | {len(sh):>8} {n1:>6} "
              f"{100.0 * n1 / len(sh) if sh else 0:>6.1f} {n2:>4} | {conf:>5} {inter:>5} "
              f"{rp['rest_new']:>8}")
        pol_csv.append([pol, round(netd, 2), round(rp["coverage_after"], 1),
                        round(netd * PREMIUM * 365), len(sh), n1, n2,
                        round(sum(rp["mints"]) / ndays, 2), rp["rest_new"], conf, inter])
        if pol != "free":
            csv_add(f"policy_{pol}", rp)
    f0 = pol_results["free"]
    for pol in ("soft", "hard2"):
        rp = pol_results[pol]
        dnet = (rp["net"] - f0["net"]) / ndays
        print(f"  {pol:<6} vs free: net {dnet:+.2f} legs/day "
              f"(${dnet * PREMIUM * 365:+,.0f}/yr), call-outs "
              f"{(len(rp['shapes']) - len(f0['shapes'])) / ndays:+.2f}/day")
    print("  a hard floor re-farms every structurally single-job mint; soft packing is free.")

    C.write_csv("10_replay_per_day.csv",
                ["scenario", "gap", "buf", "policy", "no_mint", "limit_mode", "date",
                 "base_inhouse_legs", "after_inhouse_legs", "total_legs", "mints",
                 "standby_pool"], csv_rows)
    C.write_csv("10_mint_policy_compare.csv",
                ["policy", "net_per_day", "coverage_pct", "usd_per_year", "callouts",
                 "one_leg_mints", "two_plus_mints", "mints_per_day", "new_rest_breaches",
                 "conflicts", "interleaves"], pol_csv)
    print("\nWrote: out/10_standby_pool_per_day.csv, out/10_replay_per_day.csv, "
          "out/10_mint_policy_compare.csv")
    print(f"runtime: {(dt.datetime.now() - t0).total_seconds():.1f}s")


if __name__ == "__main__":
    main()
