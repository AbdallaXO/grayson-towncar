"""
Recovery Advisor engine — detection → candidates → validation → ranking →
explanation, in one deterministic pass over the live board.

POSTURE: THIS MODULE IS STRICTLY READ-ONLY AND ADVISORY-ONLY. No model writes,
no task creation/closing, no migrations, and ZERO external calls — it never
touches ``drivers.utils.get_drive_time``, AeroAPI, Samsara HTTP, or live Google.
All timing flows through the founder static tables / cached metrics that
``scheduler.chain_clear_dt`` / ``chain_repo_minutes`` and
``board_validation.turn_slack_minutes`` already read, and every live signal it
consumes (``Leg.dispatch_*``) was persisted by the background Samsara sweep —
this module only reads what is already on the row. Deterministic by
construction: identical inputs produce identical cards in identical order.
The dispatcher always decides; nothing here is an approval.

HARD RULES (restated from the approved plan — generation enforces them here,
the apply stage re-checks every one at click time):
  * VIP legs are never farmed; true departures are never farmed.
  * Legs in status picked-up / on-location are never moved.
  * Pending-refund legs are never farmed; reassignments carry a warning.
  * Board assignment is NOT affiliate acceptance — confirmation is a phone call.
  * PRIME DIRECTIVE (scheduler.py:197): tight turns that work in reality must
    NOT be flagged impossible. A founder-built day with +0/+3 buffers produces
    ZERO cards here — the advisor cards what BREAKS or what reality DEGRADED,
    never what was deliberately planned tight.

THE TWO-CLOCK POLICY (the core correctness decision, hazard guard 1):
  * Detection clock — what is true now. Reality wins in BOTH directions: a
    recorded pickup re-anchors the chain (``chain_clear_dt_from_actual``), a
    fresh GPS ``on_time`` suppresses clock-overdue alarms, a delayed flight
    moves the deadline out so nobody is "late" for a plane still in the air.
  * Planning clock — validating future placements. NEVER optimistic:
    ``max(chain_clear_dt, chain_clear_dt_from_actual)`` for under-way legs.
  ``estimate_job_end_time`` (p75 metrics) is NEVER used for feasibility — it is
  display text and the overrun status test only (exactly what
  CHAIN_STATIC_TIMING banned from chain math).

SIGNAL DISCIPLINE (hazard guards 2, 4, 5):
  * GPS is negative-signal-only: a GPS-based disruption requires
    ``dispatch_risk_status`` in {at_risk, late} AND ``dispatch_eta_is_fresh``.
    Stale/absent/unknown GPS never generates a disruption — it only removes the
    suppression of clock flags. Stale-but-parked vehicles never raise anything.
  * The GPS-vs-clock fold is IMPORTED (``pickup_policy.pickup_risk``), never
    re-derived, so the advisor and the board pill agree at every threshold.
  * Overdue past ``pickup_policy.OVERDUE_STALE_MIN`` with nobody acting is a
    data-hygiene card ("chase the button"), never a recovery disruption.
  * Every card carries ``basis`` in {gps_fresh, gps_stale_parked, clock_only,
    flight, recorded_pickup} so a dispatcher can verify the flag, not trust it.

SCOPE NOTES:
  * Affiliate-assigned legs are DETECTION-SCOPE ONLY for flight/unacked-time
    facts (guard 7): no chain math, no "affiliate driver is late" clock cards —
    no signal exists to judge them. In-house schedules only for overlap /
    late-cascade / overrun.
  * Overnight (guard 3): the sweep is per calendar date. Tail legs (00:00–02:00)
    are chain-linked to the PREVIOUS evening via absolute datetimes, so a
    23:30 → 00:15 pair is never a false overlap and a genuine tonight→tail
    break is caught. Overnight-AMBIGUOUS arrivals (unconfirmed takeoff date)
    ABSTAIN from all projection: the card is "confirm the takeoff date".
  * Ops-task interplay (guard 9): detection uses the same ``pickup_policy``
    constants the ops scanners use, but NEVER calls
    ``ops.tasks.detect_driver_conflicts`` / ``classify_turn`` (they make raw
    Google calls per pair). Open conflict/turn/assignment tasks are mapped in
    ONE query and linked on the card — never created or closed here.

Config below follows the house advisor style: module constants, 0/False
disables the feature or the sub-behavior.
"""
from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime, time as dt_time, timedelta
from time import monotonic as _monotonic

from django.utils import timezone

# ── Flags / budgets (module-constant config; 0 = disabled) ──────────────────
ADVISOR_ENABLED = True
ADVISOR_HORIZON_UNASSIGNED_MIN = 120   # unassigned legs card inside this horizon
ADVISOR_OVERRUN_GRACE_MIN = 20         # past estimate by this much => overrunning
ADVISOR_FLIGHT_MISMATCH_MIN = 15       # controlling-flight vs booked divergence
ADVISOR_MAX_DISRUPTIONS = 6            # full plan analysis cap (rest detected_only)
ADVISOR_MAX_PLANS_PER_CARD = 3
ADVISOR_SWAP_DEPTH = 3                 # explicit find_swaps budgets — the
ADVISOR_SWAP_TIME_MS = 1200            # literal-default args (5/5000/5000) get
ADVISOR_SWAP_MAX_ITER = 2500           # silently replaced by SchedulerSettings
ADVISOR_BUDGET_MS = 4000               # hard wall-clock cap for a full compute
ADVISOR_ALLOW_PICKUP_NUDGE = False     # within-driver reorder is a non-concept
                                       # (execution order is pickup_time, a
                                       # guest commitment); nudge stays off

# The board pill's clock-overdue debounce (views._PICKUP_OVERDUE_MIN). Mirrored
# by VALUE because the engine must not import the views module; the value is
# pinned by tests_timeline_reality, so drift would fail loudly there first.
_CLOCK_OVERDUE_GRACE_MIN = 3

# GPS targets that gate a PICKUP deadline (views._GPS_PICKUP_TARGETS, same
# mirror-by-value rationale as above). 'next_pickup' = mid-trip ETA to the
# chained next pickup — the overrun detector's signal.
_GPS_PICKUP_TARGETS = ("pickup", "next_pickup")

# Previous-evening lookback for the overnight tail chain-link (guard 3): a
# tail leg's real predecessor is last night's late work.
_PREV_EVENING_FROM_HOUR = 20

_SEV_RANK = {"critical": 0, "warning": 1, "watch": 2}

# Basis vocabulary (guard 5) — every card names the signal class it stands on.
BASIS_GPS_FRESH = "gps_fresh"
BASIS_GPS_STALE_PARKED = "gps_stale_parked"
BASIS_CLOCK_ONLY = "clock_only"
BASIS_FLIGHT = "flight"
BASIS_RECORDED_PICKUP = "recorded_pickup"


# ════════════════════════════════════════════════════════════════════════════
# DATA SHAPES
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class Disruption:
    """One detected developing problem on the live board.

    ``id`` is the stable anti-flap identity key (guard 10): ``overlap`` cards
    use ``overlap:{prev_leg}:{next_leg}``, every other kind ``{kind}:{leg}``.
    ``abstain`` marks cards that must never receive recovery plans (hygiene
    cards, overnight-ambiguous "confirm the date" cards); ``hygiene`` marks the
    guard-2 chase-the-button subset. ``details`` carries engine artifacts
    (slacks, shifts) for the Stage-B2 candidate generator — never rendered raw.
    """
    id: str
    kind: str                      # overlap|late_cascade|flight_change|unassigned|overrun
    severity: str                  # critical|warning|watch
    headline: str
    narrative: str
    basis: str                     # guard-5 vocabulary above
    leg_ids: list = field(default_factory=list)
    anchor_leg_id: int = 0
    driver_id: int | None = None
    impact_dt: datetime | None = None   # naive local
    task_id: int | None = None
    abstain: bool = False
    hygiene: bool = False
    details: dict = field(default_factory=dict)


@dataclass
class BoardState:
    """Everything detection (and later, generation) reads — assembled fresh per
    request by ``build_board_state`` (~12 queries), or hand-built in tests.
    ``schedules`` covers DEPLOYABLE IN-HOUSE drivers only (DriverVehicleAssignment
    holders + leg holders): affiliate-held legs appear in ``legs`` but never in
    chain math (guard 7). ``prev_tail`` is the guard-3 cross-midnight context:
    {driver_id: [(ScheduleSlot on the PREVIOUS date — absolute clear datetimes,
    recorded_pickup_dt|None)]}."""
    target_date: date
    now: datetime                  # aware
    now_local: datetime            # naive local — the board arithmetic clock
    legs: list = field(default_factory=list)
    legs_by_id: dict = field(default_factory=dict)
    schedules: dict = field(default_factory=dict)        # {driver_id: DriverDaySchedule}
    drivers_by_id: dict = field(default_factory=dict)
    windows: dict = field(default_factory=dict)          # {driver_id: window|None} enforce_cap=True
    window_sources: dict = field(default_factory=dict)   # {driver_id: 'stub'|'configured'}
    vehicle_caps: dict = field(default_factory=dict)
    driver_vtypes: dict = field(default_factory=dict)
    sharer_partners: dict = field(default_factory=dict)
    picked_up_by_leg: dict = field(default_factory=dict) # {leg_id: naive local earliest tap}
    vip_leg_ids: set = field(default_factory=set)
    pending_refund_leg_ids: set = field(default_factory=set)
    keoi_leg_ids: set = field(default_factory=set)
    open_tasks_by_leg: dict = field(default_factory=dict)  # {leg_id: {task_type: task_id}}
    baseline_bands: dict = field(default_factory=dict)     # planning clock (picked=None)
    prev_tail: dict = field(default_factory=dict)


# ════════════════════════════════════════════════════════════════════════════
# CHANGE FINGERPRINT (hazard guard 11 — Leg has no updated_at)
# ════════════════════════════════════════════════════════════════════════════

# The driver-portal hash tuple (drivers/views.py board_state) PLUS everything a
# dispatcher-facing card can depend on.
_FP_LEG_FIELDS = (
    # driver-portal tuple
    "id", "status", "pickup_date", "pickup_time",
    "pickup_location", "dropoff_location", "vehicle_id",
    "passenger_count", "luggage_count", "private_notes", "reservation__status",
    # + advisor-relevant
    "driver_id", "pickup_time_changed_at", "pickup_change_ack_at",
    "dispatch_risk_status", "dispatch_eta_minutes", "dispatch_eta_target_time",
    # controlling-flight best-arrival chain — every LegFlight row (superset of
    # "controlling": a controlling-flag flip must bump the hash too) plus the
    # legacy flight_information OneToOne. All six best_arrival_local components.
    "legflight_set__id", "legflight_set__is_controlling",
    "legflight_set__flight__actual_gate_arrival_local",
    "legflight_set__flight__estimated_gate_arrival_local",
    "legflight_set__flight__actual_arrival_local",
    "legflight_set__flight__estimated_arrival_local",
    "legflight_set__flight__scheduled_gate_arrival_local",
    "legflight_set__flight__scheduled_arrival_local",
    "flight_information__actual_gate_arrival_local",
    "flight_information__estimated_gate_arrival_local",
    "flight_information__actual_arrival_local",
    "flight_information__estimated_arrival_local",
    "flight_information__scheduled_gate_arrival_local",
    "flight_information__scheduled_arrival_local",
)


def compute_board_fingerprint(day):
    """sha1 fingerprint of everything the advisor's cards can depend on for
    ``day``. Same idea as the driver-portal poll (Leg has no ``updated_at``):
    the GET endpoint short-circuits on an unchanged hash, so this must be cheap
    — exactly 3 indexed queries, values-only, no model instantiation.

      1. The day's legs: driver-portal tuple + driver, pickup-change stamps,
         persisted dispatch_* sweep fields, flight best-arrival chains, and a
         per-leg Max(status_history__id) — LegStatus is insert-only, so any
         status tap for the day bumps some leg's max (the plan's monotonic
         LegStatus cursor, folded into query 1 to hold the 3-query budget).
      2. The date's DriverVehicleAssignment rows (the deployable-pool roster —
         a zero-leg rostered driver appearing/vanishing changes valid receivers).
      3. The active ScheduleDraft id/state (held-day banner + apply policy).
    """
    from django.db.models import Max
    from reservations.models import Leg, ScheduleDraft
    from drivers.models import DriverVehicleAssignment

    leg_rows = list(
        Leg.objects.filter(pickup_date=day)
        .values(*_FP_LEG_FIELDS)
        .annotate(_sh_max=Max("status_history__id"))
        .order_by("id", "legflight_set__id")
        .values_list(*_FP_LEG_FIELDS, "_sh_max")
    )
    dva_rows = list(
        DriverVehicleAssignment.objects.filter(date=day)
        .order_by("id").values_list("id", "driver_id", "vehicle_id")
    )
    draft_rows = list(
        ScheduleDraft.objects.filter(
            schedule_date=day, state__in=ScheduleDraft.ACTIVE_STATES)
        .order_by("id").values_list("id", "state")
    )
    payload = repr((str(day), leg_rows, dva_rows, draft_rows))
    return hashlib.sha1(payload.encode()).hexdigest()


# ════════════════════════════════════════════════════════════════════════════
# BOARD-STATE ASSEMBLY (~12 queries, fresh per request)
# ════════════════════════════════════════════════════════════════════════════

def build_board_state(target_date, now=None):
    """Assemble the full in-memory picture of one day's live board.

    ``preload_timing_cache()`` runs FIRST (mandatory before board-wide chain
    math). One slimmed legs query in the board's `_base_legs_qs` shape carries
    every prefetch the detectors read (controlling flights, status history for
    the recorded-pickup re-anchor, active KEOI, pending-refund annotation).
    ``now`` is injected for testability; farm roster/ledger are deliberately
    NOT built here — they load lazily only when a Stage-B2 farm tier is
    reached. Read-only."""
    from django.db.models import Exists, OuterRef, Prefetch
    from reservations.models import Leg, LegKeoi, LegStatus, RefundRequest
    from drivers.models import Driver, DriverVehicleAssignment
    from ops.models import OperationalTask
    from dispatching import feasibility_guards as fg
    from dispatching.board_validation import board_turn_bands
    from dispatching.scheduler import (
        build_driver_schedules, build_sharer_partners, load_all_driver_vtypes,
        preload_timing_cache,
    )

    preload_timing_cache()  # FIRST — one query, kills per-pair metric lookups

    now = now or timezone.now()
    now_local = timezone.localtime(now).replace(tzinfo=None)

    legs = list(
        Leg.objects.filter(pickup_date=target_date)
        .exclude(reservation__status="cancelled").exclude(status="cancelled")
        .select_related(
            "reservation", "reservation__customer", "reservation__vehicle",
            "vehicle",
            # for Leg.is_vip agency-keyword check without a query
            "reservation__travel_agent", "reservation__travel_agent__agency",
            "driver", "driver__profile", "flight_information",
        )
        .prefetch_related(
            "legflight_set__flight",
            # build_driver_schedules reads per-leg stop counts and the
            # reservation's payment_status — without these two prefetches it
            # N+1s (2 queries × leg), the dominant cost at 80 legs. Same
            # prefetches _base_legs_qs carries.
            "legstop_set",
            "reservation__payments",
            Prefetch("status_history",
                     queryset=LegStatus.objects.order_by("-timestamp")),
            Prefetch("keoi_flags",
                     queryset=LegKeoi.objects.filter(closed_at__isnull=True),
                     to_attr="active_keoi_list"),
        )
        .annotate(
            has_pending_refund=Exists(
                RefundRequest.objects.filter(
                    reservation_id=OuterRef("reservation_id"),
                    status__in=["requested", "processing", "approved"],
                )
            )
        )
        .order_by("pickup_time", "id")
    )
    legs_by_id = {l.id: l for l in legs}

    # Deployable pool: DVA holders + anyone holding a leg. The union makes a
    # zero-leg rostered driver a valid receiver ("shift to later-starting
    # driver") while a leg-holding driver without a DVA row still gets a board.
    # ONE DVA fetch feeds pool + per-unit caps + vtypes + sharer partners
    # (query budget: full compute ≤15 queries, pinned by test).
    dva_rows = list(
        DriverVehicleAssignment.objects.filter(date=target_date)
        .select_related("vehicle__vehicle_type")
    )
    dva_driver_ids = {r.driver_id for r in dva_rows}
    leg_holder_ids = {l.driver_id for l in legs if l.driver_id}
    drivers = list(
        Driver.objects.filter(id__in=(dva_driver_ids | leg_holder_ids))
        .select_related("profile")
        .prefetch_related("weekly_schedule", "date_overrides")
    )
    drivers_by_id = {d.id: d for d in drivers}
    deployable = [d for d in drivers
                  if d.driver_type == "inhouse" and getattr(d, "is_active", True)]

    schedules = build_driver_schedules(legs, deployable, target_date,
                                       dva_rows=dva_rows)
    # build_driver_schedules already resolved the per-unit caps — harvest them
    # instead of re-querying.
    vehicle_caps = {did: s.vehicle_cap for did, s in schedules.items()
                    if s.vehicle_cap}
    driver_vtypes = load_all_driver_vtypes(target_date, rows=dva_rows)
    sharer_partners = build_sharer_partners(set(schedules), target_date,
                                            rows=dva_rows)

    # GENERATION windows: enforce_cap=True — the advisor is an automatic path
    # and must not auto-build 18-hour days. The APPLY stage re-resolves with
    # enforce_cap=False (manual-sovereign); that split is the caller's, per the
    # plan. Sources are tagged so a stub-window rejection can say so honestly:
    # "observed-history window (provisional), not a configured shift".
    windows, window_sources = {}, {}
    for d in deployable:
        eff = d.get_effective_availability(target_date)
        mh = eff.get("max_hours")
        cfg = {"start": eff.get("start_hour"), "end": eff.get("end_hour"),
               "max_hours": (float(mh) if mh else None),
               "flexible": bool(eff.get("flexible"))}
        windows[d.id] = fg.get_effective_window(d.id, configured=cfg,
                                                enforce_cap=True)
        window_sources[d.id] = (
            "stub" if (fg.USE_STUB_WINDOWS and d.id in fg.STUB_DRIVER_WINDOWS)
            else "configured")

    # Earliest picked-up tap per leg, naive local (the board's convention):
    # status_history is prefetched newest-first, so overwriting keeps the
    # EARLIEST — the true start the re-anchor hangs off.
    picked_up_by_leg = {}
    for l in legs:
        for sh in l.status_history.all():
            if sh.status == "picked-up":
                picked_up_by_leg[l.id] = (
                    timezone.localtime(sh.timestamp).replace(tzinfo=None))

    vip_leg_ids = {l.id for l in legs if l.is_vip}
    pending_refund_leg_ids = {l.id for l in legs
                              if getattr(l, "has_pending_refund", False)}
    keoi_leg_ids = {l.id for l in legs if getattr(l, "active_keoi_list", None)}

    # Open conflict/turn/assignment tasks in ONE query, for card linking only
    # (the advisor never creates or closes tasks).
    open_tasks_by_leg = {}
    task_rows = (OperationalTask.objects.filter(
        leg_id__in=list(legs_by_id),
        status__in=OperationalTask.OPEN_STATUSES,
        task_type__in=[OperationalTask.TaskType.DRIVER_CONFLICT,
                       OperationalTask.TaskType.TIGHT_TURN,
                       OperationalTask.TaskType.DRIVER_ASSIGNMENT])
        .order_by("id").values_list("leg_id", "id", "task_type"))
    for leg_id, task_id, task_type in task_rows:
        open_tasks_by_leg.setdefault(leg_id, {}).setdefault(task_type, task_id)

    board = BoardState(
        target_date=target_date, now=now, now_local=now_local,
        legs=legs, legs_by_id=legs_by_id, schedules=schedules,
        drivers_by_id=drivers_by_id, windows=windows,
        window_sources=window_sources, vehicle_caps=vehicle_caps,
        driver_vtypes=driver_vtypes, sharer_partners=sharer_partners,
        picked_up_by_leg=picked_up_by_leg, vip_leg_ids=vip_leg_ids,
        pending_refund_leg_ids=pending_refund_leg_ids, keoi_leg_ids=keoi_leg_ids,
        open_tasks_by_leg=open_tasks_by_leg,
        baseline_bands=board_turn_bands(schedules, target_date),
        prev_tail=_load_prev_tail(target_date, schedules),
    )
    return board


def _load_prev_tail(target_date, schedules):
    """Guard 3: when the date has tail legs (00:00–02:00) on a deployable
    driver, load that driver's PREVIOUS-evening legs so the tail chain-links to
    its real predecessor via absolute datetimes. Slots are built under the
    previous date, so their clear datetimes cross midnight naturally. Two extra
    queries, only on nights that need them."""
    from django.db.models import Prefetch
    from reservations.models import Leg, LegStatus
    from dispatching.overnight_arrival import NIGHT_TAIL_END_HOUR
    from dispatching.scheduler import _make_sim_slot

    tail_driver_ids = {
        did for did, sched in schedules.items()
        if any(s.pickup_time and s.pickup_time.hour < NIGHT_TAIL_END_HOUR
               for s in sched.slots)
    }
    if not tail_driver_ids:
        return {}

    prev_date = target_date - timedelta(days=1)
    prev_legs = list(
        Leg.objects.filter(pickup_date=prev_date,
                           driver_id__in=tail_driver_ids,
                           pickup_time__gte=dt_time(_PREV_EVENING_FROM_HOUR, 0))
        .exclude(reservation__status="cancelled").exclude(status="cancelled")
        .select_related("reservation", "flight_information")
        .prefetch_related(
            "legflight_set__flight",
            Prefetch("status_history",
                     queryset=LegStatus.objects.order_by("-timestamp")),
        )
        .order_by("pickup_time", "id")
    )
    out = {}
    for l in prev_legs:
        picked = None
        for sh in l.status_history.all():   # newest-first; keep earliest tap
            if sh.status == "picked-up":
                picked = timezone.localtime(sh.timestamp).replace(tzinfo=None)
        out.setdefault(l.driver_id, []).append((_make_sim_slot(l, prev_date), picked))
    return out


# ════════════════════════════════════════════════════════════════════════════
# THE ONE CLOCK-SELECTION FUNCTION (hazard guard 1)
# ════════════════════════════════════════════════════════════════════════════

def advisor_clear_dt(leg, target_date, picked_up_dt=None, mode="detection"):
    """When does this leg release its driver, on the requested clock?

    The verified state matrix — the ONLY clock selection the advisor uses:
      * completed / cancelled           -> None (dropped from projection);
      * overnight-AMBIGUOUS arrival     -> None (ABSTAIN — which night the
        flight lands is unconfirmed, so no clear time is knowable; the caller's
        card is "confirm the takeoff date", never a projection);
      * picked-up with a recorded tap   -> detection: chain_clear_dt_from_actual
        (the dwell is a fact, only the drive remains); planning:
        max(static, actual) — never optimistic when seating future work;
      * picked-up WITHOUT a recorded tap, on-the-way, on-location, or any
        future leg                      -> chain_clear_dt (the dwell is not yet
        a fact; the founder static planning model holds).
    ``estimate_job_end_time`` is NEVER consulted here (p75 metrics are exactly
    what CHAIN_STATIC_TIMING banned from feasibility). An unacked pickup-time
    change is evaluated by the flight-change detector under BOTH times — the
    card IS the flight change; this function always reads the CURRENT time.
    """
    from dispatching.overnight_arrival import leg_needs_overnight_confirmation
    from dispatching.scheduler import chain_clear_dt, chain_clear_dt_from_actual

    if mode not in ("detection", "planning"):
        raise ValueError(f"unknown clock mode {mode!r}")
    status = getattr(leg, "status", None) or ""
    if status in ("completed", "cancelled"):
        return None
    try:
        if leg_needs_overnight_confirmation(leg):
            return None
    except Exception:
        pass  # bare/synthetic legs without flight plumbing: not ambiguous
    if status == "picked-up" and picked_up_dt is not None:
        actual = chain_clear_dt_from_actual(leg, picked_up_dt)
        if mode == "detection":
            return actual
        return max(chain_clear_dt(leg, target_date), actual)
    return chain_clear_dt(leg, target_date)


def planning_clock_schedules(schedules, legs_by_id, picked_up_by_leg,
                             target_date):
    """Guard 1, planning half: a schedules map re-anchored on the PLANNING
    clock — every slot whose leg is under way with a recorded tap clears at
    ``advisor_clear_dt(mode='planning')`` = max(static, actual), so future
    placements are never seated behind a demonstrably-late driver while an
    EARLY pickup never makes the planning clock optimistic (max keeps the
    static value). Schedules that need no re-anchor are returned as the SAME
    objects (shared, read-only); only touched slots/schedules are cloned.
    Feed the result to check_feasibility / find_swaps /
    validate_post_move_board — every path that seats future work."""
    from dataclasses import replace as _dc_replace

    if not picked_up_by_leg:
        return schedules
    out = dict(schedules)
    for did, sched in schedules.items():
        new_slots, changed = [], False
        for s in sched.slots:
            picked = picked_up_by_leg.get(s.leg_id)
            leg = legs_by_id.get(s.leg_id)
            if picked is not None and leg is not None:
                plan_dt = advisor_clear_dt(leg, target_date,
                                           picked_up_dt=picked,
                                           mode="planning")
                if plan_dt is not None and (s.chain_clear_dt is None
                                            or plan_dt > s.chain_clear_dt):
                    s = _dc_replace(s, chain_clear_dt=plan_dt)
                    changed = True
            new_slots.append(s)
        if changed:
            out[did] = _dc_replace(sched, slots=new_slots)
    return out


def _planning(board):
    """(planning schedules, planning baseline bands) for this board — the
    guard-1 planning clock every future-placement path validates on, built
    lazily once per BoardState (same caching idiom as the farm context).
    The baseline is swept over the SAME re-anchored schedules so the
    validate_post_move_board band diff compares like with like."""
    if getattr(board, "_planning_built", False):
        return board._planning_scheds, board._planning_bands
    from dispatching.board_validation import board_turn_bands

    scheds = planning_clock_schedules(board.schedules, board.legs_by_id,
                                      board.picked_up_by_leg,
                                      board.target_date)
    if scheds is board.schedules and board.baseline_bands:
        bands = board.baseline_bands
    else:
        bands = board_turn_bands(scheds, board.target_date)
    board._planning_scheds, board._planning_bands = scheds, bands
    board._planning_built = True
    return scheds, bands


# ════════════════════════════════════════════════════════════════════════════
# SMALL SHARED READERS (pure; no queries)
# ════════════════════════════════════════════════════════════════════════════

def _minutes(delta):
    return int(delta.total_seconds() // 60)


def _naive_local(dt):
    if dt is None:
        return None
    if timezone.is_aware(dt):
        return timezone.localtime(dt).replace(tzinfo=None)
    return dt


def _fmt_t(dt):
    return dt.strftime("%I:%M %p").lstrip("0") if dt else "?"


def _is_active(leg):
    return (getattr(leg, "status", None) or "") not in ("completed", "cancelled")


def _gps_state(leg):
    """The persisted Samsara sweep snapshot on this leg (read-only; only the
    driver's active-target leg carries it — the sweep blanks all others)."""
    return {
        "status": getattr(leg, "dispatch_risk_status", "") or "",
        "eta": getattr(leg, "dispatch_eta_minutes", None),
        "reason": getattr(leg, "dispatch_risk_reason", "") or "",
        "target": getattr(leg, "dispatch_eta_target", "") or "",
        "target_time": _naive_local(getattr(leg, "dispatch_eta_target_time", None)),
        "fresh": bool(getattr(leg, "dispatch_eta_is_fresh", False)),
        "moving": getattr(leg, "dispatch_is_moving", None),
    }


def _gps_for_pickup_fold(gps):
    """The gps_status argument for pickup_policy.pickup_risk: only a FRESH
    snapshot targeting a pickup deadline counts (the board's own gate)."""
    if gps["fresh"] and gps["target"] in _GPS_PICKUP_TARGETS:
        return gps["status"]
    return ""


def _clock_flags(leg, now_local, picked_dt):
    """The board's clock-overdue flags for one leg (mirrors the
    _truthful_pill_span rules on pickup_policy primitives): overdue = the
    EXPECTED pickup (flight-aware, pickup_policy.pickup_expected_dt) has passed
    with nothing recorded; stalled = not even an en-route report; stale =
    guard 2, aged past OVERDUE_STALE_MIN into a hygiene problem."""
    from dispatching import pickup_policy

    out = {"overdue": False, "stalled": False, "mins": 0, "stale": False,
           "expected_basis": ""}
    if not _is_active(leg):
        return out
    if picked_dt is not None or (getattr(leg, "status", "") == "picked-up"):
        return out
    expected, basis = pickup_policy.pickup_expected_dt(leg, aware=False)
    if expected is None:
        return out
    mins = _minutes(now_local - expected)
    if mins < _CLOCK_OVERDUE_GRACE_MIN:
        return out
    out.update({
        "mins": mins,
        "stale": pickup_policy.is_overdue_stale(mins),
        "overdue": True,
        "stalled": (getattr(leg, "status", "") or "")
        not in ("on-the-way", "on-location"),
        "expected_basis": basis,
    })
    return out


def _effective_pickup_dt(leg, target_date):
    """The DETECTION-clock moment the driver is truly due at this pickup.

    Booked time for everything — except a flight-tracked arrival whose
    deadline (gate + ARRIVAL_MEET_GRACE_MIN) moved LATER than booked: reality
    (a delayed plane) relaxes the deadline, so nobody is late for a flight
    still in the air. An EARLY flight never pulls it earlier — the booked time
    is the guest commitment and the plan's anchor."""
    from dispatching import pickup_policy

    booked = datetime.combine(target_date, leg.pickup_time)
    try:
        if leg.is_flight_tracked_arrival():
            deadline, _ = pickup_policy.pickup_deadline(leg, aware=False)
            if deadline is not None and deadline > booked:
                return deadline
    except Exception:
        pass
    return booked


def _flight_divergence_min(leg):
    """Signed minutes the controlling flight's best arrival is off the booked
    pickup (positive = landing later), or None when unjudgeable. Uses
    pickup_policy.controlling_arrival_dt, which already guards against a
    wrong-dated flight record hijacking the deadline."""
    from dispatching import pickup_policy

    try:
        if not leg.is_flight_tracked_arrival():
            return None
    except Exception:
        return None
    arr = pickup_policy.controlling_arrival_dt(leg, aware=False)
    if arr is None or leg.pickup_time is None:
        return None
    booked = datetime.combine(leg.pickup_date, leg.pickup_time)
    return _minutes(arr - booked)


def _leg_route(leg):
    pu = (getattr(leg, "pickup_location", "") or "?").strip()
    do = (getattr(leg, "dropoff_location", "") or "?").strip()
    return f"{pu} → {do}"


def _driver_name(board, driver_id):
    sched = board.schedules.get(driver_id)
    if sched is not None and sched.driver_name:
        return sched.driver_name
    d = board.drivers_by_id.get(driver_id)
    return str(d) if d is not None else f"driver {driver_id}"


def _task_for(board, leg_ids, preferred_types):
    """First open task id linked to any of these legs, preferring the types
    that match the card's kind. Read-only; one dict lookup per leg."""
    for t in preferred_types:
        for leg_id in leg_ids:
            task_id = (board.open_tasks_by_leg.get(leg_id) or {}).get(t)
            if task_id:
                return task_id
    for leg_id in leg_ids:
        by_type = board.open_tasks_by_leg.get(leg_id) or {}
        for task_id in by_type.values():
            return task_id
    return None


def _downstream_breaks(board, driver_id, anchor_slot, projected_clear):
    """Walk the driver's chain after ``anchor_slot`` with the anchor clearing at
    ``projected_clear``: the first pickup whose slack goes NEGATIVE breaks, its
    lateness propagates (a job started N min late clears N min late), and the
    walk stops at the first pickup that ABSORBS the delay. Returns
    [(slot, slack_min), ...] for the broken pickups, in order.

    Turnaround arithmetic is the SAME formula the assignment engine uses
    (chain_repo_minutes + required_turnaround) — never raw clock gaps. The next
    pickup's due moment is the detection-clock effective time, so a delayed
    flight downstream absorbs rather than falsely breaking."""
    from dispatching import feasibility_guards as fg
    from dispatching.scheduler import chain_repo_minutes, _slot_chain_end

    sched = board.schedules.get(driver_id)
    if sched is None:
        return []
    slots = sorted(sched.slots, key=lambda s: (s.pickup_time, s.leg_id))
    try:
        idx = next(i for i, s in enumerate(slots)
                   if s.leg_id == anchor_slot.leg_id)
    except StopIteration:
        return []

    breaks = []
    prev_slot, carry_clear = anchor_slot, projected_clear
    for s in slots[idx + 1:]:
        leg = board.legs_by_id.get(s.leg_id)
        if leg is not None and not _is_active(leg):
            continue
        repo = chain_repo_minutes(prev_slot.dropoff_location, s.pickup_location,
                                  prev_slot.dropoff_category, s.pickup_category)
        req = fg.required_turnaround(
            repo, fg.is_airport_arrival(s.trip_type, s.pickup_category),
            same_terminal=(prev_slot.dropoff_category == s.pickup_category))
        due = (_effective_pickup_dt(leg, board.target_date) if leg is not None
               else datetime.combine(board.target_date, s.pickup_time))
        slack = _minutes(due - (carry_clear + timedelta(minutes=req)))
        if slack >= 0:
            break  # absorbed — everything further is the static board's story
        breaks.append((s, slack))
        carry_clear = (_slot_chain_end(s, board.target_date)
                       + timedelta(minutes=-slack))
        prev_slot = s
    return breaks


def _add(out, d):
    """Dedup on the stable id; on a collision the more severe verdict wins
    (deterministic — detectors run in a fixed order)."""
    old = out.get(d.id)
    if old is None or _SEV_RANK.get(d.severity, 9) < _SEV_RANK.get(old.severity, 9):
        out[d.id] = d


# ════════════════════════════════════════════════════════════════════════════
# DETECTION
# ════════════════════════════════════════════════════════════════════════════

def detect_disruptions(board):
    """Scan the assembled board and return ranked Disruptions (pure, in-memory,
    no queries). Ranking: severity band first, then time-to-impact, then id —
    fully deterministic. Truncation to ADVISOR_MAX_DISRUPTIONS analyzed cards
    is the caller's (compute_advisor_state, Stage B2): detection itself always
    returns everything it found."""
    if not ADVISOR_ENABLED:
        return []
    out = {}
    claimed_prev_ids = _detect_flight_changes(board, out)
    _detect_overlaps(board, out, claimed_prev_ids)
    _detect_late_cascades(board, out)
    _detect_overruns(board, out)
    _detect_unassigned(board, out)
    return sorted(
        out.values(),
        key=lambda d: (_SEV_RANK.get(d.severity, 9),
                       d.impact_dt or datetime.max, d.id))


def _detect_flight_changes(board, out):
    """Flight facts: unacked pickup-time changes and controlling-flight vs
    booked divergence >= ADVISOR_FLIGHT_MISMATCH_MIN. Severity = chain
    consequence: the turn OUT of a delayed arrival breaking is what escalates
    (the turn INTO it only relaxes — never carded). Affiliate-held legs are
    detection-scope only (guard 7): the fact is reported, no chain math, and
    the phrasing says to call the affiliate. Overnight-ambiguous arrivals
    ABSTAIN (guard 1): the card is "confirm the takeoff date".

    Returns the set of leg ids whose DELAYED flight claims their turn-out pair,
    so the overlap detector reports the cause, not the symptom."""
    from dispatching.overnight_arrival import leg_needs_overnight_confirmation
    from dispatching import pickup_policy
    from dispatching.board_validation import turn_slack_minutes
    from dispatching.scheduler import _make_sim_slot

    claimed = set()
    slot_index = {}   # leg_id -> (driver_id, slot, following_slot|None)
    for did, sched in board.schedules.items():
        slots = sorted(sched.slots, key=lambda s: (s.pickup_time, s.leg_id))
        for i, s in enumerate(slots):
            slot_index[s.leg_id] = (did, s,
                                    slots[i + 1] if i + 1 < len(slots) else None)

    for leg in board.legs:
        if not _is_active(leg) or getattr(leg, "pickup_time", None) is None:
            continue
        status = getattr(leg, "status", "") or ""
        if status == "picked-up" or leg.id in board.picked_up_by_leg:
            continue  # the pickup already happened; nothing to re-time

        # ── Overnight-ambiguous: abstain from ALL projection ──
        ambiguous = False
        try:
            ambiguous = bool(leg_needs_overnight_confirmation(leg))
        except Exception:
            ambiguous = False
        if ambiguous:
            booked = datetime.combine(board.target_date, leg.pickup_time)
            _add(out, Disruption(
                id=f"flight_change:{leg.id}", kind="flight_change",
                severity="warning", basis=BASIS_FLIGHT,
                headline=(f"Confirm which night the {_fmt_t(booked)} "
                          f"arrival lands"),
                narrative=(
                    "After-midnight flight-tracked arrival with no confirmed "
                    "takeoff date — the same flight number lands every night, "
                    "so the pickup date is unverified. The advisor abstains "
                    "from projecting this chain until the date is confirmed "
                    "via the overnight-confirmation flow."),
                leg_ids=[leg.id], anchor_leg_id=leg.id,
                driver_id=leg.driver_id, impact_dt=booked,
                task_id=_task_for(board, [leg.id],
                                  ("driver_conflict", "tight_turn")),
                abstain=True,
                details={"reason": "overnight_unconfirmed"},
            ))
            continue

        unacked = bool(getattr(leg, "has_unacked_time_change", False))
        div = _flight_divergence_min(leg)
        flagged_div = div is not None and abs(div) >= ADVISOR_FLIGHT_MISMATCH_MIN
        if not (unacked or flagged_div):
            continue

        booked = datetime.combine(board.target_date, leg.pickup_time)
        eff = _effective_pickup_dt(leg, board.target_date)
        in_house = leg.driver_id in board.schedules if leg.driver_id else False
        holder = board.drivers_by_id.get(leg.driver_id) if leg.driver_id else None
        is_affiliate = (getattr(holder, "driver_type", "") == "affiliate")

        # ── Chain consequence: the turn OUT of this leg, on the detection
        # clock (chain_clear_dt is already flight-anchored on both clocks). ──
        slack_out, broken_next_id, impact_dt = None, None, eff
        if in_house and leg.id in slot_index:
            did, slot, following = slot_index[leg.id]
            if following is not None:
                slack_out = turn_slack_minutes(slot, following,
                                               board.target_date)
                # Unacked change: evaluate BOTH times — the worst consequence
                # of old vs current pickup time drives the severity.
                old_t = getattr(leg, "pickup_time_was", None)
                if unacked and old_t and old_t != leg.pickup_time:
                    old_leg = copy.copy(leg)
                    old_leg.pickup_time = old_t
                    old_slot = _make_sim_slot(old_leg, board.target_date)
                    old_slack = turn_slack_minutes(old_slot, following,
                                                   board.target_date)
                    if old_slack is not None:
                        slack_out = (old_slack if slack_out is None
                                     else min(slack_out, old_slack))
                if slack_out is not None and slack_out < 0:
                    broken_next_id = following.leg_id
                    impact_dt = datetime.combine(board.target_date,
                                                 following.pickup_time)
                    # CAUSE OVER SYMPTOM. This card is already reporting that
                    # the turn out of this leg is broken, so the overlap
                    # detector must not file a second card for the same pair.
                    # That holds however the leg moved — a delayed plane OR an
                    # unacknowledged pickup-time change. Claiming only on a late
                    # flight left the retime case producing two cards with the
                    # same headline, the same legs and the same single fix.
                    claimed.add(leg.id)

        severity = "watch"
        if slack_out is not None:
            if slack_out < 0:
                severity = "critical"
            elif slack_out < pickup_policy.TURN_TIGHT_SLACK_MIN:
                severity = "warning"

        bits = []
        if flagged_div:
            direction = "later" if div > 0 else "early"
            bits.append(f"Booked {_fmt_t(booked)}; the controlling flight now "
                        f"lands {abs(div)} min {direction} "
                        f"({_fmt_t(booked + timedelta(minutes=div))}).")
        if unacked:
            bits.append("The pickup time moved and no dispatcher has "
                        "acknowledged the change yet.")
        if broken_next_id is not None:
            bits.append(f"That shift breaks the turn out: the "
                        f"{_fmt_t(impact_dt)} pickup goes {abs(slack_out)} min "
                        f"short.")
        elif slack_out is not None and severity == "warning":
            bits.append(f"The turn out to the next job tightens to "
                        f"{slack_out} min.")
        if is_affiliate:
            bits.append(f"Assigned to affiliate "
                        f"{_driver_name(board, leg.driver_id)} — call them "
                        f"first; you can't reliably pull back same-day, and "
                        f"board assignment is not acceptance.")

        if flagged_div and div > 0:
            head = (f"Match the {_fmt_t(booked)} pickup to its flight — now "
                    f"landing {_fmt_t(booked + timedelta(minutes=div))}")
        elif flagged_div:
            head = (f"Flight for the {_fmt_t(booked)} pickup is landing "
                    f"{abs(div)} min early")
        else:
            head = f"Acknowledge the time change on the {_fmt_t(booked)} pickup"

        leg_ids = [leg.id] + ([broken_next_id] if broken_next_id else [])
        _add(out, Disruption(
            id=f"flight_change:{leg.id}", kind="flight_change",
            severity=severity, basis=BASIS_FLIGHT, headline=head,
            narrative=" ".join(bits) or f"{_leg_route(leg)}.",
            leg_ids=leg_ids, anchor_leg_id=leg.id, driver_id=leg.driver_id,
            impact_dt=impact_dt,
            task_id=_task_for(board, leg_ids,
                              ("driver_conflict", "tight_turn")),
            details={"divergence_min": div, "unacked": unacked,
                     "slack_out": slack_out, "affiliate": is_affiliate},
        ))
    return claimed


def _detect_overlaps(board, out, claimed_prev_ids):
    """Adjacent-pair chain slack on the DETECTION clock (recorded pickups
    re-anchor; a delayed flight relaxes the turn INTO its arrival). The prime
    directive is enforced structurally:

      * detection slack < 0                       -> critical (cannot be done);
      * detection < TURN_TIGHT_SLACK_MIN while the PLANNING clock says >= it
                                                  -> warning (reality DEGRADED a
                                                     clean turn — always a
                                                     recorded-pickup fact);
      * planned-tight (+0/+3 founder patterns)    -> NO CARD, ever.

    A pair whose compression is a delayed flight's doing is skipped here — the
    flight_change card owns it (cause over symptom). A pair whose next pickup
    carries fresh on_time GPS is skipped — the vehicle is demonstrably
    positioned to make it, and a chip that shouts over live telemetry is how
    boards lose trust."""
    from dispatching import pickup_policy
    from dispatching.board_validation import (
        board_turn_bands, turn_slack_minutes, _slot_leg_shim)

    detection = board_turn_bands(board.schedules, board.target_date,
                                 picked_up_by_leg=board.picked_up_by_leg)
    baseline = board.baseline_bands or board_turn_bands(board.schedules,
                                                        board.target_date)

    def _consider(driver_id, prev_slot_leg_id, next_leg_id, slack,
                  planning_slack, prev_picked, cross_midnight=False):
        if slack is None:
            return
        next_leg = board.legs_by_id.get(next_leg_id)
        if next_leg is not None:
            if not _is_active(next_leg):
                return
            if (getattr(next_leg, "status", "") == "picked-up"
                    or next_leg_id in board.picked_up_by_leg):
                return  # the pickup was made — the turn resolved itself
            # Fresh on_time GPS on the next pickup: reality says he makes it.
            gps = _gps_state(next_leg)
            if _gps_for_pickup_fold(gps) == "on_time":
                return
            due = _effective_pickup_dt(next_leg, board.target_date)
            # Detection relax: a delayed flight moves the arrival's deadline
            # out; the turn INTO it gains that slack.
            booked = datetime.combine(board.target_date, next_leg.pickup_time)
            relax = max(0, _minutes(due - booked))
            if relax:
                slack = slack + relax
                planning_slack = (planning_slack + relax
                                  if planning_slack is not None else None)
        else:
            due = None
        impact = due
        if impact is not None and impact <= board.now_local:
            return  # impact already passed — live lateness is late_cascade's job
        if prev_slot_leg_id in claimed_prev_ids:
            return  # the delayed flight is the cause; its card carries this pair

        if slack < 0:
            severity = "critical"
        elif (slack < pickup_policy.TURN_TIGHT_SLACK_MIN
              and planning_slack is not None
              and planning_slack >= pickup_policy.TURN_TIGHT_SLACK_MIN):
            severity = "warning"
        else:
            return  # legal turn — planned-tight days are the founder's call

        basis = (BASIS_RECORDED_PICKUP if prev_picked else BASIS_CLOCK_ONLY)
        name = _driver_name(board, driver_id)
        if severity == "critical":
            head = (f"Untangle {name}'s {_fmt_t(impact)} turn — "
                    f"{abs(slack)} min short")
        else:
            head = (f"Watch {name}'s {_fmt_t(impact)} turn — down to "
                    f"{slack} min")
        planned_bit = ""
        if planning_slack is not None:
            planned_bit = (f" (planned {planning_slack} min"
                           + (", re-anchored on the recorded pickup)"
                              if prev_picked else ")"))
        narrative = (
            f"Chain math (same formula as auto-assign): previous job's clear "
            f"+ reposition + turnaround leaves {slack} min against the "
            f"{_fmt_t(impact)} pickup{planned_bit}."
            + (" Chain-linked across midnight to last night's final job."
               if cross_midnight else ""))
        leg_ids = [prev_slot_leg_id, next_leg_id]
        _add(out, Disruption(
            id=f"overlap:{prev_slot_leg_id}:{next_leg_id}", kind="overlap",
            severity=severity, basis=basis, headline=head, narrative=narrative,
            leg_ids=leg_ids, anchor_leg_id=next_leg_id, driver_id=driver_id,
            impact_dt=impact,
            task_id=_task_for(board, leg_ids,
                              ("driver_conflict", "tight_turn")),
            details={"slack": slack, "planning_slack": planning_slack,
                     "cross_midnight": cross_midnight},
        ))

    for (did, prev_id, next_id), info in detection.items():
        planning = (baseline.get((did, prev_id, next_id)) or {}).get("slack")
        _consider(did, prev_id, next_id, info["slack"], planning,
                  prev_picked=(prev_id in board.picked_up_by_leg))

    # ── Guard 3: overnight tail — the day's first slot in the 00:00–02:00
    # window chains to the PREVIOUS evening via absolute datetimes. ──
    from dispatching.overnight_arrival import NIGHT_TAIL_END_HOUR
    for did, tail_pairs in (board.prev_tail or {}).items():
        sched = board.schedules.get(did)
        if sched is None or not tail_pairs:
            continue
        slots = sorted(sched.slots, key=lambda s: (s.pickup_time, s.leg_id))
        first = next((s for s in slots
                      if s.pickup_time.hour < NIGHT_TAIL_END_HOUR), None)
        if first is None:
            continue
        prev_slot, prev_picked_dt = max(
            tail_pairs, key=lambda p: (p[0].pickup_time, p[0].leg_id))
        # prev_slot was built under the PREVIOUS date, so its chain_clear_dt is
        # an absolute datetime that crosses midnight naturally; the one shared
        # slack formula handles the rest.
        slack = turn_slack_minutes(
            prev_slot, first, board.target_date,
            prev_leg=(_slot_leg_shim(prev_slot)
                      if prev_picked_dt is not None else None),
            prev_picked_up_dt=prev_picked_dt)
        planning = turn_slack_minutes(prev_slot, first, board.target_date)
        _consider(did, prev_slot.leg_id, first.leg_id, slack, planning,
                  prev_picked=(prev_picked_dt is not None),
                  cross_midnight=True)


def _detect_late_cascades(board, out):
    """A driver demonstrably running late for a pickup, and what it knocks over.

    Anchors, in precedence order (the promoted pickup_risk fold — guard 4):
      * fresh GPS at_risk/late on a pickup target   -> basis gps_fresh;
      * clock-overdue with no status motion         -> basis clock_only (or
        gps_stale_parked when a stale parked snapshot is the only telemetry);
      * fresh GPS on_time SUPPRESSES the clock; stale/absent GPS never anchors.
    Overdue past OVERDUE_STALE_MIN -> guard-2 hygiene card, never a recovery
    disruption. The anchor's delay is propagated down the chain with the
    engine's own turnaround formula; broken pickups join the card."""
    from dispatching import pickup_policy
    from dispatching.scheduler import chain_clear_dt

    for did, sched in board.schedules.items():
        for slot in sorted(sched.slots, key=lambda s: (s.pickup_time, s.leg_id)):
            leg = board.legs_by_id.get(slot.leg_id)
            if leg is None or not _is_active(leg):
                continue
            picked_dt = board.picked_up_by_leg.get(leg.id)
            if picked_dt is not None or getattr(leg, "status", "") == "picked-up":
                continue  # under way — the overrun detector owns it now

            gps = _gps_state(leg)
            clock = _clock_flags(leg, board.now_local, picked_dt)
            gps_fold = _gps_for_pickup_fold(gps)
            gps_negative = (gps_fold in ("at_risk", "late")
                            and gps["target"] == "pickup")

            # ── Guard 2: aged-out overdue is hygiene, not a live risk ──
            if clock["stale"] and not gps_negative:
                eff = _effective_pickup_dt(leg, board.target_date)
                basis = (BASIS_GPS_STALE_PARKED
                         if (gps["status"] and not gps["fresh"]
                             and gps["moving"] is False)
                         else BASIS_CLOCK_ONLY)
                _add(out, Disruption(
                    id=f"late_cascade:{leg.id}", kind="late_cascade",
                    severity="watch", basis=basis,
                    headline=(f"Stale status — chase the button on the "
                              f"{_fmt_t(eff)} pickup"),
                    narrative=(
                        f"{clock['mins']} min past the expected pickup "
                        f"({clock['expected_basis']}) with no status recorded. "
                        f"Past {pickup_policy.OVERDUE_STALE_MIN} min this is an "
                        f"unpressed button, not a live risk — confirm the ride "
                        f"and get the status history fixed."),
                    leg_ids=[leg.id], anchor_leg_id=leg.id, driver_id=did,
                    impact_dt=eff,
                    task_id=_task_for(board, [leg.id],
                                      ("driver_conflict", "tight_turn")),
                    abstain=True, hygiene=True,
                    details={"overdue_min": clock["mins"]},
                ))
                continue

            risk = pickup_policy.pickup_risk(
                pickup_overdue=clock["overdue"],
                pickup_stalled=clock["stalled"],
                overdue_mins=clock["mins"],
                gps_status=gps_fold,
                gps_eta_mins=gps["eta"], gps_reason=gps["reason"])
            if not risk["tier"]:
                continue  # incl. fresh on_time suppressing the clock
            if risk["source"] == "gps" and not gps_negative:
                continue  # GPS 'watch' is not a negative signal (guard 4)

            eff = _effective_pickup_dt(leg, board.target_date)
            if risk["source"] == "gps":
                target_dt = gps["target_time"] or eff
                projected_pickup = (board.now_local
                                    + timedelta(minutes=gps["eta"] or 0))
                shift = max(0, _minutes(projected_pickup - max(target_dt, eff)))
                basis = BASIS_GPS_FRESH
            else:
                shift = max(0, clock["mins"])
                basis = (BASIS_GPS_STALE_PARKED
                         if (gps["status"] and not gps["fresh"]
                             and gps["moving"] is False)
                         else BASIS_CLOCK_ONLY)

            projected_clear = (chain_clear_dt(leg, board.target_date)
                               + timedelta(minutes=shift))
            breaks = _downstream_breaks(board, did, slot, projected_clear)

            if risk["source"] == "gps":
                severity = ("critical" if (gps_fold == "late" or breaks)
                            else "warning")
            else:
                if risk["tier"] == "critical" or breaks:
                    severity = "critical"
                else:
                    continue  # overdue-but-moving with nothing downstream:
                              # the board pill's amber is the right volume

            name = _driver_name(board, did)
            first_break = breaks[0] if breaks else None
            impact = (datetime.combine(board.target_date,
                                       first_break[0].pickup_time)
                      if first_break else eff)
            if first_break:
                head = (f"Line up cover — {name}'s lateness breaks the "
                        f"{_fmt_t(impact)} pickup")
            else:
                head = f"Get eyes on {name} — {_fmt_t(eff)} pickup at risk"
            bits = []
            if risk["source"] == "gps":
                bits.append(risk["reason"])  # dispatch_risk_reason, verbatim
            else:
                bits.append(f"{clock['mins']} min past the expected pickup "
                            f"({clock['expected_basis']}) with "
                            + ("no en-route status."
                               if clock["stalled"] else "the driver en route."))
            if breaks:
                broken_times = ", ".join(
                    _fmt_t(datetime.combine(board.target_date, s.pickup_time))
                    for s, _ in breaks)
                bits.append(f"Projected clear ~{_fmt_t(projected_clear)} puts "
                            f"{broken_times} {abs(breaks[0][1])} min short "
                            f"(engine turnaround math).")
            leg_ids = [leg.id] + [s.leg_id for s, _ in breaks]
            _add(out, Disruption(
                id=f"late_cascade:{leg.id}", kind="late_cascade",
                severity=severity, basis=basis, headline=head,
                narrative=" ".join(bits),
                leg_ids=leg_ids, anchor_leg_id=leg.id, driver_id=did,
                impact_dt=impact,
                task_id=_task_for(board, leg_ids,
                                  ("driver_conflict", "tight_turn")),
                details={"shift_min": shift,
                         "breaks": [(s.leg_id, sl) for s, sl in breaks],
                         "risk_source": risk["source"]},
            ))


def _detect_overruns(board, out):
    """A job still running past its estimate, or mid-trip GPS saying the
    chained NEXT pickup is blowing. ``estimate_job_end_time`` is used here for
    the STATUS test only (is the driver still on a job that should be done) —
    the projection of what it breaks runs on the guard-1 clock matrix plus
    "he is still on the job NOW", never on the p75 estimate."""
    for did, sched in board.schedules.items():
        slots = sorted(sched.slots, key=lambda s: (s.pickup_time, s.leg_id))
        for i, slot in enumerate(slots):
            leg = board.legs_by_id.get(slot.leg_id)
            if leg is None or not _is_active(leg):
                continue
            picked_dt = board.picked_up_by_leg.get(leg.id)
            under_way = (picked_dt is not None
                         or getattr(leg, "status", "") == "picked-up")
            if not under_way:
                continue

            gps = _gps_state(leg)
            midtrip_gps = (gps["fresh"] and gps["target"] == "next_pickup"
                           and gps["status"] in ("at_risk", "late"))
            est_end = slot.estimated_end_time
            overrun_clock = (
                ADVISOR_OVERRUN_GRACE_MIN > 0 and est_end is not None
                and board.now_local > est_end
                + timedelta(minutes=ADVISOR_OVERRUN_GRACE_MIN))
            if not (overrun_clock or midtrip_gps):
                continue

            # Earliest possible clear: the detection clock, floored at NOW —
            # he is demonstrably still on the job this minute.
            det_clear = advisor_clear_dt(leg, board.target_date,
                                         picked_up_dt=picked_dt,
                                         mode="detection")
            earliest_clear = max(board.now_local, det_clear or board.now_local)
            breaks = _downstream_breaks(board, did, slot, earliest_clear)

            severity = "critical" if (midtrip_gps or breaks) else "warning"
            basis = (BASIS_GPS_FRESH if midtrip_gps else
                     (BASIS_RECORDED_PICKUP if picked_dt is not None
                      else BASIS_CLOCK_ONLY))
            name = _driver_name(board, did)
            overrun_min = (_minutes(board.now_local - est_end)
                           if est_end is not None else 0)
            first_break = breaks[0] if breaks else None
            impact = (datetime.combine(board.target_date,
                                       first_break[0].pickup_time)
                      if first_break else (est_end or board.now_local))
            if first_break:
                head = (f"Protect the {_fmt_t(impact)} pickup — {name}'s "
                        f"current job is running long")
            else:
                head = (f"Check on {name} — job running "
                        f"{overrun_min} min past its estimate")
            bits = []
            if overrun_clock:
                bits.append(f"Still on {_leg_route(leg)} "
                            f"{overrun_min} min past the estimated end "
                            f"(estimate is display-grade, so this is a nudge "
                            f"until the chain is threatened).")
            if midtrip_gps:
                bits.append(gps["reason"]
                            or "Live ETA to the next pickup exceeds the time "
                               "remaining.")
            if breaks:
                bits.append(f"Clearing no earlier than "
                            f"{_fmt_t(earliest_clear)} puts the "
                            f"{_fmt_t(impact)} pickup "
                            f"{abs(breaks[0][1])} min short.")
            leg_ids = [leg.id] + [s.leg_id for s, _ in breaks]
            _add(out, Disruption(
                id=f"overrun:{leg.id}", kind="overrun", severity=severity,
                basis=basis, headline=head, narrative=" ".join(bits),
                leg_ids=leg_ids, anchor_leg_id=leg.id, driver_id=did,
                impact_dt=impact,
                task_id=_task_for(board, leg_ids,
                                  ("driver_conflict", "tight_turn")),
                details={"overrun_min": overrun_min, "midtrip_gps": midtrip_gps,
                         "breaks": [(s.leg_id, sl) for s, sl in breaks]},
            ))


def _detect_unassigned(board, out):
    """Driverless legs inside the ADVISOR_HORIZON_UNASSIGNED_MIN window.
    Escalates to critical inside half the horizon (or once past due); an
    unassigned pickup aged past OVERDUE_STALE_MIN is hygiene — almost always a
    ride covered off-book, not a guest standing on a curb for an hour."""
    from dispatching import pickup_policy

    if ADVISOR_HORIZON_UNASSIGNED_MIN <= 0:
        return
    for leg in board.legs:
        if getattr(leg, "driver_id", None):
            continue
        if not _is_active(leg) or getattr(leg, "pickup_time", None) is None:
            continue
        if getattr(leg, "status", "") == "picked-up" \
                or leg.id in board.picked_up_by_leg:
            continue
        eff = _effective_pickup_dt(leg, board.target_date)
        mins_to = _minutes(eff - board.now_local)
        if mins_to > ADVISOR_HORIZON_UNASSIGNED_MIN:
            continue
        vip = leg.id in board.vip_leg_ids
        route = _leg_route(leg)
        if mins_to < 0 and pickup_policy.is_overdue_stale(-mins_to):
            _add(out, Disruption(
                id=f"unassigned:{leg.id}", kind="unassigned",
                severity="watch", basis=BASIS_CLOCK_ONLY,
                headline=(f"Confirm coverage — {_fmt_t(eff)} {route} pickup "
                          f"still shows unassigned"),
                narrative=(f"{-mins_to} min past pickup with no driver on the "
                           f"board. At this age it's usually a ride covered "
                           f"off-book — confirm and fix the record."),
                leg_ids=[leg.id], anchor_leg_id=leg.id, impact_dt=eff,
                task_id=_task_for(board, [leg.id], ("driver_assign",)),
                abstain=True, hygiene=True,
                details={"mins_to_pickup": mins_to},
            ))
            continue
        severity = ("critical"
                    if mins_to <= ADVISOR_HORIZON_UNASSIGNED_MIN // 2
                    else "warning")
        when = (f"{mins_to} min out" if mins_to >= 0
                else f"{-mins_to} min PAST pickup")
        _add(out, Disruption(
            id=f"unassigned:{leg.id}", kind="unassigned", severity=severity,
            basis=BASIS_CLOCK_ONLY,
            headline=(f"Cover the {_fmt_t(eff)} {route} pickup — "
                      f"unassigned, {when}"),
            narrative=(f"No driver on the board for this leg"
                       + (" (VIP reservation)" if vip else "")
                       + f". Pickup {when}."),
            leg_ids=[leg.id], anchor_leg_id=leg.id, impact_dt=eff,
            task_id=_task_for(board, [leg.id], ("driver_assign",)),
            details={"mins_to_pickup": mins_to, "vip": vip},
        ))


# ════════════════════════════════════════════════════════════════════════════
# SERIALIZATION — detection-only card contract
# ════════════════════════════════════════════════════════════════════════════

def serialize_disruption(d):
    """Detection-only card in the plan's contract shape: id, kind, severity,
    headline, narrative, impact_at (ISO minutes, naive local), leg_ids,
    task_id, basis. Plans are attached by the Stage-B2 generator; ids only,
    JSON-safe throughout."""
    return {
        "id": d.id,
        "kind": d.kind,
        "severity": d.severity,
        "headline": d.headline,
        "narrative": d.narrative,
        "impact_at": (d.impact_dt.isoformat(timespec="minutes")
                      if d.impact_dt else None),
        "leg_ids": list(d.leg_ids),
        "task_id": d.task_id,
        "basis": d.basis,
    }


# ════════════════════════════════════════════════════════════════════════════
# PLAN SHAPES (candidate generation → validation → ranking)
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class PlanMove:
    """One board mutation inside a CandidatePlan. ``op`` follows the apply
    contract verbatim: ``reassign | farm_out | unassign | retime``.
    ``to_driver_id`` is an in-house driver for reassign, the AFFILIATE driver id
    for farm_out (board assignment only — never acceptance), None for unassign;
    ``retime`` carries ``new_pickup_time`` and never an assignment change."""
    leg_id: int
    op: str
    from_driver_id: int | None = None
    to_driver_id: int | None = None
    new_pickup_time: dt_time | None = None
    summary: str = ""
    from_label: str = ""
    to_label: str = ""
    resulting_slack_min: int | None = None
    note: str = ""


@dataclass
class PickupTimeChange:
    """A simulated pickup-time move (tier 1 match_flight). ``new_time`` is
    computed exactly the way the board's Match-flight endpoint computes it —
    the controlling flight's best arrival, ``.time()`` — so the apply stage can
    hand it to ``pickup_moves.apply_pickup_time_move`` unchanged."""
    leg_id: int
    old_time: dt_time
    new_time: dt_time


@dataclass
class CandidatePlan:
    """One recovery plan for one disruption, pre-serialization. Tiers follow
    the ops playbook ladder: 0 monitor / 1 match_flight / 2 in-house
    (reassign, swap_chain, takeback) / 3 farm (farm_out, evict_and_farm).
    ``price_impact``/``farm_base`` are display/score dollars — NEVER serialized
    into the apply payload (ids only there). ``validation`` is the
    board_validation verdict the plan passed."""
    kind: str
    tier: int
    title: str
    target_leg_id: int = 0
    moves: list = field(default_factory=list)          # [PlanMove]
    time_changes: list = field(default_factory=list)   # [PickupTimeChange]
    why: list = field(default_factory=list)
    risks: list = field(default_factory=list)
    risk_flags: list = field(default_factory=list)
    farm_out: bool = False
    price_impact: float | None = None
    farm_base: float = 0.0
    validation: object = None
    score: int = 0


# Guard 6 — status safety. Never-move is hard (generation here, 409 at apply);
# warned statuses move but say so (Leg.save resets progressed statuses on a
# driver change, so the new driver must re-accept).
_STATUS_NEVER_MOVE = ("picked-up", "on-location")
_STATUS_MOVE_WARN = ("on-the-way", "confirmed")

# Guard 8 — verbatim reuse of the board's existing warning string
# (views.update_leg_assignment), so the advisor and the assignment modal say
# the same words.
_PENDING_REFUND_WARNING = "Warning: This reservation has a pending refund request."

_FARM_CONFIRM_LINE = ("Assigns on the board only — call {aff} to confirm "
                      "before considering this covered.")
_STUB_WINDOW_NOTE = ("{name}'s availability is an observed-history window "
                     "(provisional), not a configured shift.")

# Farm hard gates, in the plan's order. ``pending_refund`` is the
# owner-confirmed NEW rule (advisor-only); ``far_unknown`` is Approach-A
# abstention (uncomputable, not zero).
_FARM_GATE_PHRASES = {
    "vip": "the {when} {route} is VIP — never farmed",
    "departure": "the {when} {route} is a true departure — stays in-house",
    "pending_refund": "the {when} {route} has a refund in flight — never farmed",
    "far_unknown": ("the {when} {route} has a far/unknown endpoint — farm "
                    "feasibility unverifiable, the advisor abstains"),
}


# ════════════════════════════════════════════════════════════════════════════
# SMALL PLAN PREDICATES (pure)
# ════════════════════════════════════════════════════════════════════════════

def _fmt_clock(t):
    return t.strftime("%I:%M %p").lstrip("0") if t else "?"


def _fmt_money(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "$?"
    return f"${f:,.0f}" if abs(f - round(f)) < 0.005 else f"${f:,.2f}"


def _movable(board, leg):
    """Guard 6: may this leg change driver at all? Picked-up / on-location
    legs (by status OR by a recorded tap) never move."""
    if leg is None or not _is_active(leg):
        return False
    if (getattr(leg, "status", "") or "") in _STATUS_NEVER_MOVE:
        return False
    return leg.id not in board.picked_up_by_leg


def _is_affiliate_held(board, leg):
    did = getattr(leg, "driver_id", None)
    if not did or did in board.schedules:
        return False
    d = board.drivers_by_id.get(did)
    return bool(d is not None and getattr(d, "driver_type", "") == "affiliate")


def _leg_vtype_of(leg):
    v = getattr(getattr(getattr(leg, "reservation", None), "vehicle", None),
                "vehicle_type", None)
    return str(v) if v else None


def _vehicle_ok(driver_vtype, leg_vtype):
    """Mirror of swap_optimizer._vehicle_compatible: an untyped leg fits any
    car; an unknown driver type allows all (same live-board semantics)."""
    if not leg_vtype:
        return True
    from dispatching.scheduler import get_compatible_vehicle_types
    return leg_vtype in get_compatible_vehicle_types(driver_vtype or "")


def _may_move_work(d):
    """Guard 5: a plan that moves work OFF a driver requires fresh GPS
    at_risk/late, flight arithmetic, or a hard (negative-slack) break —
    including one anchored on a recorded pickup. Placing UNOWNED work
    (unassigned legs) is not moving work off anyone.

    "Critical" alone is NOT sanction: a clock-only critical (an unpressed
    button a few minutes past the grace) with nothing downstream negative must
    never strip a leg off a driver whose only sin is not tapping the app. A
    critical OVERLAP is negative slack by construction (a hard break); a
    cascade/overrun earns the sanction only when it carries concrete
    downstream breaks (negative-slack pickups in details['breaks'])."""
    if d.kind == "unassigned":
        return True
    if d.basis in (BASIS_GPS_FRESH, BASIS_FLIGHT):
        return True
    if d.severity != "critical":
        return False
    if d.kind == "overlap":
        return True                      # critical overlap == negative slack
    return bool(d.details.get("breaks"))  # hard downstream fact required


def _far_unknown(leg):
    from dispatching.farmout_optimizer import _drive_uncomputable_far
    try:
        return _drive_uncomputable_far(leg)
    except Exception:
        return True   # unjudgeable endpoint — treat as far/unknown, never guess


def _farm_gate_reason(board, leg):
    """First farm hard-gate this leg trips, in the plan's order, or None."""
    if leg.id in board.vip_leg_ids:
        return "vip"
    from dispatching.farmout_optimizer import is_departure
    try:
        if is_departure(leg):
            return "departure"
    except Exception:
        pass
    if leg.id in board.pending_refund_leg_ids:
        return "pending_refund"
    if _far_unknown(leg):
        return "far_unknown"
    return None


def _protect_phrase(leg, gate):
    return _FARM_GATE_PHRASES[gate].format(
        when=_fmt_clock(getattr(leg, "pickup_time", None)),
        route=_leg_route(leg))


# ════════════════════════════════════════════════════════════════════════════
# LAZY FARM CONTEXT (roster + shared day ledger — built only when a farm tier
# is actually reached; committed farm-outs seeded first so a one-man affiliate
# with a real 12:54 run is never quoted for a 12:58 job)
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class _FarmCtx:
    roster: list
    ledger: object

    def quote(self, leg, day):
        from dispatching.farmout_optimizer import cheapest_affiliate_for_leg
        return cheapest_affiliate_for_leg(leg, day, self.ledger, self.roster)

    def options(self, leg, day):
        from dispatching.farmout_optimizer import quote_affiliate_options
        return quote_affiliate_options(leg, day, self.ledger, self.roster)


def _farm_context(board):
    """Resolve (once per BoardState) the carded-affiliate roster and a
    capacity ledger seeded with the day's REAL committed farm-outs. Returns
    None when no carded affiliate exists (uncomputable → the farm tier
    abstains, never invents a price)."""
    if getattr(board, "_farm_ctx_built", False):
        return board._farm_ctx
    from dispatching.farmout_optimizer import (
        WaterfallLedger, resolve_affiliate_roster, seed_committed_farmouts)
    roster, _warnings, _flat = resolve_affiliate_roster()
    ctx = None
    if roster:
        ledger = WaterfallLedger.for_roster(roster)
        seed_committed_farmouts(
            ledger, [l for l in board.legs if _is_active(l)], board.target_date)
        ctx = _FarmCtx(roster=roster, ledger=ledger)
    board._farm_ctx = ctx
    board._farm_ctx_built = True
    return ctx


# ════════════════════════════════════════════════════════════════════════════
# EXPLANATION HELPERS (engine artifacts only — never free-composed guesses)
# ════════════════════════════════════════════════════════════════════════════

def _cause_lines(board, d):
    """The 'why act at all' line(s) for a plan, from the engine's own
    artifacts: the clearing-formula breakdown for overlaps, flight arithmetic
    for flight cards, the detection narrative (which already quotes
    dispatch_risk_reason verbatim) otherwise."""
    if d.kind == "overlap" and d.leg_ids:
        prev = board.legs_by_id.get(d.leg_ids[0])
        if prev is not None:
            from dispatching.scheduler import get_clearing_breakdown
            try:
                bd = get_clearing_breakdown(prev, board.target_date)
                return [f"Chain math (auto-assign formula): {bd['formula']} — "
                        f"leaves {d.details.get('slack')} min for the "
                        f"{_fmt_t(d.impact_dt)} pickup."]
            except Exception:
                pass
        return [d.narrative]
    if d.kind == "flight_change":
        div = d.details.get("divergence_min")
        if div:
            return [f"The controlling flight lands {abs(div)} min "
                    f"{'later' if div > 0 else 'earlier'} than the booked "
                    f"pickup anchors."]
        return [d.narrative]
    return [d.narrative]


_SWAP_SKIP_PHRASES = {
    "vehicle_incompatible": "vehicle class can't take this job",
    "car_share_conflict": "shares the physical car with a working partner",
}


def _swap_diag_narrative(board, result):
    """Serialize find_swaps' empty-result diagnostic into the 'why nobody can
    take it' narrative — per-driver reasons verbatim (feasibility reasons are
    FeasibilityResult.reason), stub windows attributed honestly."""
    bits, skipped = [], 0
    for att in result.diagnostic:
        if att.skipped_reason == "same_driver":
            continue
        if att.skipped_reason:
            reason = _SWAP_SKIP_PHRASES.get(att.skipped_reason,
                                            att.skipped_reason)
        elif att.direct_fail_reason:
            reason = att.direct_fail_reason
            if (reason.startswith("Outside driver window")
                    and board.window_sources.get(att.driver_id) == "stub"):
                reason += (" — observed-history window (provisional), not a "
                           "configured shift")
        else:
            continue
        if len(bits) < 4:
            bits.append(f"{att.driver_name}: {reason}")
        else:
            skipped += 1
    if not bits:
        return "No in-house driver can absorb it."
    tail = f" (+{skipped} more)" if skipped else ""
    return "No in-house driver can absorb it — " + "; ".join(bits) + tail


# ════════════════════════════════════════════════════════════════════════════
# CANDIDATE BUILDERS
# ════════════════════════════════════════════════════════════════════════════

def _mk_move(board, leg, op, to_driver_id=None, resulting_slack=None, note=""):
    from_did = getattr(leg, "driver_id", None)
    return PlanMove(
        leg_id=leg.id, op=op, from_driver_id=from_did,
        to_driver_id=to_driver_id,
        summary=f"{_leg_route(leg)} · {_fmt_clock(leg.pickup_time)}",
        from_label=(_driver_name(board, from_did) if from_did else ""),
        to_label=(_driver_name(board, to_driver_id)
                  if (op == "reassign" and to_driver_id is not None) else ""),
        resulting_slack_min=resulting_slack, note=note)


def _flag(plan, flag, risk_line):
    if flag not in plan.risk_flags:
        plan.risk_flags.append(flag)
        plan.risks.append(risk_line)


def _finish_plan(board, d, plan, diag):
    """The whole-board gate every candidate passes, plus shared risk
    decoration. Runs board_validation.validate_post_move_board — the SAME
    promoted function the apply path re-runs against the DB, so the advisor
    and apply can never disagree at the threshold. Hard rejections are dropped
    (reason recorded for the card diagnostic); '' → 'tight' demotions survive,
    penalized and NAMED in the plan's risks. Returns the plan or None."""
    from dispatching import pickup_policy
    from dispatching.board_validation import validate_post_move_board

    assign_moves = [(m.leg_id, m.to_driver_id if m.op == "reassign" else None)
                    for m in plan.moves if m.op != "retime"]
    time_changes = {c.leg_id: c.new_time for c in plan.time_changes}
    if assign_moves or time_changes:
        # Guard 1, planning half: validate on the PLANNING-clock schedules
        # (recorded pickups re-anchored via max(static, actual)) with a
        # baseline swept over the same clock, so a plan is never blessed by
        # seating work behind a demonstrably-late driver.
        p_scheds, p_bands = _planning(board)
        v = validate_post_move_board(
            p_scheds, board.legs_by_id, assign_moves, board.target_date,
            windows=board.windows, sharer_partners=board.sharer_partners,
            baseline_bands=p_bands,
            time_changes=time_changes or None)
        if not v.ok:
            diag.append(f"{plan.title}: {v.reason}")
            return None
        plan.validation = v
        for w in v.worsened_pairs:
            plan.risks.append(
                f"Creates a tight turn on {_driver_name(board, w['driver_id'])}:"
                f" legs {w['prev_leg_id']}→{w['next_leg_id']} drop to "
                f"{w['slack']} min after turnaround.")
        if 0 <= (v.min_buffer_after or 0) < pickup_policy.TURN_TIGHT_SLACK_MIN:
            _flag(plan, "depends_tight_turn",
                  f"Leaves only {v.min_buffer_after} min at the tightest turn "
                  f"after the move.")
        if v.new_tight_count and plan.tier == 2:
            # A ''→tight-worsening in-house plan is DEMOTED so cleaner in-house
            # options outrank it. It is NOT demoted below farming: farm-out is
            # a last resort, never a competitor to keeping the work in-house
            # (see generate_plans — the farm tiers only run when in-house is
            # exhausted, and the sort puts every farm plan last regardless).
            plan.tier = 3

    seen_stub = set()
    for m in plan.moves:
        leg = board.legs_by_id.get(m.leg_id)
        status = (getattr(leg, "status", "") or "") if leg is not None else ""
        if m.op in ("reassign", "farm_out", "unassign"):
            if status in _STATUS_MOVE_WARN:
                plan.risks.append(
                    f"Leg {m.leg_id} is already {status} — moving it resets "
                    f"the status to in-progress and the new driver must "
                    f"re-accept.")
            if m.leg_id in board.pending_refund_leg_ids:
                plan.risks.append(_PENDING_REFUND_WARNING)
        if m.leg_id in board.keoi_leg_ids:
            _flag(plan, "keoi_flagged",
                  f"Leg {m.leg_id} carries an open keep-an-eye-on-it flag.")
        if m.op == "reassign" and leg is not None and _far_unknown(leg):
            _flag(plan, "far_unknown_route",
                  f"{_leg_route(leg)} has a far/unknown endpoint — drive times "
                  f"for it are coarse table guesses.")
        if (m.op == "reassign" and m.to_driver_id is not None
                and m.to_driver_id not in seen_stub
                and board.window_sources.get(m.to_driver_id) == "stub"
                and board.windows.get(m.to_driver_id) is not None):
            seen_stub.add(m.to_driver_id)
            _flag(plan, "stub_window", _STUB_WINDOW_NOTE.format(
                name=_driver_name(board, m.to_driver_id)))
    if plan.farm_out:
        aff = next((m.to_label for m in plan.moves if m.op == "farm_out"),
                   "") or "the affiliate"
        _flag(plan, "gps_blind_affiliate",
              f"No live GPS once farmed — {aff} runs outside the Samsara "
              f"fleet.")
        plan.risks.append(_FARM_CONFIRM_LINE.format(aff=aff))
    return plan


def _recovery_targets(board, d):
    """The legs the move tiers try to re-home for this disruption, in fix
    order, filtered by guard 6 (status safety) and guard 7 (affiliate-held
    legs never enter in-house move tiers). Capped at two."""
    if d.kind == "unassigned":
        ids = [d.anchor_leg_id]
    elif d.kind == "overlap":
        ids = ([d.leg_ids[1], d.leg_ids[0]] if len(d.leg_ids) >= 2
               else list(d.leg_ids))
    elif d.kind == "late_cascade":
        ids = [d.anchor_leg_id] + [lid for lid, _s in
                                   d.details.get("breaks", [])]
    elif d.kind == "overrun":
        ids = [lid for lid, _s in d.details.get("breaks", [])]
    elif d.kind == "flight_change":
        ids = ([d.leg_ids[1]]
               if (d.details.get("slack_out") is not None
                   and d.details["slack_out"] < 0 and len(d.leg_ids) >= 2)
               else [])
    else:
        ids = []
    out, seen = [], set()
    for lid in ids:
        if lid in seen:
            continue
        seen.add(lid)
        leg = board.legs_by_id.get(lid)
        if (leg is not None and _movable(board, leg)
                and not _is_affiliate_held(board, leg)):
            out.append(leg)
    return out[:2]


def _receiver_candidates(board, leg, exclude=()):
    """Every deployable in-house receiver that can legally take ``leg`` on the
    PLANNING-clock schedules (guard 1: recorded pickups re-anchored via
    max(static, actual) — a demonstrably-late receiver never looks free):
    vehicle compat, car-share gate, check_feasibility with the generation
    windows (enforce_cap=True — the advisor is an automatic path). Sorted
    best-buffer-first, deterministic."""
    from dispatching.scheduler import check_feasibility, sharers_conflict

    p_scheds, _ = _planning(board)
    lvt = _leg_vtype_of(leg)
    cur = getattr(leg, "driver_id", None)
    out = []
    for did in sorted(p_scheds):
        if did == cur or did in exclude:
            continue
        sched = p_scheds[did]
        if getattr(sched, "driver_type", "inhouse") != "inhouse":
            continue
        if not _vehicle_ok(board.driver_vtypes.get(did), lvt):
            continue
        if board.sharer_partners and sharers_conflict(
                leg, did, board.sharer_partners, board.schedules,
                board.target_date):
            continue
        feas = check_feasibility(sched, leg, board.target_date,
                                 driver_window=board.windows.get(did))
        if feas.feasible:
            out.append((did, feas))
    out.sort(key=lambda t: (-t[1].buffer_minutes, t[0]))
    return out


def _monitor_plan(board, d):
    """Tier 0 — when the right move is no move: GPS suppressing a clock alarm,
    tight-but-legal slack, a flight fact with nothing broken behind it."""
    why = [f"Signal basis: {d.basis}."]
    if d.kind == "overlap":
        why.append(f"The turn is tight but legal ({d.details.get('slack')} min "
                   f"after turnaround) — reality thinned a clean turn, it has "
                   f"not broken. Planned-tight days are the founder's call.")
    elif d.kind == "flight_change":
        why.append("Nothing downstream breaks at the current times — "
                   "acknowledge the change and keep watching the flight.")
    else:
        why.append("Nothing downstream breaks yet — keep eyes on it before "
                   "moving work.")
    return CandidatePlan(kind="monitor", tier=0,
                         title="Monitor — no move is warranted yet",
                         target_leg_id=d.anchor_leg_id, why=why)


def _match_flight_plans(board, d, diag):
    """Tier 1 — retime the booked pickup onto the controlling flight's best
    arrival (delayed flights only; an early plane never pulls a guest
    commitment earlier). Simulated in-memory via a leg clone + slot rebuild
    (validate_post_move_board's time_changes path). When the delay has ALREADY
    broken the turn out, the retime alone cannot re-seat the broken pickup, so
    tier 1 becomes the COMBINED plan — match the time AND cover the broken leg
    (what a dispatcher actually does); the cover-only variant still comes from
    the tier-2 ladder."""
    from dispatching import pickup_policy

    leg = board.legs_by_id.get(d.anchor_leg_id)
    div = d.details.get("divergence_min")
    if (leg is None or div is None or div < ADVISOR_FLIGHT_MISMATCH_MIN
            or not _movable(board, leg)):
        return []
    arr = pickup_policy.controlling_arrival_dt(leg, aware=False)
    if arr is None or arr.time() == leg.pickup_time:
        return []
    new_time = arr.time()
    booked = _fmt_clock(leg.pickup_time)
    tc = PickupTimeChange(leg.id, leg.pickup_time, new_time)
    retime = PlanMove(
        leg_id=leg.id, op="retime",
        from_driver_id=getattr(leg, "driver_id", None),
        new_pickup_time=new_time,
        summary=f"{_leg_route(leg)} · {booked} → {_fmt_clock(new_time)}",
        from_label=booked, to_label=_fmt_clock(new_time),
        note="Flight match (Recovery Advisor)")
    why = _cause_lines(board, d) + [
        f"Move the booked pickup {booked} → {_fmt_clock(new_time)} — the same "
        f"retime the board's Match-flight button applies, so the guest record "
        f"matches the plane."]

    broken_id = (d.leg_ids[1]
                 if (d.details.get("slack_out") is not None
                     and d.details["slack_out"] < 0 and len(d.leg_ids) >= 2)
                 else None)
    if broken_id is None:
        p = CandidatePlan(
            kind="match_flight", tier=1,
            title=f"Match the {booked} pickup to its flight "
                  f"({_fmt_clock(new_time)})",
            target_leg_id=leg.id, moves=[retime], time_changes=[tc],
            why=list(why))
        p = _finish_plan(board, d, p, diag)
        return [p] if p else []

    nxt = board.legs_by_id.get(broken_id)
    if nxt is None or not _movable(board, nxt) \
            or _is_affiliate_held(board, nxt):
        return []
    plans = []
    for did, feas in _receiver_candidates(board, nxt)[:1]:
        mv = _mk_move(board, nxt, "reassign", to_driver_id=did,
                      resulting_slack=feas.buffer_minutes)
        p = CandidatePlan(
            kind="match_flight", tier=1,
            title=f"Match the {booked} pickup to its flight and move the "
                  f"{_fmt_clock(nxt.pickup_time)} job to "
                  f"{_driver_name(board, did)}",
            target_leg_id=leg.id, moves=[retime, mv], time_changes=[tc],
            why=list(why) + [
                f"{_driver_name(board, did)} takes the broken "
                f"{_fmt_clock(nxt.pickup_time)} pickup with "
                f"{feas.buffer_minutes} min to spare (engine feasibility)."])
        p = _finish_plan(board, d, p, diag)
        if p:
            plans.append(p)
    return plans


def _reassign_plans(board, d, leg, diag):
    """Tier 2a — direct reassignment to the best receivers (top 3 by buffer)."""
    plans = []
    cause = _cause_lines(board, d)
    for did, feas in _receiver_candidates(board, leg)[:ADVISOR_MAX_PLANS_PER_CARD]:
        mv = _mk_move(board, leg, "reassign", to_driver_id=did,
                      resulting_slack=feas.buffer_minutes)
        p = CandidatePlan(
            kind="reassign", tier=2,
            title=f"Move the {_fmt_clock(leg.pickup_time)} {_leg_route(leg)} "
                  f"to {_driver_name(board, did)}",
            target_leg_id=leg.id, moves=[mv],
            why=cause + [f"{_driver_name(board, did)} can take it with "
                         f"{feas.buffer_minutes} min to spare "
                         f"(engine feasibility)."])
        p = _finish_plan(board, d, p, diag)
        if p:
            plans.append(p)
    return plans


def _swap_plans(board, d, leg, swap_ms, diag):
    """Tier 2c — cascading displacement chains via swap_optimizer.find_swaps,
    with EXPLICIT budgets (the literal defaults 5/5000/5000 would be silently
    replaced by SchedulerSettings — swap_optimizer.py:286), the board's
    authoritative windows (bypasses the stub lookup and restricts receivers to
    the deployable pool) and the car-share map ALWAYS passed. VIP-touching
    solutions are rejected. Returns (plans, SwapSearchResult) so the caller
    can keep the empty-result diagnostic."""
    from dispatching.swap_optimizer import find_swaps

    p_scheds, _ = _planning(board)   # guard 1: never seat behind a late driver
    result = find_swaps(
        leg, p_scheds, board.legs_by_id, board.driver_vtypes,
        board.target_date,
        max_depth=ADVISOR_SWAP_DEPTH,
        time_limit_ms=swap_ms,
        max_iterations=ADVISOR_SWAP_MAX_ITER,
        driver_windows=board.windows,
        sharer_partners=board.sharer_partners)
    plans = []
    for sol in result.solutions:
        if len(plans) >= 2:
            break
        # sol.moves is the FULL execution-ordered chain, target placement
        # included (SwapSolution.target_* are derived from its last hop).
        moves, bad = [], ""
        for m in sol.moves:
            mleg = board.legs_by_id.get(m.leg_id)
            if m.leg_id in board.vip_leg_ids:
                bad = f"displaces VIP leg {m.leg_id} — rejected"
                break
            if mleg is None or not _movable(board, mleg):
                bad = f"leg {m.leg_id} is under way — cannot be displaced"
                break
            moves.append(_mk_move(board, mleg, "reassign",
                                  to_driver_id=m.to_driver_id,
                                  resulting_slack=m.buffer_minutes))
        if bad:
            diag.append(f"swap for leg {leg.id}: {bad}")
            continue
        why = _cause_lines(board, d)
        for m in sol.moves:
            if m.leg_id == leg.id:
                continue
            mleg = board.legs_by_id.get(m.leg_id)
            why.append(f"{m.from_driver_name or 'unassigned'} hands the "
                       f"{_fmt_clock(mleg.pickup_time)} {_leg_route(mleg)} to "
                       f"{m.to_driver_name} ({m.buffer_minutes} min buffer).")
        why.append(f"{sol.target_driver_name} then takes the "
                   f"{_fmt_clock(leg.pickup_time)} {_leg_route(leg)} with "
                   f"{sol.target_buffer_minutes} min to spare.")
        p = CandidatePlan(
            kind="swap_chain", tier=2,
            title=f"Shuffle {len(sol.moves)} jobs — {_leg_route(leg)} "
                  f"lands on {sol.target_driver_name}",
            target_leg_id=leg.id, moves=moves, why=why)
        p = _finish_plan(board, d, p, diag)
        if p:
            plans.append(p)
    return plans, result


def _takeback_plans(board, d, diag):
    """Guard 7 — an affiliate-assigned leg's ONLY offered recovery: take it
    back onto the best in-house receiver, with the advisor's own whole-board
    validation BEFORE any write path runs (execute_takeback does none)."""
    leg = board.legs_by_id.get(d.anchor_leg_id)
    if leg is None or not _movable(board, leg):
        return []
    aff = _driver_name(board, getattr(leg, "driver_id", None))
    plans = []
    for did, feas in _receiver_candidates(board, leg)[:1]:
        mv = _mk_move(board, leg, "reassign", to_driver_id=did,
                      resulting_slack=feas.buffer_minutes)
        p = CandidatePlan(
            kind="takeback", tier=2,
            title=f"Take the {_fmt_clock(leg.pickup_time)} {_leg_route(leg)} "
                  f"back from {aff} to {_driver_name(board, did)}",
            target_leg_id=leg.id, moves=[mv],
            why=_cause_lines(board, d) + [
                f"{_driver_name(board, did)} can absorb it with "
                f"{feas.buffer_minutes} min to spare (engine feasibility)."],
            risks=[f"Call {aff} first — you can't reliably pull back same-day; "
                   f"board assignment is not acceptance."])
        p = _finish_plan(board, d, p, diag)
        if p:
            plans.append(p)
    return plans


def _direct_farm_plan(board, d, targets, diag, protected):
    """Tier 3 — farm the first target that clears the hard gates, priced by
    the capacity-aware waterfall (cheapest eligible affiliate). Gated targets
    are recorded as 'kept in-house' phrases so the card can explain what
    protected the alternative."""
    for leg in targets:
        gate = _farm_gate_reason(board, leg)
        if gate:
            protected.append(_protect_phrase(leg, gate))
            continue
        ctx = _farm_context(board)
        if ctx is None:
            diag.append("no carded affiliate — farm pricing uncomputable "
                        "(abstain)")
            return None
        q = ctx.quote(leg, board.target_date)
        if q.get("status") != "ok":
            diag.append(f"farm leg {leg.id}: {q.get('status')}")
            continue
        try:
            opts, _skipped = ctx.options(leg, board.target_date)
        except Exception:
            opts = []
        base, night = q.get("base"), q.get("night")
        mv = PlanMove(
            leg_id=leg.id, op="farm_out",
            from_driver_id=getattr(leg, "driver_id", None),
            to_driver_id=q.get("affiliate_id"),
            summary=f"{_leg_route(leg)} · {_fmt_clock(leg.pickup_time)}",
            from_label=(_driver_name(board, leg.driver_id)
                        if getattr(leg, "driver_id", None) else ""),
            to_label=q.get("affiliate") or "affiliate")
        why = _cause_lines(board, d) + [
            f"Farm the {_fmt_clock(leg.pickup_time)} {_leg_route(leg)} to "
            f"{q.get('affiliate')} — {_fmt_money(base)} base"
            + (f" + {_fmt_money(night)} night" if night else "")
            + f" ({max(len(opts), 1)} affiliate option"
            + ("s" if len(opts) != 1 else "")
            + " priced; cheapest shown)."]
        why += [f"Kept in-house: {ph}." for ph in protected]
        p = CandidatePlan(
            kind="farm_out", tier=3,
            title=f"Farm the {_fmt_clock(leg.pickup_time)} {_leg_route(leg)} "
                  f"to {q.get('affiliate')}",
            target_leg_id=leg.id, moves=[mv], why=why, farm_out=True,
            price_impact=float(q.get("total") or base or 0),
            farm_base=float(base or 0))
        return _finish_plan(board, d, p, diag)
    return None


def _evict_farm_plans(board, d, leg, diag, protected):
    """Tier 3 — evict_and_farm: seat the target IN-HOUSE by farming a
    DIFFERENT leg. Each receiver's displaceable slots come from
    swap_optimizer._get_conflicting_slots; the displaced leg must itself clear
    the farm gates. Arrivals are the farm currency: cheapest base, then
    arrival-type, then lowest founder leg_value, deterministic tie-break."""
    from dispatching.scheduler import leg_value, sharers_conflict
    from dispatching.swap_optimizer import _get_conflicting_slots

    if not _movable(board, leg) or _is_affiliate_held(board, leg):
        return []
    p_scheds, _ = _planning(board)   # guard 1: planning clock for placements
    lvt = _leg_vtype_of(leg)
    cur = getattr(leg, "driver_id", None)
    cands = []
    for did in sorted(p_scheds):
        if did == cur:
            continue
        sched = p_scheds[did]
        if getattr(sched, "driver_type", "inhouse") != "inhouse" \
                or not sched.slots:
            continue
        if not _vehicle_ok(board.driver_vtypes.get(did), lvt):
            continue
        if board.sharer_partners and sharers_conflict(
                leg, did, board.sharer_partners, board.schedules,
                board.target_date):
            continue
        for slot, buf in _get_conflicting_slots(
                sched, leg, board.target_date,
                driver_window=board.windows.get(did)):
            dleg = board.legs_by_id.get(slot.leg_id)
            if dleg is None or not _movable(board, dleg):
                continue
            gate = _farm_gate_reason(board, dleg)
            if gate:
                protected.append(_protect_phrase(dleg, gate))
                continue
            ctx = _farm_context(board)
            if ctx is None:
                diag.append("no carded affiliate — farm pricing uncomputable "
                            "(abstain)")
                return []
            q = ctx.quote(dleg, board.target_date)
            if q.get("status") != "ok":
                diag.append(f"farm leg {dleg.id}: {q.get('status')}")
                continue
            try:
                lv = leg_value(dleg)
            except Exception:
                lv = 0.0
            is_arr = 0 if (getattr(dleg, "get_trip_type", lambda: "")()
                           == "arrival") else 1
            cands.append((float(q.get("base") or 0), is_arr, lv, did,
                          dleg.id, buf, q, dleg))
    if not cands:
        return []
    cands.sort(key=lambda c: (c[0], c[1], c[2], c[3], c[4]))
    base, _arr, _lv, did, _dlid, buf, q, dleg = cands[0]
    farm_mv = PlanMove(
        leg_id=dleg.id, op="farm_out", from_driver_id=did,
        to_driver_id=q.get("affiliate_id"),
        summary=f"{_leg_route(dleg)} · {_fmt_clock(dleg.pickup_time)}",
        from_label=_driver_name(board, did),
        to_label=q.get("affiliate") or "affiliate")
    take_mv = _mk_move(board, leg, "reassign", to_driver_id=did,
                       resulting_slack=buf)
    name = _driver_name(board, did)
    why = _cause_lines(board, d) + [
        f"Free {name} by farming his {_fmt_clock(dleg.pickup_time)} "
        f"{_leg_route(dleg)} to {q.get('affiliate')} "
        f"({_fmt_money(q.get('base'))} base); {name} then takes the "
        f"{_fmt_clock(leg.pickup_time)} {_leg_route(leg)} with {buf} min to "
        f"spare.",
        "Arrivals are the farm currency — the displaced leg is the cheapest "
        "farm-eligible job on the receiving driver's day."]
    why += [f"Kept in-house: {ph}." for ph in protected]
    p = CandidatePlan(
        kind="evict_and_farm", tier=3,
        title=f"Farm {name}'s {_fmt_clock(dleg.pickup_time)} "
              f"{_leg_route(dleg)}; he takes the "
              f"{_fmt_clock(leg.pickup_time)} {_leg_route(leg)}",
        target_leg_id=leg.id, moves=[farm_mv, take_mv], why=why,
        farm_out=True,
        price_impact=float(q.get("total") or base or 0),
        farm_base=float(base or 0))
    p = _finish_plan(board, d, p, diag)
    return [p] if p else []


# ════════════════════════════════════════════════════════════════════════════
# GENERATION + RANKING
# ════════════════════════════════════════════════════════════════════════════

def _score_plan(board, plan, cfg):
    """The plan's ranking formula, reusing the SchedulerSettings swap weights:
    1000 − depth_penalty·moves + buffer_weight·clamp(min_buffer, 0, 90)
    − 120·new_tight + revenue term − farm_cost_base − 60·risk_flags
    − 40·time_changes. Monitor plans score 0 (tier orders them anyway)."""
    if plan.tier == 0:
        return 0
    v = plan.validation
    n_moves = sum(1 for m in plan.moves if m.op != "retime")
    score = 1000 - cfg.swap_depth_penalty * n_moves
    mb = v.min_buffer_after if v is not None else 999
    score += cfg.swap_buffer_weight * max(0, min(mb, 90))
    score -= 120 * (v.new_tight_count if v is not None else 0)
    target = board.legs_by_id.get(plan.target_leg_id)
    try:
        rev = float(getattr(target, "revenue_share", 0) or 0)
    except (TypeError, ValueError):
        rev = 0.0
    score += int(min(rev / max(cfg.revenue_divisor, 1), cfg.revenue_cap)
                 * cfg.swap_revenue_weight / 10)
    score -= int(plan.farm_base)
    score -= 60 * len(plan.risk_flags)
    score -= 40 * len(plan.time_changes)
    return score


def generate_plans(board, disruption, *, swap_time_ms=None):
    """Ranked CandidatePlans for one disruption, mirroring the ops playbook
    ladder (monitor / match_flight / reassign / swap chains / farm tiers).
    Read-only; deterministic; every board-changing candidate passes
    board_validation.validate_post_move_board. Disruptions with abstain=True
    (hygiene, overnight-unconfirmed) NEVER receive plans; a plan that moves
    work off a driver requires guard-5 sanction (_may_move_work). Dropped
    candidates' reasons land in details['plan_diagnostic']; farm-gate
    protections in details['farm_protected']."""
    if not ADVISOR_ENABLED or disruption.abstain:
        return []
    d = disruption
    diag, protected, plans = [], [], []

    # Tier 0 — warning-band cards lead with "monitor" (an unassigned leg is
    # never monitored: it needs a driver, not eyes).
    if d.severity != "critical" and d.kind != "unassigned":
        plans.append(_monitor_plan(board, d))

    if d.kind == "flight_change" and d.details.get("affiliate"):
        plans += _takeback_plans(board, d, diag)   # guard 7 — takeback only
    else:
        if d.kind == "flight_change":
            plans += _match_flight_plans(board, d, diag)
        if _may_move_work(d):
            targets = _recovery_targets(board, d)
            for i, leg in enumerate(targets):
                direct = _reassign_plans(board, d, leg, diag)
                plans += direct
                # Swap search when nobody can take it directly — OR when every
                # direct taker would manufacture a tight turn. Shuffling the
                # board is still "keeping it in-house", so it has to be tried
                # before farming is even considered.
                clean_direct = [
                    p for p in direct
                    if not (p.validation is not None
                            and p.validation.new_tight_count)]
                if i == 0 and not clean_direct:
                    sw, res = _swap_plans(
                        board, d, leg,
                        swap_time_ms or ADVISOR_SWAP_TIME_MS, diag)
                    plans += sw
                    if not res.solutions and not direct:
                        d.details["swap_diagnostic"] = \
                            _swap_diag_narrative(board, res)
            # FARM IS A LAST RESORT (owner rule). An affiliate is offered only
            # when the work cannot be kept in-house at all — not as a cheaper
            # or cleaner alternative to a move we could make ourselves. If any
            # in-house plan survived validation, the farm tiers do not run: no
            # pricing, no card, no option.
            in_house = [p for p in plans if p.moves and not p.farm_out]
            if targets and not in_house:
                fp = _direct_farm_plan(board, d, targets, diag, protected)
                if fp:
                    plans.append(fp)
                plans += _evict_farm_plans(board, d, targets[0], diag,
                                           protected)
            elif targets and in_house:
                diag.append("farm tiers skipped — the work can stay in-house")

    from dispatching.models import SchedulerSettings
    cfg = SchedulerSettings.get_settings()
    for p in plans:
        p.score = _score_plan(board, p, cfg)
    plans.sort(key=lambda p: (
        # Farming NEVER outranks keeping the work in-house — dollars and a
        # clean turn do not buy a farm-out its way past a move we can make
        # ourselves. This leads the key so no score can invert it.
        bool(p.farm_out),
        p.tier, -p.score, p.kind, p.target_leg_id,
        tuple((m.leg_id, m.op, m.to_driver_id or 0) for m in p.moves)))
    if diag:
        d.details["plan_diagnostic"] = diag[:8]
    if protected:
        d.details["farm_protected"] = protected[:4]
    return plans[:ADVISOR_MAX_PLANS_PER_CARD]


# ════════════════════════════════════════════════════════════════════════════
# SERIALIZATION — the one card/apply payload contract (engine → UI → apply)
# ════════════════════════════════════════════════════════════════════════════

def _serialize_plan(board, d, plan, rank):
    """Plan dict in the contract shape. ``apply`` (present only when the plan
    changes the board) carries IDS ONLY — never dollars; ``expected`` maps
    every touched leg to its CURRENT driver (the from-driver staleness map,
    farmout_actions contract), ``expected_times`` every retimed leg to its
    CURRENT pickup time. ``key`` is the plan's content-stable identity (the
    move signature — ``id`` is positional, #p{rank}) and ``score`` the ranking
    score: together they drive the rail's guard-10 replacement hysteresis (a
    displayed plan is swapped only for a ~15%-better or -surviving one)."""
    moves_out = []
    for m in plan.moves:
        moves_out.append({
            "leg_id": m.leg_id,
            "summary": m.summary,
            "action": m.op,
            "from": m.from_label or None,
            "to": m.to_label or None,
            "resulting_slack_min": m.resulting_slack_min,
        })
    key_bits = [f"{m.op}:{m.leg_id}:{m.to_driver_id or ''}"
                + (f":{m.new_pickup_time.strftime('%H%M')}"
                   if m.new_pickup_time else "")
                for m in plan.moves]
    out = {
        "id": f"{d.id}#p{rank}",
        "key": plan.kind + "|" + ";".join(key_bits),
        "rank": rank,
        "title": plan.title,
        "why": list(plan.why),
        "risks": list(plan.risks),
        "farm_out": plan.farm_out,
        "price_impact": plan.price_impact,
        "score": plan.score,
        "moves": moves_out,
    }
    # Dispatcher-facing view of the same plan (plain language + the after-move
    # picture). ADDITIVE: every field above stays byte-for-byte what it was —
    # `title` still lands in the task resolution note, `why`/`risks` still feed
    # the "Show the math" expander. A display failure degrades this plan to the
    # old rendering; it never costs the dispatcher the Apply button.
    from dispatching.advisor_display import plan_display, safe_display
    out["display"] = safe_display(plan_display, board, d, plan, rank)
    if plan.moves:
        actions, expected, expected_times = [], {}, {}
        for m in plan.moves:
            leg = board.legs_by_id.get(m.leg_id)
            a = {"op": m.op, "leg_id": m.leg_id}
            if m.op in ("reassign", "farm_out"):
                a["to_driver_id"] = m.to_driver_id
            elif m.op == "unassign":
                a["to_driver_id"] = None
            if m.op == "retime" and m.new_pickup_time is not None:
                a["new_pickup_time"] = m.new_pickup_time.strftime("%H:%M")
            if m.note:
                a["note"] = m.note
            actions.append(a)
            expected[str(m.leg_id)] = (getattr(leg, "driver_id", None)
                                       if leg is not None else None)
        for c in plan.time_changes:
            expected_times[str(c.leg_id)] = c.old_time.strftime("%H:%M")
        out["apply"] = {
            "schema": 1,
            "date": board.target_date.isoformat(),
            "disruption_id": d.id,
            "plan_id": out["id"],
            "task_id": d.task_id,
            "title": plan.title,   # task resolution notes name the plan
            "actions": actions,
            "expected": expected,
            "expected_times": expected_times,
        }
        if plan.kind == "takeback":
            # Guard 7: the reused farmout hard rule refuses to pull an
            # affiliate-committed leg without confirm_pullback. A takeback
            # plan IS that pull-back, its card leads with the "call {aff}
            # first" risk line, and the dispatcher's Apply after reading it is
            # the explicit confirmation — so the payload carries the opt-in.
            out["apply"]["confirm_pullback"] = True
    return out


# ════════════════════════════════════════════════════════════════════════════
# PUBLIC ENTRY — full advisor state under the global wall-clock budget
# ════════════════════════════════════════════════════════════════════════════

def _advisor_state(board, fingerprint, for_leg_id=None):
    """Detection + per-card generation over an assembled BoardState. The first
    ADVISOR_MAX_DISRUPTIONS analyzable cards get full plans; the rest come
    back detected_only (truncated=True). Wall clock capped at
    ADVISOR_BUDGET_MS; each card's swap search draws
    min(ADVISOR_SWAP_TIME_MS, remaining_ms // remaining_cards). Split from
    compute_advisor_state so tests can hand-build boards."""
    t0 = _monotonic()
    disruptions = detect_disruptions(board)
    if for_leg_id:
        disruptions = [d for d in disruptions if for_leg_id in d.leg_ids]
    n_analyzable = sum(1 for d in disruptions if not d.abstain)
    n_plan = min(n_analyzable, ADVISOR_MAX_DISRUPTIONS)
    truncated = n_analyzable > ADVISOR_MAX_DISRUPTIONS

    from dispatching.advisor_display import card_display, safe_display

    # Presentation time is NOT charged against the analysis budget. Everything
    # below the display calls is pure rendering: if it counted, a slower
    # drawing could shrink a later card's swap search or tip it into
    # detected_only — a presentation change silently altering which plans the
    # engine finds. The budget stays a budget for analysis only.
    display_ms = 0

    cards, analyzed = [], 0
    for d in disruptions:
        card = serialize_disruption(d)
        # Plain-language headline/story + the conflict drawn once as a timeline.
        # Additive (see _serialize_plan): headline/narrative/basis stay verbatim
        # for the expander and for every consumer that predates this.
        _t = _monotonic()
        card["display"] = safe_display(card_display, board, d)
        display_ms += int((_monotonic() - _t) * 1000)
        do = (not d.abstain) and analyzed < ADVISOR_MAX_DISRUPTIONS
        remaining_ms = (ADVISOR_BUDGET_MS - int((_monotonic() - t0) * 1000)
                        + display_ms)
        if do and remaining_ms <= 0:
            do = False
            truncated = True
        plans = []
        if do:
            swap_ms = max(50, min(ADVISOR_SWAP_TIME_MS,
                                  remaining_ms // max(1, n_plan - analyzed)))
            plans = generate_plans(board, d, swap_time_ms=swap_ms)
            analyzed += 1
        _t = _monotonic()
        card["plans"] = [_serialize_plan(board, d, p, i + 1)
                         for i, p in enumerate(plans)]
        display_ms += int((_monotonic() - _t) * 1000)   # per-plan display, ditto
        card["detected_only"] = (not d.abstain) and not do
        # "No in-house fix" means exactly that: no plan that keeps the work on
        # our own drivers. Testing tiers instead would call a tight-turn
        # reassign (demoted to tier 3) an absence of in-house options, while
        # the card displays it.
        card["no_internal_solution"] = bool(
            do and _may_move_work(d)
            and not any(p.moves and not p.farm_out for p in plans))
        if card["no_internal_solution"] and d.details.get("swap_diagnostic"):
            card["diagnostic"] = d.details["swap_diagnostic"]
        # Guard 9: cards deep-link to their open task; ONLY when none exists
        # (and the card is a real disruption, not a hygiene/abstain card) they
        # OFFER filing one. The offer is serialized data — creation happens in
        # the file-task endpoint through ops.services.create_task, inheriting
        # its dedup/cooldown; the engine itself still never writes a task.
        if card["task_id"] is None and not d.abstain and d.leg_ids:
            card["file_task"] = {
                "leg_id": d.anchor_leg_id or d.leg_ids[0],
                "task_type": ("driver_assign" if d.kind == "unassigned"
                              else "driver_conflict"),
                "title": d.headline,
            }
        cards.append(card)
    return {
        "fingerprint": fingerprint,
        "computed_at": board.now.isoformat(),
        "truncated": truncated,
        "disruptions": cards,
    }


def compute_advisor_state(day, now=None, for_leg_id=None):
    """The advisor's public contract (consumed by the state endpoint and the
    ops task-detail integration): fingerprint + assembled board + detection +
    ranked, validated, explained plans — everything JSON-serializable.
    ``for_leg_id`` narrows the cards to those touching one leg (task detail).
    Read-only; ZERO external calls anywhere below."""
    fp = compute_board_fingerprint(day)
    board = build_board_state(day, now=now)
    return _advisor_state(board, fp, for_leg_id=for_leg_id)
