"""The assignment build pipeline — one callable, one home (Build 3a, P1).

WHAT THIS IS
------------
Every pass that turns "a date's legs + a roster" into "{leg_id: driver_id}"
used to live inline inside ``views.auto_assign_drivers``, reachable only by
POSTing to an endpoint. This module is that build, extracted verbatim behind
one function:

    assignments, warnings, moves = run_assignment_pipeline(
        legs, drivers, target_date, windows, locked, dva_rows=None)

``auto_assign_drivers`` is now a caller: it parses the request, loads the
day, calls this, and renders. Nothing about the build changed — the extraction
gate (docs/scheduling-redesign/analysis/14_pipeline_parity.py) captures the
view's complete JSON response over 10 replayed dates x 4 payload scenarios,
before and after, and fails on any difference.

WHY IT EXISTS (01 §A3 — the Candidate-Plan Outer Loop)
------------------------------------------------------
Build 3's optimizer materialises a candidate plan (roster, driver/vehicle
pairing, share cuts) as UNSAVED ``DriverVehicleAssignment`` objects and scores
it by running it through the shipped engine, so a candidate's feasibility can
never diverge from what production would do with it. That needs the build to
accept a hypothetical roster instead of reading the live one. Hence:

  * ``drivers`` and ``windows`` are supplied by the caller, not discovered —
    the optimizer names who works and when;
  * ``dva_rows`` threads a hypothetical roster through every DVA-reading
    engine call in the pipeline (``build_driver_schedules`` for the vehicle
    caps, ``build_sharer_partners`` for the co-driver map,
    ``load_all_driver_vtypes`` for the class gate). Left None — which is what
    the view passes — each of those queries the database exactly as before.

The eight passes, in order (unchanged):
  1. "Build first" priority seeding      build_smart_schedule, per pinned driver
  2. Greedy placement                    suggest_assignments_clustered
  3. Pre-farm swap recovery              recover_residuals_via_swaps
  4. Evict-to-farm value pass            evict_to_farm_for_value
  5. Span-cap coverage rescue            rescue_span_blocked_residuals
  6. Span-trim relocation                trim_spans_via_relocation
  7. Gap-compaction relocation           compact_gaps_via_relocation
  8. Final free-insertion sweep          evict_to_farm_for_value(free_insert_only)

POSTURE: READ-ONLY. This module never writes a Leg, a DVA row or anything
else. It reads the database (the day's board, yesterday's clear times, the
roster) and returns a proposal. Applying it is the caller's job, through the
existing validated doors — ``apply_day_setup`` and the view's own apply
branch. There is no new write door here and no third copy of
``set_leg_driver`` semantics (00 §B3).

One in-place mutation is intentional and reversed before return: build-first
seeding temporarily stamps ``leg.driver`` on the seeded legs so the board
rebuild sees those drivers as busy, then restores them. The legs are Python
objects the caller owns; nothing is saved.
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════════════
# INPUTS
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class PipelineWindows:
    """Per-driver working intent for this run — who works, when, how long.

    The view fills this from the Auto-Assign modal payload (falling back to
    saved availability); Build 3's optimizer fills it from the candidate plan.
    The pipeline turns it into the cap-clamped ``capped_windows`` map every
    pass reads, via the one ``feasibility_guards.get_effective_window`` funnel.

    driver_hours       {driver_id: (start_hour, end_hour)} — a HARD window
                       unless the driver is in ``flexible_drivers``.
    flexible_drivers   drivers who work anytime (window not enforced).
    driver_max_hours   {driver_id: hours} — intent; may raise past the default
                       cap, up to the absolute ceiling.
    strict_span_caps   {driver_id: hours} the dispatcher explicitly TYPED —
                       the span rescue never lifts these.
    preferences        {driver_id: "prefer_arrival"} overrides layered on top
                       of each driver's saved preference.
    run_min_buffer     turn buffer for this run (already resolved through
                       ``scheduler.resolve_run_min_buffer``).
    driver_min_buffers {driver_id: minutes} per-driver overrides.
    """
    driver_hours: Dict[int, tuple] = field(default_factory=dict)
    flexible_drivers: Set[int] = field(default_factory=set)
    driver_max_hours: Dict[int, float] = field(default_factory=dict)
    strict_span_caps: Dict[int, float] = field(default_factory=dict)
    preferences: Dict[int, str] = field(default_factory=dict)
    run_min_buffer: Optional[int] = None
    driver_min_buffers: Dict[int, int] = field(default_factory=dict)


@dataclass
class PipelineLocks:
    """What the human already decided, which the passes may not undo.

    manual_assignments {leg_id: driver_id} pinned by the dispatcher — placed
                       verbatim and never relocated by the swap/trim/gap passes.
    build_first        driver ids whose FULL day is seeded before the general
                       assignment (their seeded legs are locked too).
    excluded_leg_ids   legs the dispatcher removed from the day entirely.
    exclude_unpaid     drop unpaid reservations from the AUTO pool (manual
                       assignments are kept — a deliberate override).
    """
    manual_assignments: Dict[int, int] = field(default_factory=dict)
    build_first: List = field(default_factory=list)
    excluded_leg_ids: List[int] = field(default_factory=list)
    exclude_unpaid: bool = False


# ════════════════════════════════════════════════════════════════════════════
# OUTPUT
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class PipelineResult:
    """The build's result.

    The documented contract is the three-tuple — ``assignments, warnings,
    moves = run_assignment_pipeline(...)`` unpacks exactly that, because
    ``__iter__`` yields those three in order. Everything below them is derived
    state the pipeline computed on the way and callers would otherwise have to
    recompute (and could recompute differently): the board it started from, the
    locks as they stand after the trim pass, the residual pool. Reading more
    fields is additive; the tuple never changes shape.
    """
    # ── the contract ──
    assignments: Dict[int, int]          # {leg_id: driver_id} — the proposal
    warnings: List[dict]                 # span-governor warnings (rescued /
                                         # strict_blocked / ceiling_blocked)
    moves: Dict[str, list]               # {"evict": [...], "trim": [...],
                                         # "gap": [...]} — relocation logs

    # ── derived state, for callers that render or advise on the result ──
    board: Dict[int, object] = field(default_factory=dict)      # pre-build board
    seeded: Dict[int, int] = field(default_factory=dict)        # build-first legs
    locked_ids: Set[int] = field(default_factory=set)           # AFTER the trim pass
    priority_ids: List[int] = field(default_factory=list)       # build-first, ordered
    prev_end_by_driver: Dict[int, datetime] = field(default_factory=dict)
    capped_windows: Dict[int, dict] = field(default_factory=dict)
    sharer_partners: Dict[int, set] = field(default_factory=dict)
    legs_by_id: Dict[int, object] = field(default_factory=dict)
    drivers_by_id: Dict[int, object] = field(default_factory=dict)
    unassigned: List = field(default_factory=list)              # incl. manual pool
    auto_unassigned: List = field(default_factory=list)         # auto pool, post-seeding

    def __iter__(self):
        """``a, w, m = run_assignment_pipeline(...)`` — the 04 §4 contract."""
        yield self.assignments
        yield self.warnings
        yield self.moves


# ════════════════════════════════════════════════════════════════════════════
# THE PIPELINE
# ════════════════════════════════════════════════════════════════════════════

def run_assignment_pipeline(legs, drivers, target_date, windows, locked,
                            *, dva_rows=None):
    """Run the full assignment build for one date. Returns a PipelineResult.

    legs         the date's legs, already filtered (no cancelled) and
                 prefetched. ``build_driver_schedules`` counts stops per leg
                 and runs once PER PASS, so ``legstop_set`` / ``legflight_set``
                 prefetches and ``flight_information`` select_related matter a
                 lot here — without them each rebuild fires two COUNT queries
                 per leg.
    drivers      the WORKING in-house drivers (already filtered to the roster
                 this run is building for).
    target_date  the date being built.
    windows      a PipelineWindows.
    locked       a PipelineLocks.
    dva_rows     optional pre-fetched or HYPOTHETICAL DriverVehicleAssignment
                 rows for the date. None (the view's call) => every DVA-reading
                 engine call queries the database exactly as it always has.
    """
    from dispatching import feasibility_guards as fg
    from dispatching.scheduler import (
        build_driver_schedules, build_sharer_partners, load_all_driver_vtypes,
        suggest_assignments_clustered,
    )
    import dispatching.scheduler as sch

    # The route-timing cache is process-global and read once (1 query) so the
    # build + swap + gap passes don't each fall back to per-leg RouteTimingMetric
    # hits (~1,500 queries -> 1). The view warms it before calling; warm it here
    # too for any other caller. A no-op when it is already warm.
    if sch._timing_cache is None:
        sch.preload_timing_cache()

    driver_hours = windows.driver_hours
    flexible_drivers = windows.flexible_drivers
    driver_max_hours = windows.driver_max_hours
    strict_span_caps = windows.strict_span_caps
    run_min_buffer = windows.run_min_buffer
    driver_min_buffers = windows.driver_min_buffers

    manual_assignments = locked.manual_assignments
    raw_build_first = locked.build_first

    # ── Span Governor: one cap-clamped, modal-aware window per working driver ──
    # max_hours via the get_effective_window funnel: min(stub, 15h default) — but a
    # modal-typed/DB per-driver value is INTENT and may raise past the default, up to
    # the 17h absolute ceiling.
    # Built for EVERY working driver and handed to the swap + rescue passes (find_swaps
    # restricts its receiver pool to this dict's keys, so a partial map would silently
    # shrink swap recovery). The greedy + gap passes get the same caps through their own
    # get_effective_window calls.
    capped_windows = {}
    for d in drivers:
        _sh_eh = driver_hours.get(d.id)
        capped_windows[d.id] = fg.get_effective_window(d.id, configured={
            "start": _sh_eh[0] if _sh_eh else None,
            "end": _sh_eh[1] if _sh_eh else None,
            "max_hours": driver_max_hours.get(d.id),
            "flexible": d.id in flexible_drivers,
        })

    # Shared-car partner map: two WORKING drivers on one physical unit (Day Setup planned
    # AM/PM share or an advisor freed-unit accept). Every engine pass gates inserts against
    # the partner's jobs — the planned windows alone are not airtight (modal End is a
    # last-pickup bound; a 14:50 pickup clears past the partner's 15:05 start).
    sharer_partners = build_sharer_partners(
        {d.id for d in drivers}, target_date, rows=dva_rows)

    schedules = build_driver_schedules(legs, drivers, target_date, dva_rows=dva_rows)

    # Per-driver trip preferences: {driver_id: "prefer_arrival"}
    # Start with driver availability defaults, then apply caller overrides
    driver_preferences = {}
    for d in drivers:
        avail = d.get_availability_for_date(target_date)
        if avail[3]:  # preference
            driver_preferences[d.id] = avail[3]

    for did, pref in (windows.preferences or {}).items():
        driver_preferences[did] = pref

    legs_by_id = {l.id: l for l in legs}
    drivers_by_id = {d.id: d for d in drivers}

    # Get unassigned legs (excluding user-excluded ones)
    excluded_set = set(locked.excluded_leg_ids)
    unassigned = [l for l in legs if not l.driver and l.id not in excluded_set]

    # Separate manually-assigned legs from auto-assign pool
    manual_leg_ids = set(manual_assignments.keys())
    auto_unassigned = [l for l in unassigned if l.id not in manual_leg_ids]

    # Drop unpaid reservations from the auto pool when the dispatcher asked to skip
    # them. Manual assignments are kept (deliberate override).
    if locked.exclude_unpaid:
        auto_unassigned = [
            l for l in auto_unassigned
            if l.reservation and l.reservation.payment_status == 'paid'
        ]

    # ── "Build first" priority seeding ──
    # Drivers the dispatcher marked "Build first" get their FULL day built BEFORE the general
    # assignment — mirrors building a fixed driver's day (e.g. Yovanny) by hand and shuffling the
    # rest around it, so flexible drivers don't out-compete them for legs they could do. Coverage
    # and feasibility are unchanged (build_smart_schedule gates every leg); this only reserves their
    # legs first. Most-constrained (narrowest window) priority driver is seeded first.
    seeded_assignments = {}
    assign_board = schedules   # board the general assigner sees (gets seeded occupancy below)
    _priority_ids = [int(x) for x in raw_build_first if str(x).isdigit() and int(x) in driver_hours
                     and int(x) not in sharer_partners]  # seeding bypasses the shared-car gate
    _priority_ids.sort(key=lambda did: 24 if did in flexible_drivers else (driver_hours[did][1] - driver_hours[did][0]))
    if _priority_ids:
        from dispatching.scheduler import build_smart_schedule
        _pool = list(auto_unassigned)
        for did in _priority_ids:
            sh, eh = driver_hours[did]
            existing = schedules.get(did)
            existing_ids = {s.leg_id for s in existing.slots} if existing else set()
            res = build_smart_schedule(
                driver_id=did, driver_name=str(drivers_by_id[did]),
                available_legs=_pool, target_date=target_date,
                start_hour=sh, end_hour=eh, existing_schedule=existing,
                # Span Governor: Build-1st seeding was the one path with NO span bound
                # (max_hours used to be hardcoded None) — pass the same clamped cap the
                # rest of the pipeline enforces.
                max_hours=(capped_windows.get(did) or {}).get("max_hours"),
                # Build-1st seeding is still the ENGINE choosing legs, so it pays the same
                # turn buffer as the general pass (build_smart_schedule applies this
                # driver's own typed override on top).
                min_buffer=run_min_buffer,
            )
            for s in res.get('schedule', []):
                if s.leg_id not in existing_ids and s.leg_id not in seeded_assignments:
                    seeded_assignments[s.leg_id] = did
            _pool = [l for l in _pool if l.id not in seeded_assignments]
        # Build a SEPARATE board (assign_board) that includes the seeded occupancy so the general
        # pass sees these drivers as busy. Do NOT mutate `schedules` itself: the preview deepcopies
        # `schedules` as the pre-existing board and re-adds final_assignments on top, so seeded legs
        # must live ONLY in final_assignments — else they render twice (the "15 legs" duplication).
        for lid, did in seeded_assignments.items():
            lg = legs_by_id.get(lid)
            if lg is not None:
                lg.driver = drivers_by_id.get(did); lg.driver_id = did
        assign_board = build_driver_schedules(legs, drivers, target_date, dva_rows=dva_rows)
        for lid in seeded_assignments:   # restore: seeded are tracked via final_assignments, not leg.driver
            lg = legs_by_id.get(lid)
            if lg is not None:
                lg.driver = None; lg.driver_id = None
        auto_unassigned = [l for l in auto_unassigned if l.id not in seeded_assignments]

    # ── Rest Advisor: previous day's last drop-off per working driver ──
    # Feeds the overnight-rest deficit penalty (suggest_assignments scorer) AND the rest
    # advisory cards the caller renders. max(end) across ALL of yesterday's legs = the real
    # clear time (a slightly earlier pickup with a longer drive can be the one that clears
    # last). A driver with no legs yesterday is absent from the map => treated as fully rested.
    prev_end_by_driver = {}
    try:
        from reservations.models import Leg
        from dispatching.scheduler import estimate_job_end_time as _est_end
        _prev_day = target_date - timedelta(days=1)
        _wids = set(driver_hours.keys())
        if _wids:
            _prev_legs = (Leg.objects.filter(pickup_date=_prev_day, driver_id__in=_wids)
                          .exclude(status="cancelled")
                          .select_related("reservation", "flight_information"))
            for _pl in _prev_legs:
                try:
                    _end = _est_end(_pl, _prev_day)
                except Exception:
                    continue
                if _end > prev_end_by_driver.get(_pl.driver_id, datetime.min):
                    prev_end_by_driver[_pl.driver_id] = _end
    except Exception:
        prev_end_by_driver = {}

    # Run suggestion engine on remaining unassigned legs
    suggestions = suggest_assignments_clustered(auto_unassigned, assign_board, target_date,
                                                driver_hours=driver_hours or None,
                                                driver_preferences=driver_preferences or None,
                                                flexible_drivers=flexible_drivers or None,
                                                driver_max_hours=driver_max_hours or None,
                                                sharer_partners=sharer_partners or None,
                                                prev_end_by_driver=prev_end_by_driver or None,
                                                min_buffer=run_min_buffer,
                                                driver_min_buffers=driver_min_buffers) if auto_unassigned else []

    # Merge: auto suggestions + manual overrides
    valid_suggestions = [
        s for s in suggestions
        if s.suggested_driver_id and legs_by_id.get(s.leg_id) and drivers_by_id.get(s.suggested_driver_id)
    ]
    # Build final assignment map: {leg_id: driver_id}
    final_assignments = {}
    for s in valid_suggestions:
        final_assignments[s.leg_id] = s.suggested_driver_id
    for lid, did in manual_assignments.items():
        if legs_by_id.get(lid) and drivers_by_id.get(did):
            final_assignments[lid] = did
    # "Build first" seeded legs are part of the final board (and locked from later passes).
    for lid, did in seeded_assignments.items():
        final_assignments[lid] = did

    # Manual + seeded assignments are LOCKED — never relocated by the swap / gap passes.
    locked_ids = set(manual_assignments.keys()) | set(seeded_assignments.keys())

    # ── Auto pre-farm swap pass ──
    # The greedy build is single-leg and can't rearrange, so it farms legs that a cascade of
    # existing assignments could absorb. Before finalizing the farm list, try to recover each
    # would-be-farmed auto leg in-house via find_swaps. Read-only; updates final_assignments
    # (recovered + any moved legs). Manual + build-first assignments are locked (never relocated).
    _span_warnings = []
    _evict_moves = []
    if auto_unassigned:
        from dispatching.scheduler import (
            recover_residuals_via_swaps, rescue_span_blocked_residuals,
            evict_to_farm_for_value,
        )
        _dvtypes = load_all_driver_vtypes(target_date, rows=dva_rows)
        final_assignments, _swap_recovered = recover_residuals_via_swaps(
            final_assignments, [l.id for l in auto_unassigned], legs_by_id,
            drivers, drivers_by_id, target_date, _dvtypes,
            locked_leg_ids=locked_ids,
            driver_windows=capped_windows or None,
            driver_hours=driver_hours or None,
            flexible_drivers=flexible_drivers or None,
            sharer_partners=sharer_partners or None,
        )
        # ── Evict-to-farm value pass (founder brain R1+R2) ──
        # An assigned leg is not sacred: a residual that outvalues an engine-proposed
        # ARRIVAL (a departure, a higher booked class) evicts it to the farm pool and
        # takes the seat — arrivals are the farm-out currency; true departures are never
        # evicted (is_departure parity with the farm-out optimizer). Runs AFTER the swap
        # pass (cheaper cascades first), BEFORE the span rescue (so the rescue re-seats
        # evicted arrivals anywhere they still fit) and BEFORE the trim/gap passes
        # (which polish a settled board). Manual/seeded/pre-existing stay locked; every
        # move re-validates the whole chain through the guards.
        final_assignments, _evict_moves = evict_to_farm_for_value(
            final_assignments, [l.id for l in auto_unassigned], legs_by_id,
            drivers, drivers_by_id, target_date, _dvtypes,
            locked_leg_ids=locked_ids,
            driver_windows=capped_windows or None,
            driver_hours=driver_hours or None,
            flexible_drivers=flexible_drivers or None,
            sharer_partners=sharer_partners or None,
            min_buffer=run_min_buffer, driver_min_buffers=driver_min_buffers,
        )
        if _evict_moves:
            for _mv in _evict_moves:
                logger.info("AUTO-ASSIGN evict pass: %s", _mv["reason"])
        # ── Span-cap coverage rescue ──
        # Priority #1: the duty-span cap may never cost an in-house job. Any residual whose
        # ONLY blocker was the cap is assigned anyway with a loud RED preview warning —
        # except drivers with a dispatcher-TYPED Max hrs (strict; the leg stays residual
        # with a named reason). Runs BEFORE gap compaction so rescued legs can still be healed.
        final_assignments, _span_rescued, _span_warnings = rescue_span_blocked_residuals(
            final_assignments, [l.id for l in auto_unassigned], legs_by_id,
            drivers, drivers_by_id, target_date, _dvtypes,
            capped_windows,
            driver_hours=driver_hours or None,
            flexible_drivers=flexible_drivers or None,
            strict_cap_driver_ids=set(strict_span_caps.keys()),
            locked_leg_ids=locked_ids,
            sharer_partners=sharer_partners or None,
            min_buffer=run_min_buffer, driver_min_buffers=driver_min_buffers,
        )

    # ── Span-trim relocation pass ──
    # Coverage is settled; now actively SHORTEN over-long days: peel a long driver's first or
    # last leg onto a driver with room (the founder's "Roberto just starts later" move). Never
    # farms (keyset asserted unchanged); moved legs are locked against the gap pass below.
    from dispatching.scheduler import trim_spans_via_relocation, compact_gaps_via_relocation
    final_assignments, _trim_moves = trim_spans_via_relocation(
        final_assignments, legs_by_id, drivers, drivers_by_id, target_date,
        load_all_driver_vtypes(target_date, rows=dva_rows),
        locked_leg_ids=locked_ids,
        driver_hours=driver_hours or None,
        flexible_drivers=flexible_drivers or None,
        capped_windows=capped_windows or None,
        sharer_partners=sharer_partners or None,
        min_buffer=run_min_buffer, driver_min_buffers=driver_min_buffers,
    )
    locked_ids = locked_ids | {m["leg_id"] for m in _trim_moves}

    # ── Gap-compaction relocation pass ──
    # Coverage is settled above; now compact for quality. If a driver has a big internal hole
    # and another driver holds a job sitting inside it, relocate that job to fill the hole (the
    # donor just starts later / finishes earlier) — but only when it heals more gap than it
    # opens. Manual assignments stay locked (never relocated). Read-only; updates final_assignments.
    final_assignments, _gap_moves = compact_gaps_via_relocation(
        final_assignments, legs_by_id, drivers, drivers_by_id, target_date,
        load_all_driver_vtypes(target_date, rows=dva_rows),
        locked_leg_ids=locked_ids,
        driver_hours=driver_hours or None,
        flexible_drivers=flexible_drivers or None,
        sharer_partners=sharer_partners or None,
        min_buffer=run_min_buffer, driver_min_buffers=driver_min_buffers,
    )

    # ── Final free-insertion sweep (founder brain) ──
    # The trim/gap relocations above can open seats that did not exist when coverage was
    # settled — never leave a leg farmed that fits the FINAL board as-is (the founder's
    # answer key missed two such insertions on 6/14; the engine must not). No evictions
    # here (free_insert_only) — pure coverage wins, every insert re-runs the guards.
    if auto_unassigned:
        from dispatching.scheduler import evict_to_farm_for_value as _evict_pass
        final_assignments, _final_inserts = _evict_pass(
            final_assignments, [l.id for l in auto_unassigned], legs_by_id,
            drivers, drivers_by_id, target_date,
            load_all_driver_vtypes(target_date, rows=dva_rows),
            locked_leg_ids=locked_ids,
            driver_windows=capped_windows or None,
            driver_hours=driver_hours or None,
            flexible_drivers=flexible_drivers or None,
            sharer_partners=sharer_partners or None,
            free_insert_only=True,
        )
        _evict_moves.extend(_final_inserts)

    return PipelineResult(
        assignments=final_assignments,
        warnings=_span_warnings,
        moves={"evict": _evict_moves, "trim": _trim_moves, "gap": _gap_moves},
        board=schedules,
        seeded=seeded_assignments,
        locked_ids=locked_ids,
        priority_ids=_priority_ids,
        prev_end_by_driver=prev_end_by_driver,
        capped_windows=capped_windows,
        sharer_partners=sharer_partners,
        legs_by_id=legs_by_id,
        drivers_by_id=drivers_by_id,
        unassigned=unassigned,
        auto_unassigned=auto_unassigned,
    )
