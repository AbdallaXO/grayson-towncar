"""The one receiver gate — shared by every advisor that relocates a leg
(Build 3b, the Ticket A3 prerequisite; 05 §2 A3, 00 §B3).

WHAT THIS IS
------------
"Can driver ``rid`` absorb ``leg`` on this simulated board — and how good a
home is he?" ``fold_advisor._simulate`` and ``rebalance_advisor._gate_receiver``
each carried their own hand-maintained copy of that stack, and the copies had
drifted (00 §B3). Both now call this one function; Build 3b's Pass A pre-screen
leans on fold's ranking, which is why the drift had to be reconciled BEFORE the
optimizer was built on top of it.

The stack, in order — the same order both copies already used:

    idle -> window -> tier -> occupancy -> feasibility -> span [-> hollow]

  idle         the receiver has no board to judge (see the drift note below)
  window       the modal hard window, unless the receiver is flexible
  tier         vehicle-tier compatibility (get_compatible_vehicle_types)
  occupancy    the co-driver car-share gate (car_share convention A,
               via scheduler.sharers_conflict)
  feasibility  check_feasibility under the receiver's cap-clamped window
  span         post-insert ceilings: raw <= SPAN_TRIM_RAW_MAX_HOURS AND
               effective <= the 13.5h soft target (trim-pass idiom; the gap
               credit comes from the PRE-insert slots, so an insert can never
               earn credit by minting the hole it is being priced against)
  hollow       rebalance only — never mint a day the rebalance advisor would
               flag next preview (_is_hollow on the post-insert slots)

On success the gate returns the same rank key both copies computed —
``(tier_waste, round(stretch_min), deadhead_min, rid)`` — so caller-side
"best receiver" selection is unchanged.

THE RECONCILED DRIFT — parameterized, deliberately NOT unified
--------------------------------------------------------------
The two copies disagreed in exactly two places (00 §B3), and both differences
are REAL POLICY, not accidents, so this extraction makes them explicit
parameters instead of silently picking a side (the Build 3a discipline —
a unification that changes a verdict goes back to the founder, 04 §1):

* ``require_carrying_work`` — the ``idle`` gate's meaning.
  Fold (True): a receiver must ALREADY be carrying work — moving a thin day
  onto an idle body just swaps who gets released, so it never consolidates.
  Rebalance (False): a schedule merely has to exist. In rebalance's current
  call pattern every receiver already carries work (fill targets and compress
  receivers both come from the jobs map), so the flag is latent there — but it
  is the documented semantic of its copy and is preserved verbatim.

* ``hollow_gate`` — rebalance's seventh gate.
  Rebalance (True): reject an insert that would leave the receiver hollow
  (long span wrapped around a >=4h hole) — its own anti-oscillation invariant
  ("never mint a day this advisor would flag next preview"). Fold (False):
  fold predates the hollow predicate and adding it would change fold verdicts
  on real boards — a founder call, not a refactor's.

Gate: analysis/14_pipeline_parity.py re-captured before/after this extraction
over 10 replayed dates x 4 scenarios — the view's complete JSON response,
advisor cards included, byte-identical. Neither flag combination changes any
verdict anywhere; that is what "reconciled" means here.

``gates`` is the explain channel both advisors already had: a dict the failing
gate's name is counted into (``gates[name] = gates.get(name, 0) + 1``). Fold
pre-seeds fixed keys (zero counts render in its rejection payload); rebalance
passes a bare dict that accumulates only what fired. Both behaviours flow
through unchanged.
"""
from datetime import datetime, time as dt_time


def gate_receiver(leg, rid, sim, *, target_date, driver_hours, flexible_drivers,
                  capped_windows, sharer_partners, dvtypes,
                  require_carrying_work, hollow_gate, gates=None):
    """Run the receiver stack for ``leg`` against ``rid`` on the ``sim`` board.

    Returns ``(ok, raw_after, eff_after, rank_key)``; on failure the three
    trailing values are None and ``gates[<gate name>]`` is incremented.

    sim             {driver_id: DriverDaySchedule} — the SIMULATED board the
                    caller is mutating as it places legs.
    driver_hours    {driver_id: (start_hour, end_hour)} — every rid passed in
                    must be a key (both callers guarantee it).
    capped_windows  {driver_id: get_effective_window dict} — cap-clamped,
                    night-rule-carrying windows for check_feasibility.
    dvtypes         load_all_driver_vtypes(target_date) — receiver tiers.
    """
    from dispatching import feasibility_guards as fg
    from dispatching.analytics import categorize_location
    from dispatching.scheduler import (
        check_feasibility, sharers_conflict, _span_gap_credit_minutes,
        estimate_job_end_time, get_vehicle_tier, get_compatible_vehicle_types,
        resolve_drive_minutes, SPAN_TRIM_RAW_MAX_HOURS,
    )

    def fail(name):
        if gates is not None:
            gates[name] = gates.get(name, 0) + 1
        return (False, None, None, None)

    rsched = sim.get(rid)
    if rsched is None or (require_carrying_work and not rsched.slots):
        return fail("idle")
    if not (flexible_drivers and rid in flexible_drivers):
        sh, eh = driver_hours[rid]
        if not (dt_time(sh, 0) <= leg.pickup_time <= dt_time(eh, 59)):
            return fail("window")
    lvtype = leg.effective_vehicle_type
    rv = dvtypes.get(rid)
    if rv and lvtype and str(lvtype) not in get_compatible_vehicle_types(rv):
        return fail("tier")
    if sharers_conflict(leg, rid, sharer_partners, sim, target_date):
        return fail("occupancy")
    feas = check_feasibility(rsched, leg, target_date,
                             driver_window=(capped_windows or {}).get(rid))
    if not feas.feasible:
        return fail("feasibility")

    # Post-insert span ceilings (trim-pass idiom; credit from PRE-insert slots).
    pickup_dt = datetime.combine(target_date, leg.pickup_time)
    new_end = estimate_job_end_time(leg, target_date)
    rslots = sorted(rsched.slots, key=lambda x: x.pickup_time)
    if rslots:
        or_first = datetime.combine(target_date, rslots[0].pickup_time)
        or_last = max(x.estimated_end_time for x in rslots)
        nr_first, nr_last = min(or_first, pickup_dt), max(or_last, new_end)
        credit_h = _span_gap_credit_minutes(rslots, target_date) / 60.0
        raw_before = (or_last - or_first).total_seconds() / 3600
    else:
        nr_first, nr_last, credit_h, raw_before = pickup_dt, new_end, 0.0, 0.0
    raw_after = (nr_last - nr_first).total_seconds() / 3600
    eff_after = max(0.0, raw_after - credit_h)
    if raw_after > SPAN_TRIM_RAW_MAX_HOURS or eff_after > fg.SPAN_SOFT_EFFECTIVE_HOURS:
        return fail("span")

    if hollow_gate:
        # Rebalance's no-new-hollow invariant. Lazy imports: rebalance imports
        # this module, so importing its predicate at module level would cycle.
        from dispatching.fold_advisor import _slot_for_leg
        from dispatching.rebalance_advisor import _is_hollow
        if _is_hollow(rslots + [_slot_for_leg(leg, target_date)], target_date):
            return fail("hollow")

    # The shared rank key: prefer no tier waste, then least day-stretch, then
    # least deadhead around the insertion point, then the lower driver id.
    stretch_min = max(0.0, (raw_after - raw_before) * 60)
    l_tier = get_vehicle_tier(str(lvtype)) if lvtype else -1
    tier_waste = ((get_vehicle_tier(rv) - l_tier)
                  if (rv and l_tier >= 0 and get_vehicle_tier(rv) > l_tier) else 0)
    prev = max((x for x in rslots if x.pickup_time <= leg.pickup_time),
               key=lambda x: x.pickup_time, default=None)
    nxt = min((x for x in rslots if x.pickup_time > leg.pickup_time),
              key=lambda x: x.pickup_time, default=None)
    dh = 0
    if prev is not None:
        dh += resolve_drive_minutes(prev.dropoff_location, leg.pickup_location,
                                    prev.dropoff_category,
                                    categorize_location(leg.pickup_location))
    if nxt is not None:
        dh += resolve_drive_minutes(leg.dropoff_location, nxt.pickup_location,
                                    categorize_location(leg.dropoff_location),
                                    nxt.pickup_category)
    return (True, raw_after, eff_after, (tier_waste, round(stretch_min), dh, rid))
