"""The Day-Builder (Build 3b, Ticket A — rescoped by the Ticket-C verdict).

WHAT THIS IS
------------
"Build a plan" for one service date: evaluate the day as the dispatcher has
set it up, then search a small, bounded set of PAIRING variants (which driver
takes which unit) for one that covers more of the day — scored through the
SHIPPED assignment pipeline so a plan's feasibility can never diverge from
what production would do with it (01 §A3, the Candidate-Plan Outer Loop; the
``dva_rows`` hook built in Build 3a is the whole mechanism).

THE RESCOPE (Ticket C, 2026-08-25 — decided under founder delegation)
---------------------------------------------------------------------
The surrogate-noise gate (analysis/16, 05 §4) CUT Pass A's roster-size ladder:
between-roster-size score differences do not exceed within-size jitter, on
every scalar, both rule readings, both test days. Per 05 §4's own cut path,
this builder therefore optimizes **pairing and splits at the dispatcher's
chosen headcount** — it never proposes changing how many drivers work.

Two further calls made under the same delegation, both documented here so
they can be revisited:

* **No call-outs baked into the plan itself.** Standby second shifts (mints)
  are phone calls to real people — call-first, dispatcher-owned, willingness
  unrecorded (03 §1). The plan never bakes one in; mint proposals stay where
  Build 2 put them, as dispatcher-ticked cards in the same panel. This also
  keeps Gate-4 criterion 9 (driver-days <= baseline) exact.
  **Refined by D16 (founder, 2026-08-25):** when trips are farming while
  certified drivers sit available and cars sit free, the plan DOES carry
  "catch the rest" suggestions — a NAMED bench driver on a NAMED free car,
  each verified by a full-pipeline evaluation to capture >=1 otherwise-farmed
  trip under the same walls (his rest, his window, certification). They ride
  in ``additions``, separate from ``assignments``; the dispatcher still ticks
  and makes the call. This is not the cut roster ladder — no size search,
  only targeted named additions that pay for themselves in captured trips.
* **Walls are counted as deltas against the day's pre-plan board.** 05's
  wall table sets every threshold at 0; on a cold day (the Gate-4 replay)
  delta == absolute. On a partially built day, a conflict the dispatcher's
  own existing assignments already carry is not the plan's fault (the same
  principle 05 states for the rest wall) — the plan must only never ADD one.

THE SCORE (05 §2 A1 — binding)
------------------------------
Lexicographic, never summed:  (driver_days, farm_outs, farm_cost, quality).
A candidate is ADMISSIBLE only if it breaks no wall and
``farm_outs <= baseline.farm_outs + epsilon`` (the Ticket-B dial; epsilon
buys farm-outs ONLY, never a wall). Because driver_days leads the tuple and
the baseline is always a candidate, the chosen plan can never use more
working drivers than the dispatcher's own setup produces — criterion 9 holds
structurally.

Walls (each discards, never penalizes):
  * NEW hard-infeasible turn pair      (board_validation + pickup_policy)
  * NEW driver-day over the 15.0h ceiling        (span_exception_max_hours)
  * NEW overnight-rest breach vs actual adjacent-day work  (510 both sides)
  * NEW co-driver share conflict                 (car_share convention A)
  * >2 drivers on one vehicle-date
  * a RED handoff band on a share THIS PLAN introduces (RED is shown,
    never suggested — 04 §3.2b)

quality = opt_w_span * Σ max(0, eff_span − 13.5)
        + opt_w_fairness * stdev(legs per working driver)
        + opt_w_handoff * (# AMBER handoff bands)
        + opt_w_gaps * (hours of internal gaps above idle_gap_threshold)
Weights are [assumed] starting values, live-editable, tie-break only.

POSTURE: STRICTLY READ-ONLY. This module never writes a Leg, a DVA row, or
anything else — candidate pairings are UNSAVED DriverVehicleAssignment
objects threaded through the pipeline. v1 is propose-only (05 Ticket E): the
panel tells the dispatcher what to change in Day Setup; the only write doors
remain ``apply_day_setup`` and ``auto_assign_drivers(apply=True)``, clicked
by a human. On a held (sandbox) date the builder REFUSES and names the draft
— the no-leak invariant is never risked.

RUNTIME: one evaluation is a full pipeline run (measured 3–27s on real
dates). ``pass_b_max_evals`` caps evaluations, ``opt_runtime_budget_s`` caps
wall-clock; hitting the budget returns best-so-far with
``budget_exhausted=True`` — visible, never silent. Never call this in the
request cycle — Ticket D's job wrapper (``start_day_plan_job``) runs it on
the existing ``_run_in_background`` daemon-thread pattern.
"""
import json
import logging
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


class PlanRefused(Exception):
    """The builder declines to plan this date, with a reason a dispatcher can act on."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


# ════════════════════════════════════════════════════════════════════════════
# RESULT
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class DayPlanResult:
    """The plan. Everything the panel renders and the Gate-4 harness judges.

    ``assignments`` covers legs the plan would place (legs already assigned on
    the live board are respected as-is and counted in ``assigned_existing``).
    ``shares`` lists ONLY shares this plan introduces (a share the roster
    already carries is the dispatcher's standing arrangement, reported in
    ``existing_shares`` for context, never gated as "proposed").
    """
    date: str
    epsilon: int
    roster_driver_ids: list
    dva_rows: list                     # [(driver_id, vehicle_id)] — the chosen pairing
    assignments: dict                  # {leg_id: driver_id} planned by this run
    assigned_existing: int
    farmed_leg_ids: list
    farm_cost_usd: float
    farm_cost_fallback_legs: int       # legs priced at the flat premium (rate unknown)
    exceptions: list                   # [{driver_id, driver_name, eff_hours, legs_kept, price_usd}]
    shares: list                       # plan-INTRODUCED shares [{vehicle_id, band, ...}]
    existing_shares: list
    swaps: list                        # human-readable pairing changes vs today
    baseline: dict                     # the seed (as-set-today) metrics
    score: dict                        # the chosen plan's metrics
    epsilon_note: str
    instructions: str
    evaluations: int
    wall_clock_s: float
    budget_exhausted: bool
    computed_at: str
    bookings_as_of: str
    farmed_summaries: list = field(default_factory=list)
    #: the engine build's own hard-rule issues beyond the live board — shown,
    #: never hidden (the Gate-4 harness still judges the plan absolutely)
    seed_wall_notes: list = field(default_factory=list)
    #: "catch the rest" (D16): named bench drivers on named free cars, each
    #: verifiably capturing >=1 otherwise-farmed trip. Propose-only — the
    #: dispatcher ticks and makes the call. NOT part of `assignments`.
    additions: list = field(default_factory=list)
    #: the day's numbers IF every addition is ticked (from the full-pipeline
    #: evaluation of the augmented roster), or None when there are none
    with_additions: dict = None
    #: plain-language reason when farmed trips remain and no addition helps
    additions_note: str = ""

    def to_payload(self):
        d = dict(self.__dict__)
        d["assignments"] = {str(k): v for k, v in self.assignments.items()}
        return d


# ════════════════════════════════════════════════════════════════════════════
# DAY LOADING — the view's bare-payload derivation, verbatim
# ════════════════════════════════════════════════════════════════════════════

def _day_roster(target_date):
    """DVA-eligible in-house, active, saved availability — exactly the roster
    ``views.auto_assign_drivers`` derives with no modal payload."""
    from drivers.models import Driver, DriverVehicleAssignment
    eligible = set(DriverVehicleAssignment.objects.filter(
        date=target_date, driver__driver_type="inhouse")
        .values_list("driver_id", flat=True))
    drivers = list(Driver.objects.filter(
        driver_type="inhouse", is_active=True, id__in=eligible)
        .select_related("profile")
        .prefetch_related("weekly_schedule", "date_overrides"))
    driver_hours, flexible = {}, set()
    for d in drivers:
        is_avail, sh, eh, _pref, flex = d.get_availability_for_date(target_date)
        if is_avail:
            driver_hours[d.id] = (sh, eh)
            if flex:
                flexible.add(d.id)
    drivers = [d for d in drivers if d.id in driver_hours]
    driver_max_hours = {}
    for d in drivers:
        fa = d.get_full_availability(target_date)
        if fa.get("max_hours"):
            driver_max_hours.setdefault(d.id, float(fa["max_hours"]))
    return drivers, driver_hours, flexible, driver_max_hours


def _load_day_legs(target_date):
    from reservations.models import Leg
    return list(
        Leg.objects.filter(pickup_date=target_date)
        .exclude(reservation__status="cancelled")
        .exclude(status="cancelled")
        .select_related("driver", "driver__profile", "reservation",
                        "reservation__vehicle", "vehicle", "flight_information")
        .prefetch_related("legstop_set", "legflight_set")
    )


def _vtype_str(vehicle):
    try:
        return str(vehicle.vehicle_type.vehicle_type) if vehicle and vehicle.vehicle_type else ""
    except Exception:
        return ""


def _leg_cost(leg):
    """Farm price of one leg: the captured affiliate rate, else the flat premium."""
    from dispatching.fleet_intel import affiliate_base_cost
    from dispatching.standby_mints import FARMOUT_PREMIUM_PER_LEG
    c = affiliate_base_cost(leg) if leg is not None else None
    if c is None:
        return float(FARMOUT_PREMIUM_PER_LEG), True
    return float(c), False


# ════════════════════════════════════════════════════════════════════════════
# ONE EVALUATION — pipeline run + walls + score, all re-derived from the board
# ════════════════════════════════════════════════════════════════════════════

class _Blk:
    """Engine-clock block (pick / start / end) for standby_mints.best_window —
    used only to price the 13.5–15h crunch exception ('what would a capped day
    shed?')."""
    __slots__ = ("pick", "start", "end", "leg_id")

    def __init__(self, pick, start, end, leg_id):
        self.pick, self.start, self.end, self.leg_id = pick, start, end, leg_id


def _evaluate(label, dva_rows, ctx):
    """Run the shipped pipeline under a candidate pairing and re-derive every
    judgeable figure from the resulting board. Returns a dict; never writes."""
    from dispatching.assignment_pipeline import (
        PipelineLocks, PipelineWindows, run_assignment_pipeline)
    from dispatching.scheduler import (
        build_driver_schedules, build_sharer_partners, effective_span_hours,
        sharers_conflict)
    from dispatching.board_validation import board_turn_bands
    from dispatching.route_distance import probe_mode
    from dispatching import feasibility_guards as fg

    t0 = _time.monotonic()
    with probe_mode():
        return _evaluate_inner(
            label, dva_rows, ctx, t0, run_assignment_pipeline, PipelineWindows,
            PipelineLocks, build_driver_schedules, build_sharer_partners,
            effective_span_hours, sharers_conflict, board_turn_bands, fg)


def _evaluate_inner(label, dva_rows, ctx, t0, run_assignment_pipeline,
                    PipelineWindows, PipelineLocks, build_driver_schedules,
                    build_sharer_partners, effective_span_hours,
                    sharers_conflict, board_turn_bands, fg):
    """Body of _evaluate, inside the route-distance probe window: a candidate
    evaluation may read every known drive time but must never enqueue one —
    a hypothetical board is not allowed to spend money (Ticket D)."""
    res = run_assignment_pipeline(
        ctx["legs"], ctx["drivers"], ctx["date"],
        PipelineWindows(driver_hours=ctx["driver_hours"],
                        flexible_drivers=ctx["flexible"],
                        driver_max_hours=ctx["driver_max_hours"],
                        run_min_buffer=ctx["run_min_buffer"],
                        driver_min_buffers=ctx["driver_min_buffers"]),
        PipelineLocks(), dva_rows=dva_rows)

    # Full post-plan board: existing assignments + this run's placements.
    stamped = []
    for lid, did in res.assignments.items():
        lg = ctx["legs_by_id"].get(lid)
        if lg is not None and did in ctx["drivers_by_id"]:
            lg.driver = ctx["drivers_by_id"][did]
            lg.driver_id = did
            stamped.append(lg)
    board = build_driver_schedules(ctx["legs"], ctx["drivers"], ctx["date"],
                                   dva_rows=dva_rows)
    bands = board_turn_bands(board, ctx["date"])
    partners = build_sharer_partners(
        {d.id for d in ctx["drivers"]}, ctx["date"], rows=dva_rows)
    share_conflicts = set()
    if partners:
        for did in partners:
            sched = board.get(did)
            for s in (sched.slots if sched else []):
                lg = ctx["legs_by_id"].get(s.leg_id)
                if lg is not None and sharers_conflict(
                        lg, did, partners, board, ctx["date"]):
                    share_conflicts.add((did, s.leg_id))
    # Planned legs are disjoint from live-assigned legs (the pipeline only
    # places unassigned ones), so unstamping restores the exact input state.
    for lg in stamped:
        lg.driver = None
        lg.driver_id = None

    farm_outs = len(res.unassigned) - len(res.assignments)
    farmed = sorted({l.id for l in res.unassigned} - set(res.assignments.keys()))
    farm_cost, fallback = 0.0, 0
    for lid in farmed:
        c, fb = _leg_cost(ctx["legs_by_id"].get(lid))
        farm_cost += c
        fallback += 1 if fb else 0

    criticals = {k for k, i in bands.items() if i["band"] == "critical"}
    spans, counts = {}, []
    span_pressure = idle_gap_h = 0.0
    for did, sched in board.items():
        slots = sorted(sched.slots, key=lambda s: (s.pickup_time, s.leg_id))
        if not slots:
            continue
        counts.append(len(slots))
        raw, eff = effective_span_hours(slots, ctx["date"])
        spans[did] = (raw, eff)
        span_pressure += max(0.0, eff - fg.SPAN_SOFT_EFFECTIVE_HOURS)
        for a, b in zip(slots, slots[1:]):
            gap_min = (datetime.combine(ctx["date"], b.pickup_time)
                       - a.estimated_end_time).total_seconds() / 60.0
            if gap_min > ctx["idle_gap_threshold"]:
                idle_gap_h += gap_min / 60.0
    over_hard = {d for d, (_r, e) in spans.items() if e > ctx["hard_cap"]}
    over_soft = {d for d, (_r, e) in spans.items()
                 if ctx["hard_cap"] >= e > fg.SPAN_SOFT_EFFECTIVE_HOURS}

    # Rest vs ACTUAL adjacent-day work (skipped entirely when the floor is 0).
    rest_breaches = set()
    if ctx["rest_min"] > 0:
        for did, sched in board.items():
            slots = sorted(sched.slots, key=lambda s: (s.pickup_time, s.leg_id))
            if not slots:
                continue
            first = datetime.combine(ctx["date"], slots[0].pickup_time)
            last = max(s.estimated_end_time for s in slots)
            pe = ctx["prev_end_by_driver"].get(did)
            if pe is not None and (first - pe).total_seconds() / 60.0 < ctx["rest_min"]:
                rest_breaches.add((did, "prev"))
            nf = ctx["next_first_by_driver"].get(did)
            if nf is not None and (nf - last).total_seconds() / 60.0 < ctx["rest_min"]:
                rest_breaches.add((did, "next"))

    # Shares under this pairing (+ band each) — the handoff quality/wall input.
    shares = _bands_for_shares(dva_rows, board, ctx)
    amber = sum(1 for s in shares if s["band"] == "amber")

    if counts:
        mean = sum(counts) / len(counts)
        fairness = (sum((c - mean) ** 2 for c in counts) / len(counts)) ** 0.5
    else:
        fairness = 0.0
    quality = (ctx["cfg"].opt_w_span * span_pressure
               + ctx["cfg"].opt_w_fairness * fairness
               + ctx["cfg"].opt_w_handoff * amber
               + ctx["cfg"].opt_w_gaps * idle_gap_h)

    driver_days = sum(1 for sched in board.values() if sched.slots)
    return {
        "label": label, "dva_rows": dva_rows,
        "assignments": dict(res.assignments),
        "farm_outs": farm_outs, "farmed": farmed,
        "farm_cost": round(farm_cost, 2), "cost_fallback": fallback,
        "driver_days": driver_days,
        "criticals": criticals, "over_hard": over_hard, "over_soft": over_soft,
        "rest_breaches": rest_breaches, "share_conflicts": share_conflicts,
        "spans": spans, "span_pressure": round(span_pressure, 2),
        "fairness": round(fairness, 3), "idle_gap_h": round(idle_gap_h, 2),
        "amber": amber, "shares": shares,
        "quality": round(quality, 3),
        "eval_s": round(_time.monotonic() - t0, 1),
    }


def _bands_for_shares(dva_rows, board, ctx):
    """Every unit held by two roster drivers under this pairing, banded via the
    Build-2 chain (clear = last booked pickup + P50 occupancy tail — the same
    arithmetic the panel and analysis/11's calibration use)."""
    from dispatching.car_share import holders_by_unit
    from dispatching.analytics import categorize_location
    from dispatching.handoff_chain import (
        handoff_band, occupancy_kind, OCCUPANCY_LEAD_TAIL_P50)
    from dispatching import feasibility_guards as fg

    roster = {d.id for d in ctx["drivers"]}
    units = holders_by_unit(
        (r.driver_id, r.vehicle_id) for r in dva_rows if r.driver_id in roster)
    out = []
    for vid, holders in units.items():
        if len(holders) < 2:
            continue
        active = [h for h in holders
                  if board.get(h) is not None and board[h].slots]
        entry = {"vehicle_id": vid, "driver_ids": sorted(holders),
                 "band": None, "cut_hour": ctx["cfg"].share_split_hour,
                 "reason": ""}
        if len(active) >= 2:
            a, b = sorted(active, key=lambda h: min(
                s.pickup_time for s in board[h].slots))[:2]
            a_slots = sorted(board[a].slots, key=lambda s: s.pickup_time)
            b_slots = sorted(board[b].slots, key=lambda s: s.pickup_time)
            out_slot, in_slot = a_slots[-1], b_slots[0]
            drop_zone = categorize_location(out_slot.dropoff_location)
            pick_zone = categorize_location(in_slot.pickup_location)
            kind = occupancy_kind(
                categorize_location(out_slot.pickup_location), drop_zone)
            tail = OCCUPANCY_LEAD_TAIL_P50[kind][1]
            clear = (datetime.combine(ctx["date"], out_slot.pickup_time)
                     + timedelta(minutes=tail))
            gap_min = (datetime.combine(ctx["date"], in_slot.pickup_time)
                       - clear).total_seconds() / 60.0
            bd = handoff_band(
                drop_zone, pick_zone, gap_min,
                incoming_is_arrival=(pick_zone in fg.AIRPORT_TERMINALS),
                green_pct=ctx["cfg"].handoff_gap_green_pct,
                amber_floor_pct=ctx["cfg"].handoff_gap_amber_floor_pct)
            mid = clear + (datetime.combine(ctx["date"], in_slot.pickup_time) - clear) / 2
            entry.update({"band": bd["band"], "reason": bd["reason"],
                          "cut_hour": mid.hour,
                          "am_driver_id": a, "pm_driver_id": b})
        out.append(entry)
    return out


# ════════════════════════════════════════════════════════════════════════════
# THE BUILD
# ════════════════════════════════════════════════════════════════════════════

def build_day_plan(target_date, *, epsilon=None, runtime_budget_s=None):
    """Build the day's plan. Read-only; returns a DayPlanResult or raises
    PlanRefused with a dispatcher-actionable reason. See the module docstring
    for scope and scoring. Minutes-scale on a real day — run it through
    ``start_day_plan_job``, never in the request cycle."""
    from django.utils import timezone
    from drivers.models import DriverVehicleAssignment
    from dispatching.assignment import _active_draft_for_date
    from dispatching.models import SchedulerSettings
    from dispatching.scheduler import (
        preload_timing_cache, resolve_run_min_buffer, load_driver_min_buffers,
        estimate_job_end_time, get_vehicle_tier)
    from dispatching import feasibility_guards as fg
    from reservations.models import Leg

    t_start = _time.monotonic()
    bookings_as_of = timezone.now()
    cfg = SchedulerSettings.get_settings()
    if epsilon is None:
        epsilon = int(cfg.opt_epsilon_farmouts or 0)
    epsilon = max(0, min(3, int(epsilon)))
    budget_s = float(runtime_budget_s or cfg.opt_runtime_budget_s or 240)

    # ── Ticket E refusals: held date, no roster, no demand ──
    draft = _active_draft_for_date(target_date)
    if draft is not None:
        state = (draft.get_state_display()
                 if hasattr(draft, "get_state_display") else draft.state)
        raise PlanRefused(
            f"This date is held for review (draft #{draft.pk}, {state}). "
            f"The builder never plans against a day someone is reviewing — "
            f"publish or discard the draft first.")

    drivers, driver_hours, flexible, driver_max_hours = _day_roster(target_date)
    if not drivers:
        raise PlanRefused(
            "No roster for this date yet — run Day Setup (tick the drivers, "
            "Apply) and try again. The builder plans at YOUR chosen headcount; "
            "it never picks the crew.")
    legs = _load_day_legs(target_date)
    if not legs:
        raise PlanRefused("No trips booked for this date — nothing to plan.")

    preload_timing_cache()
    dva_all = list(DriverVehicleAssignment.objects
                   .filter(date=target_date)
                   .select_related("vehicle", "vehicle__vehicle_type"))

    legs_by_id = {l.id: l for l in legs}
    drivers_by_id = {d.id: d for d in drivers}
    existing_assign = {l.id: l.driver_id for l in legs if l.driver_id}

    # Adjacent-day ACTUAL work, engine clocks (rest wall inputs).
    prev_day, next_day = (target_date - timedelta(days=1),
                          target_date + timedelta(days=1))
    prev_end_by_driver, next_first_by_driver = {}, {}
    ids = set(driver_hours.keys())
    for pl in (Leg.objects.filter(pickup_date=prev_day, driver_id__in=ids)
               .exclude(status="cancelled")
               .select_related("reservation", "flight_information")):
        try:
            end = estimate_job_end_time(pl, prev_day)
        except Exception:
            continue
        if end > prev_end_by_driver.get(pl.driver_id, datetime.min):
            prev_end_by_driver[pl.driver_id] = end
    for nl in (Leg.objects.filter(pickup_date=next_day, driver_id__in=ids)
               .exclude(status="cancelled")):
        if nl.pickup_time is None:
            continue
        dtm = datetime.combine(next_day, nl.pickup_time)
        if dtm < next_first_by_driver.get(nl.driver_id, datetime.max):
            next_first_by_driver[nl.driver_id] = dtm

    ctx = {
        "date": target_date, "cfg": cfg, "legs": legs, "drivers": drivers,
        "legs_by_id": legs_by_id, "drivers_by_id": drivers_by_id,
        "driver_hours": driver_hours, "flexible": flexible,
        "driver_max_hours": driver_max_hours,
        "run_min_buffer": resolve_run_min_buffer(None),
        "driver_min_buffers": load_driver_min_buffers(list(ids)),
        "existing_assign": existing_assign,
        "prev_end_by_driver": prev_end_by_driver,
        "next_first_by_driver": next_first_by_driver,
        "rest_min": float(cfg.rest_min_gap_minutes or 0),
        "hard_cap": float(cfg.span_exception_max_hours),
        "idle_gap_threshold": float(cfg.idle_gap_threshold or 120),
    }

    # ── The seed: the dispatcher's pairing exactly as Day Setup applied it. ──
    evals = [_evaluate("as set today", dva_all, ctx)]
    seed = evals[0]

    # ── Walls: NO-WORSE-THAN-SEED (decided under founder delegation,
    # 2026-08-25, after the first cold replay). The seed is what the ordinary
    # Build Schedule click hands the dispatcher today; the shipped engine
    # treats overnight rest as a soft penalty, so on a cold day its own build
    # can sit rest-tight against the adjacent days' ACTUAL boards. Reading
    # 05's walls as absolute-vs-empty would then discard every candidate —
    # including the exact board the ordinary button produces — and the tool
    # would refuse days it should serve. So: a candidate is discarded when it
    # is WORSE than the seed on any wall dimension (it may never ADD a
    # critical turn, an over-15h day, a rest breach, or a share conflict, and
    # may never introduce a RED-band share — that one absolute, since
    # introducing a RED is always the plan's own act). The seed's own issues
    # are not hidden: they ship in the payload as `seed_wall_notes`, and the
    # Gate-4 harness (analysis/17) still judges the FINAL plan absolutely —
    # this wall never weakens the acceptance bar, only keeps the tool
    # well-defined on days the shipped engine itself is imperfect.
    pre = _preexisting_walls(ctx)          # vs the LIVE board — for the notes
    seed_units = {frozenset(s["driver_ids"]): s["vehicle_id"]
                  for s in seed["shares"]}

    def walls(e, base=None):
        base = base if base is not None else seed
        out = []
        if e["criticals"] - base["criticals"]:
            out.append(f"adds hard-infeasible turn pair(s): "
                       f"{sorted(e['criticals'] - base['criticals'])[:3]}")
        if e["over_hard"] - base["over_hard"]:
            out.append(f"adds a driver-day over {ctx['hard_cap']:g}h: "
                       f"{sorted(e['over_hard'] - base['over_hard'])}")
        if e["rest_breaches"] - base["rest_breaches"]:
            out.append(f"adds a rest-floor breach: "
                       f"{sorted(e['rest_breaches'] - base['rest_breaches'])}")
        if e["share_conflicts"] - base["share_conflicts"]:
            out.append("double-books a shared car")
        over2 = [s for s in e["shares"] if len(s["driver_ids"]) > 2]
        if over2:
            out.append(f">2 drivers on one unit: {[s['vehicle_id'] for s in over2]}")
        for s in e["shares"]:
            if (frozenset(s["driver_ids"]) not in seed_units
                    and s["band"] == "red"):
                out.append(f"introduces a RED handoff on unit {s['vehicle_id']}")
        return out

    # The engine build's own issues beyond the live board — shown, never hidden.
    seed_wall_notes = []
    if seed["criticals"] - pre["criticals"]:
        seed_wall_notes.append(
            f"{len(seed['criticals'] - pre['criticals'])} hard-infeasible turn "
            f"pair(s) in the engine build itself")
    if seed["over_hard"] - pre["over_hard"]:
        seed_wall_notes.append(
            f"engine build puts driver(s) {sorted(seed['over_hard'] - pre['over_hard'])} "
            f"over {ctx['hard_cap']:g}h")
    _new_rest = seed["rest_breaches"] - pre["rest_breaches"]
    if _new_rest:
        _names = sorted({str(drivers_by_id.get(d, d)) for d, _side in _new_rest})
        seed_wall_notes.append(
            f"engine build sits under the {ctx['rest_min']:.0f}-min overnight "
            f"rest floor for: {', '.join(_names)} (vs their actual adjacent-day "
            f"work) — same as an ordinary Build Schedule run today")

    # ── Targeted pairing swaps (05 §2 A4.2): tier-changing or share-reshaping. ──
    unit_by_driver = {}
    for r in dva_all:
        if r.driver_id in drivers_by_id and r.vehicle is not None:
            unit_by_driver[r.driver_id] = r.vehicle
    farmed_tiers = []
    for lid in seed["farmed"]:
        lg = legs_by_id.get(lid)
        vt = lg.effective_vehicle_type if lg is not None else None
        farmed_tiers.append((lid, get_vehicle_tier(str(vt)) if vt else -1,
                             lg.pickup_time if lg is not None else None))
    cand_swaps = []
    dids = sorted(unit_by_driver.keys())
    for i, a in enumerate(dids):
        for b in dids[i + 1:]:
            ua, ub = unit_by_driver[a], unit_by_driver[b]
            ta = get_vehicle_tier(_vtype_str(ua))
            tb = get_vehicle_tier(_vtype_str(ub))
            if ta == tb:
                continue                       # not a tier-constraint change
            lo_d, hi_d = (a, b) if ta < tb else (b, a)
            lo_t, hi_t = min(ta, tb), max(ta, tb)
            win = driver_hours.get(lo_d)
            benefit = 0
            for _lid, lt, pt in farmed_tiers:
                if lo_t < lt <= hi_t and pt is not None and win is not None:
                    if lo_d in flexible or win[0] <= pt.hour <= win[1]:
                        benefit += 1
            if benefit > 0:
                cand_swaps.append((-benefit, a, b))
    cand_swaps.sort()
    max_swaps = int(cfg.pass_b_max_swaps or 6)
    max_evals = int(cfg.pass_b_max_evals or 10)
    budget_exhausted = False
    for _negb, a, b in cand_swaps[:max_swaps]:
        if len(evals) >= max_evals:
            break
        if _time.monotonic() - t_start > budget_s:
            budget_exhausted = True
            break
        swapped = []
        for r in dva_all:
            if r.driver_id == a:
                swapped.append(DriverVehicleAssignment(
                    date=target_date, driver_id=a, vehicle=unit_by_driver[b]))
            elif r.driver_id == b:
                swapped.append(DriverVehicleAssignment(
                    date=target_date, driver_id=b, vehicle=unit_by_driver[a]))
            else:
                swapped.append(r)
        label = (f"{drivers_by_id[a]} and {drivers_by_id[b]} swap units: "
                 f"{drivers_by_id[a]} takes #{unit_by_driver[b].vehicle_number} "
                 f"({_vtype_str(unit_by_driver[b])}), "
                 f"{drivers_by_id[b]} takes #{unit_by_driver[a].vehicle_number} "
                 f"({_vtype_str(unit_by_driver[a])})")
        evals.append(_evaluate(label, swapped, ctx))
    # An evaluation that STRADDLES the ceiling must still flag it — the budget
    # is a hard, visible truncation, never a silent one (05 §2 A5).
    if _time.monotonic() - t_start > budget_s:
        budget_exhausted = True

    # ── Admission + the lexicographic choice (A1). Seed first: stable ties. ──
    def key(e):
        return (e["driver_days"], e["farm_outs"], e["farm_cost"], e["quality"])

    def admissible(eps):
        return [e for e in evals
                if not walls(e) and e["farm_outs"] <= seed["farm_outs"] + eps]

    adm = admissible(epsilon)
    if not adm:
        # Unreachable by construction (the seed passes its own walls and its
        # own farm-out constraint) — kept as a loud safety net.
        raise PlanRefused("No admissible plan could be built for this day.")
    best = min(adm, key=key)
    epsilon_note = ""
    if epsilon > 0:
        adm0 = admissible(0)
        best0 = min(adm0, key=key) if adm0 else None
        if best0 is not None and best0 is not best:
            dfarm = best["farm_outs"] - best0["farm_outs"]
            dcost = best["farm_cost"] - best0["farm_cost"]
            dspan = best0["span_pressure"] - best["span_pressure"]
            epsilon_note = (
                f"Allowing {epsilon} more farm-out(s) changes the answer: "
                f"'{best['label']}' farms {dfarm} more leg(s) (≈ ${dcost:,.0f}) "
                f"and takes {max(dspan, 0):.1f} h of over-target hours off the "
                f"crew vs the strict plan.")

    # ── "Catch the rest" — named bench additions (founder ruling D16,
    # 2026-08-25: when trips are farming while certified drivers sit available
    # and cars sit free, propose ticking them — he would "rather have 100%
    # in-house and give a driver a few jobs than farm a job"). This is NOT the
    # cut roster ladder: no size search — each suggestion is a NAMED driver on
    # a NAMED free car that verifiably captures ≥1 otherwise-farmed trip
    # through the same full-pipeline evaluation, passes the same walls (his
    # overnight rest, availability window, certification), or it is not shown.
    # Propose-only: the dispatcher ticks and makes the call, exactly like a
    # Build-2 second-shift card. The fixed-headcount plan above is unchanged.
    additions = []
    additions_note = ""
    current = best
    if best["farmed"] and (len(evals) >= max_evals or budget_exhausted):
        additions_note = ("Ran out of time before testing extra drivers — "
                          "re-build to try again.")
    if best["farmed"] and len(evals) < max_evals and not budget_exhausted:
        from dispatching.day_setup import _is_excluded, _unit_label
        from drivers.models import FleetVehicle

        held_unit_ids = {r.vehicle_id for r in dva_all if r.vehicle_id}
        free_units = [u for u in FleetVehicle.objects.filter(is_active=True)
                      .select_related("vehicle_type")
                      if u.id not in held_unit_ids
                      and not u.is_out_of_service_on(target_date)]
        roster_ids = {d.id for d in drivers}
        from drivers.models import Driver
        bench = []
        for d in (Driver.objects.filter(driver_type="inhouse", is_active=True)
                  .exclude(id__in=roster_ids)
                  .select_related("profile")
                  .prefetch_related("weekly_schedule", "date_overrides",
                                    "certified_vehicle_types")):
            if _is_excluded(d):
                continue
            is_avail, sh, eh, _pref, flex = d.get_availability_for_date(target_date)
            if is_avail:
                bench.append((d, sh, eh, flex))
        if not bench:
            additions_note = ("No spare drivers today — everyone available is "
                              "already working or off, so the farmed trip(s) "
                              "stay farmed.")
        elif not free_units:
            additions_note = ("Every roadworthy car is already out — no free "
                              "car to put another driver in, so the farmed "
                              "trip(s) stay farmed.")
        if bench and free_units:
            # Extend the adjacent-day rest maps to the bench, so an addition's
            # overnight rest is walled exactly like a rostered driver's.
            bench_ids = [d.id for d, _s, _e, _f in bench]
            for pl in (Leg.objects.filter(pickup_date=prev_day,
                                          driver_id__in=bench_ids)
                       .exclude(status="cancelled")
                       .select_related("reservation", "flight_information")):
                try:
                    end = estimate_job_end_time(pl, prev_day)
                except Exception:
                    continue
                if end > prev_end_by_driver.get(pl.driver_id, datetime.min):
                    prev_end_by_driver[pl.driver_id] = end
            for nl in (Leg.objects.filter(pickup_date=next_day,
                                          driver_id__in=bench_ids)
                       .exclude(status="cancelled")):
                if nl.pickup_time is None:
                    continue
                dtm = datetime.combine(next_day, nl.pickup_time)
                if dtm < next_first_by_driver.get(nl.driver_id, datetime.max):
                    next_first_by_driver[nl.driver_id] = dtm

            def _farmed_meta(e):
                out = []
                for lid in e["farmed"]:
                    lg = legs_by_id.get(lid)
                    if lg is None or lg.pickup_time is None:
                        continue
                    vt = lg.effective_vehicle_type
                    out.append((lid, get_vehicle_tier(str(vt)) if vt else 0,
                                lg.pickup_time))
                return out

            def _rank(cands, farmed_meta):
                ranked = []
                for d, sh, eh, flex in cands:
                    reach = [(lid, t) for lid, t, pt in farmed_meta
                             if flex or sh <= pt.hour <= eh]
                    if not reach:
                        continue
                    units = [u for u in free_units if d.can_drive(u.vehicle_type)]
                    if not units:
                        continue
                    best_u, best_key = None, None
                    for u in units:
                        ut = get_vehicle_tier(_vtype_str(u))
                        catch = sum(1 for _lid, t in reach if t <= ut)
                        k = (-catch, ut, u.id)   # most catchable, smallest car
                        if best_key is None or k < best_key:
                            best_u, best_key = u, k
                    if best_key[0] == 0:         # his best car catches nothing
                        continue
                    ranked.append((best_key[0], d.id, d, sh, eh, flex, best_u))
                # most potential captures first (-catch ascending), then lower id
                ranked.sort(key=lambda r: (r[0], r[1]))
                return [(d, sh, eh, flex, u) for _negc, _did, d, sh, eh, flex, u
                        in ranked]

            tried = set()
            for d, sh, eh, flex, unit in _rank(bench, _farmed_meta(current)):
                if (not current["farmed"] or len(evals) >= max_evals
                        or _time.monotonic() - t_start > budget_s):
                    if _time.monotonic() - t_start > budget_s:
                        budget_exhausted = True
                    break
                if d.id in tried:
                    continue
                tried.add(d.id)
                rows2 = list(current["dva_rows"]) + [DriverVehicleAssignment(
                    date=target_date, driver_id=d.id, vehicle=unit)]
                ctx2 = dict(ctx)
                ctx2["drivers"] = list(ctx["drivers"]) + [d]
                ctx2["drivers_by_id"] = dict(ctx["drivers_by_id"])
                ctx2["drivers_by_id"][d.id] = d
                ctx2["driver_hours"] = dict(ctx["driver_hours"])
                ctx2["driver_hours"][d.id] = (sh, eh)
                ctx2["driver_max_hours"] = dict(ctx["driver_max_hours"])
                fa = d.get_full_availability(target_date)
                if fa.get("max_hours"):
                    ctx2["driver_max_hours"].setdefault(d.id, float(fa["max_hours"]))
                if flex:
                    ctx2["flexible"] = set(ctx["flexible"]) | {d.id}
                ev = _evaluate(f"+ {d} on {_unit_label(unit)}", rows2, ctx2)
                evals.append(ev)
                captured = sorted(set(current["farmed"]) - set(ev["farmed"]))
                if (not captured or ev["farm_outs"] >= current["farm_outs"]
                        or walls(ev, base=current)):
                    continue
                kept = 0.0
                cap_detail = []
                for lid in captured:
                    lg = legs_by_id.get(lid)
                    c, _fb = _leg_cost(lg)
                    kept += c
                    cap_detail.append({
                        "leg_id": lid,
                        "pickup": (lg.pickup_time.strftime("%I:%M %p").lstrip("0")
                                   if lg is not None and lg.pickup_time else ""),
                        "route": (f"{(lg.pickup_location or '')[:30]} → "
                                  f"{(lg.dropoff_location or '')[:30]}"
                                  if lg is not None else ""),
                    })
                additions.append({
                    "driver_id": d.id, "driver_name": str(d),
                    "vehicle_id": unit.id, "vehicle_label": _unit_label(unit),
                    "window": (f"{sh}:00–{eh}:00" if not flex else "flexible"),
                    "captured": cap_detail,
                    "captured_leg_ids": captured,
                    "kept_usd": round(kept, 2),
                    "farmed_after": ev["farm_outs"],
                })
                # Update the maps the next candidate is judged against.
                ctx["drivers"] = ctx2["drivers"]
                ctx["drivers_by_id"] = ctx2["drivers_by_id"]
                ctx["driver_hours"] = ctx2["driver_hours"]
                ctx["driver_max_hours"] = ctx2["driver_max_hours"]
                ctx["flexible"] = ctx2["flexible"] if flex else ctx["flexible"]
                current = ev

            if current["farm_outs"] > 0 and not additions_note:
                if additions:
                    additions_note = (
                        f"{current['farm_outs']} trip(s) still farm even with "
                        f"the tick(s) above — no driver/car combination "
                        f"reaches them.")
                else:
                    additions_note = (
                        "Tried putting a spare driver on a free car — none of "
                        "them can reach the farmed trip(s) (timing, overnight "
                        "rest, or vehicle size). Farming them is the right "
                        "call today.")

    # ── Render-ready pieces ──
    total = len(legs)
    def cov(e):
        return 100.0 * (len(existing_assign) + len(e["assignments"])) / total if total else 0.0

    exceptions = _priced_exceptions(best, ctx)
    proposed_shares = [s for s in best["shares"]
                       if frozenset(s["driver_ids"]) not in seed_units]
    swaps_desc = [] if best is seed else [best["label"]]
    farmed_summaries = []
    for lid in best["farmed"][:60]:
        lg = legs_by_id.get(lid)
        if lg is None:
            continue
        c, _fb = _leg_cost(lg)
        farmed_summaries.append({
            "leg_id": lid,
            "pickup": lg.pickup_time.strftime("%I:%M %p").lstrip("0") if lg.pickup_time else "",
            "route": f"{(lg.pickup_location or '')[:30]} → {(lg.dropoff_location or '')[:30]}",
            "cost_usd": round(c, 2),
        })

    instructions = (
        "Your current setup is already the best this search found — build the "
        "schedule as usual." if best is seed else
        f"In Day Setup: {best['label']}, then Apply and re-build the schedule. "
        f"Nothing is changed until you do — this is a proposal.")
    if additions:
        instructions += (
            f" To go further: tick the {len(additions)} suggested driver"
            f"{'s' if len(additions) != 1 else ''} below, Apply, and re-build "
            f"— each one catches trips that would otherwise farm out.")

    with_additions = None
    if additions:
        with_additions = {
            "coverage_pct": round(cov(current), 1),
            "farm_outs": current["farm_outs"],
            "farm_cost_usd": current["farm_cost"],
            "driver_days": current["driver_days"],
            "kept_usd": round(sum(a["kept_usd"] for a in additions), 2),
        }

    result = DayPlanResult(
        date=target_date.isoformat(),
        epsilon=epsilon,
        roster_driver_ids=sorted(d.id for d in drivers),
        dva_rows=sorted((r.driver_id, r.vehicle_id) for r in best["dva_rows"]
                        if r.vehicle_id is not None),
        assignments=best["assignments"],
        assigned_existing=len(existing_assign),
        farmed_leg_ids=best["farmed"],
        farm_cost_usd=best["farm_cost"],
        farm_cost_fallback_legs=best["cost_fallback"],
        exceptions=exceptions,
        shares=proposed_shares,
        existing_shares=[s for s in best["shares"] if s not in proposed_shares],
        swaps=swaps_desc,
        baseline={"coverage_pct": round(cov(seed), 1),
                  "farm_outs": seed["farm_outs"],
                  "farm_cost_usd": seed["farm_cost"],
                  "driver_days": seed["driver_days"],
                  "span_pressure_h": seed["span_pressure"]},
        score={"coverage_pct": round(cov(best), 1),
               "farm_outs": best["farm_outs"],
               "farm_cost_usd": best["farm_cost"],
               "driver_days": best["driver_days"],
               "span_pressure_h": best["span_pressure"],
               "quality": best["quality"],
               "label": best["label"]},
        epsilon_note=epsilon_note,
        instructions=instructions,
        evaluations=len(evals),
        wall_clock_s=round(_time.monotonic() - t_start, 1),
        budget_exhausted=budget_exhausted,
        computed_at=timezone.now().isoformat(),
        bookings_as_of=bookings_as_of.isoformat(),
        farmed_summaries=farmed_summaries,
        seed_wall_notes=seed_wall_notes,
        additions=additions,
        with_additions=with_additions,
        additions_note=additions_note,
    )
    return result


def _preexisting_walls(ctx):
    """What the day's EXISTING board already carries (nothing placed by us):
    the delta baseline for every wall. Cold day => everything empty."""
    from dispatching.scheduler import (
        build_driver_schedules, build_sharer_partners, effective_span_hours,
        sharers_conflict)
    from dispatching.board_validation import board_turn_bands
    from dispatching.route_distance import probe_mode
    from drivers.models import DriverVehicleAssignment

    rows = list(DriverVehicleAssignment.objects
                .filter(date=ctx["date"])
                .select_related("vehicle", "vehicle__vehicle_type"))
    with probe_mode():
        return _preexisting_walls_inner(
            ctx, rows, build_driver_schedules, build_sharer_partners,
            effective_span_hours, sharers_conflict, board_turn_bands)


def _preexisting_walls_inner(ctx, rows, build_driver_schedules,
                             build_sharer_partners, effective_span_hours,
                             sharers_conflict, board_turn_bands):
    board = build_driver_schedules(ctx["legs"], ctx["drivers"], ctx["date"],
                                   dva_rows=rows)
    bands = board_turn_bands(board, ctx["date"])
    criticals = {k for k, i in bands.items() if i["band"] == "critical"}
    over_hard = set()
    rest = set()
    for did, sched in board.items():
        slots = sorted(sched.slots, key=lambda s: (s.pickup_time, s.leg_id))
        if not slots:
            continue
        _raw, eff = effective_span_hours(slots, ctx["date"])
        if eff > ctx["hard_cap"]:
            over_hard.add(did)
        if ctx["rest_min"] > 0:
            first = datetime.combine(ctx["date"], slots[0].pickup_time)
            last = max(s.estimated_end_time for s in slots)
            pe = ctx["prev_end_by_driver"].get(did)
            if pe is not None and (first - pe).total_seconds() / 60.0 < ctx["rest_min"]:
                rest.add((did, "prev"))
            nf = ctx["next_first_by_driver"].get(did)
            if nf is not None and (nf - last).total_seconds() / 60.0 < ctx["rest_min"]:
                rest.add((did, "next"))
    partners = build_sharer_partners({d.id for d in ctx["drivers"]},
                                     ctx["date"], rows=rows)
    conflicts = set()
    if partners:
        for did in partners:
            sched = board.get(did)
            for s in (sched.slots if sched else []):
                lg = ctx["legs_by_id"].get(s.leg_id)
                if lg is not None and sharers_conflict(
                        lg, did, partners, board, ctx["date"]):
                    conflicts.add((did, s.leg_id))
    return {"criticals": criticals, "over_hard": over_hard,
            "rest_breaches": rest, "share_conflicts": conflicts}


def _priced_exceptions(e, ctx):
    """Every 13.5–15h driver-day in the plan, priced (D4): what would a
    13.5h-capped day shed, and what would farming those legs cost? Reuses
    standby_mints.best_window on engine clocks — an ≈ estimate, labeled so."""
    from dispatching.standby_mints import best_window
    from dispatching.scheduler import estimate_job_end_time
    from dispatching import feasibility_guards as fg

    out = []
    for did in sorted(e["over_soft"]):
        planned = [lid for lid, d in e["assignments"].items() if d == did]
        existing = [lid for lid, d in ctx["existing_assign"].items() if d == did]
        blks = []
        for lid in planned + existing:
            lg = ctx["legs_by_id"].get(lid)
            if lg is None or lg.pickup_time is None:
                continue
            pick = datetime.combine(ctx["date"], lg.pickup_time)
            try:
                end = estimate_job_end_time(lg, ctx["date"])
            except Exception:
                continue
            blks.append(_Blk(pick, pick, end, lid))
        blks.sort(key=lambda b: b.pick)
        _kept, shed = best_window(blks, cap_h=fg.SPAN_SOFT_EFFECTIVE_HOURS)
        price = 0.0
        for b in shed:
            c, _fb = _leg_cost(ctx["legs_by_id"].get(b.leg_id))
            price += c
        _raw, eff = e["spans"].get(did, (0.0, 0.0))
        out.append({
            "driver_id": did,
            "driver_name": str(ctx["drivers_by_id"].get(did, did)),
            "eff_hours": round(eff, 1),
            "legs_kept": len(shed),
            "price_usd": round(price, 2),
        })
    return out


# ════════════════════════════════════════════════════════════════════════════
# TICKET D — the async job (existing background pattern; never in-request)
# ════════════════════════════════════════════════════════════════════════════

STALE_CLAIM_MIN = 15    # a 'running' row older than this is a crashed job — reclaimable


def start_day_plan_job(target_date, user=None, epsilon=None):
    """Claim the date's DayPlan row and run the build in a background daemon
    thread (reservations.utils._run_in_background — its wrapper closes the
    thread's DB connection on exit, the 2026-07-18 standing rule).

    Returns (started: bool, row). started=False => a job is already running
    for this date (the row-level claim makes a double-click a no-op)."""
    from django.db.models import Q
    from django.utils import timezone
    from dispatching.models import DayPlan
    from reservations.utils import _run_in_background

    now = timezone.now()
    row, _created = DayPlan.objects.get_or_create(date=target_date)
    claimed = (DayPlan.objects
               .filter(pk=row.pk)
               .filter(~Q(status="running")
                       | Q(requested_at__lt=now - timedelta(minutes=STALE_CLAIM_MIN)))
               .update(status="running",
                       requested_by=(user if getattr(user, "pk", None) else None),
                       requested_at=now, epsilon=(epsilon if epsilon is not None else 0),
                       error="", budget_exhausted=False))
    if not claimed:
        row.refresh_from_db()
        return False, row
    row.refresh_from_db()
    _run_in_background(_run_plan_job, target_date, epsilon, row.pk)
    return True, row


def _run_plan_job(target_date, epsilon, row_pk):
    """The background body. Read-only over the schedule; writes only its own
    DayPlan ledger row."""
    from django.utils import timezone
    from dispatching.models import DayPlan

    row = DayPlan.objects.get(pk=row_pk)
    row.bookings_as_of = timezone.now()
    row.save(update_fields=["bookings_as_of"])
    try:
        result = build_day_plan(target_date, epsilon=epsilon)
    except PlanRefused as ref:
        DayPlan.objects.filter(pk=row_pk).update(
            status="refused", error=ref.reason, computed_at=timezone.now(),
            result_json="")
        return
    except Exception:
        logger.exception("Day-Builder failed for %s", target_date)
        DayPlan.objects.filter(pk=row_pk).update(
            status="error",
            error="The builder hit an unexpected error — see the server log.",
            computed_at=timezone.now(), result_json="")
        return
    DayPlan.objects.filter(pk=row_pk).update(
        status="done", error="", computed_at=timezone.now(),
        result_json=json.dumps(result.to_payload(), default=str),
        budget_exhausted=result.budget_exhausted,
        epsilon=result.epsilon)
