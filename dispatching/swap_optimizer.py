"""
Swap Optimizer — Cascading Displacement Search

When suggest_assignments() can't place a leg, this module searches for
rearrangements of existing driver assignments that create room for the
unplaceable leg.  Think of it as a graph search over board states.

Algorithm: Iterative Deepening DFS
  - Depth 1 first (single displacement), then 2, 3, … up to max_depth
  - At each depth, try displacing legs from drivers, cascading as needed
  - Every intermediate state is validated with check_feasibility()
  - Vehicle tier compatibility enforced at every step
  - Configurable time + iteration budget prevents runaway searches
"""

import logging
import time as _time
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Dict, List, Optional, Set, Tuple

from dispatching.scheduler import (
    DriverDaySchedule,
    FeasibilityResult,
    ScheduleSlot,
    check_feasibility,
    estimate_job_end_time,
    get_compatible_vehicle_types,
    get_drive_time,
    get_vehicle_tier,
)

logger = logging.getLogger("dispatching.swap")


# ── Dataclasses ───────────────────────────────────────────────────────

@dataclass
class SwapMove:
    leg_id: int
    leg_pickup_time: str  # display string e.g. "10:30 AM"
    leg_route: str  # "pickup → dropoff" summary
    from_driver_id: Optional[int]
    from_driver_name: Optional[str]
    to_driver_id: int
    to_driver_name: str
    buffer_minutes: int  # buffer on receiving driver after this move


@dataclass
class SwapSolution:
    moves: List[SwapMove]
    target_leg_id: int
    target_driver_id: int
    target_driver_name: str
    target_buffer_minutes: int
    score: int
    depth: int  # number of moves


@dataclass
class DriverAttempt:
    """Diagnostic record for one driver during swap search."""
    driver_id: int
    driver_name: str
    vehicle_type: Optional[str]
    num_jobs: int
    skipped_reason: Optional[str]  # "vehicle_incompatible", "same_driver", None
    direct_feasible: Optional[bool]
    direct_buffer: Optional[int]
    direct_fail_reason: Optional[str]
    displacements_tried: int  # how many legs we tried removing
    displacements_detail: List[dict] = field(default_factory=list)  # [{leg_id, pickup_time, buffer_after_removal, rehomed: bool}]


@dataclass
class SwapSearchResult:
    solutions: List[SwapSolution]
    target_leg_id: int
    states_explored: int
    time_ms: int
    hit_time_limit: bool
    hit_depth_limit: bool
    diagnostic: List[DriverAttempt] = field(default_factory=list)


# ── Helpers ───────────────────────────────────────────────────────────

def _get_leg_vtype(leg) -> Optional[str]:
    """Get vehicle type string for a leg from its reservation."""
    vtype = getattr(
        getattr(getattr(leg, "reservation", None), "vehicle", None),
        "vehicle_type",
        None,
    )
    return str(vtype) if vtype else None


def _vehicle_compatible(driver_vtype: Optional[str], leg_vtype: Optional[str]) -> bool:
    """Check if a driver's vehicle can handle a leg's required vehicle type."""
    if not leg_vtype:
        return True
    compatible = get_compatible_vehicle_types(driver_vtype or "")
    return leg_vtype in compatible


def _leg_to_slot(leg, target_date: date) -> ScheduleSlot:
    """Convert a Leg model object to a ScheduleSlot for schedule simulation."""
    from dispatching.analytics import categorize_location

    pickup_cat = categorize_location(leg.pickup_location)
    dropoff_cat = categorize_location(leg.dropoff_location)
    end_time = estimate_job_end_time(leg, target_date)

    customer_name = ""
    if leg.reservation and leg.reservation.customer:
        customer_name = leg.reservation.customer.get_full_name()

    has_flight = False
    try:
        has_flight = bool(getattr(leg, "flight_information_id", None))
    except Exception:
        pass

    leg_vtype = _get_leg_vtype(leg)

    return ScheduleSlot(
        leg_id=leg.id,
        pickup_time=leg.pickup_time,
        pickup_location=leg.pickup_location,
        pickup_category=pickup_cat,
        dropoff_location=leg.dropoff_location,
        dropoff_category=dropoff_cat,
        trip_type=leg.get_trip_type(),
        estimated_end_time=end_time,
        reservation_id=leg.reservation_id,
        customer_name=customer_name,
        status=leg.status or "in-progress",
        has_flight=has_flight,
        revenue=leg.revenue_share,
        vehicle_type=str(leg_vtype) if leg_vtype else None,
    )


def _build_modified_schedule(
    schedule: DriverDaySchedule,
    remove_leg_ids: Set[int] = None,
    add_leg=None,
    target_date: date = None,
) -> DriverDaySchedule:
    """Create a copy of a schedule with specified modifications."""
    new_slots = [s for s in schedule.slots if s.leg_id not in (remove_leg_ids or set())]

    if add_leg and target_date:
        new_slots.append(_leg_to_slot(add_leg, target_date))

    new_slots.sort(key=lambda s: s.pickup_time)

    return DriverDaySchedule(
        driver_id=schedule.driver_id,
        driver_name=schedule.driver_name,
        driver_type=schedule.driver_type,
        slots=new_slots,
    )


def _get_conflicting_slots(
    schedule: DriverDaySchedule,
    candidate_leg,
    target_date: date,
    inter_job_buffer: int = None,
) -> List[Tuple[ScheduleSlot, int]]:
    """
    Find slots that, if individually removed, would make candidate_leg feasible.

    Returns list of (slot, buffer_minutes) sorted by buffer descending
    (highest-buffer removals are easiest to relocate).
    """
    results = []
    for slot in schedule.slots:
        modified = _build_modified_schedule(schedule, remove_leg_ids={slot.leg_id})
        result = check_feasibility(modified, candidate_leg, target_date, inter_job_buffer)
        if result.feasible:
            results.append((slot, result.buffer_minutes))
    results.sort(key=lambda x: x[1], reverse=True)
    return results


def _budget_exceeded(iterations: int, start_time: float, max_iterations: int, time_limit_ms: int) -> bool:
    if iterations >= max_iterations:
        return True
    elapsed_ms = (_time.time() - start_time) * 1000
    return elapsed_ms >= time_limit_ms


def _score_solution(
    moves: List[SwapMove],
    target_leg,
    driver_vtypes: Dict[int, str],
    all_legs_by_id: dict,
    cfg,
) -> int:
    """Score a swap solution. Higher is better."""
    depth = len(moves)
    score = 1000 - (depth * cfg.swap_depth_penalty)

    # Min buffer across all moves
    min_buffer = min(m.buffer_minutes for m in moves) if moves else 0
    score += min_buffer * cfg.swap_buffer_weight

    # Revenue bonus (normalized)
    revenue = float(target_leg.revenue_share or 0)
    rev_divisor = max(cfg.revenue_divisor, 1)
    normalized_rev = min(revenue / rev_divisor, cfg.revenue_cap)
    score += int(normalized_rev * cfg.swap_revenue_weight / 10)

    # Tier bonus for exact matches
    for move in moves:
        driver_vtype = driver_vtypes.get(move.to_driver_id)
        if not driver_vtype:
            continue
        moved_leg = all_legs_by_id.get(move.leg_id)
        if not moved_leg:
            continue
        leg_vtype = _get_leg_vtype(moved_leg)
        if leg_vtype and leg_vtype == driver_vtype:
            score += cfg.swap_tier_bonus

    return score


# ── Core Search ──────────────────────────────────────────────────────

def find_swaps(
    target_leg,
    inhouse_schedules: Dict[int, DriverDaySchedule],
    all_legs_by_id: Dict[int, object],
    driver_vtypes: Dict[int, str],
    target_date: date,
    max_depth: int = 5,
    time_limit_ms: int = 5000,
    max_iterations: int = 5000,
) -> SwapSearchResult:
    """
    Search for swap chains that make room for target_leg.

    Parameters
    ----------
    target_leg : Leg model instance (unassigned or to be reassigned)
    inhouse_schedules : current driver schedules (from build_driver_schedules)
    all_legs_by_id : {leg_id: Leg} for all assigned legs on the date
    driver_vtypes : {driver_id: vehicle_type_str} from load_all_driver_vtypes
    target_date : the scheduling date
    max_depth, time_limit_ms, max_iterations : search budget

    Returns
    -------
    SwapSearchResult with scored solutions
    """
    from dispatching.models import SchedulerSettings

    cfg = SchedulerSettings.get_settings()

    # Override defaults with settings if caller used defaults
    if max_depth == 5:
        max_depth = cfg.swap_max_depth
    if time_limit_ms == 5000:
        time_limit_ms = cfg.swap_time_limit_ms
    if max_iterations == 5000:
        max_iterations = cfg.swap_max_iterations

    solutions: List[SwapSolution] = []
    iterations = [0]  # mutable counter for recursion
    start = _time.time()

    target_vtype = _get_leg_vtype(target_leg)
    hit_time = False
    hit_depth = False

    # Only consider in-house drivers
    inhouse_driver_ids = [
        did for did, sched in inhouse_schedules.items()
        if sched.driver_type == "inhouse"
    ]

    for depth_limit in range(1, max_depth + 1):
        if _budget_exceeded(iterations[0], start, max_iterations, time_limit_ms):
            hit_time = (_time.time() - start) * 1000 >= time_limit_ms
            break

        _search(
            leg_to_place=target_leg,
            schedules=inhouse_schedules,
            moves_so_far=[],
            visited=set(),  # (leg_id, driver_id) pairs — cycle detection
            depth_remaining=depth_limit,
            target_leg=target_leg,
            target_vtype=target_vtype,
            all_legs_by_id=all_legs_by_id,
            driver_vtypes=driver_vtypes,
            inhouse_driver_ids=inhouse_driver_ids,
            target_date=target_date,
            cfg=cfg,
            solutions=solutions,
            iterations=iterations,
            start=start,
            max_iterations=max_iterations,
            time_limit_ms=time_limit_ms,
        )

        if solutions:
            break  # found solutions at this depth, prefer shallower

        if depth_limit == max_depth:
            hit_depth = True

    elapsed_ms = int((_time.time() - start) * 1000)
    if not solutions and (_time.time() - start) * 1000 >= time_limit_ms:
        hit_time = True

    # Score and sort solutions
    for sol in solutions:
        sol.score = _score_solution(
            sol.moves, target_leg, driver_vtypes, all_legs_by_id, cfg
        )

    solutions.sort(key=lambda s: s.score, reverse=True)

    logger.info(
        "Swap search for leg %d: %d solutions, %d states, %dms (time_limit=%s, depth_limit=%s)",
        target_leg.id,
        len(solutions),
        iterations[0],
        elapsed_ms,
        hit_time,
        hit_depth,
    )

    # Build diagnostic report when no solutions found
    diagnostic: List[DriverAttempt] = []
    if not solutions:
        diagnostic = _build_diagnostic(
            target_leg, target_vtype, inhouse_schedules,
            inhouse_driver_ids, driver_vtypes, all_legs_by_id, target_date, cfg,
        )

    return SwapSearchResult(
        solutions=solutions[:10],  # top 10
        target_leg_id=target_leg.id,
        states_explored=iterations[0],
        time_ms=elapsed_ms,
        hit_time_limit=hit_time,
        hit_depth_limit=hit_depth,
        diagnostic=diagnostic,
    )


def _search(
    leg_to_place,
    schedules: Dict[int, DriverDaySchedule],
    moves_so_far: List[SwapMove],
    visited: Set[Tuple[int, int]],
    depth_remaining: int,
    target_leg,
    target_vtype: Optional[str],
    all_legs_by_id: Dict[int, object],
    driver_vtypes: Dict[int, str],
    inhouse_driver_ids: List[int],
    target_date: date,
    cfg,
    solutions: List[SwapSolution],
    iterations: list,
    start: float,
    max_iterations: int,
    time_limit_ms: int,
):
    """Recursive DFS: try to place leg_to_place on any compatible driver."""
    if _budget_exceeded(iterations[0], start, max_iterations, time_limit_ms):
        return

    leg_vtype = _get_leg_vtype(leg_to_place)
    current_driver_id = getattr(leg_to_place, "driver_id", None)

    # Sort drivers: fewer legs first (more room), exact vehicle match preferred
    def _driver_sort_key(did):
        sched = schedules.get(did)
        slot_count = len(sched.slots) if sched else 99
        dvtype = driver_vtypes.get(did, "")
        exact = 0 if (leg_vtype and dvtype == leg_vtype) else 1
        return (exact, slot_count)

    sorted_drivers = sorted(inhouse_driver_ids, key=_driver_sort_key)

    for driver_id in sorted_drivers:
        if _budget_exceeded(iterations[0], start, max_iterations, time_limit_ms):
            return

        iterations[0] += 1

        # Skip: leg already on this driver (no-op)
        if current_driver_id == driver_id:
            continue

        # Skip: cycle detection
        if (leg_to_place.id, driver_id) in visited:
            continue

        # Skip: vehicle incompatible
        if not _vehicle_compatible(driver_vtypes.get(driver_id), leg_vtype):
            continue

        schedule = schedules.get(driver_id)
        if schedule is None:
            continue

        # ── Try direct placement ──────────────────────────────────
        feasibility = check_feasibility(schedule, leg_to_place, target_date, cfg.inter_job_buffer)

        if feasibility.feasible:
            pickup_str = leg_to_place.pickup_time.strftime("%I:%M %p").lstrip("0") if hasattr(leg_to_place.pickup_time, "strftime") else str(leg_to_place.pickup_time)
            move = SwapMove(
                leg_id=leg_to_place.id,
                leg_pickup_time=pickup_str,
                leg_route=f"{leg_to_place.pickup_location[:30]} → {leg_to_place.dropoff_location[:30]}",
                from_driver_id=current_driver_id,
                from_driver_name=_driver_name_for_id(current_driver_id, schedules),
                to_driver_id=driver_id,
                to_driver_name=schedule.driver_name,
                buffer_minutes=feasibility.buffer_minutes,
            )
            # Moves are in discovery order (reverse of execution).
            # Reverse them for the final solution so execution order is correct.
            all_moves = list(reversed(moves_so_far + [move]))
            # Target leg placement is now the LAST move (after reversal)
            target_move = next(m for m in all_moves if m.leg_id == target_leg.id)

            solution = SwapSolution(
                moves=all_moves,
                target_leg_id=target_leg.id,
                target_driver_id=target_move.to_driver_id,
                target_driver_name=target_move.to_driver_name,
                target_buffer_minutes=target_move.buffer_minutes,
                score=0,  # scored later
                depth=len(all_moves),
            )
            solutions.append(solution)
            # Don't return — keep searching for more solutions at this depth
            # but cap per-depth solutions to avoid explosion
            if len(solutions) >= 20:
                return
            continue

        # ── Try displacement ──────────────────────────────────────
        if depth_remaining <= 0:
            continue

        conflicting = _get_conflicting_slots(schedule, leg_to_place, target_date, cfg.inter_job_buffer)

        for slot, buffer_after_removal in conflicting:
            if _budget_exceeded(iterations[0], start, max_iterations, time_limit_ms):
                return

            displaced_leg = all_legs_by_id.get(slot.leg_id)
            if not displaced_leg:
                continue

            # Build modified schedules:
            #   - Remove displaced leg from this driver
            #   - Add leg_to_place to this driver
            modified_schedule = _build_modified_schedule(
                schedule,
                remove_leg_ids={slot.leg_id},
                add_leg=leg_to_place,
                target_date=target_date,
            )

            new_schedules = dict(schedules)
            new_schedules[driver_id] = modified_schedule

            pickup_str = leg_to_place.pickup_time.strftime("%I:%M %p").lstrip("0") if hasattr(leg_to_place.pickup_time, "strftime") else str(leg_to_place.pickup_time)
            place_move = SwapMove(
                leg_id=leg_to_place.id,
                leg_pickup_time=pickup_str,
                leg_route=f"{leg_to_place.pickup_location[:30]} → {leg_to_place.dropoff_location[:30]}",
                from_driver_id=current_driver_id,
                from_driver_name=_driver_name_for_id(current_driver_id, schedules),
                to_driver_id=driver_id,
                to_driver_name=schedule.driver_name,
                buffer_minutes=buffer_after_removal,
            )

            new_visited = visited | {
                (leg_to_place.id, driver_id),
                (slot.leg_id, driver_id),  # don't move displaced leg back
            }

            _search(
                leg_to_place=displaced_leg,
                schedules=new_schedules,
                moves_so_far=moves_so_far + [place_move],
                visited=new_visited,
                depth_remaining=depth_remaining - 1,
                target_leg=target_leg,
                target_vtype=target_vtype,
                all_legs_by_id=all_legs_by_id,
                driver_vtypes=driver_vtypes,
                inhouse_driver_ids=inhouse_driver_ids,
                target_date=target_date,
                cfg=cfg,
                solutions=solutions,
                iterations=iterations,
                start=start,
                max_iterations=max_iterations,
                time_limit_ms=time_limit_ms,
            )

            if len(solutions) >= 20:
                return


def _build_diagnostic(
    target_leg,
    target_vtype: Optional[str],
    inhouse_schedules: Dict[int, DriverDaySchedule],
    inhouse_driver_ids: List[int],
    driver_vtypes: Dict[int, str],
    all_legs_by_id: Dict[int, object],
    target_date: date,
    cfg,
) -> List[DriverAttempt]:
    """Build a per-driver diagnostic report showing why no swap was found."""
    report = []
    current_driver_id = getattr(target_leg, "driver_id", None)

    for driver_id in inhouse_driver_ids:
        schedule = inhouse_schedules.get(driver_id)
        if schedule is None:
            continue

        dvtype = driver_vtypes.get(driver_id)
        attempt = DriverAttempt(
            driver_id=driver_id,
            driver_name=schedule.driver_name,
            vehicle_type=dvtype,
            num_jobs=len(schedule.slots),
            skipped_reason=None,
            direct_feasible=None,
            direct_buffer=None,
            direct_fail_reason=None,
            displacements_tried=0,
        )

        # Same driver skip
        if current_driver_id == driver_id:
            attempt.skipped_reason = "same_driver"
            report.append(attempt)
            continue

        # Vehicle compatibility
        if not _vehicle_compatible(dvtype, target_vtype):
            attempt.skipped_reason = "vehicle_incompatible"
            report.append(attempt)
            continue

        # Direct placement
        feas = check_feasibility(schedule, target_leg, target_date, cfg.inter_job_buffer)
        attempt.direct_feasible = feas.feasible
        attempt.direct_buffer = feas.buffer_minutes
        attempt.direct_fail_reason = feas.reason if not feas.feasible else None

        if not feas.feasible:
            # Try displacement for each slot
            for slot in schedule.slots:
                modified = _build_modified_schedule(schedule, remove_leg_ids={slot.leg_id})
                mod_feas = check_feasibility(modified, target_leg, target_date, cfg.inter_job_buffer)
                if mod_feas.feasible:
                    # Could place target here if we remove this leg — can we rehome it?
                    displaced_leg = all_legs_by_id.get(slot.leg_id)
                    rehomed = False
                    if displaced_leg:
                        displaced_vtype = _get_leg_vtype(displaced_leg)
                        for other_did in inhouse_driver_ids:
                            if other_did == driver_id or other_did == current_driver_id:
                                continue
                            if not _vehicle_compatible(driver_vtypes.get(other_did), displaced_vtype):
                                continue
                            other_sched = inhouse_schedules.get(other_did)
                            if other_sched is None:
                                continue
                            other_feas = check_feasibility(other_sched, displaced_leg, target_date, cfg.inter_job_buffer)
                            if other_feas.feasible:
                                rehomed = True
                                break

                    pickup_str = slot.pickup_time.strftime("%I:%M %p").lstrip("0") if hasattr(slot.pickup_time, "strftime") else str(slot.pickup_time)
                    attempt.displacements_detail.append({
                        "leg_id": slot.leg_id,
                        "pickup_time": pickup_str,
                        "route": f"{slot.pickup_location[:25]} → {slot.dropoff_location[:25]}",
                        "buffer_after_removal": mod_feas.buffer_minutes,
                        "rehomed": rehomed,
                    })
                attempt.displacements_tried += 1

        report.append(attempt)

    return report


def _driver_name_for_id(
    driver_id: Optional[int], schedules: Dict[int, DriverDaySchedule]
) -> Optional[str]:
    if driver_id is None:
        return None
    sched = schedules.get(driver_id)
    return sched.driver_name if sched else None
