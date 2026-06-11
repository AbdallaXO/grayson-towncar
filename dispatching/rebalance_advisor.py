"""Rebalance Advisor (demand-aware staffing, round 2) — "spread it evenly, keep it dense."

The founder's RELATIVE balance rule (his correction, verbatim intent): distribute whatever
the day has roughly evenly across the working drivers (3 each on a slow day is fine), and
keep every day DENSE — short-and-tight or full, never long-and-empty, and never 1-vs-7
imbalance without a physical reason (peak or vehicle tier). "A driver looking at his own
day and the guy next to him shouldn't feel cheated."

Two card directions, one kind ("rebalance"):
  fill      "Aftab is needed for the 9 AM peak but has only 1 job — move these 3 jobs
            from runer/shelley to him."  (moves TO the thin driver, heaviest donors first)
  compress  "Raymond's 16:45 and 22:24 stretch a 1.5h morning shift to 14.8h — move them
            to evening drivers; Raymond ends at 10:30."  (boundary legs move OFF the
            hollow driver so his day collapses to short-and-dense)

The deliberate complement to the engine passes: gap compaction never strips a <=3-job
donor and only heals holes; the trim pass fires only past 13.5h-effective/15h-raw; fold
releases whole thin days. None of them looks at RELATIVE job counts or at hollow-but-
not-long days — this advisor does, and ONLY this advisor does.

Propose-only. Accept = manual pins ONLY (sovereign + locked on every re-preview) —
zero DB writes, trivially undoable (delete the pins). Every move passes the same gate
stack as the fold advisor: modal hard window, vehicle tier, the shared-car occupancy
gate, check_feasibility under the cap-clamped window (incl. the night rule), and the
13.5h-effective / 15h-raw post-insert ceilings. Anti-oscillation: accepted moves become
locked pins (a moved leg can never move again), each move must not increase the day's
job-count spread, and a move that would MINT a hollow day (static predicate: raw >= 10h
with a >= 4h internal hole) on its donor or receiver is rejected.

Manual-sovereign per LEG (not per driver, unlike fold): only THIS run's unlocked
engine-proposed legs move; a donor with one hand-placed leg can still donate his engine
legs, but the hand-placed leg itself never moves. Build-1st drivers are excluded as
subjects AND donors (the dispatcher said "build him first" — we neither strip nor
reshape him). Subjects with a live fold card are excluded (fold wins; a fold-refused
thin driver — Aftab — flows through to fill, the designed hand-off).
"""
import copy
from datetime import datetime, date, time as dt_time
from math import ceil, floor

# ── Flags ────────────────────────────────────────────────────────────────────
REBALANCE_ENABLED = True
REBALANCE_MAX_PROPOSALS = 2          # fill + compress combined; one seat reserved for the
                                     # best compress card so fill can never starve it
REBALANCE_MAX_MOVES = 3              # per card — the founder's "move these 3 jobs"
REBALANCE_THIN_FRACTION = 0.5        # thin: jobs <= max(1, floor(mean_jobs * this))
REBALANCE_MIN_SPREAD = 3             # ...AND (max-min jobs) >= this ("never 1-vs-7";
                                     # a 4/3/3 day is "roughly even" -> silent)
REBALANCE_HOLLOW_MIN_RAW_HOURS = 10.0    # hollow: raw span at least this AND...
REBALANCE_HOLLOW_MIN_GAP_MIN = 240       # ...a biggest internal hole at least this (static)
REBALANCE_HOLLOW_MIN_COLLAPSE_HOURS = 4.0  # compress card worth showing only past this
REBALANCE_SUPPRESS_ON_RESIDUALS = True
REBALANCE_INFO_CARDS = 1             # at most one "why not" physical-reason card


def _is_hollow(slots, target_date):
    """Static hollow-day predicate — long span wrapped around a big empty hole.
    Shared by the trigger, the no-new-hollow invariant, and the backtest metric so
    the three can never drift apart."""
    from dispatching.scheduler import effective_span_hours, _max_internal_gap_minutes
    if not slots:
        return False
    raw, _eff = effective_span_hours(slots, target_date)
    return (raw >= REBALANCE_HOLLOW_MIN_RAW_HOURS
            and _max_internal_gap_minutes(slots, target_date) >= REBALANCE_HOLLOW_MIN_GAP_MIN)


def build_rebalance_proposals(target_date: date, proposed_schedules, final_assignments,
                              locked_leg_ids, driver_hours, flexible_drivers,
                              capped_windows, sharer_partners, legs_by_id,
                              drivers_by_id, build_first_ids=None, residual_count=0,
                              exclude_driver_ids=None, explain=False):
    """Pure read. Returns [] or kind='rebalance' cards (direction 'fill'|'compress').
    explain=True returns (proposals, rejections) — same contract as the fold advisor."""
    rejections = []

    def _done(proposals):
        return (proposals, rejections) if explain else proposals

    def _reject(did, reason, **extra):
        rejections.append(dict({"driver_id": did,
                                "driver_name": str(drivers_by_id.get(did, did)),
                                "reason": reason}, **extra))

    if not REBALANCE_ENABLED:
        _reject(None, "advisor_disabled")
        return _done([])
    if residual_count > 0 and REBALANCE_SUPPRESS_ON_RESIDUALS:
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
    from dispatching.fold_advisor import _slot_for_leg, _fmt

    locked = set(locked_leg_ids or [])
    build_first = set(build_first_ids or [])
    excluded = set(exclude_driver_ids or [])
    dvtypes = load_all_driver_vtypes(target_date)
    target_eff = fg.SPAN_SOFT_EFFECTIVE_HOURS

    # Working set: modal drivers actually carrying work. Zero-leg working drivers are
    # the fold advisor's territory (empty-day card), never a fill target or a donor.
    jobs = {}
    for did in driver_hours.keys():
        s = proposed_schedules.get(did)
        n = len(s.slots) if s else 0
        if n > 0:
            jobs[did] = n
    if len(jobs) < 2:
        _reject(None, "fewer_than_two_working_drivers")
        return _done([])
    mean_jobs = sum(jobs.values()) / len(jobs)
    spread = max(jobs.values()) - min(jobs.values())
    thin_cut = max(1, floor(mean_jobs * REBALANCE_THIN_FRACTION))

    def _movable(leg_id, did):
        return final_assignments.get(leg_id) == did and leg_id not in locked

    def _gate_receiver(leg, rid, sim, gates=None):
        """The fold receiver stack against rid on the sim board. Returns
        (ok, raw_after, eff_after, rank_key); on failure bumps gates[name]."""
        rsched = sim.get(rid)

        def fail(name):
            if gates is not None:
                gates[name] = gates.get(name, 0) + 1
            return (False, None, None, None)

        if rsched is None:
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
        if raw_after > SPAN_TRIM_RAW_MAX_HOURS or eff_after > target_eff:
            return fail("span")
        # No-new-hollow: never mint a day this advisor would flag next preview.
        if _is_hollow(rslots + [_slot_for_leg(leg, target_date)], target_date):
            return fail("hollow")
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

    def _raw_span(slots):
        if not slots:
            return 0.0
        first = datetime.combine(target_date, min(x.pickup_time for x in slots))
        last = max(x.estimated_end_time for x in slots)
        return (last - first).total_seconds() / 3600

    touched = set(excluded)   # one card per driver per response; fold subjects excluded

    # ── FILL cards: thinnest first ───────────────────────────────────────────
    fill_cards, info_candidates = [], []
    thin = sorted((did for did, n in jobs.items()
                   if n <= thin_cut and did not in build_first and did not in touched),
                  key=lambda d: (jobs[d], d))
    if spread < REBALANCE_MIN_SPREAD and thin:
        for did in thin:
            _reject(did, "spread_too_small", spread=spread)
        thin = []
    for T in thin:
        sim = copy.deepcopy(proposed_schedules)
        sim_jobs = dict(jobs)
        moves, donor_after = [], {}
        gate_totals = {}
        while len(moves) < REBALANCE_MAX_MOVES and sim_jobs[T] < floor(mean_jobs):
            donors = sorted((d for d in sim_jobs
                             if d != T and d not in build_first and d not in touched
                             and sim_jobs[d] - 1 >= ceil(mean_jobs)
                             and sim_jobs[d] - 1 >= sim_jobs[T] + 1),
                            key=lambda d: (-sim_jobs[d], d))
            best = None   # (rank, leg, donor, raw_after, eff_after)
            for D in donors:
                dsched = sim.get(D)
                dslots = sorted(dsched.slots, key=lambda x: x.pickup_time) if dsched else []
                for slot in dslots:
                    if not _movable(slot.leg_id, D):
                        continue
                    leg = legs_by_id.get(slot.leg_id)
                    if leg is None:
                        continue
                    # donor must stay dense after losing this leg
                    remaining = [x for x in dslots if x.leg_id != slot.leg_id]
                    if _is_hollow(remaining, target_date):
                        gate_totals["donor_hollow"] = gate_totals.get("donor_hollow", 0) + 1
                        continue
                    ok, raw_after, eff_after, rank = _gate_receiver(leg, T, sim, gate_totals)
                    if not ok:
                        continue
                    key = (rank[2], -sim_jobs[D], slot.leg_id)   # deadhead, heaviest donor, id
                    if best is None or key < best[0]:
                        best = (key, leg, D, raw_after, eff_after)
            if best is None:
                break
            _, leg, D, raw_after, eff_after = best
            sim[D].slots = [x for x in sim[D].slots if x.leg_id != leg.id]
            sim[T].slots.append(_slot_for_leg(leg, target_date))
            sim[T].slots.sort(key=lambda x: x.pickup_time)
            sim_jobs[D] -= 1
            sim_jobs[T] += 1
            donor_after[D] = sim_jobs[D]
            moves.append({
                "leg_id": leg.id, "pickup": _fmt(leg.pickup_time),
                "route": f"{(leg.pickup_location or '')[:25]} → {(leg.dropoff_location or '')[:25]}",
                "from_driver_id": D, "from_driver_name": str(drivers_by_id.get(D, D)),
                "from_jobs_before": jobs[D], "from_jobs_after": sim_jobs[D],
                "to_driver_id": T, "to_driver_name": str(drivers_by_id.get(T, T)),
                "receiver_raw_after": round(raw_after, 1),
                "receiver_eff_after": round(eff_after, 1),
            })
        spread_after = max(sim_jobs.values()) - min(sim_jobs.values())
        if moves and spread_after <= spread:
            fill_cards.append({
                "signature": f"rebal-fill-{T}",
                "kind": "rebalance", "direction": "fill",
                "driver_id": T, "driver_name": str(drivers_by_id.get(T, T)),
                "jobs_before": jobs[T], "jobs_after": sim_jobs[T],
                "mean_jobs": round(mean_jobs, 1),
                "spread_before": spread, "spread_after": spread_after,
                "moves": moves,
                "donors": [{"driver_id": d, "name": str(drivers_by_id.get(d, d)),
                            "jobs_before": jobs[d], "jobs_after": donor_after[d]}
                           for d in sorted(donor_after)],
                "note": (f"{str(drivers_by_id.get(T, T))} has {jobs[T]} job(s) vs the day's "
                         f"~{mean_jobs:.1f} average — these even him up."),
            })
            touched.add(T)
            touched.update(donor_after)
        elif moves:
            _reject(T, "spread_would_not_improve", spread_after=spread_after)
        else:
            _reject(T, "no_feasible_moves", eliminated=gate_totals)
            info_candidates.append((jobs[T], T, gate_totals))

    # ── COMPRESS cards: hollow days, biggest potential collapse first ───────
    compress_cards = []
    hollow_subjects = []
    for did, n in sorted(jobs.items()):
        if did in build_first or did in touched or n < 2:
            continue
        s = proposed_schedules.get(did)
        slots = sorted(s.slots, key=lambda x: x.pickup_time) if s else []
        if not _is_hollow(slots, target_date):
            continue
        _raw, _eff = effective_span_hours(slots, target_date)
        if _eff > target_eff:
            _reject(did, "eff_over_target_trim_owns")
            continue
        hollow_subjects.append((-_raw, did, slots))
    hollow_subjects.sort()
    for _negraw, H, hslots in hollow_subjects:
        sim = copy.deepcopy(proposed_schedules)
        cur = sorted(sim[H].slots, key=lambda x: x.pickup_time)
        span_before = _raw_span(cur)
        moves = []
        gate_totals = {}
        while len(moves) < REBALANCE_MAX_MOVES and len(cur) > 1:
            # Peel the boundary leg whose removal collapses the span more; if it has
            # no feasible receiver, try the other end before giving up.
            ends = sorted({cur[0].leg_id, cur[-1].leg_id},
                          key=lambda lid: -abs(span_before - _raw_span(
                              [x for x in cur if x.leg_id != lid])))
            placed = False
            for lid in ends:
                if not _movable(lid, H):
                    continue
                leg = legs_by_id.get(lid)
                if leg is None:
                    continue
                best = None
                for rid in sorted(jobs):
                    if rid == H or rid in build_first:
                        continue
                    ok, raw_after, eff_after, rank = _gate_receiver(leg, rid, sim, gate_totals)
                    if ok and (best is None or rank < best[0]):
                        best = (rank, rid, raw_after, eff_after)
                if best is None:
                    continue
                rank, rid, raw_after, eff_after = best
                sim[H].slots = [x for x in sim[H].slots if x.leg_id != lid]
                sim[rid].slots.append(_slot_for_leg(leg, target_date))
                sim[rid].slots.sort(key=lambda x: x.pickup_time)
                cur = sorted(sim[H].slots, key=lambda x: x.pickup_time)
                moves.append({
                    "leg_id": leg.id, "pickup": _fmt(leg.pickup_time),
                    "route": f"{(leg.pickup_location or '')[:25]} → {(leg.dropoff_location or '')[:25]}",
                    "from_driver_id": H, "from_driver_name": str(drivers_by_id.get(H, H)),
                    "to_driver_id": rid, "to_driver_name": str(drivers_by_id.get(rid, rid)),
                    "receiver_raw_after": round(raw_after, 1),
                    "receiver_eff_after": round(eff_after, 1),
                })
                placed = True
                break
            if not placed:
                break
            if not _is_hollow(cur, target_date):
                break   # dense enough — stop peeling
        span_after = _raw_span(cur)
        collapse = span_before - span_after
        if moves and collapse >= REBALANCE_HOLLOW_MIN_COLLAPSE_HOURS:
            ends_at = max(x.estimated_end_time for x in cur)
            compress_cards.append({
                "signature": f"rebal-compress-{H}",
                "kind": "rebalance", "direction": "compress",
                "driver_id": H, "driver_name": str(drivers_by_id.get(H, H)),
                "jobs_before": jobs[H], "jobs_after": len(cur),
                "mean_jobs": round(mean_jobs, 1),
                "span_before": round(span_before, 1), "span_after": round(span_after, 1),
                "ends_at": _fmt(ends_at.time()),
                "moves": moves,
                "donors": [],
                "note": (f"{str(drivers_by_id.get(H, H))}'s outlier job(s) stretch "
                         f"{span_before:.1f}h around a hollow day — move them and he "
                         f"ends at {_fmt(ends_at.time())}."),
            })
            touched.add(H)
            touched.update(m["to_driver_id"] for m in moves)
        elif moves:
            _reject(H, "collapse_below_threshold", collapse_h=round(collapse, 1))
        else:
            _reject(H, "no_feasible_moves", eliminated=gate_totals)

    # ── Cap with a compress fairness seat; fill first (the founder's main pain) ──
    proposals = fill_cards + compress_cards
    if len(proposals) > REBALANCE_MAX_PROPOSALS:
        kept = proposals[:REBALANCE_MAX_PROPOSALS]
        if compress_cards and not any(p["direction"] == "compress" for p in kept):
            kept = kept[:-1] + [compress_cards[0]]
        for p in proposals:
            if p not in kept:
                _reject(p["driver_id"], "over_max_proposals")
        proposals = kept

    # ── Physical-reason info card (the founder's "make it make sense") ──────
    if info_candidates and REBALANCE_INFO_CARDS > 0:
        info_candidates.sort()
        n_jobs, T, gates = info_candidates[0]
        dominant = max(gates, key=gates.get) if gates else None
        phrase = {
            "feasibility": "every mover is mid-job around his hours",
            "window": "the others' working windows don't cover his hours",
            "tier": "nobody's jobs fit his vehicle size",
            "occupancy": "the shared cars are spoken for at those times",
            "span": "filling him would over-stretch someone's day",
            "hollow": "every candidate move would hollow out another day",
            "donor_hollow": "taking jobs off the others would hollow THEIR days",
            "idle": "the other drivers aren't carrying work yet",
        }.get(dominant, "no movable job fits him")
        # Peak-anchored qualifier: concurrency at his anchor hour vs the day's peak.
        anchor_note = ""
        t_slots = (proposed_schedules.get(T).slots if proposed_schedules.get(T) else [])
        if dominant == "feasibility" and t_slots:
            anchor = sorted(t_slots, key=lambda x: x.pickup_time)[0]
            anchor_dt = datetime.combine(target_date, anchor.pickup_time)
            inflight = peak = 0
            events = []
            for sch_ in proposed_schedules.values():
                for x in sch_.slots:
                    events.append((datetime.combine(target_date, x.pickup_time), 1))
                    events.append((x.estimated_end_time, -1))
            # arrivals before departures at ties — the conservative peak reading
            # (same convention as day_setup.peak_concurrency).
            events.sort(key=lambda e: (e[0], -e[1]))
            curn = 0
            for t, d in events:
                curn += d
                peak = max(peak, curn)
                if t <= anchor_dt:
                    inflight = curn
            if peak and inflight >= 0.8 * peak:
                anchor_note = (f" His {_fmt(anchor.pickup_time)} job IS the peak — a short "
                               f"day here is the physical reason.")
        proposals.append({
            "signature": f"_rebal-noop-{T}", "kind": "info", "direction": "fill",
            "leg_count": 0,
            "text": (f"{str(drivers_by_id.get(T, T))} stays at {n_jobs} job(s) — "
                     f"{phrase} ({sum(gates.values())} candidate move(s) checked)."
                     f"{anchor_note}"),
        })

    return _done(proposals)
