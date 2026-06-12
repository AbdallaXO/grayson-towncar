"""Apply Farm-Out Optimizer plans — the WRITE side of the farm-out page.

The optimizer engine (``farmout_optimizer.py``) stays strictly read-only; this module is the
separate, deliberate write path behind the page's Apply / Farm buttons. Each recommendation
carries a ready-to-POST plan (ids only — never client dollars); this module re-validates the
CURRENT database state before touching anything:

  * STALENESS — the plan carries ``expected`` ({leg_id: driver_id-or-None} as of analysis time).
    Any drift (someone reassigned a touched leg since the page rendered) => 409, nothing written.
  * HARD RULES — VIP legs and TRUE departures are never farmed (same definitions as the engine:
    ``resolve_protected_vip_leg_ids`` / ``is_departure``); cancelled legs are never touched.
  * AFFILIATE — the chosen affiliate (engine suggestion OR founder override) must pass the SAME
    capability/permit/rate gates the engine used (``_gate_affiliate``) and have REAL remaining
    capacity that day, measured against the legs ACTUALLY assigned to them in the DB — not the
    replay ledger (at apply time the truth exists; use it).
  * IN-HOUSE FEASIBILITY — live mode re-validates every receiving driver's full resulting day
    (Guard B turnaround + Guard C window via ``check_feasibility``, mirroring
    ``views._revalidate_swap_feasibility``) plus vehicle-class compatibility, INSIDE the
    transaction, before writing.

All writes go through ``dispatching.assignment.set_leg_driver`` (THE front door), so held-day
sandbox routing (staged vs live), pay auto-fill from the affiliate's real DriverPayRate card,
status reset, LegStatus audit and ops-task auto-close behave exactly like a dispatch-board
assignment. On a held day with a granted user the whole plan stages into the draft overlay and
the response says so (``held: true``) — the live board is unchanged until publish, same contract
as ``execute_swap``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

_KEEP_AND_FARM = ("opportunity_swap", "policy_departure_rescue")
ALL_KINDS = ("free_rescue", "keep_unassign", "farm_direct") + _KEEP_AND_FARM

# Roles a write can play (drives validation + the human confirmation message).
_KEEP, _MOVE, _FARM, _UNASSIGN = "keep", "move", "farm", "unassign"


class PlanRejected(Exception):
    """Validation failure -> (http_status, human error). Nothing is ever written."""

    def __init__(self, status: int, error: str):
        self.status = status
        self.error = error
        super().__init__(error)


# ── Page-cache versioning ───────────────────────────────────────────────────────────────────
# The page context is cached per (date, min_savings); min_savings varies so the keys can't be
# enumerated for a plain cache.delete. A per-date version token in the key lets one bump
# invalidate every threshold's entry at once, so an Apply is visible on the next render.

def farmout_page_cache_version(day: date) -> int:
    return cache.get(f"farmout_ver_{day.isoformat()}") or 0


def bump_farmout_page_cache(day: date) -> None:
    key = f"farmout_ver_{day.isoformat()}"
    try:
        cache.incr(key)
    except ValueError:  # key expired / never set
        cache.set(key, 1, 86400)


# ── Plan parsing ────────────────────────────────────────────────────────────────────────────
@dataclass
class _Plan:
    kind: str
    day: date
    target_leg_id: int
    writes: List[Tuple[int, Optional[int], str]]  # (leg_id, new_driver_id|None, role)
    expected: Dict[int, Optional[int]]            # leg_id -> driver_id at analysis time
    keep_driver_id: Optional[int] = None
    farm_leg_id: Optional[int] = None
    farm_affiliate_id: Optional[int] = None
    moves: List[Tuple[int, int]] = field(default_factory=list)
    confirm_pullback: bool = False                # explicit opt-in to un-farm a committed leg


def _as_int(v, name: str, *, allow_none: bool = False) -> Optional[int]:
    if v is None:
        if allow_none:
            return None
        raise PlanRejected(400, f"Missing {name}")
    try:
        return int(v)
    except (TypeError, ValueError):
        raise PlanRejected(400, f"Invalid {name}")


def parse_plan(data: dict) -> _Plan:
    kind = data.get("kind")
    if kind not in ALL_KINDS:
        raise PlanRejected(400, f"Unknown plan kind {kind!r}")
    try:
        day = datetime.strptime(data.get("date") or "", "%Y-%m-%d").date()
    except ValueError:
        raise PlanRejected(400, "Invalid or missing date")
    if day < timezone.localdate():
        raise PlanRejected(400, "That service date is in the past — past days are graded, "
                                "never re-dispatched")
    target_leg_id = _as_int(data.get("target_leg_id"), "target_leg_id")

    writes: List[Tuple[int, Optional[int], str]] = []
    moves: List[Tuple[int, int]] = []
    keep_driver_id = farm_leg_id = farm_affiliate_id = None

    if kind == "free_rescue":
        keep_driver_id = _as_int(data.get("keep_driver_id"), "keep_driver_id")
        raw_moves = data.get("moves") or []
        if not isinstance(raw_moves, list) or not raw_moves:
            raise PlanRejected(400, "free_rescue plan has no moves")
        for mv in raw_moves:
            if not isinstance(mv, (list, tuple)) or len(mv) != 2:
                raise PlanRejected(400, "Each move must be [leg_id, to_driver_id]")
            leg_id = _as_int(mv[0], "move leg_id")
            to_did = _as_int(mv[1], "move to_driver_id")
            moves.append((leg_id, to_did))
            writes.append((leg_id, to_did, _KEEP if leg_id == target_leg_id else _MOVE))
        if target_leg_id not in (m[0] for m in moves):
            raise PlanRejected(400, "free_rescue moves must include the target leg")
    elif kind in _KEEP_AND_FARM:
        keep_driver_id = _as_int(data.get("keep_driver_id"), "keep_driver_id")
        farm_leg_id = _as_int(data.get("farm_leg_id"), "farm_leg_id")
        farm_affiliate_id = _as_int(
            data.get("farm_affiliate_id", data.get("suggested_affiliate_id")),
            "farm_affiliate_id")
        if farm_leg_id == target_leg_id:
            raise PlanRejected(400, "farm_leg_id cannot be the target leg")
        writes.append((target_leg_id, keep_driver_id, _KEEP))
        writes.append((farm_leg_id, farm_affiliate_id, _FARM))
    elif kind == "keep_unassign":
        keep_driver_id = _as_int(data.get("keep_driver_id"), "keep_driver_id")
        farm_leg_id = _as_int(data.get("farm_leg_id"), "farm_leg_id")
        if farm_leg_id == target_leg_id:
            raise PlanRejected(400, "farm_leg_id cannot be the target leg")
        writes.append((target_leg_id, keep_driver_id, _KEEP))
        writes.append((farm_leg_id, None, _UNASSIGN))
    else:  # farm_direct
        farm_affiliate_id = _as_int(data.get("farm_affiliate_id"), "farm_affiliate_id")
        writes.append((target_leg_id, farm_affiliate_id, _FARM))

    leg_ids = [w[0] for w in writes]
    if len(set(leg_ids)) != len(leg_ids):
        raise PlanRejected(400, "Plan touches the same leg twice")

    raw_expected = data.get("expected")
    if not isinstance(raw_expected, dict):
        raise PlanRejected(400, "Missing expected assignment map")
    expected: Dict[int, Optional[int]] = {}
    for k, v in raw_expected.items():
        expected[_as_int(k, "expected leg id")] = _as_int(v, "expected driver id",
                                                          allow_none=True)
    for leg_id in leg_ids:
        if leg_id not in expected:
            raise PlanRejected(400, f"No expected assignment for leg {leg_id}")

    return _Plan(kind=kind, day=day, target_leg_id=target_leg_id, writes=writes,
                 expected=expected, keep_driver_id=keep_driver_id, farm_leg_id=farm_leg_id,
                 farm_affiliate_id=farm_affiliate_id, moves=moves,
                 confirm_pullback=bool(data.get("confirm_pullback")))


# ── Validation (all reads; raise PlanRejected, never write) ─────────────────────────────────
def _load_touched_legs(plan: _Plan) -> dict:
    """The plan's legs with every relation validation needs (incl. the VIP agency chain)."""
    from reservations.models import Leg

    leg_ids = [w[0] for w in plan.writes]
    legs = {l.id: l for l in Leg.objects.filter(id__in=leg_ids).select_related(
        "driver", "route", "reservation", "reservation__customer", "reservation__vehicle",
        "vehicle", "flight_information", "reservation__travel_agent",
        "reservation__travel_agent__agency")}
    for leg_id in leg_ids:
        if leg_id not in legs:
            raise PlanRejected(404, f"Leg {leg_id} not found")
    for l in legs.values():
        if l.pickup_date != plan.day:
            raise PlanRejected(409, f"Leg {l.id} is no longer on {plan.day} — re-run Analyze")
        if l.status == "cancelled" or (l.reservation_id and l.reservation.status == "cancelled"):
            raise PlanRejected(409, f"Leg {l.id} was cancelled — re-run Analyze")
        if l.status == "completed":
            raise PlanRejected(409, f"Leg {l.id} is already completed — re-run Analyze")
    return legs


def _check_expected(plan: _Plan, legs: dict, draft=None) -> None:
    """Staleness guard. On a HELD day the truth the dispatcher is editing is the draft OVERLAY
    (DraftAssignment row if present, live Leg.driver otherwise) — validating against it lets
    in-order Apply-all chains work while staging, exactly as they do live (each staged write
    updates the overlay the next plan's ``expected`` assumes)."""
    overlay: Dict[int, Optional[int]] = {}
    if draft is not None:
        from reservations.models import DraftAssignment
        overlay = {da.leg_id: da.proposed_driver_id
                   for da in DraftAssignment.objects.filter(draft=draft,
                                                            leg_id__in=list(plan.expected))}
    for leg_id, exp_did in plan.expected.items():
        l = legs.get(leg_id)
        if l is None:
            continue  # only plan legs are fetched; extra expected entries are harmless
        effective = overlay[leg_id] if leg_id in overlay else (l.driver_id or None)
        if effective != exp_did:
            cur = f"driver {effective}" if effective else "unassigned"
            where = " in the draft" if leg_id in overlay else ""
            raise PlanRejected(409, f"Schedule changed since this was computed (leg {leg_id} is "
                                    f"now {cur}{where}) — re-run Analyze")


def _check_hard_rules(plan: _Plan, legs: dict) -> None:
    """VIP legs are never farmed AND never displaced (farmed/moved/unassigned); TRUE departures
    are never farmed — same definitions as the engine. Pulling a committed farm-out back
    in-house requires the plan's explicit ``confirm_pullback`` opt-in (the page asks first)."""
    from dispatching.farmout_optimizer import is_departure, resolve_protected_vip_leg_ids

    vip_ids = resolve_protected_vip_leg_ids(list(legs.values()))
    for leg_id, _did, role in plan.writes:
        if role == _FARM:
            if leg_id in vip_ids:
                raise PlanRejected(400, f"Leg {leg_id} is VIP — never farmed")
            if is_departure(legs[leg_id]):
                raise PlanRejected(400, f"Leg {leg_id} is a departure — policy keeps "
                                        f"departures in-house, never farmed")
        elif role == _UNASSIGN and leg_id in vip_ids:
            raise PlanRejected(400, f"Leg {leg_id} is VIP — never displaced")
        elif role in (_KEEP, _MOVE) and leg_id != plan.target_leg_id and leg_id in vip_ids:
            raise PlanRejected(400, f"Leg {leg_id} is VIP — never moved by a swap plan")
        if role in (_KEEP, _MOVE):
            cur = legs[leg_id].driver
            if (cur is not None and cur.driver_type == "affiliate"
                    and not plan.confirm_pullback):
                raise PlanRejected(400, f"Leg {leg_id} is committed to {cur} — pulling it back "
                                        f"in-house needs explicit confirmation "
                                        f"(confirm_pullback)")


def _check_inhouse_receivers(plan: _Plan) -> dict:
    """Every keep/move destination must be an ACTIVE IN-HOUSE driver. Returns {id: Driver}."""
    from drivers.models import Driver

    dids = {did for _l, did, role in plan.writes if role in (_KEEP, _MOVE) and did is not None}
    if not dids:
        return {}
    found = {d.id: d for d in Driver.objects.filter(id__in=dids, driver_type="inhouse",
                                                    is_active=True)}
    missing = dids - set(found)
    if missing:
        raise PlanRejected(400, f"Driver {sorted(missing)[0]} is not an active in-house driver")
    return found


def _check_affiliate(plan: _Plan, legs: dict):
    """The chosen affiliate must pass the engine's capability/permit/rate gates AND have real
    remaining capacity that day (counted/chained against their ACTUAL assigned legs). Returns
    the affiliate Driver, or None when the plan farms nothing."""
    from reservations.models import Leg
    from dispatching.farmout_optimizer import (
        ANTHONY_MAX_LEGS_PER_DAY, _CAP_COUNT, _CAP_SINGLE_CHAIN, _gate_affiliate,
        _leg_pricing_ctx, resolve_affiliate_roster)
    from dispatching.scheduler import build_driver_schedules, check_feasibility

    farm_writes = [(l, did) for l, did, role in plan.writes if role == _FARM]
    if not farm_writes:
        return None
    farm_leg_id, aff_id = farm_writes[0]

    roster, _warn, _flat = resolve_affiliate_roster()
    pair = next(((d, p) for d, p in roster if d.id == aff_id), None)
    if pair is None:
        raise PlanRejected(400, "That affiliate has no usable rate card (not rate-ready) — "
                                "add DriverPayRate rows first")
    drv, prof = pair
    leg = legs[farm_leg_id]
    base, reason = _gate_affiliate(_leg_pricing_ctx(leg), drv, prof)
    if base is None:
        human = {"vehicle_tier": f"{drv} can't serve this vehicle class",
                 "van_unproven": f"{drv} has no van-class rate on file — add a van rate row "
                                 f"or set max vehicle on their affiliate profile",
                 "port_pickup_permit": f"{drv} can't pick up at Port Canaveral / Sanford "
                                       f"(no permit)",
                 "no_rate": f"{drv} has no rate for this route/vehicle"}
        raise PlanRejected(400, human.get(reason, f"{drv} is not eligible for this leg"))

    # REAL remaining capacity: the affiliate's actual day in the DB (excluding this leg if it
    # already sits with them — a re-farm to the same affiliate is a no-op the expected-guard
    # would normally catch anyway).
    mode = prof.capacity_mode if prof else _CAP_SINGLE_CHAIN
    aff_legs = list(Leg.objects.filter(pickup_date=plan.day, driver=drv)
                    .exclude(status="cancelled").exclude(reservation__status="cancelled")
                    .exclude(id=leg.id)
                    .select_related("reservation", "reservation__vehicle", "vehicle",
                                    "flight_information"))
    if mode == _CAP_SINGLE_CHAIN:
        sched = build_driver_schedules(aff_legs, [drv], plan.day).get(drv.id)
        feas = check_feasibility(sched, leg, plan.day)
        if not feas.feasible:
            raise PlanRejected(409, f"{drv} can't fit this job into their day "
                                    f"({feas.reason}) — pick another affiliate")
    else:
        cap = prof.daily_cap if (prof and prof.daily_cap is not None) else (
            ANTHONY_MAX_LEGS_PER_DAY if mode == _CAP_COUNT else None)
        if cap is not None and len(aff_legs) >= cap:
            raise PlanRejected(409, f"{drv} is at their daily cap ({cap} jobs) — "
                                    f"pick another affiliate")
    return drv


def _revalidate_inhouse(plan: _Plan, inhouse: dict) -> Tuple[bool, str]:
    """Re-run the FULL feasibility check (Guard B turnaround + Guard C window) on the board that
    WOULD result from this plan — mirror of ``views._revalidate_swap_feasibility``, extended to
    REMOVE legs that leave the in-house board (farmed / unassigned). Read-only: mutates only
    in-memory copies. Only drivers that GAIN a leg need checking."""
    from reservations.models import Leg
    from dispatching import feasibility_guards as fg
    from dispatching.scheduler import (build_driver_schedules, check_feasibility,
                                       estimate_job_end_time, get_compatible_vehicle_types,
                                       load_all_driver_vtypes)

    new_driver_by_leg = {leg_id: did for leg_id, did, _role in plan.writes}
    receiving = {did for leg_id, did, role in plan.writes
                 if role in (_KEEP, _MOVE) and did is not None}

    legs = list(Leg.objects.filter(pickup_date=plan.day)
                .exclude(reservation__status="cancelled").exclude(status="cancelled")
                .select_related("driver", "reservation", "reservation__vehicle", "vehicle",
                                "flight_information"))
    legs_by_id = {l.id: l for l in legs}
    for leg_id in new_driver_by_leg:
        if leg_id not in legs_by_id:
            return False, f"leg {leg_id} not found on {plan.day}"

    # Apply the plan in memory: keeps/moves land on their in-house receiver; farmed/unassigned
    # legs leave the in-house board entirely.
    for leg_id, did in new_driver_by_leg.items():
        l = legs_by_id[leg_id]
        l.driver = inhouse.get(did)
        l.driver_id = did if did in inhouse else None
    for l in legs:
        l._estimated_end_dt = estimate_job_end_time(l, plan.day)

    # Vehicle-class compatibility for every gaining placement (check_feasibility has no vehicle
    # gate — this mirrors the engine's compat_inhouse filter against today's vehicle plan).
    dvtypes = load_all_driver_vtypes(plan.day)
    for leg_id, did, role in plan.writes:
        if role not in (_KEEP, _MOVE) or did is None:
            continue
        dv = dvtypes.get(did)
        if dv is None:
            return False, f"{inhouse[did]} has no vehicle assigned on {plan.day}"
        vt = legs_by_id[leg_id].effective_vehicle_type or ""
        if vt and vt not in get_compatible_vehicle_types(dv):
            return False, (f"leg {leg_id} needs a {vt}; {inhouse[did]}'s {dv} can't serve it")

    def _cfg_window(d):
        eff = d.get_effective_availability(plan.day)
        mh = eff.get("max_hours")
        return {"start": eff.get("start_hour"), "end": eff.get("end_hour"),
                "max_hours": (float(mh) if mh else None), "flexible": bool(eff.get("flexible"))}

    for did in receiving:
        drv = inhouse[did]
        drv_legs = [l for l in legs if l.driver_id == did]
        # enforce_cap=False: this validates a plan the FOUNDER explicitly chose — the duty-span
        # cap must never hard-block an intentional manual move (same stance as execute_swap).
        window = fg.get_effective_window(did, configured=_cfg_window(drv), enforce_cap=False)
        for L in drv_legs:
            others = [l for l in drv_legs if l.id != L.id]
            sched = build_driver_schedules(others, [drv], plan.day).get(did)
            feas = check_feasibility(sched, L, plan.day, driver_window=window)
            if not feas.feasible:
                return False, f"leg {L.id} on {drv} would be infeasible: {feas.reason}"
    return True, ""


# ── Entry point ─────────────────────────────────────────────────────────────────────────────
def apply_farmout_plan(data: dict, user) -> Tuple[int, dict]:
    """Validate + execute one Apply/Farm plan. Returns ``(http_status, json_payload)``.
    Writes happen in ONE transaction through ``set_leg_driver`` (the front door) — on a held day
    with a granted user every write stages into the draft overlay instead (``held: true``)."""
    from drivers.models import Driver
    from reservations.models import Leg
    from dispatching.assignment import _active_draft_for_date, can_use_sandbox, set_leg_driver

    plan = None
    try:
        plan = parse_plan(data)
        leg_ids = [w[0] for w in plan.writes]

        with transaction.atomic():
            # Row locks first (no joins — avoids FOR-UPDATE-on-outer-join restrictions), then
            # all validation reads run serialized behind them. NOTE: like execute_swap, only the
            # plan's own legs are locked — two simultaneous applies targeting DIFFERENT legs on
            # the same driver can still race; acceptable for a single-dispatcher staff tool.
            locked = list(Leg.objects.select_for_update().filter(id__in=leg_ids)
                          .values_list("id", flat=True))
            if len(locked) != len(leg_ids):
                raise PlanRejected(404, "Leg not found")

            # Stage-vs-live decided INSIDE the transaction; the post-write mode check below
            # rolls everything back if set_leg_driver disagrees (hold opened/closed mid-apply).
            draft = _active_draft_for_date(plan.day)
            staged = bool(draft) and can_use_sandbox(user)

            legs = _load_touched_legs(plan)
            _check_expected(plan, legs, draft if staged else None)
            _check_hard_rules(plan, legs)
            inhouse = _check_inhouse_receivers(plan)
            affiliate = _check_affiliate(plan, legs)

            # Held-day staging skips live board revalidation (drafts may be messy; the manager
            # reviews before publish — same contract as execute_swap / drag-drop staging).
            if not staged:
                ok, reason = _revalidate_inhouse(plan, inhouse)
                if not ok:
                    raise PlanRejected(409, f"Rejected — would create an infeasible "
                                            f"schedule: {reason}")

            applied = []
            modes = set()
            for leg_id, did, role in plan.writes:
                new_driver = affiliate if role == _FARM else (
                    inhouse.get(did) if did is not None else None)
                mode, _ = set_leg_driver(legs[leg_id], new_driver, user,
                                         source="farmout_optimizer")
                modes.add(mode)
                applied.append({"leg_id": leg_id, "driver_id": did, "role": role})
            if modes != {"staged" if staged else "live"}:
                # A draft was opened/published between the decision and a write — never leave a
                # plan half-staged half-live. Roll the whole thing back.
                raise PlanRejected(409, "The day's hold state changed mid-apply — nothing was "
                                        "written. Try again.")
    except PlanRejected as e:
        if plan is not None and e.status == 409:
            # The page's picture of the board is provably stale — bump the version so the user's
            # next Analyze recomputes instead of re-serving the same cached recommendations.
            bump_farmout_page_cache(plan.day)
        return e.status, {"success": False, "error": e.error}
    except Exception:
        logger.exception("farmout apply failed")
        return 500, {"success": False, "error": "Apply failed — nothing was changed. "
                                                "Check the logs."}

    cache.delete(f"capacity_planner_{plan.day.isoformat()}")
    bump_farmout_page_cache(plan.day)

    held = staged
    bits = []
    for leg_id, did, role in plan.writes:
        if role in (_KEEP, _MOVE):
            verb = "kept" if role == _KEEP else "moved"
            bits.append(f"{verb} leg {leg_id} on {inhouse.get(did, did)}")
        elif role == _FARM:
            pay = legs[leg_id].driver_base_pay if not held else None
            pay_txt = f" (pay ${pay:,.2f})" if pay is not None else ""
            bits.append(f"farmed leg {leg_id} to {affiliate}{pay_txt}")
        else:
            bits.append(f"left leg {leg_id} unassigned")
    msg = "; ".join(bits) + "."
    msg = msg[0].upper() + msg[1:]
    if held:
        msg = f"Staged in the draft ({plan.day} is held — live board unchanged until publish): {msg}"
    return 200, {"success": True, "held": held, "applied": applied, "message": msg}
