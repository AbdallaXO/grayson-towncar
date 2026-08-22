"""
Board Validation — shared post-move whole-board simulation + turn-slack primitives.

POSTURE: THIS MODULE IS STRICTLY READ-ONLY. No model writes, no migrations, no
external HTTP — same contract as ``fleet_intel.py`` / ``farmout_optimizer.py``.
It never calls ``drivers.utils.get_drive_time``, AeroAPI, Samsara, or live
Google; all timing comes from the founder static tables / cached metrics that
``scheduler.chain_repo_minutes`` and friends already read.

WHY IT EXISTS: three pieces of board arithmetic lived inline in ``views.py`` and
are needed verbatim by the Recovery Advisor (``conflict_advisor.py``), whose
core promise is "the advisor and the apply path can never disagree at the
threshold". Promoting them to one shared home is what makes that promise
structural rather than aspirational:

  * ``turn_slack_minutes``       — the ONE turnaround-slack formula (was
                                   ``views._gap_turn_slack``): the same
                                   arithmetic ``scheduler.check_feasibility``
                                   uses, including the recorded-pickup
                                   re-anchor.
  * ``board_turn_bands``         — that formula swept over every adjacent slot
                                   pair of every driver, banded by
                                   ``pickup_policy.turn_band``.
  * ``validate_post_move_board`` — the in-memory "would this set of moves
                                   create any NEW problem?" simulation (the
                                   core of ``views._revalidate_swap_feasibility``,
                                   generalized to arbitrary moves + pickup-time
                                   changes and a baseline band diff).
  * ``revalidate_moves_against_db`` — the DB-loading wrapper promoted verbatim
                                   from ``views._revalidate_swap_feasibility``;
                                   ``execute_swap`` (via its thin views
                                   delegate) still runs exactly this.

THE "NO NEW PROBLEMS" TEST (risk-review wording, implemented here precisely):
hard-reject any NEW negative buffer, any car-share (sharers) conflict on an
affected driver, and any turn band worsening to ``critical`` — while
pre-existing negatives elsewhere on the board NEVER veto an unrelated fix. A
``'' -> 'tight'`` worsening is legal but demoted: recorded in
``worsened_pairs`` / ``new_tight_count`` so the caller can penalize and name it.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta


# ════════════════════════════════════════════════════════════════════════════
# TURN SLACK — the one formula (promoted views._gap_turn_slack)
# ════════════════════════════════════════════════════════════════════════════

def turn_slack_minutes(prev_slot, next_slot, target_date, prev_leg=None,
                       prev_picked_up_dt=None, prev_store_state=None):
    """Real turnaround slack (minutes) between two consecutive slots of one driver.

        slack = next pickup - (prev clear + required_turnaround)

    This is the SAME arithmetic scheduler.check_feasibility uses to decide whether a
    leg may be seated, so a turn the assignment engine calls legal can no longer
    render red on the board, and one it calls impossible can no longer render clean.

    What it replaces: a raw clock gap banded at <20 / <10 with no repositioning drive
    and no deplaning grace. That made a same-terminal MCO drop -> arrival look
    "critical" (the engine considers it fine — the guest is still deplaning) while a
    25-minute gap that needs a 30-minute reposition looked perfectly healthy.

    REALITY BEATS THE PLAN, but only on facts: when the previous leg has a RECORDED
    pickup (``prev_picked_up_dt``), the clear time is re-anchored on it — the dwell
    already happened, so only the drive is left. Without that, a driver who picked up
    15 min early kept showing an amber "tight" chip while the live GPS badge beside it
    read on-time, and the two contradicted each other on screen. A live GPS projection
    is deliberately NOT used here: it's a forecast, and a chip that wrongly goes green
    is worse than one that wrongly goes amber.

    Returns None when the slots don't carry enough information to judge.
    """
    from dispatching.scheduler import (
        _slot_chain_end, chain_repo_minutes, chain_clear_dt_from_actual,
    )
    from dispatching import feasibility_guards as fg

    if prev_slot is None or next_slot is None or next_slot.pickup_time is None:
        return None
    try:
        if prev_leg is not None and prev_picked_up_dt is not None:
            prev_end = chain_clear_dt_from_actual(
                prev_leg, prev_picked_up_dt, store_state=prev_store_state)
        else:
            prev_end = _slot_chain_end(prev_slot, target_date)
        next_pickup = datetime.combine(target_date, next_slot.pickup_time)
        repo = chain_repo_minutes(
            prev_slot.dropoff_location, next_slot.pickup_location,
            prev_slot.dropoff_category, next_slot.pickup_category,
        )
        req = fg.required_turnaround(
            repo,
            fg.is_airport_arrival(next_slot.trip_type, next_slot.pickup_category),
            same_terminal=(prev_slot.dropoff_category == next_slot.pickup_category),
        )
        return int((next_pickup - (prev_end + timedelta(minutes=req))).total_seconds() / 60)
    except Exception:
        # Never let a timing-table miss break the board render; fall back to no chip.
        return None


def _slot_leg_shim(slot):
    """Minimal leg-shaped object built from a ScheduleSlot, for the few scheduler
    primitives that want a leg (``chain_clear_dt_from_actual``) when the caller
    only holds slots. Carries exactly the fields those primitives read."""
    from types import SimpleNamespace
    return SimpleNamespace(
        id=slot.leg_id,
        pickup_time=slot.pickup_time,
        pickup_location=slot.pickup_location,
        dropoff_location=slot.dropoff_location,
        get_trip_type=lambda trip=slot.trip_type: trip,
        reservation=SimpleNamespace(store_stop=bool(getattr(slot, "store_stop", False))),
        flight_information=None,
    )


def board_turn_bands(schedules, target_date, picked_up_by_leg=None,
                     store_states=None):
    """Sweep ``turn_slack_minutes`` over every adjacent slot pair of every driver.

    schedules: {driver_id: DriverDaySchedule} (scheduler.build_driver_schedules).
    picked_up_by_leg: optional {leg_id: recorded_pickup_datetime} — a pair whose
    PREVIOUS leg has a recorded pickup is re-anchored on that fact (detection
    clock). Leave it None on planning paths: the planning clock is never
    optimistic, and validate_post_move_board diffs planning-clock bands on both
    sides, so pass a baseline computed the same way.

    Returns {(driver_id, prev_leg_id, next_leg_id): {"slack": int|None, "band":
    ''|'tight'|'critical'}} — the stable pair keys the advisor's "no new
    problems" diff and anti-flap identity both hang off.
    """
    from dispatching import pickup_policy

    picked = picked_up_by_leg or {}
    stores = store_states or {}
    bands = {}
    for did, sched in schedules.items():
        slots = sorted(sched.slots, key=lambda s: (s.pickup_time, s.leg_id))
        for prev_slot, next_slot in zip(slots, slots[1:]):
            picked_dt = picked.get(prev_slot.leg_id)
            slack = turn_slack_minutes(
                prev_slot, next_slot, target_date,
                prev_leg=(_slot_leg_shim(prev_slot) if picked_dt is not None else None),
                prev_picked_up_dt=picked_dt,
                # The shim is built from a slot and has no status history, so a
                # recorded store stop can only reach the math by being handed in.
                prev_store_state=stores.get(prev_slot.leg_id),
            )
            bands[(did, prev_slot.leg_id, next_slot.leg_id)] = {
                "slack": slack, "band": pickup_policy.turn_band(slack),
            }
    return bands


# ════════════════════════════════════════════════════════════════════════════
# POST-MOVE WHOLE-BOARD VALIDATION (in-memory core)
# ════════════════════════════════════════════════════════════════════════════

_BAND_RANK = {"": 0, "tight": 1, "critical": 2}


@dataclass
class BoardValidation:
    """Verdict of validate_post_move_board. ``ok=False`` means the move set
    creates a NEW problem (new negative buffer, car-share conflict, or a band
    worsened to critical) and must be rejected; demotions (`` '' -> 'tight' ``)
    keep ok=True but are named so the caller can penalize and disclose them."""
    ok: bool
    reason: str = ""
    min_buffer_after: int = 999
    worsened_pairs: list = field(default_factory=list)
    new_tight_count: int = 0
    per_driver: dict = field(default_factory=dict)


def validate_post_move_board(schedules, legs_by_id, moves, target_date, *,
                             windows, sharer_partners, baseline_bands,
                             time_changes=None):
    """Simulate ``moves`` (+ optional pickup-time changes) on COPIES of the board
    and answer: does the resulting board have any problem it didn't already have?

    Args:
        schedules: {driver_id: DriverDaySchedule} — the CURRENT board. Never
            mutated; slots are reused read-only, retimed slots are cloned.
        legs_by_id: {leg_id: Leg} for the day (moved/retimed legs REQUIRED here;
            untouched legs fall back to a slot shim if absent).
        moves: iterable of (leg_id, to_driver_id) — to_driver_id None = unassign.
        windows: {driver_id: window dict|None} — pre-resolved effective windows
            (the caller owns the enforce_cap decision; see the plan's
            generation-vs-apply split).
        sharer_partners: scheduler.build_sharer_partners output (or {}).
        baseline_bands: board_turn_bands over the PRE-move schedules, computed
            with picked_up_by_leg=None (planning clock — both sides of the diff
            must use the same clock).
        time_changes: optional {leg_id: new datetime.time} applied in-memory via
            cloned legs/slots (match_flight / nudge_pickup simulation).

    The precise "no new problems" test:
      * hard-reject a NEW negative buffer — a leave-one-out check_feasibility
        failure involving a touched leg, or one that was NOT already failing on
        the same driver pre-move (pre-existing negatives elsewhere never veto);
      * hard-reject any sharers_conflict on an affected driver;
      * hard-reject any pair whose band worsens to 'critical' vs baseline;
      * record '' -> 'tight' worsenings as demotions (worsened_pairs /
        new_tight_count) — valid but penalized;
      * min_buffer_after = smallest post-move pair slack across AFFECTED drivers.

    Read-only; returns a BoardValidation.
    """
    from dispatching.scheduler import (
        DriverDaySchedule, _make_sim_slot, chain_clear_dt, check_feasibility,
        estimate_job_end_time, sharers_conflict,
    )

    time_changes = {int(lid): t for lid, t in (time_changes or {}).items()
                    if t is not None}
    windows = windows or {}

    # ── current assignment + slot index (from the schedules, not the DB) ──
    current_assign, slot_by_leg = {}, {}
    for did, sched in schedules.items():
        for s in sched.slots:
            current_assign[s.leg_id] = did
            slot_by_leg[s.leg_id] = s

    move_map = {}
    for leg_id, to_did in moves:
        move_map[int(leg_id)] = (int(to_did) if to_did is not None else None)
    retimed_ids = set(time_changes)
    touched_ids = set(move_map) | retimed_ids

    for leg_id in touched_ids:
        if leg_id not in legs_by_id:
            return BoardValidation(ok=False,
                                   reason=f"leg {leg_id} not found on {target_date}")
    for leg_id, to_did in move_map.items():
        if to_did is not None and to_did not in schedules:
            return BoardValidation(ok=False,
                                   reason=f"driver {to_did} not on the board")

    # ── effective legs: clones for retimes, originals otherwise ──
    _eff_cache = {}

    def _eff_leg(leg_id):
        if leg_id in _eff_cache:
            return _eff_cache[leg_id]
        leg = legs_by_id.get(leg_id)
        if leg is None:
            # Untouched leg the caller didn't load — judge it from its slot.
            leg = _slot_leg_shim(slot_by_leg[leg_id])
        elif leg_id in retimed_ids and time_changes[leg_id] != leg.pickup_time:
            leg = copy.copy(leg)
            leg.pickup_time = time_changes[leg_id]
        _eff_cache[leg_id] = leg
        return leg

    # ── post-move assignment map + copied schedules ──
    post_assign = dict(current_assign)
    for leg_id, to_did in move_map.items():
        if to_did is None:
            post_assign.pop(leg_id, None)
        else:
            post_assign[leg_id] = to_did

    post = {
        did: DriverDaySchedule(driver_id=did, driver_name=sched.driver_name,
                               driver_type=sched.driver_type, slots=[],
                               vehicle_cap=sched.vehicle_cap)
        for did, sched in schedules.items()
    }
    for leg_id, did in post_assign.items():
        if did not in post:
            continue
        slot = slot_by_leg.get(leg_id)
        if leg_id in retimed_ids:
            eff = _eff_leg(leg_id)
            if slot is not None:
                slot = replace(slot, pickup_time=eff.pickup_time,
                               estimated_end_time=estimate_job_end_time(eff, target_date),
                               chain_clear_dt=chain_clear_dt(eff, target_date))
            else:
                slot = _make_sim_slot(eff, target_date)
        elif slot is None:
            slot = _make_sim_slot(_eff_leg(leg_id), target_date)
        post[did].slots.append(slot)
    for sched in post.values():
        sched.slots.sort(key=lambda s: (s.pickup_time, s.leg_id))

    # Affected = drivers whose day CHANGED in a way that can newly break: every
    # receiver, plus holders of retimed legs. A pure donor only loses work
    # (cannot go newly negative); the band diff below still watches him.
    affected = {d for d in move_map.values() if d is not None}
    affected |= {post_assign[lid] for lid in retimed_ids if lid in post_assign}
    affected &= set(post)

    def _loo(scheds, did, leg_obj, leg_id):
        """Leave-one-out feasibility of `leg_obj` against the rest of `did`'s day."""
        sched = scheds[did]
        others = DriverDaySchedule(
            driver_id=did, driver_name=sched.driver_name,
            driver_type=sched.driver_type,
            slots=[s for s in sched.slots if s.leg_id != leg_id],
            vehicle_cap=sched.vehicle_cap)
        return check_feasibility(others, leg_obj, target_date,
                                 driver_window=windows.get(did))

    # ── leave-one-out feasibility + car-share gate over affected drivers ──
    for did in sorted(affected):
        for s in list(post[did].slots):
            L = _eff_leg(s.leg_id)
            feas = _loo(post, did, L, s.leg_id)
            if not feas.feasible:
                # Pre-existing failure on the SAME driver, not touched by this
                # plan -> not our problem; anything else is a NEW negative.
                pre_existing = (
                    s.leg_id not in touched_ids
                    and current_assign.get(s.leg_id) == did
                    and not _loo(schedules, did, L, s.leg_id).feasible
                )
                if not pre_existing:
                    return BoardValidation(
                        ok=False,
                        reason=f"leg {s.leg_id} on driver {did} would be "
                               f"infeasible: {feas.reason}")
            # One physical car: reject if this leg overlaps a car-share partner's jobs.
            if sharer_partners and sharers_conflict(
                    L, did, sharer_partners, post, target_date):
                return BoardValidation(
                    ok=False,
                    reason=f"leg {s.leg_id} on driver {did} would overlap a "
                           f"car-share partner's job (shared vehicle)")

    # ── band diff vs baseline: no pair may get WORSE than it already was ──
    post_bands = board_turn_bands(post, target_date)
    baseline_bands = baseline_bands or {}
    worsened, new_tight = [], 0
    for key, info in post_bands.items():
        after = info["band"]
        before = (baseline_bands.get(key) or {}).get("band", "")
        if _BAND_RANK[after] <= _BAND_RANK[before]:
            continue
        did, prev_id, next_id = key
        if after == "critical":
            return BoardValidation(
                ok=False,
                reason=f"turn {prev_id}->{next_id} on driver {did} would go "
                       f"{before or 'clean'} -> critical "
                       f"({info['slack']} min slack)",
                worsened_pairs=worsened, new_tight_count=new_tight)
        worsened.append({"driver_id": did, "prev_leg_id": prev_id,
                         "next_leg_id": next_id, "before": before,
                         "after": after, "slack": info["slack"]})
        new_tight += 1

    # ── summary numbers over the affected drivers ──
    min_buffer = 999
    per_driver = {}
    for did in sorted(affected):
        d_slacks = [i["slack"] for k, i in post_bands.items()
                    if k[0] == did and i["slack"] is not None]
        d_min = min(d_slacks) if d_slacks else 999
        min_buffer = min(min_buffer, d_min)
        per_driver[did] = {
            "min_buffer": d_min,
            "n_slots": len(post[did].slots),
            "worsened": sum(1 for w in worsened if w["driver_id"] == did),
        }

    return BoardValidation(ok=True, reason="", min_buffer_after=min_buffer,
                           worsened_pairs=worsened, new_tight_count=new_tight,
                           per_driver=per_driver)


# ════════════════════════════════════════════════════════════════════════════
# DB-LOADING WRAPPER (promoted views._revalidate_swap_feasibility, verbatim)
# ════════════════════════════════════════════════════════════════════════════

def revalidate_moves_against_db(valid_moves, target_date):
    """Re-run the FULL feasibility check (Guards A capacity + B turnaround + C window)
    on the board that WOULD result from applying `valid_moves`. Returns (ok, reason).

    Only drivers that GAIN a leg need checking (removing a leg can't make a driver's
    remaining legs infeasible). Read-only; mutates only in-memory copies."""
    from reservations.models import Leg as _Leg
    from drivers.models import Driver
    from dispatching.scheduler import (
        check_feasibility, build_driver_schedules, estimate_job_end_time,
        build_sharer_partners, sharers_conflict,
    )
    from dispatching import feasibility_guards as fg

    move_map = {leg_id: to_driver_id for leg_id, to_driver_id in valid_moves}
    receiving_driver_ids = set(move_map.values())

    legs = list(
        _Leg.objects.filter(pickup_date=target_date)
        .exclude(reservation__status="cancelled").exclude(status="cancelled")
        .select_related("driver", "reservation", "reservation__vehicle", "vehicle", "flight_information")
    )
    legs_by_id = {l.id: l for l in legs}
    for leg_id in move_map:
        if leg_id not in legs_by_id:
            return False, f"leg {leg_id} not found on {target_date}"

    # Apply the moves in memory.
    drv_objs = {d.id: d for d in Driver.objects.filter(id__in=receiving_driver_ids)}
    for leg_id, to_did in move_map.items():
        if to_did not in drv_objs:
            return False, f"driver {to_did} not found"
        l = legs_by_id[leg_id]
        l.driver = drv_objs[to_did]
        l.driver_id = to_did
    for l in legs:
        l._estimated_end_dt = estimate_job_end_time(l, target_date)

    # Shared-car gate: a receiving driver who SHARES a physical vehicle with another
    # working driver can't take a leg that overlaps the partner's jobs — the car is one
    # unit. Build the partner map over every driver holding a leg, plus post-move
    # schedules so sharers_conflict() can compare against the partner's real slots.
    all_leg_driver_ids = {l.driver_id for l in legs if l.driver_id}
    sharer_partners = build_sharer_partners(all_leg_driver_ids, target_date)
    if sharer_partners:
        share_drv_ids = set(all_leg_driver_ids)
        for _ps in sharer_partners.values():
            share_drv_ids.update(_ps)
        share_drv_objs = {d.id: d for d in Driver.objects.filter(id__in=share_drv_ids)}
        post_move_schedules = build_driver_schedules(
            legs, list(share_drv_objs.values()), target_date)

    def _cfg_window(did):
        d = drv_objs.get(did)
        if not d:
            return None
        eff = d.get_effective_availability(target_date)
        mh = eff.get("max_hours")
        return {"start": eff.get("start_hour"), "end": eff.get("end_hour"),
                "max_hours": (float(mh) if mh else None), "flexible": bool(eff.get("flexible"))}

    for did in receiving_driver_ids:
        drv_legs = [l for l in legs if l.driver_id == did]
        # enforce_cap=False: this validates a swap the DISPATCHER explicitly chose — the
        # duty-span cap (Span Governor) must never hard-block an intentional manual move.
        window = fg.get_effective_window(did, configured=_cfg_window(did), enforce_cap=False)
        for L in drv_legs:
            others = [l for l in drv_legs if l.id != L.id]
            sched = build_driver_schedules(others, [drv_objs[did]], target_date).get(did)
            feas = check_feasibility(sched, L, target_date, driver_window=window)
            if not feas.feasible:
                return False, f"leg {L.id} on driver {did} would be infeasible: {feas.reason}"
            # One physical car: reject if this leg overlaps a car-share partner's jobs.
            if sharer_partners and sharers_conflict(
                    L, did, sharer_partners, post_move_schedules, target_date):
                return False, (f"leg {L.id} on driver {did} would overlap a car-share "
                               f"partner's job (shared vehicle)")
    return True, ""
