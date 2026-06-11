"""Fold-Out Advisor (demand-aware staffing) — "this day can run leaner."

The mirror image of the Second-Shift Advisor: after the full auto-assign pipeline
(build -> swap -> rescue -> trim -> gap passes), look for THIN drivers — a working
vehicle-holder with only a few engine-proposed legs — and propose releasing them:
"sereen has only 3 jobs and they all fit on ken/george/rizwan — fold her out and
free her car?" Accepting relocates the legs (client-side manual pins, sovereign on
re-preview), takes the driver off the day, and deletes his vehicle row — the freed
unit then shows up as a SPARE unit the Second-Shift Advisor can monetize.

Propose-only, never automatic. Every relocation in a card was validated against the
proposed board through the same gate stack every engine pass uses: the receiver's
modal hard window, vehicle-tier compatibility, the shared-car occupancy gate
(sharers_conflict), check_feasibility under his cap-clamped window (turnaround +
night rule + max_hours), and post-insert span ceilings (effective <= the 13.5h soft
target AND raw <= SPAN_TRIM_RAW_MAX_HOURS — the trim pass's own trigger, so a fold
can never mint a day the next preview tries to unwind). ALL of a candidate's legs
must place or there is no card — a fold is whole-day or nothing.

Complement to the gap pass: GAP_COMPACT_PROTECT_DONOR_MAX_JOBS deliberately refuses
to strip <=3-job donors piecemeal, so a thin driver's day arrives here INTACT —
which is exactly what makes a whole-day fold possible. Fold-out never partially
drains a driver, so the protection's intent (never leave a thin driver holding a
gutted day) is honored; the two never fight.

Manual-sovereign invariant: a driver holding ANY leg the dispatcher placed or
locked (manual pin, Build-1st seed, trim-moved leg, or a pre-existing DB
assignment — those are absent from final_assignments entirely) is NEVER proposed
for fold-out. Sharers are excluded in v1 (folding an AM sharer would orphan the
partner's planned handoff window).
"""
import copy
from datetime import datetime, date, time as dt_time

# ── Flags ────────────────────────────────────────────────────────────────────
FOLD_OUT_ENABLED = True
FOLD_OUT_MAX_LEGS = 3            # founder's "only 3 jobs"; mirrors GAP_COMPACT_PROTECT_DONOR_MAX_JOBS
FOLD_OUT_MAX_PROPOSALS = 2       # each accept reshapes the board; rePreview regenerates the rest
FOLD_OUT_REQUIRE_VEHICLE = True  # freeing a car is the point; flip later for body-only folds
FOLD_OUT_SUPPRESS_ON_RESIDUALS = True   # a day needing MORE coverage never shows a release card
FOLD_OUT_INCLUDE_EMPTY = True    # a working vehicle-holder with 0 legs = trivially foldable


def _slot_for_leg(leg, target_date):
    """Local mirror of views._leg_to_slot — just enough for span/occupancy math."""
    from dispatching.analytics import categorize_location
    from dispatching.scheduler import ScheduleSlot, estimate_job_end_time
    return ScheduleSlot(
        leg_id=leg.id, pickup_time=leg.pickup_time,
        pickup_location=leg.pickup_location,
        pickup_category=categorize_location(leg.pickup_location),
        dropoff_location=leg.dropoff_location,
        dropoff_category=categorize_location(leg.dropoff_location),
        trip_type=leg.get_trip_type(),
        estimated_end_time=estimate_job_end_time(leg, target_date),
        reservation_id=getattr(leg, "reservation_id", 0) or 0, customer_name="",
        status=getattr(leg, "status", "pending") or "pending", has_flight=False,
        revenue=getattr(leg, "revenue_share", None),
    )


def _fmt(t):
    return t.strftime("%I:%M %p").lstrip("0")


def build_fold_out_proposals(target_date: date, proposed_schedules, final_assignments,
                             locked_leg_ids, driver_hours, flexible_drivers,
                             capped_windows, sharer_partners, legs_by_id,
                             drivers_by_id, build_first_ids=None, residual_count=0,
                             explain=False):
    """Pure read (one DVA query for the candidates' vehicles). Returns [] or a list of
    kind='fold_out' proposal dicts, ranked fewest-legs / least-revenue first.

    explain=True returns (proposals, rejections) instead — rejections name WHY each
    working driver got no card (gate name, or the failing leg + per-gate receiver
    elimination counts for an all-or-nothing simulation failure). Diagnostic only
    (harness / founder "why no card for X?" questions); the views hook never sets it.

    proposed_schedules: the PROPOSED board {driver_id: DriverDaySchedule} (post all passes).
    final_assignments:  {leg_id: driver_id} — THIS run's proposals (pre-existing DB
                        assignments are absent; that absence is the manual-sovereign detector).
    locked_leg_ids:     manual pins + Build-1st seeds + trim-moved legs.
    driver_hours:       {driver_id: (start_hour, end_hour)} — the modal's authoritative
                        working set; receivers' hard windows.
    capped_windows:     {driver_id: get_effective_window(enforce_cap=True) dict} — carries
                        max_hours + flexible + night_exempt=False for the night rule.
    """
    rejections = []

    def _done(proposals):
        return (proposals, rejections) if explain else proposals

    def _reject(did, reason, **extra):
        rejections.append(dict({"driver_id": did,
                                "driver_name": str(drivers_by_id.get(did, did)),
                                "reason": reason}, **extra))

    if not FOLD_OUT_ENABLED:
        _reject(None, "advisor_disabled")
        return _done([])
    if residual_count > 0 and FOLD_OUT_SUPPRESS_ON_RESIDUALS:
        _reject(None, f"suppressed_on_residuals({residual_count})")
        return _done([])
    if not driver_hours:
        _reject(None, "no_working_drivers")
        return _done([])
    from dispatching import feasibility_guards as fg
    from dispatching.analytics import categorize_location
    from dispatching.scheduler import (
        check_feasibility, sharers_conflict, effective_span_hours,
        _span_gap_credit_minutes, estimate_job_end_time, get_vehicle_tier,
        get_compatible_vehicle_types, resolve_drive_minutes, load_all_driver_vtypes,
        SPAN_TRIM_RAW_MAX_HOURS,
    )
    from dispatching.day_setup import _unit_label
    from drivers.models import DriverVehicleAssignment

    locked = set(locked_leg_ids or [])
    build_first = set(build_first_ids or [])
    dvtypes = load_all_driver_vtypes(target_date)

    # Vehicle per working driver (the unit a fold would free).
    veh_by_driver = {}
    for a in (DriverVehicleAssignment.objects
              .filter(date=target_date, vehicle__isnull=False,
                      driver_id__in=set(driver_hours.keys()))
              .select_related("vehicle", "vehicle__vehicle_type")):
        veh_by_driver[a.driver_id] = a.vehicle

    # ── Candidates: thin working vehicle-holders whose WHOLE day is movable ──
    candidates = []
    for did in sorted(driver_hours.keys()):
        if did in build_first:
            _reject(did, "build_first")
            continue
        if did in (sharer_partners or {}):
            _reject(did, "share_partner")
            continue
        unit = veh_by_driver.get(did)
        if unit is None and FOLD_OUT_REQUIRE_VEHICLE:
            _reject(did, "no_vehicle")
            continue
        sched = proposed_schedules.get(did)
        slots = sorted(sched.slots, key=lambda s: (s.pickup_time, s.leg_id)) if sched else []
        if len(slots) > FOLD_OUT_MAX_LEGS:
            _reject(did, "not_thin", legs=len(slots))
            continue
        if not slots and not (FOLD_OUT_INCLUDE_EMPTY and unit is not None):
            _reject(did, "empty_day_excluded")
            continue
        # Manual-sovereign: every leg must be THIS run's unlocked proposal.
        if any(final_assignments.get(s.leg_id) != did or s.leg_id in locked
               for s in slots):
            _reject(did, "locked_or_dispatcher_leg")
            continue
        if any(legs_by_id.get(s.leg_id) is None for s in slots):
            _reject(did, "missing_leg_object")
            continue
        revenue = sum(float(getattr(legs_by_id[s.leg_id], "revenue_share", 0) or 0)
                      for s in slots)
        candidates.append((len(slots), revenue, did, unit, slots))
    candidates.sort(key=lambda c: (c[0], c[1], c[2]))   # fewest legs / least at stake first

    target_eff = fg.SPAN_SOFT_EFFECTIVE_HOURS

    def _simulate(did, slots):
        """All-or-nothing greedy placement of `did`'s legs onto other working drivers.
        Returns (relocations, receivers_summary) or None when any leg won't place."""
        sim = copy.deepcopy(proposed_schedules)
        if did in sim:
            sim[did].slots = []
        eff_before = {}
        relocations = []
        for s in slots:
            leg = legs_by_id[s.leg_id]
            lvtype = leg.effective_vehicle_type
            l_tier = get_vehicle_tier(str(lvtype)) if lvtype else -1
            pickup_dt = datetime.combine(target_date, leg.pickup_time)
            new_end = estimate_job_end_time(leg, target_date)
            # Per-gate receiver elimination counts — the explain channel's evidence for
            # WHY an all-or-nothing fold failed ("everyone busy at the peak" reads as a
            # high feasibility count; "nobody else drives a van" as a high tier count).
            gates = {"idle": 0, "window": 0, "tier": 0, "occupancy": 0,
                     "feasibility": 0, "span": 0}
            best = None   # (rank_key, rid, raw_after, eff_after)
            for rid in sorted(driver_hours.keys()):
                if rid == did:
                    continue
                rsched = sim.get(rid)
                # Receivers must already be carrying work — moving a thin day onto an
                # idle body just swaps who gets released, so it never consolidates.
                if rsched is None or not rsched.slots:
                    gates["idle"] += 1
                    continue
                # Modal hard window (parity with the greedy/trim/gap passes; every rid
                # here comes from driver_hours, so no membership re-check needed).
                if not (flexible_drivers and rid in flexible_drivers):
                    sh, eh = driver_hours[rid]
                    if not (dt_time(sh, 0) <= leg.pickup_time <= dt_time(eh, 59)):
                        gates["window"] += 1
                        continue
                rv = dvtypes.get(rid)
                if rv and lvtype and str(lvtype) not in get_compatible_vehicle_types(rv):
                    gates["tier"] += 1
                    continue
                if sharers_conflict(leg, rid, sharer_partners, sim, target_date):
                    gates["occupancy"] += 1
                    continue
                feas = check_feasibility(rsched, leg, target_date,
                                         driver_window=(capped_windows or {}).get(rid))
                if not feas.feasible:
                    gates["feasibility"] += 1
                    continue
                # Post-insert span ceilings (trim-pass idiom; credit from PRE-insert slots).
                rslots = sorted(rsched.slots, key=lambda x: x.pickup_time)
                or_first = datetime.combine(target_date, rslots[0].pickup_time)
                or_last = max(x.estimated_end_time for x in rslots)
                nr_first = min(or_first, pickup_dt)
                nr_last = max(or_last, new_end)
                raw_after = (nr_last - nr_first).total_seconds() / 3600
                credit_h = _span_gap_credit_minutes(rslots, target_date) / 60.0
                eff_after = max(0.0, raw_after - credit_h)
                if raw_after > SPAN_TRIM_RAW_MAX_HOURS or eff_after > target_eff:
                    gates["span"] += 1
                    continue
                raw_before = (or_last - or_first).total_seconds() / 3600
                stretch_min = max(0.0, (raw_after - raw_before) * 60)
                tier_waste = ((get_vehicle_tier(rv) - l_tier)
                              if (rv and l_tier >= 0 and get_vehicle_tier(rv) > l_tier) else 0)
                # Deadhead around the insertion point — tiebreak only, like the gap pass.
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
                key = (tier_waste, round(stretch_min), dh, rid)
                if best is None or key < best[0]:
                    best = (key, rid, raw_after, eff_after)
            if best is None:
                # all-or-nothing: one unplaceable leg kills the card
                return False, {"failed_leg_id": leg.id,
                               "failed_pickup": _fmt(leg.pickup_time),
                               "eliminated": gates}
            _, rid, raw_after, eff_after = best
            if rid not in eff_before:
                eff_before[rid] = effective_span_hours(sim[rid].slots, target_date)[1]
            sim[rid].slots.append(_slot_for_leg(leg, target_date))
            sim[rid].slots.sort(key=lambda x: x.pickup_time)
            relocations.append({
                "leg_id": leg.id,
                "pickup": _fmt(leg.pickup_time),
                "route": f"{(leg.pickup_location or '')[:25]} → {(leg.dropoff_location or '')[:25]}",
                "to_driver_id": rid,
                "to_driver_name": str(drivers_by_id.get(rid, rid)),
                "receiver_eff_before": round(eff_before[rid], 1),
                "receiver_eff_after": round(eff_after, 1),
                "receiver_raw_after": round(raw_after, 1),
            })
        receivers = []
        for rid in sorted({r["to_driver_id"] for r in relocations}):
            mine = [r for r in relocations if r["to_driver_id"] == rid]
            receivers.append({
                "driver_id": rid, "name": mine[0]["to_driver_name"],
                "legs_added": len(mine),
                "eff_before": eff_before[rid],
                "eff_after": mine[-1]["receiver_eff_after"],
            })
        return True, (relocations, receivers)

    proposals = []
    dropped = 0
    for n_legs, revenue, did, unit, slots in candidates:
        if len(proposals) >= FOLD_OUT_MAX_PROPOSALS:
            dropped += 1
            _reject(did, "over_max_proposals")
            continue
        ok, payload = _simulate(did, slots)
        if not ok:
            _reject(did, "simulation_failed", **payload)
            continue
        relocations, receivers = payload
        unit_label = _unit_label(unit) if unit is not None else ""
        if slots:
            window = (f"{_fmt(slots[0].pickup_time)}–"
                      f"{_fmt(max(s.estimated_end_time for s in slots))}")
        else:
            window = ""
        proposals.append({
            "signature": f"fold-{did}",
            "kind": "fold_out",
            "driver_id": did,
            "driver_name": str(drivers_by_id.get(did, did)),
            "vehicle_id": unit.id if unit is not None else None,
            "vehicle_label": unit_label,
            "leg_count": n_legs,
            "revenue": round(revenue),
            "window": window,
            "relocations": relocations,
            "receivers": receivers,
            "freed_note": (f"Frees {unit_label} — a spare unit for a second shift or "
                           f"tomorrow's plan." if unit is not None else ""),
        })

    if dropped:
        proposals.append({
            "signature": "_fold_more", "kind": "info", "leg_count": 0,
            "text": "More thin days could fold — accept one of these and rebuild to see the rest.",
        })
    return _done(proposals)
