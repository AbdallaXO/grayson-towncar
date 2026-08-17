"""Apply Recovery Advisor plans — the WRITE side of the advisor rail.

The advisor engine (``conflict_advisor.py``) stays strictly read-only; this module is the
separate, deliberate write path behind the rail's Apply button, mirroring
``farmout_actions.py`` (parse -> LOCK -> staleness -> hard rules -> whole-board revalidate ->
snapshot -> front-door writes -> cache invalidation). Each card's plan carries a ready-to-POST
``apply`` payload (ids only — never dollars); this module re-validates the CURRENT database
state before touching anything:

  * STALENESS — the plan carries ``expected`` ({leg_id: driver_id-or-None} — the from-driver
    of EVERY move, farmout_actions contract) and ``expected_times`` ({leg_id: "HH:MM"} for
    retimes). Any drift since the card was computed => 409, nothing written.
  * HARD RULES — reuses ``farmout_actions._check_hard_rules`` / ``_check_affiliate`` /
    ``_check_inhouse_receivers`` via a thin ``_Plan`` adapter (never reimplemented): VIP and
    true departures are never farmed, VIP never displaced, affiliate gates + REAL remaining
    capacity re-checked. Plus the advisor-only owner rule: pending-refund legs are never
    farmed. Guard 6: picked-up / on-location legs never move (409 at apply).
  * WHOLE-BOARD REVALIDATION — ``board_validation.validate_post_move_board`` against the DB
    inside the transaction (the SAME formula the engine ranked with, so advisor and apply can
    never disagree at the threshold), windows resolved ``enforce_cap=False`` — the dispatcher
    explicitly chose this plan (manual-sovereign, matching execute_swap) — cap/window strain
    surfaces as warnings, never a block; retimes applied in-memory. Any NEW problem => 409.
  * HELD-DAY POLICY (owner decision) — advisor applies go LIVE. When a draft is active the
    payload must carry the explicit ``live_override_confirmed`` flag (=> 409 without it);
    ``set_leg_driver(..., live_override=True)`` then writes live and mirrors into the overlay.
    Staging is offered only when the payload says ``stage: true`` AND the user holds the
    sandbox permission. A plan may never land half-staged half-live — mixed modes roll back.

All assignment writes go through ``dispatching.assignment.set_leg_driver`` (THE front door,
``source="conflict_advisor"``); retimes through ``pickup_moves.apply_pickup_time_move`` (same
write the board's Match-flight button uses). AUDIT: set_leg_driver -> Leg.save() already
raises the durable trail — reservations.signals.log_leg_changes writes the
``action='driver_assigned'`` AuditLog row and ops.signals auto-closes conflict/tight-turn
tasks attributed via ``leg._reassigned_by`` — so this module adds NO extra AuditLog write
(pinned by test). Retime-only plans have no driver change to trigger that auto-close, so they
explicitly ``close_task(...)`` with "Resolved via Recovery Advisor: <title>".

Farm-out applies NEVER mark the disruption resolved: board assignment is not affiliate
acceptance (SOP) — the response repeats "call {affiliate} to confirm".
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time as dt_time
from typing import Dict, List, Optional, Tuple

from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from dispatching import farmout_actions as fa
from dispatching.farmout_actions import PlanRejected, bump_farmout_page_cache
from business.datefmt import strf

logger = logging.getLogger(__name__)

_OPS = ("reassign", "farm_out", "unassign", "retime")
_ASSIGN_OPS = ("reassign", "farm_out", "unassign")

# Guard 6 phrasing shared with the engine (conflict_advisor._STATUS_NEVER_MOVE /
# _STATUS_MOVE_WARN) — imported lazily in the checks to keep module import light.
_HELD_CONFIRM_MSG = (
    "{day} is held by a draft — advisor applies go LIVE. Confirm with "
    "live_override_confirmed, or stage into the draft with stage=true.")
_BOARD_CHANGED = "Board changed since this plan was computed"

# Farm applies NEVER resolve a disruption (board assignment != acceptance):
# each one is recorded here so the rail keeps a "farmed — awaiting affiliate
# confirm" card up until the reminder ages out. Cache-only (zero migrations,
# same idiom as the snooze list); TTL mirrors the snooze cap.
RA_FARM_PENDING_TTL_MIN = 240


def _farm_pending_key(day) -> str:
    return f"ra_farm_pending_{day.isoformat()}"


def list_farm_pending(day) -> List[dict]:
    """Live 'farmed — awaiting affiliate confirm' entries for a date, expired
    ones pruned on read. Entries: {"leg_id", "affiliate", "until": epoch}."""
    import time as _time
    now = _time.time()
    return [e for e in (cache.get(_farm_pending_key(day)) or [])
            if isinstance(e, dict) and e.get("until", 0) > now]


def record_farm_pending(day, leg_id: int, affiliate_name: str) -> None:
    """Remember a just-applied farm-out so the rail can show the persistent
    'farmed — awaiting {affiliate} confirm' card (SOP: call to confirm)."""
    import time as _time
    entries = [e for e in list_farm_pending(day) if e.get("leg_id") != leg_id]
    entries.append({"leg_id": leg_id,
                    "affiliate": (affiliate_name or "").strip(),
                    "until": _time.time() + RA_FARM_PENDING_TTL_MIN * 60})
    cache.set(_farm_pending_key(day), entries, RA_FARM_PENDING_TTL_MIN * 60)


# ── Plan parsing ────────────────────────────────────────────────────────────────────────────
@dataclass
class _Action:
    op: str
    leg_id: int
    to_driver_id: Optional[int] = None
    new_time: Optional[dt_time] = None
    note: str = ""


@dataclass
class _AdvisorPlan:
    day: date
    disruption_id: str
    plan_id: str
    task_id: Optional[int]
    actions: List[_Action]
    expected: Dict[int, Optional[int]]                 # leg_id -> driver_id at card time
    expected_times: Dict[int, str] = field(default_factory=dict)  # leg_id -> "HH:MM"
    stage: bool = False
    live_override_confirmed: bool = False
    confirm_pullback: bool = False                     # takeback opt-in (call affiliate first)
    title: str = ""                                    # plan title, for task resolution notes

    @property
    def leg_ids(self) -> List[int]:
        return sorted({a.leg_id for a in self.actions})

    @property
    def has_assignment_ops(self) -> bool:
        return any(a.op in _ASSIGN_OPS for a in self.actions)

    @property
    def retimes(self) -> List[_Action]:
        return [a for a in self.actions if a.op == "retime"]


def parse_advisor_plan(data: dict) -> _AdvisorPlan:
    """Validate the card's ``apply`` payload shape (schema 1). 400 on anything
    malformed or a past service date; never touches the database."""
    if not isinstance(data, dict):
        raise PlanRejected(400, "Malformed plan payload")
    if data.get("schema") != 1:
        raise PlanRejected(400, "Unknown plan schema — refresh the advisor")
    try:
        day = datetime.strptime(data.get("date") or "", "%Y-%m-%d").date()
    except ValueError:
        raise PlanRejected(400, "Invalid or missing date")
    if day < timezone.localdate():
        raise PlanRejected(400, "That service date is in the past — past days are graded, "
                                "never re-dispatched")

    raw_actions = data.get("actions")
    if not isinstance(raw_actions, list) or not raw_actions:
        raise PlanRejected(400, "Plan has no actions")
    actions: List[_Action] = []
    seen_assign, seen_retime = set(), set()
    for raw in raw_actions:
        if not isinstance(raw, dict):
            raise PlanRejected(400, "Malformed action")
        op = raw.get("op")
        if op not in _OPS:
            raise PlanRejected(400, f"Unknown action op {op!r}")
        leg_id = fa._as_int(raw.get("leg_id"), "action leg_id")
        a = _Action(op=op, leg_id=leg_id, note=str(raw.get("note") or ""))
        if op in ("reassign", "farm_out"):
            a.to_driver_id = fa._as_int(raw.get("to_driver_id"), "action to_driver_id")
        elif op == "unassign":
            if raw.get("to_driver_id") is not None:
                raise PlanRejected(400, "unassign action must not carry to_driver_id")
        else:  # retime
            try:
                a.new_time = datetime.strptime(raw.get("new_pickup_time") or "",
                                               "%H:%M").time()
            except ValueError:
                raise PlanRejected(400, f"Invalid new_pickup_time for leg {leg_id}")
        bucket = seen_retime if op == "retime" else seen_assign
        if leg_id in bucket:
            raise PlanRejected(400, "Plan touches the same leg twice")
        bucket.add(leg_id)
        actions.append(a)
    # One farm per plan — the engine never emits more, and the write path
    # validates/pays exactly ONE affiliate (farmout_actions._check_affiliate
    # checks farm_writes[0] only). Accepting a second farm action would
    # silently hand its leg to the FIRST affiliate, ungated.
    if sum(1 for a in actions if a.op == "farm_out") > 1:
        raise PlanRejected(400, "Plan farms more than one leg — advisor plans "
                                "carry at most one farm_out action")

    raw_expected = data.get("expected")
    if not isinstance(raw_expected, dict):
        raise PlanRejected(400, "Missing expected assignment map")
    expected: Dict[int, Optional[int]] = {}
    for k, v in raw_expected.items():
        expected[fa._as_int(k, "expected leg id")] = fa._as_int(
            v, "expected driver id", allow_none=True)
    for a in actions:
        # The from-driver of EVERY move (retimes included) is staleness-guarded.
        if a.leg_id not in expected:
            raise PlanRejected(400, f"No expected assignment for leg {a.leg_id}")

    raw_times = data.get("expected_times")
    if raw_times is None:
        raw_times = {}
    if not isinstance(raw_times, dict):
        raise PlanRejected(400, "Malformed expected_times map")
    expected_times: Dict[int, str] = {}
    for k, v in raw_times.items():
        leg_id = fa._as_int(k, "expected_times leg id")
        try:
            datetime.strptime(str(v), "%H:%M")
        except ValueError:
            raise PlanRejected(400, f"Invalid expected time for leg {leg_id}")
        expected_times[leg_id] = str(v)
    for a in actions:
        if a.op == "retime" and a.leg_id not in expected_times:
            raise PlanRejected(400, f"No expected pickup time for retimed leg {a.leg_id}")

    task_id = data.get("task_id")
    if task_id is not None:
        task_id = fa._as_int(task_id, "task_id")

    return _AdvisorPlan(
        day=day,
        disruption_id=str(data.get("disruption_id") or ""),
        plan_id=str(data.get("plan_id") or ""),
        task_id=task_id,
        actions=actions,
        expected=expected,
        expected_times=expected_times,
        stage=bool(data.get("stage")),
        live_override_confirmed=bool(data.get("live_override_confirmed")),
        confirm_pullback=bool(data.get("confirm_pullback")),
        title=str(data.get("title") or ""),
    )


# ── farmout_actions adapter (reuse, don't reimplement) ──────────────────────────────────────
def _fa_shim(plan: _AdvisorPlan) -> fa._Plan:
    """Thin ``farmout_actions._Plan`` adapter so its validators run unmodified.

    Role mapping: farm_out -> _FARM, unassign -> _UNASSIGN, reassign -> _MOVE
    (or _KEEP when it is the plan's SOLE reassign — the disruption's own target
    leg, which may legitimately be VIP; multi-move chains never touch VIP, so
    any VIP leg inside one correctly 400s as "never moved by a swap plan").
    Retimes ride along with a role none of the farmout validators react to —
    they still get loaded, locked and staleness-checked."""
    reassigns = [a for a in plan.actions if a.op == "reassign"]
    target = reassigns[0].leg_id if len(reassigns) == 1 else 0
    writes: List[Tuple[int, Optional[int], str]] = []
    for a in plan.actions:
        if a.op == "reassign":
            role = fa._KEEP if a.leg_id == target else fa._MOVE
            writes.append((a.leg_id, a.to_driver_id, role))
        elif a.op == "farm_out":
            writes.append((a.leg_id, a.to_driver_id, fa._FARM))
        elif a.op == "unassign":
            writes.append((a.leg_id, None, fa._UNASSIGN))
        else:  # retime — inert role: loaded + staleness-checked, no farmout rule fires
            writes.append((a.leg_id, None, "retime"))
    return fa._Plan(kind="advisor", day=plan.day, target_leg_id=target, writes=writes,
                    expected=plan.expected, confirm_pullback=plan.confirm_pullback)


# ── Validation (all reads; raise PlanRejected, never write) ─────────────────────────────────
def _check_stale(plan: _AdvisorPlan, shim: fa._Plan, legs: dict, draft=None) -> None:
    """farmout_actions._check_expected (drivers, draft-overlay aware) extended
    with the plan's ``expected_times`` map. Any drift => 409."""
    try:
        fa._check_expected(shim, legs, draft)
    except PlanRejected as e:
        raise PlanRejected(409, f"{_BOARD_CHANGED} — refresh the advisor. {e.error}")
    for leg_id, exp_hhmm in plan.expected_times.items():
        l = legs.get(leg_id)
        if l is None:
            continue
        cur = l.pickup_time.strftime("%H:%M") if l.pickup_time else None
        if cur != exp_hhmm:
            raise PlanRejected(
                409, f"{_BOARD_CHANGED} (leg {leg_id} pickup is now {cur or 'unset'}, "
                     f"the plan assumed {exp_hhmm}) — refresh the advisor")


def _check_status_safety(plan: _AdvisorPlan, legs: dict) -> List[str]:
    """Guard 6 at the apply layer: picked-up / on-location legs never move, and
    neither does an assigned leg whose pickup moment has gone by (409 —
    generation already hard-excludes both; this catches drift). Moving an
    on-the-way / confirmed leg is legal but warned: Leg.save() resets
    progressed statuses on a driver change, the new driver must re-accept.

    The clock half re-derives NOW rather than trusting the card's age: a rail
    left open through a long phone call can still be showing a plan that was
    valid when it was drawn and is nonsense by the time it is clicked."""
    from dispatching.conflict_advisor import (
        _STATUS_MOVE_WARN, _STATUS_NEVER_MOVE, _effective_pickup_dt)

    now_local = timezone.localtime(timezone.now()).replace(tzinfo=None)
    warnings = []
    for a in plan.actions:
        leg = legs[a.leg_id]
        status = (leg.status or "")
        if status in _STATUS_NEVER_MOVE:
            raise PlanRejected(
                409, f"Leg {a.leg_id} is already {status} — it can no longer be "
                     f"moved. Refresh the advisor.")
        if (a.op in _ASSIGN_OPS and leg.driver_id
                and leg.pickup_time is not None
                and _effective_pickup_dt(leg, leg.pickup_date) <= now_local):
            raise PlanRejected(
                409, f"Leg {a.leg_id}'s {strf(leg.pickup_time, '%-I:%M %p')} "
                     f"pickup has already come and gone — it can't be handed to "
                     f"another driver now. Refresh the advisor.")
        if a.op in _ASSIGN_OPS and status in _STATUS_MOVE_WARN:
            warnings.append(
                f"Leg {a.leg_id} driver is already "
                f"{'en route' if status == 'on-the-way' else 'accepted'} — status "
                f"resets to in-progress, the new driver must re-accept.")
    return warnings


def _check_advisor_farm_gates(plan: _AdvisorPlan, legs: dict) -> None:
    """Owner-confirmed advisor-only rule (guard 8): pending-refund legs are
    never farmed. VIP / true-departure farm refusals already came from the
    reused farmout_actions._check_hard_rules."""
    farm_leg_ids = [a.leg_id for a in plan.actions if a.op == "farm_out"]
    if not farm_leg_ids:
        return
    from reservations.models import RefundRequest

    res_ids = {legs[lid].reservation_id for lid in farm_leg_ids
               if legs[lid].reservation_id}
    pending = set(RefundRequest.objects.filter(
        reservation_id__in=list(res_ids),
        status__in=["requested", "processing", "approved"],
    ).values_list("reservation_id", flat=True))
    for lid in farm_leg_ids:
        if legs[lid].reservation_id in pending:
            raise PlanRejected(400, f"Leg {lid} has a refund in flight — never farmed")


def _reassign_warnings(plan: _AdvisorPlan, legs: dict) -> List[str]:
    """Guard 8 warning half: reassigning a pending-refund leg is allowed but
    carries the board's existing warning string verbatim."""
    from dispatching.conflict_advisor import _PENDING_REFUND_WARNING
    from reservations.models import RefundRequest

    move_ids = [a.leg_id for a in plan.actions if a.op == "reassign"]
    if not move_ids:
        return []
    res_ids = {legs[lid].reservation_id for lid in move_ids if legs[lid].reservation_id}
    pending = set(RefundRequest.objects.filter(
        reservation_id__in=list(res_ids),
        status__in=["requested", "processing", "approved"],
    ).values_list("reservation_id", flat=True))
    return [f"Leg {lid}: {_PENDING_REFUND_WARNING}" for lid in move_ids
            if legs[lid].reservation_id in pending]


def _revalidate_board(plan: _AdvisorPlan, inhouse: dict):
    """Full-board revalidation against the DB, INSIDE the transaction, through
    the SAME formula the engine ranked with (board_validation.validate_post_move_board)
    — so advisor and apply can never disagree at the threshold. Windows are
    resolved ``enforce_cap=False`` (the dispatcher explicitly chose this plan —
    manual-sovereign, matching execute_swap); retimes applied in-memory; farmed
    and unassigned legs leave the in-house board. Returns the BoardValidation
    (its worsened_pairs become response warnings) or raises 409."""
    from drivers.models import Driver
    from reservations.models import Leg, LegStatus
    from dispatching import feasibility_guards as fg
    from dispatching.board_validation import board_turn_bands, validate_post_move_board
    from dispatching.conflict_advisor import planning_clock_schedules
    from dispatching.scheduler import (build_driver_schedules, build_sharer_partners,
                                       estimate_job_end_time, get_compatible_vehicle_types,
                                       load_all_driver_vtypes, preload_timing_cache)

    preload_timing_cache()
    day = plan.day
    legs = list(Leg.objects.filter(pickup_date=day)
                .exclude(reservation__status="cancelled").exclude(status="cancelled")
                .select_related("driver", "reservation", "reservation__vehicle", "vehicle",
                                "flight_information"))
    legs_by_id = {l.id: l for l in legs}
    for l in legs:
        l._estimated_end_dt = estimate_job_end_time(l, day)

    # In-house board = current in-house leg holders + every receiving driver.
    holder_ids = {l.driver_id for l in legs if l.driver_id}
    pool = {d.id: d for d in Driver.objects.filter(id__in=(holder_ids | set(inhouse)))}
    board_drivers = [d for d in pool.values() if d.driver_type == "inhouse"]
    schedules = build_driver_schedules(legs, board_drivers, day)
    sharer_partners = build_sharer_partners(set(schedules), day)

    # Guard 1, planning half, at APPLY: re-anchor under-way slots on their
    # recorded pickup (max(static, actual) — never optimistic), same clock the
    # engine ranked with, so a receiver who is demonstrably running late can't
    # look free to the click-time revalidation. Newest-first iteration keeps
    # the EARLIEST tap (build_board_state convention).
    picked_up_by_leg = {}
    for lid, ts in (LegStatus.objects
                    .filter(leg__pickup_date=day, status="picked-up")
                    .order_by("-timestamp").values_list("leg_id", "timestamp")):
        picked_up_by_leg[lid] = timezone.localtime(ts).replace(tzinfo=None)
    schedules = planning_clock_schedules(schedules, legs_by_id,
                                         picked_up_by_leg, day)
    baseline_bands = board_turn_bands(schedules, day)

    windows = {}
    for d in board_drivers:
        eff = d.get_effective_availability(day)
        mh = eff.get("max_hours")
        cfg = {"start": eff.get("start_hour"), "end": eff.get("end_hour"),
               "max_hours": (float(mh) if mh else None),
               "flexible": bool(eff.get("flexible"))}
        # enforce_cap=False: manual-sovereign — the duty-span cap never
        # hard-blocks a plan the dispatcher explicitly chose.
        windows[d.id] = fg.get_effective_window(d.id, configured=cfg, enforce_cap=False)

    # Vehicle-class compatibility for gaining placements (check_feasibility has
    # no vehicle gate — mirrors farmout_actions._revalidate_inhouse).
    dvtypes = load_all_driver_vtypes(day)
    for a in plan.actions:
        if a.op != "reassign":
            continue
        dv = dvtypes.get(a.to_driver_id)
        if dv is None:
            raise PlanRejected(409, f"{inhouse[a.to_driver_id]} has no vehicle "
                                    f"assigned on {day}")
        leg = legs_by_id.get(a.leg_id)
        vt = (leg.effective_vehicle_type or "") if leg is not None else ""
        if vt and vt not in get_compatible_vehicle_types(dv):
            raise PlanRejected(409, f"leg {a.leg_id} needs a {vt}; "
                                    f"{inhouse[a.to_driver_id]}'s {dv} can't serve it")

    moves = []
    for a in plan.actions:
        if a.op == "reassign":
            moves.append((a.leg_id, a.to_driver_id))
        elif a.op in ("farm_out", "unassign"):
            moves.append((a.leg_id, None))   # leaves the in-house board
    time_changes = {a.leg_id: a.new_time for a in plan.retimes}

    verdict = validate_post_move_board(
        schedules, legs_by_id, moves, day,
        windows=windows, sharer_partners=sharer_partners,
        baseline_bands=baseline_bands, time_changes=time_changes or None)
    if not verdict.ok:
        raise PlanRejected(409, f"Rejected — the board changed and this plan would now "
                                f"create a new problem: {verdict.reason}")
    return verdict


def _needs_snapshot(plan: _AdvisorPlan) -> bool:
    """Snapshot policy: >=2 actions or any farm/retime. A single simple
    reassign skips — parity with drag-drop."""
    return (len(plan.actions) >= 2
            or any(a.op in ("farm_out", "retime") for a in plan.actions))


def _close_retime_task(plan: _AdvisorPlan, user) -> Optional[int]:
    """Retime-only plans have no driver change, so ops/signals never auto-closes
    the linked task — close it explicitly, attributed to the applier."""
    if not plan.task_id:
        return None
    from ops.models import OperationalTask
    from ops.services import close_task

    task = OperationalTask.objects.filter(id=plan.task_id).first()
    if task is None or not task.is_open:
        return None
    label = plan.title or plan.plan_id or plan.disruption_id
    close_task(task, resolved_by=user,
               resolution_notes=f"Resolved via Recovery Advisor: {label}")
    return task.id


# ── Entry point ─────────────────────────────────────────────────────────────────────────────
def apply_advisor_plan(data: dict, user) -> Tuple[int, dict]:
    """Validate + execute one advisor plan. Returns ``(http_status, json_payload)``.
    One ``transaction.atomic()``: row locks FIRST, then every validation read runs
    serialized behind them (farmout ordering). Writes go through ``set_leg_driver``
    (``source="conflict_advisor"``) and ``apply_pickup_time_move`` only."""
    from reservations.models import Leg
    from dispatching.assignment import _active_draft_for_date, can_use_sandbox, set_leg_driver
    from dispatching.conflict_advisor import _FARM_CONFIRM_LINE
    from dispatching.pickup_moves import apply_pickup_time_move
    from dispatching.views import _create_schedule_snapshot

    plan = None
    try:
        plan = parse_advisor_plan(data)
        leg_ids = plan.leg_ids

        with transaction.atomic():
            # Row locks first (no joins), then all validation reads run behind
            # them. Like farmout/execute_swap: only the plan's own legs are
            # locked — acceptable for a single-dispatcher staff tool.
            locked = list(Leg.objects.select_for_update().filter(id__in=leg_ids)
                          .values_list("id", flat=True))
            if len(locked) != len(leg_ids):
                raise PlanRejected(404, "Leg not found")

            # Held-day policy (owner decision): advisor applies go LIVE, with an
            # explicit extra confirmation while a draft is active; staging only
            # on request by a sandbox-granted user. Decided INSIDE the
            # transaction; the post-write mode check below rolls back if the
            # hold state changes mid-apply.
            draft = _active_draft_for_date(plan.day)
            staged = bool(draft) and plan.stage and can_use_sandbox(user)
            live_override = False
            if draft and not staged:
                if plan.stage:
                    raise PlanRejected(403, "You don't have sandbox access — apply to "
                                            "the LIVE board (live_override_confirmed) "
                                            "instead")
                if not plan.live_override_confirmed:
                    raise PlanRejected(409, _HELD_CONFIRM_MSG.format(day=plan.day))
                live_override = True
            if staged and plan.retimes:
                raise PlanRejected(400, "Pickup-time moves always write live — this plan "
                                        "can't be staged while the day is held")

            shim = _fa_shim(plan)
            legs = fa._load_touched_legs(shim)       # 404 missing / 409 completed+cancelled
            _check_stale(plan, shim, legs, draft if staged else None)
            warnings = _check_status_safety(plan, legs)
            fa._check_hard_rules(shim, legs)         # VIP/departure farm + displacement
            _check_advisor_farm_gates(plan, legs)    # pending-refund never farmed
            inhouse = fa._check_inhouse_receivers(shim)
            affiliate = fa._check_affiliate(shim, legs)
            warnings += _reassign_warnings(plan, legs)

            # Held-day staging skips live-board revalidation (drafts may be
            # messy; the manager reviews before publish — farmout contract).
            verdict = None
            if not staged:
                verdict = _revalidate_board(plan, inhouse)
                for w in verdict.worsened_pairs:
                    warnings.append(
                        f"Turn {w['prev_leg_id']}->{w['next_leg_id']} on driver "
                        f"{w['driver_id']} goes {w['before'] or 'clean'} -> "
                        f"{w['after']} ({w['slack']} min slack).")

            snapshot = None
            if not staged and _needs_snapshot(plan):
                snapshot = _create_schedule_snapshot(plan.day, user, "conflict_advisor")

            applied, modes = [], set()
            for a in plan.actions:
                leg = legs[a.leg_id]
                if a.op == "retime":
                    apply_pickup_time_move(leg, a.new_time, user=user,
                                           note=a.note or "Recovery Advisor")
                    applied.append({"leg_id": a.leg_id, "op": a.op,
                                    "new_pickup_time": a.new_time.strftime("%H:%M")})
                    continue
                new_driver = (affiliate if a.op == "farm_out"
                              else inhouse.get(a.to_driver_id)
                              if a.to_driver_id is not None else None)
                mode, _ = set_leg_driver(leg, new_driver, user,
                                         live_override=live_override,
                                         source="conflict_advisor")
                modes.add(mode)
                applied.append({"leg_id": a.leg_id, "op": a.op,
                                "to_driver_id": a.to_driver_id})
            if modes and modes != {"staged" if staged else "live"}:
                # Hold opened/closed between the decision and a write — never
                # leave a plan half-staged half-live.
                raise PlanRejected(409, "The day's hold state changed mid-apply — "
                                        "nothing was written. Try again.")

            closed_task_id = None
            if not plan.has_assignment_ops:
                # Driver-change plans rely on ops/signals auto-close (attributed
                # via leg._reassigned_by inside set_leg_driver).
                closed_task_id = _close_retime_task(plan, user)
    except PlanRejected as e:
        return e.status, {"success": False, "error": e.error}
    except Exception:
        logger.exception("advisor apply failed")
        return 500, {"success": False, "error": "Apply failed — nothing was changed. "
                                                "Check the logs."}

    # Cache invalidation (plan step 9). Advisor cards are fingerprint-keyed and
    # self-invalidate; the fingerprint itself changed with the writes above.
    cache.delete(f"capacity_planner_{plan.day.isoformat()}")
    bump_farmout_page_cache(plan.day)
    cache.delete("ops_pending_task_count")
    cache.delete("ra_crit_count")

    # A farm apply never resolves the card — flip it to "farmed — awaiting
    # affiliate confirm" on the rail (the state endpoint reads this list).
    if affiliate is not None and not staged:
        for a in plan.actions:
            if a.op == "farm_out":
                record_farm_pending(plan.day, a.leg_id, str(affiliate))

    bits = []
    for a in plan.actions:
        if a.op == "reassign":
            bits.append(f"moved leg {a.leg_id} to {inhouse.get(a.to_driver_id, a.to_driver_id)}")
        elif a.op == "farm_out":
            pay = legs[a.leg_id].driver_base_pay if not staged else None
            pay_txt = f" (pay ${pay:,.2f})" if pay is not None else ""
            bits.append(f"farmed leg {a.leg_id} to {affiliate}{pay_txt}")
        elif a.op == "unassign":
            bits.append(f"left leg {a.leg_id} unassigned")
        else:
            bits.append(f"moved leg {a.leg_id} pickup to "
                        f"{a.new_time.strftime('%I:%M %p').lstrip('0')}")
    msg = "; ".join(bits) + "."
    msg = msg[0].upper() + msg[1:]
    if affiliate is not None:
        # SOP: board assignment != acceptance — the disruption is NOT resolved
        # until the affiliate confirms; the card stays up saying so.
        msg += " " + _FARM_CONFIRM_LINE.format(aff=affiliate)
    if staged:
        msg = (f"Staged in the draft ({plan.day} is held — live board unchanged "
               f"until publish): {msg}")

    return 200, {
        "success": True,
        "held": staged,
        "mode": "staged" if staged else "live",
        "live_override": live_override,
        "applied": applied,
        "snapshot_id": snapshot.id if snapshot else None,
        "closed_task_id": closed_task_id,
        "warnings": warnings,
        "message": msg,
    }
