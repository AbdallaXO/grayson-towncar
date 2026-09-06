"""Warn-only validation for the manual assign path (scheduling redesign, Build 1a).

``update_leg_assignment`` (the board drag-drop and every driver dropdown) wrote
``leg.driver`` with no feasibility, turn, or car-share check of any kind. This
module computes ADVISORY warnings for a proposed manual assignment:

  (i)  turn slack against the driver's adjacent legs that day, using
       ``board_validation.turn_slack_minutes()`` — the ONE slack formula the
       board, the advisors and ``check_feasibility`` already share (which calls
       ``feasibility_guards.required_turnaround`` internally); and
  (ii) the co-driver car-share check: when the driver's vehicle that date is
       shared (two drivers, one physical unit), the two drivers' occupancy
       blocks (handoff_chain lead/tail, P75 — the single-leg feasibility
       convention) must not overlap or interleave, and adjacent cross-driver
       pickups must clear ``SchedulerSettings.vehicle_share_pad_min``.

CONTRACT — NEVER BLOCKS. The caller receives a list of warning dicts
({"code", "severity", "text"}) and must proceed with the assignment regardless;
severity is "warning" or "info" and is purely presentational. Warnings are
scoped to what THIS assignment creates: pairs and boundaries involving the leg
being assigned — pre-existing problems elsewhere on the board are never blamed
on this action (same philosophy as board_validation's "no new problems" test).

Clock: the PLANNING clock, never re-anchored on recorded pickups — a manual
assignment is a planning decision, and the precision replay
(docs/scheduling-redesign/analysis/12_warn_precision.py) scores exactly this
logic, so script and product cannot drift.

Board basis: COMMITTED rows only. The caller (update_leg_assignment) computes
warnings only for LIVE writes — a staged held-day draft edit lives in the
sandbox overlay, which this module cannot see, so scoring it against the live
board would contradict the draft on screen. Draft-aware warnings are a
Build-2+ concern.

READ-ONLY: no model writes anywhere in this module (the SchedulerSettings
singleton read lazily creates its one row on a fresh database, the same as
every existing engine path that calls get_settings()).
"""
import logging

from datetime import datetime

logger = logging.getLogger(__name__)

# Statuses that occupy a driver's day for planning purposes — the same set the
# manual feasibility endpoint (views.check_driver_feasibility) builds boards from.
ACTIVE_STATUSES = ("confirmed", "in-progress", "on-the-way", "picked-up", "on-location")

# Severity per warning class, gated by the alert-precision bar (04 §1 rule 2:
# a class ships visibly as "warning" only after demonstrating >=70% precision
# on the replayed regime days — analysis/12_warn_precision.py; anything short
# of that demonstration ships as a passive "info" row).
#   turn_critical / turn_tight  — measured 93.6% / 79.2% precision against the
#       analysis/09 hard/tight pair definitions (28 replayed days, 511 fired
#       pairs, 12_warn_precision.py run 2026-08-23) -> warning.
#   share_overlap / share_interleave / share_pad — "info" pending founder
#       review: 09 carries no cross-driver share truth, so no rule-2 precision
#       number exists for these classes. The case for promoting the first two
#       is that they describe a physically impossible unit-day (one car, two
#       places / more than one hand-back — the gate whose absence minted ~2
#       impossible legs/day in the first replay, 01 §A3); but the 12-replay
#       fire-rate evidence is thin (2 overlap + 1 pad fires on 32 shared
#       unit-days that all operated), so the strict reading of rule 2 wins
#       until the founder ratifies otherwise. share_pad stays info regardless
#       (an under-pad handoff is AMBER per 03 §3.2 — a plan prompt, not an
#       alarm; ~12% of real executed handoffs ran under 120 min).
CLASS_SEVERITY = {
    "turn_critical": "warning",
    "turn_tight": "warning",
    "share_overlap": "info",
    "share_interleave": "info",
    "share_pad": "info",
}


def _fmt_t(t):
    """9:05 AM — board-style clock. Accepts time or datetime."""
    if isinstance(t, datetime):
        t = t.time()
    return t.strftime("%I:%M %p").lstrip("0")


def _warn(code, text):
    return {"code": code, "severity": CLASS_SEVERITY.get(code, "info"), "text": text}


# ════════════════════════════════════════════════════════════════════════════
# (i) turn slack vs the driver's adjacent legs
# ════════════════════════════════════════════════════════════════════════════

def _turn_warnings(new_slot, day_slots, target_date):
    """Warnings for the two pairs the new leg forms with its neighbors."""
    from dispatching import pickup_policy
    from dispatching.board_validation import turn_slack_minutes

    out = []
    ordered = sorted(day_slots + [new_slot], key=lambda s: (s.pickup_time, s.leg_id))
    idx = next(i for i, s in enumerate(ordered) if s is new_slot)
    pairs = []
    if idx > 0:
        pairs.append((ordered[idx - 1], new_slot))
    if idx + 1 < len(ordered):
        pairs.append((new_slot, ordered[idx + 1]))

    for prev_slot, next_slot in pairs:
        slack = turn_slack_minutes(prev_slot, next_slot, target_date)
        band = pickup_policy.turn_band(slack)
        if not band:
            continue
        incoming = next_slot is new_slot   # the new leg is the LATER of the pair
        # Rule-3 labeling (04 §1): the minutes are modeled from booked times
        # and the planning drive tables, and the text says so — never dressed
        # as fact.
        if band == "critical":
            text = (f"Turn conflict: about {abs(slack)} min short of the "
                    f"required turnaround between the {_fmt_t(prev_slot.pickup_time)} "
                    f"job and the {_fmt_t(next_slot.pickup_time)} pickup "
                    f"(estimated from booked times).")
        else:
            text = (f"Tight turn: only about {slack} spare min between the "
                    f"{_fmt_t(prev_slot.pickup_time)} job and the "
                    f"{_fmt_t(next_slot.pickup_time)} pickup "
                    f"(estimated from booked times).")
        if not incoming:
            text += " (This assignment sits before that job.)"
        out.append(_warn(f"turn_{band}", text))
    return out


# ════════════════════════════════════════════════════════════════════════════
# (ii) co-driver car-share check (one physical unit, two drivers)
# ════════════════════════════════════════════════════════════════════════════

# The decision core and the entry builder live in dispatching/car_share.py —
# the one home for every co-driver rule (Build 3a, P2). Re-exported here
# because the precision replay imports them from this module
# (docs/scheduling-redesign/analysis/12_warn_precision.py), so script and
# product cannot drift.
from dispatching.car_share import (           # noqa: E402  (re-export)
    build_share_entry, share_conflicts,
)


def _share_warnings(leg, driver, target_date, pad_min):
    """Occupancy-block overlap / interleave / handoff-pad warnings against the
    car-share partner's day, for the unit the driver holds on ``target_date``."""
    from django.db.models import Q

    from dispatching.analytics import categorize_location
    from drivers.models import DriverVehicleAssignment
    from reservations.models import Leg

    dva_rows = list(
        DriverVehicleAssignment.objects
        .filter(date=target_date, vehicle__isnull=False)
        .select_related("vehicle", "driver", "driver__profile")
    )
    mine = next((r for r in dva_rows if r.driver_id == driver.id), None)
    if mine is None:
        return []
    partners = {r.driver_id: r.driver for r in dva_rows
                if r.vehicle_id == mine.vehicle_id and r.driver_id != driver.id}
    if not partners:
        return []
    unit = f"#{mine.vehicle.vehicle_number}" if mine.vehicle else "the shared car"

    # The unit's day: both drivers' active legs, plus the leg being assigned.
    unit_legs = list(
        Leg.objects
        .filter(pickup_date=target_date, status__in=ACTIVE_STATUSES)
        .filter(Q(driver_id=driver.id) | Q(driver_id__in=partners))
        .exclude(id=leg.id)
    )

    def entry(l, did):
        if l.pickup_time is None:
            return None
        return build_share_entry(
            l.id, did, datetime.combine(target_date, l.pickup_time),
            categorize_location(l.pickup_location),
            categorize_location(l.dropoff_location))

    entries = [e for e in
               ([entry(l, l.driver_id) for l in unit_legs] + [entry(leg, driver.id)])
               if e is not None]
    if not any(e["leg_id"] == leg.id for e in entries):
        return []

    def name_of(did):
        if did == driver.id:
            return str(driver)
        return str(partners.get(did, "the co-driver"))

    out = []
    for c in share_conflicts(entries, pad_min, focus_leg_id=leg.id):
        if c["code"] == "share_overlap":
            other = c["b"] if c["a"]["leg_id"] == leg.id else c["a"]
            out.append(_warn(
                "share_overlap",
                f"Shared car {unit}: this job looks like it would overlap "
                f"{name_of(other['did'])}'s {_fmt_t(other['pick'])} job — one "
                f"car can't cover both (estimated from booked times)."))
        elif c["code"] == "share_interleave":
            pname = str(next(iter(partners.values())))
            out.append(_warn(
                "share_interleave",
                f"Shared car {unit}: this job would make the day alternate "
                f"between {driver} and {pname} — the car would have to change "
                f"hands more than once."))
        elif c["code"] == "share_pad":
            a, b = c["a"], c["b"]
            gap = int((b["pick"] - a["pick"]).total_seconds() / 60)
            out.append(_warn(
                "share_pad",
                f"Shared car {unit}: only {gap} min between "
                f"{name_of(a['did'])}'s {_fmt_t(a['pick'])} pickup and "
                f"{name_of(b['did'])}'s {_fmt_t(b['pick'])} pickup — the "
                f"handoff pad is {pad_min} min (wash, fuel, base). Plan the "
                f"hand-off explicitly if this is intended."))
    return out


# ════════════════════════════════════════════════════════════════════════════
# entry point
# ════════════════════════════════════════════════════════════════════════════

def compute_manual_assign_warnings(leg, driver, target_date=None):
    """Advisory warnings for manually assigning ``leg`` to ``driver``.

    Returns a list of {"code", "severity", "text"} dicts — possibly empty.
    Never raises and never blocks: any internal failure logs and degrades to
    fewer warnings. Returns [] when the ``manual_assign_warnings`` flag is off,
    for affiliate drivers (their rows are companies, not bodies — a "board" for
    one is meaningless), or for a leg with no pickup time.
    """
    from dispatching.models import SchedulerSettings

    try:
        cfg = SchedulerSettings.get_settings()
        if not cfg.manual_assign_warnings:
            return []
        if getattr(driver, "driver_type", None) != "inhouse":
            return []
        if leg.pickup_time is None:
            return []
        target_date = target_date or leg.pickup_date
        if target_date is None:
            return []
    except Exception:
        logger.exception("manual-assign warnings: settings/precondition check failed")
        return []

    warnings = []

    try:
        import dispatching.scheduler as sch
        from dispatching.scheduler import _make_sim_slot, build_driver_schedules
        from reservations.models import Leg

        if sch._timing_cache is None:      # warm once per process (day_setup's guard)
            sch.preload_timing_cache()
        existing = list(
            Leg.objects
            .filter(driver=driver, pickup_date=target_date,
                    status__in=ACTIVE_STATUSES)
            .exclude(id=leg.id)
            .select_related("reservation", "flight_information", "cruise_information")
            .order_by("pickup_time")
        )
        sched = build_driver_schedules(existing, [driver], target_date).get(driver.id)
        day_slots = list(sched.slots) if sched else []
        new_slot = _make_sim_slot(leg, target_date)
        warnings.extend(_turn_warnings(new_slot, day_slots, target_date))
    except Exception:
        logger.exception("manual-assign warnings: turn-slack check failed for leg %s", leg.id)

    try:
        warnings.extend(
            _share_warnings(leg, driver, target_date, cfg.vehicle_share_pad_min))
    except Exception:
        logger.exception("manual-assign warnings: car-share check failed for leg %s", leg.id)

    return warnings
