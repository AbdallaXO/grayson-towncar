"""Standby pool + second-shift mint engine — the SHARED core (Build 2c).

The adopted standby rule (03 §1, founder 2026-08-23) and the fixed-strict
cap+mint replay machinery from docs/scheduling-redesign/analysis/10 live HERE,
imported by BOTH the evidence script and the Day Setup panel, so script and
product cannot drift (04 §3.2c). The extraction gate: analysis/10's output is
byte-identical before and after the refactor.

POSTURE: pure functions over plain data. No Django imports, no ORM, no writes
— importable from the analysis scripts without booting Django (same contract
as handoff_chain.py). Callers assemble the day's state from their own source
(raw sqlite in the scripts, the ORM in day_setup) and pass it in.

STRICT co-driver car-share rules, non-negotiable (an earlier lenient replay
booked one car in two places; the adversarial verifier cut +4.0/day to +2.4):
  * co-driver occupancy blocks on a shared car may never overlap;
  * no interleaving — a driver's pickups sit entirely >= GAP before the
    co-driver's first pickup or >= GAP after their last;
  * out-of-service cars are never minted on;
  * <= 2 drivers per vehicle-day;
  * rest (the live 510-min floor) on BOTH sides of every mint, against the
    driver's ACTUAL adjacent-day boards — via caller-supplied callbacks.
"""
from collections import Counter
from datetime import timedelta

from dispatching.feasibility_guards import SPAN_SOFT_EFFECTIVE_HOURS
from dispatching.handoff_chain import OCCUPANCY_LEAD_TAIL_P50

# Daily span cap for a legal day — D4's 13.5h soft cap, read from its shipped
# home (feasibility_guards) so there is exactly one 13.5 in the codebase.
SPAN_CAP_H = SPAN_SOFT_EFFECTIVE_HOURS

# Farm-out premium per leg, $ — [measured, 00 §B / analysis 03_farmout,
# range 68.13..75.45]. Used only for the "saves ≈ $Z" labels on proposals.
FARMOUT_PREMIUM_PER_LEG = 70.99

# The adopted central mint setting (02 §3, the ~$78k/yr row): gap 120 (the
# min pickup-to-pickup separation on a shared car — the product reads the
# live SchedulerSettings.vehicle_share_pad_min, default 120) x buffer 30 (min
# occupancy-interval clearance vs board neighbours when inserting a leg)
# [modeled central — adversarially verified 2026-08-23].
MINT_GAP_CENTRAL_MIN = 120
MINT_BUF_CENTRAL_MIN = 30

# Vehicle capability ladder — mirrors VEHICLE_TIER_ORDER in
# dispatching/scheduler.py (a driver/car at tier t can run any leg of tier
# <= t); unknown types default to suv, exactly as the replay evidence did.
VEHICLE_TIER = {"towncar": 0, "mini_van": 1, "suv": 2, "van": 3, "Van(14 Pax)": 4}
VEHICLE_TIER_DEFAULT = 2


class MintLeg:
    """One leg as the mint engine sees it: booked pickup + the A3.5 P50
    occupancy interval (aggregate arithmetic — the replay convention)."""
    __slots__ = ("id", "day", "pick", "kind", "did", "tier", "start", "end")

    def __init__(self, id, day, pick, kind, did, tier):
        self.id, self.day, self.pick = id, day, pick
        self.kind, self.did, self.tier = kind, did, tier
        lead, tail = OCCUPANCY_LEAD_TAIL_P50[kind]
        self.start = pick - timedelta(minutes=lead)
        self.end = pick + timedelta(minutes=tail)


def span_h(legs):
    if not legs:
        return 0.0
    return (max(l.end for l in legs) - min(l.start for l in legs)).total_seconds() / 3600.0


def best_window(legs, cap_h=SPAN_CAP_H):
    """Longest contiguous run (pickup order) with span <= cap_h; ties ->
    smallest span. Everything outside the kept window is shed to the pool."""
    n = len(legs)
    bi, bj, bcount, bspan = 0, -1, 0, 1e9
    for i in range(n):
        for j in range(i, n):
            sp = span_h(legs[i:j + 1])
            if sp <= cap_h:
                cnt = j - i + 1
                if cnt > bcount or (cnt == bcount and sp < bspan):
                    bcount, bspan, bi, bj = cnt, sp, i, j
    return legs[bi:bj + 1], legs[:bi] + legs[bj + 1:]


def buffer_ok(seq, newleg, buf):
    """Occupancy-interval clearance >= buf vs immediate board neighbours."""
    before = [l for l in seq if l.pick <= newleg.pick]
    after = [l for l in seq if l.pick > newleg.pick]
    if before:
        prevl = max(before, key=lambda l: l.pick)
        if (newleg.start - prevl.end).total_seconds() / 60.0 < buf:
            return False
    if after:
        nxtl = min(after, key=lambda l: l.pick)
        if (nxtl.start - newleg.end).total_seconds() / 60.0 < buf:
            return False
    return True


def standby_pool_ids(candidate_ids, works_today, dva_today, off_today):
    """The ADOPTED standby rule (03 §1, founder 2026-08-23): from
    ``candidate_ids`` (ACTIVE in-house drivers), those with zero legs today,
    zero roster (DVA) row today, and no approved full-day time off. NO
    activity-history filter — a new hire is visible from day one. Rest (510
    both sides) is checked per proposed shift, not at membership — the
    shift's own times decide it."""
    return sorted(d for d in candidate_ids
                  if d not in works_today and d not in dva_today
                  and d not in off_today)


def replay_one_day(day, boards, farmed, dva_day, fleet, standby, *,
                   gap, buf, cap_h=SPAN_CAP_H, policy="free", limit_mode=None,
                   no_mint=False, rest_ok_first=None, rest_ok_last=None,
                   is_oos=None, synth_counter=None):
    """ONE day of the fixed-strict cap + mint replay — analysis/10's per-day
    engine, verbatim, behind an explicit interface.

    boards        {driver_id: [MintLeg]} — the day's in-house boards (rebound,
                  never mutated in place; read the returned copy).
    farmed        [MintLeg] pool legs not held in-house (farmed to affiliates
                  and — product only — still unassigned; the replayed evidence
                  dates carry no unassigned legs, so the two callers agree).
    dva_day       {driver_id: vehicle_id} — the day's roster rows.
    fleet         {vehicle_id: {"active": bool, "tier": int}}.
    standby       [driver_id] pool (ignored under a limit_mode).
    policy        free | soft (D6 packing preference) | hard2 (evidence only).
    limit_mode    None (real bodies) | 'dva' | 'all' — the car-ceiling cases.
    rest_ok_*     f(driver_id, day, dt) -> bool against ACTUAL adjacent boards
                  (None = no rest constraint, test use only).
    is_oos        f(vehicle_id) -> bool for this day (None = nothing OOS).
    synth_counter mutable [n] shared across a multi-day run so synthetic
                  limit-mode driver names never collide across days.
    """
    boards = {did: list(ls) for did, ls in boards.items()}
    standby = list(standby) if standby is not None else None
    rest_ok_first = rest_ok_first or (lambda did, d, t: True)
    rest_ok_last = rest_ok_last or (lambda did, d, t: True)
    is_oos = is_oos or (lambda v: False)
    synth = synth_counter if synth_counter is not None else [0]

    fail_reasons = Counter()
    used_standby = Counter()
    residual_farm_hours = Counter()
    capped_info = []
    roster_refill = 0
    refill_shed = refill_farm = 0
    capped_days = 0
    post_viol = 0

    # driver capability tier: DVA vehicle's tier, or the max leg tier run that day
    drv_tier = {}
    for did, ls in boards.items():
        t = -1
        v = dva_day.get(did)
        if v is not None and v in fleet:
            t = fleet[v]["tier"]
        drv_tier[did] = max(t, max(l.tier for l in ls))
    # vehicle -> rostered drivers (the car-share ledger)
    veh_drivers = {}
    for did2, v in dva_day.items():
        if v is not None:
            veh_drivers.setdefault(v, []).append(did2)

    # ---- STEP 1: enforce the cap, shed edges ----
    pool = []
    for did in sorted(boards):
        ls = boards[did]
        sp = span_h(ls)
        if sp > cap_h:
            capped_days += 1
            keep, shed = best_window(ls, cap_h)
            boards[did] = keep
            pool.extend(shed)
            capped_info.append((day, did, sp, span_h(keep), len(shed)))
    shed_n = len(pool)
    pool.extend(farmed)
    pool.sort(key=lambda l: l.pick)
    for ls in boards.values():
        if span_h(ls) > cap_h + 1e-9:
            post_viol += 1

    mints = []      # {"veh", "side", "bound", "driver", "legs"}

    def veh_free_sides(v):
        """Usable (side, roster boundary) pairs. 'free' = no rostered pickups.
        <= 2 drivers per vehicle-day: a car with 2 roster rows takes no mint."""
        dids = [x for x in veh_drivers.get(v, []) if boards.get(x)]
        alld = veh_drivers.get(v, [])
        if limit_mode != "all" and not alld:
            return []
        if len(alld) >= 2:
            return []
        if not dids:
            return [("free", None)]
        picks = [l.pick for x in dids for l in boards[x]]
        return [("early", min(picks)), ("late", max(picks))]

    def mint_fits(m, leg):
        """May `leg` join existing mint `m`? Tier, anchor gap, buffer, cap, the
        co-driver overlap ban, and both-side rest."""
        if leg.tier > fleet[m["veh"]]["tier"]:
            return False
        if m["side"] == "early" and (m["bound"] - leg.pick).total_seconds() / 60.0 < gap:
            return False
        if m["side"] == "late" and (leg.pick - m["bound"]).total_seconds() / 60.0 < gap:
            return False
        if not buffer_ok(m["legs"], leg, buf):
            return False
        if span_h(m["legs"] + [leg]) > cap_h:
            return False
        for od in veh_drivers.get(m["veh"], []):
            for ol in (boards.get(od) or []):
                if leg.start < ol.end and ol.start < leg.end:
                    return False
        if limit_mode is None:
            cur = m["legs"]
            nf = min([l.start for l in cur] + [leg.start])
            nl = max([l.end for l in cur] + [leg.end])
            if not rest_ok_first(m["driver"], day, nf):
                return False
            if not rest_ok_last(m["driver"], day, nl):
                return False
        return True

    def mint_fits_nodrv(m, leg):
        """mint_fits minus the standby-driver rest checks — candidate RANKING
        only for the soft policy; full mint_fits still gates every placement."""
        if leg.tier > fleet[m["veh"]]["tier"]:
            return False
        if m["side"] == "early" and (m["bound"] - leg.pick).total_seconds() / 60.0 < gap:
            return False
        if m["side"] == "late" and (leg.pick - m["bound"]).total_seconds() / 60.0 < gap:
            return False
        if not buffer_ok(m["legs"], leg, buf):
            return False
        if span_h(m["legs"] + [leg]) > cap_h:
            return False
        for od in veh_drivers.get(m["veh"], []):
            for ol in (boards.get(od) or []):
                if leg.start < ol.end and ol.start < leg.end:
                    return False
        return True

    def roster_gap_ok(did, leg):
        """A rostered driver's edge extension must keep GAP vs any mint on his car."""
        v = dva_day.get(did)
        if v is None:
            return True
        for m in mints:
            if m["veh"] != v:
                continue
            if m["side"] == "late":
                if (min(l.pick for l in m["legs"]) - leg.pick).total_seconds() / 60.0 < gap:
                    return False
            if m["side"] == "early":
                if (leg.pick - max(l.pick for l in m["legs"])).total_seconds() / 60.0 < gap:
                    return False
        return True

    def car_share_ok(did, leg):
        """STRICT car sharing: vs everything the co-driver(s) on the same car
        hold (roster board + mints), the new leg may neither overlap in
        occupancy nor interleave — its pickup must sit entirely >= GAP before
        their first pickup or >= GAP after their last."""
        v = dva_day.get(did)
        if v is None:
            return True
        others = []
        for od in veh_drivers.get(v, []):
            if od != did and boards.get(od):
                others.extend(boards[od])
        for m in mints:
            if m["veh"] == v and m["driver"] != did:
                others.extend(m["legs"])
        if not others:
            return True
        for ol in others:
            if leg.start < ol.end and ol.start < leg.end:
                return False
        pmin = min(l.pick for l in others)
        pmax = max(l.pick for l in others)
        lo = (pmin - leg.pick).total_seconds() / 60.0
        hi = (leg.pick - pmax).total_seconds() / 60.0
        return lo >= gap or hi >= gap

    # ---- STEP 3: fill — rostered drivers first (waterfall), then mints ----
    farmed_set = set(id(l) for l in farmed)

    def try_roster(leg):
        for did in sorted(boards, key=lambda x: (drv_tier[x], x)):
            if drv_tier[did] < leg.tier:
                continue
            ls = boards[did]
            if not buffer_ok(ls, leg, buf):
                continue
            if span_h(ls + [leg]) > cap_h:
                continue
            if not roster_gap_ok(did, leg):
                continue
            if not car_share_ok(did, leg):
                continue
            nf = min([l.start for l in ls] + [leg.start]) if ls else leg.start
            nl = max([l.end for l in ls] + [leg.end]) if ls else leg.end
            of = min(l.start for l in ls) if ls else None
            ol = max(l.end for l in ls) if ls else None
            if (of is None or nf < of) and not rest_ok_first(did, day, nf):
                continue
            if (ol is None or nl > ol) and not rest_ok_last(did, day, nl):
                continue
            boards[did] = sorted(ls + [leg], key=lambda l: l.pick)
            return True
        return False

    def try_mints(leg, only_single=False):
        for m in mints:
            if only_single and len(m["legs"]) != 1:
                continue
            if mint_fits(m, leg):
                m["legs"] = sorted(m["legs"] + [leg], key=lambda l: l.pick)
                return True
        return False

    def open_mint(leg, remaining):
        cand = []
        vehicles = set(veh_drivers)
        if limit_mode == "all":
            vehicles |= {v for v, f in fleet.items() if f["active"]}
        for v in vehicles:
            if v not in fleet or not fleet[v]["active"]:
                continue
            if is_oos(v):                    # OOS cars never minted
                continue
            if fleet[v]["tier"] < leg.tier:
                continue
            n_mints_here = sum(1 for m in mints if m["veh"] == v)
            n_roster = len(veh_drivers.get(v, []))
            if n_roster + n_mints_here >= 2:  # <= 2 drivers per vehicle-day
                continue
            for side, bound in veh_free_sides(v):
                if side == "early" and (bound - leg.pick).total_seconds() / 60.0 < gap:
                    continue
                if side == "late" and (leg.pick - bound).total_seconds() / 60.0 < gap:
                    continue
                # the seed leg may not overlap co-driver occupancy
                if any(leg.start < ol.end and ol.start < leg.end
                       for od in veh_drivers.get(v, [])
                       for ol in (boards.get(od) or [])):
                    continue
                cand.append((fleet[v]["tier"], v, side, bound))
        if not cand:
            fail_reasons["no_car_side"] += 1
            return False
        if policy in ("soft", "hard2") and remaining:
            # D6 soft packing: prefer a (car, side) that could feasibly capture
            # a second leg still waiting in the pool
            ranked = []
            for tier_, v, side, bound in cand:
                m0 = {"veh": v, "side": side, "bound": bound, "legs": [leg]}
                cap2 = any(mint_fits_nodrv(m0, rl) for rl in remaining)
                ranked.append((0 if cap2 else 1, tier_, v, side, bound))
            ranked.sort()
            cand = [(t, v, s, b) for _, t, v, s, b in ranked]
        else:
            cand.sort()
        got = None
        for tier_, v, side, bound in cand:
            if limit_mode is not None:
                synth[0] += 1
                got = (f"synth{synth[0]}", v, side, bound)
                break
            for sdid in standby:
                if not rest_ok_first(sdid, day, leg.start):
                    continue
                if not rest_ok_last(sdid, day, leg.end):
                    continue
                got = (sdid, v, side, bound)
                break
            if got:
                break
        if not got:
            fail_reasons["no_standby_body"] += 1
            return False
        sdid, v, side, bound = got
        if limit_mode is None:
            standby.remove(sdid)
            used_standby[sdid] += 1
        mints.append({"veh": v, "side": side, "bound": bound, "driver": sdid,
                      "legs": [leg]})
        return True

    for idx, leg in enumerate(pool):
        where = None
        if policy in ("soft", "hard2") and not no_mint and try_mints(leg, only_single=True):
            where = "mint"
        if where is None and try_roster(leg):
            where = "roster"
        if where is None and not no_mint:
            if try_mints(leg):
                where = "mint"
            elif open_mint(leg, pool[idx + 1:]):
                where = "mint"
        if where is None:
            if id(leg) in farmed_set:
                residual_farm_hours[leg.pick.hour] += 1
            continue
        if where == "roster":
            roster_refill += 1
        if id(leg) in farmed_set:
            refill_farm += 1
        else:
            refill_shed += 1

    # ---- hard floor (D6 'hard2', evidence only): cancel 1-leg mints ----
    if policy == "hard2" and not no_mint:
        while True:
            singles = [m for m in mints if len(m["legs"]) == 1]
            if not singles:
                break
            m = singles[0]
            mints.remove(m)
            fail_reasons["cancelled_1leg_mint"] += 1
            if limit_mode is None:
                used_standby[m["driver"]] -= 1
                if used_standby[m["driver"]] <= 0:
                    del used_standby[m["driver"]]
                standby.append(m["driver"])
                standby.sort()
            leg = m["legs"][0]
            if try_mints(leg, only_single=True):
                fail_reasons["cancel_leg_replaced"] += 1
            elif try_roster(leg):
                fail_reasons["cancel_leg_replaced"] += 1
                roster_refill += 1
            elif try_mints(leg):
                fail_reasons["cancel_leg_replaced"] += 1
            else:
                if id(leg) in farmed_set:
                    refill_farm -= 1
                    residual_farm_hours[leg.pick.hour] += 1
                else:
                    refill_shed -= 1

    mint_shapes = [(day, m["side"], min(l.pick for l in m["legs"]),
                    max(l.pick for l in m["legs"]), span_h(m["legs"]),
                    fleet[m["veh"]]["tier"], len(m["legs"]))
                   for m in mints]

    return {"boards": boards, "mints": mints, "shed": shed_n,
            "refill_shed": refill_shed, "refill_farm": refill_farm,
            "roster_refill": roster_refill, "capped_days": capped_days,
            "capped_info": capped_info, "fail_reasons": fail_reasons,
            "residual_farm_hours": residual_farm_hours,
            "used_standby": used_standby, "mint_shapes": mint_shapes,
            "post_viol": post_viol}
