"""
The automated checks behind the Open / Close shift checklists.

POSTURE: STRICTLY READ-ONLY. Nothing here writes a Leg, a task, a flag or a
checklist row. Every count is computed at the moment it is shown — never cached
as the truth (docs/dispatch-ops/SHIFT-SYSTEM-AUDIT.md §9.3, §13.3).

ONE LEG SET. `board_legs()` is the single filtered queryset every check starts
from, matching the board (dispatching/views.py:150) because that is where the
drill-through links land.

WHAT A COUNT MEANS. A count is work a dispatcher can still DO something about.
Two consequences, both deliberate:

  * A leg whose pickup time has already gone is reported separately, never in
    the red number. At 7:15 a missed 5 AM trip is a post-mortem, not a pending
    action, and burying it in the gate count makes the gate unreachable.
  * Unacknowledged pickup-time changes are reported separately from flight
    mismatches. They are a different job (tick a badge vs re-time a trip) and
    mixing them made the flight number unreadable.
"""

from dataclasses import dataclass, field

from django.db.models import Count, F, Min, Q
from django.urls import reverse
from django.utils import timezone

from .models import (
    CHECK_CONFLICTS,
    CHECK_MOVES,
    CHECK_FLIGHT,
    CHECK_UNASSIGNED,
    CHECK_UNCONFIRMED,
    OperationalTask,
)

# A chauffeur has taken the job once the leg reaches any of these. "in-progress"
# is the un-accepted state every leg starts in and returns to on reassignment.
CONFIRMED_OR_LATER = ("confirmed", "on-the-way", "on-location", "picked-up", "completed")

#: How many actionable items a row lists before it collapses behind "show more".
ITEM_LIMIT = 8


@dataclass
class CheckResult:
    """One system row's live answer."""

    key: str
    count: int
    #: Structured, actionable rows — each a dict the template renders with a button.
    items: list = field(default_factory=list)
    #: Total actionable items, so "show N more" can be honest.
    total_items: int = 0
    url: str = ""
    url_label: str = "Board"
    #: A grey line that never feeds the red count (past pickups, pickup-change acks).
    aside: str = ""
    aside_action: str = ""
    aside_count: int = 0
    #: What the aside is actually about, so nobody acknowledges blind.
    aside_items: list = field(default_factory=list)
    #: "red" or "amber" — how loudly a non-zero count should read. A flight
    #: mismatch wants a judgement; an uncovered trip wants a chauffeur.
    tone: str = "red"

    @property
    def is_clear(self):
        return self.count == 0

    @property
    def more(self):
        return max(0, self.total_items - len(self.items))


# ── Shared helpers ────────────────────────────────────────────────────

def board_legs(target_date):
    """Every leg the dispatch board shows for ``target_date``."""
    from reservations.models import Leg

    return (
        Leg.objects.filter(pickup_date=target_date)
        .exclude(reservation__status="cancelled")
        .exclude(status="cancelled")
    )


def _is_today(target_date):
    return target_date == timezone.localdate()


def _now_time():
    return timezone.localtime(timezone.now()).time()


def _actionable(qs, target_date):
    """Drop legs whose pickup has already gone — today only.

    On a close checklist the target is tomorrow, where nothing is past, so the
    queryset comes back untouched and no grey line is produced.
    """
    if not _is_today(target_date):
        return qs, 0
    now = _now_time()
    past = qs.filter(pickup_time__lt=now).count()
    return qs.filter(pickup_time__gte=now), past


def driver_name(driver):
    """Full name, else username — the same rule the board uses
    (dispatching/views.py::_driver_label). Reading first/last straight off a
    values() query produced "Unnamed chauffeur" for every driver whose profile
    carries only a username."""
    if driver is None:
        return "Unassigned"
    try:
        return driver.profile.get_full_name() or driver.profile.username
    except Exception:
        return f"Driver {driver.pk}"


def _guest(leg):
    try:
        return leg.reservation.customer.get_full_name()
    except Exception:
        return "guest"


def _fmt(t):
    return t.strftime("%-I:%M %p") if t else ""


#: Inside this many minutes, a pickup that still has nobody on it is worth
#: shouting about rather than just listing.
URGENT_MIN = 45


def _pickup_dt(target_date, pickup_time):
    """Pickup date + time as an aware datetime, or None."""
    from datetime import datetime

    if pickup_time is None:
        return None
    naive = datetime.combine(target_date, pickup_time)
    return timezone.make_aware(naive, timezone.get_current_timezone())


def _countdown(target_date, pickup_time, now=None):
    """How long until the pickup, in words: ``("in 50 min", True)``.

    The second value is whether it is close enough to be urgent. Reads "in 3h
    40m" rather than a bare clock time because a dispatcher deciding what to do
    first is asking how long they have, not what time it is.
    """
    dt = _pickup_dt(target_date, pickup_time)
    if dt is None:
        return "", False
    now = now or timezone.now()
    minutes = int((dt - now).total_seconds() // 60)

    if minutes < 0:
        over = abs(minutes)
        if over < 60:
            return f"{over} min ago", True
        hrs, mins = divmod(over, 60)
        return (f"{hrs}h {mins}m ago" if mins else f"{hrs}h ago"), True
    if minutes < 1:
        return "now", True
    if minutes < 60:
        return f"in {minutes} min", minutes <= URGENT_MIN
    hrs, mins = divmod(minutes, 60)
    if hrs < 24:
        return (f"in {hrs}h {mins}m" if mins else f"in {hrs}h"), False
    days, rem = divmod(hrs, 24)
    return (f"in {days}d {rem}h" if rem else f"in {days}d"), False


def _phone(driver):
    """A tel: number for the Call button, or empty."""
    if driver is None:
        return ""
    raw = (getattr(driver, "phone_number", "") or "").strip()
    return raw


# ── 1. Unassigned trips ───────────────────────────────────────────────

def unassigned_legs(target_date):
    """Legs on ``target_date`` with nobody assigned and the pickup still ahead.

    A farmed-out leg is ASSIGNED — the operator company sits in `Leg.driver`.
    """
    base = board_legs(target_date).filter(driver__isnull=True)
    qs, past = _actionable(base, target_date)
    qs = qs.select_related("reservation", "reservation__customer").order_by("pickup_time")

    total = qs.count()
    now = timezone.now()
    items = []
    for leg in qs[:ITEM_LIMIT]:
        until, urgent = _countdown(target_date, leg.pickup_time, now)
        items.append({
            "when": _fmt(leg.pickup_time),
            "until": until,
            "urgent": urgent,
            "who": _guest(leg),
            "where": (leg.pickup_location or "")[:44],
            "leg_id": leg.id,
            "action": "Assign",
            "action_url": f"{reverse('schedule_board')}?date={target_date:%Y-%m-%d}"
                          f"&driver=unassigned&highlight={leg.id}",
        })

    return CheckResult(
        key=CHECK_UNASSIGNED, count=total, items=items, total_items=total,
        url=f"{reverse('schedule_board')}?date={target_date:%Y-%m-%d}&driver=unassigned",
        aside=(f"{past} already past pickup" if past else ""),
        aside_count=past,
    )


# ── 2. Chauffeurs who have not confirmed ──────────────────────────────

def _chauffeur_confirmed_leg_ids(legs):
    """Leg ids the CHAUFFEUR himself took, not the desk.

    In-house: a status row at 'confirmed' or beyond written by the driver's own
    login — the Accept Job button, the status dropdown in the driver app, or a
    guest-text tap (drivers/views.py). A dispatcher setting the same status from
    the board writes a row under THEIR user, so it does not count.

    Affiliate: `Leg.operator_accepted_at`, which is what an operator sets when
    they take a farm-out in their own portal. Affiliates never touch the
    in-house status ladder at all.
    """
    from reservations.models import LegStatus

    by_leg_user, affiliate_ok = {}, set()
    for leg in legs:
        driver = leg.driver
        if driver is None:
            continue
        if getattr(driver, "driver_type", "") == "affiliate":
            if leg.operator_accepted_at is not None:
                affiliate_ok.add(leg.id)
        else:
            by_leg_user[leg.id] = driver.profile_id

    confirmed = set(affiliate_ok)
    if by_leg_user:
        rows = LegStatus.objects.filter(
            leg_id__in=list(by_leg_user), status__in=CONFIRMED_OR_LATER,
        ).values_list("leg_id", "updated_by_id")
        for leg_id, updated_by_id in rows:
            if updated_by_id and updated_by_id == by_leg_user.get(leg_id):
                confirmed.add(leg_id)
    return confirmed


def unconfirmed_chauffeurs(target_date):
    """Chauffeurs with at least one leg they have not taken themselves.

    GROUPED BY CHAUFFEUR — a dispatcher chases a person, not a row.
    """
    base = board_legs(target_date).filter(driver__isnull=False)
    qs, past = _actionable(base, target_date)
    legs = list(
        qs.select_related("driver", "driver__profile").order_by("pickup_time")
    )

    confirmed = _chauffeur_confirmed_leg_ids(legs)
    outstanding = [leg for leg in legs if leg.id not in confirmed]

    by_driver = {}
    for leg in outstanding:
        entry = by_driver.setdefault(leg.driver_id, {
            "driver": leg.driver, "legs": 0, "first": leg.pickup_time,
        })
        entry["legs"] += 1
        if leg.pickup_time < entry["first"]:
            entry["first"] = leg.pickup_time

    ordered = sorted(by_driver.values(), key=lambda e: e["first"])
    now = timezone.now()
    items = [{
        "who": driver_name(e["driver"]),
        "when": _fmt(e["first"]),
        "until": _countdown(target_date, e["first"], now)[0],
        "urgent": _countdown(target_date, e["first"], now)[1],
        "trips": e["legs"],
        "phone": _phone(e["driver"]),
        "driver_id": e["driver"].pk,
        # The tel: link is the Call button; this one goes to their lane on the
        # board, so the pair reads as two different moves rather than twice the
        # same word.
        "action": "Board",
        "action_url": f"{reverse('schedule_board')}?date={target_date:%Y-%m-%d}"
                      f"&driver={e['driver'].pk}",
    } for e in ordered[:ITEM_LIMIT]]

    return CheckResult(
        key=CHECK_UNCONFIRMED, count=len(ordered), items=items,
        total_items=len(ordered),
        url=f"{reverse('schedule_board')}?date={target_date:%Y-%m-%d}",
        aside=(f"{past} already past pickup" if past else ""),
        aside_count=past,
    )


# ── 3. Flight alerts ──────────────────────────────────────────────────

def unreviewed_flight_alerts(target_date):
    """Open flight-verification tasks — real mismatches somebody must judge.

    Unacknowledged pickup-time changes are NOT counted here. They are a
    different job: a trip whose time already moved, needing a tick to say it
    has been seen. They ride along as a grey aside with one Acknowledge-all.
    """
    legs = board_legs(target_date)

    tasks = (
        OperationalTask.objects.filter(
            task_type=OperationalTask.TaskType.FLIGHT_VERIFICATION,
            status__in=list(OperationalTask.OPEN_STATUSES),
            leg__in=legs,
        )
        .select_related(
            "leg", "leg__flight_information",
            "leg__reservation", "leg__reservation__customer",
        )
        .order_by("leg__pickup_time")
    )
    total = tasks.count()

    items = []
    for task in tasks[:ITEM_LIMIT]:
        leg = task.leg
        meta = task.metadata or {}
        flight = getattr(leg, "flight_information", None)
        label = ""
        if flight is not None:
            label = " ".join(str(p) for p in (
                flight.airline_display_name or flight.airline or "",
                flight.flight_number or "",
            ) if p).strip()
        items.append({
            "flight": label or meta.get("flight_ident") or "flight",
            "who": _guest(leg) if leg else "guest",
            "delta": meta.get("mismatch_label") or "",
            "when": _fmt(leg.pickup_time) if leg else "",
            "task_id": task.id,
            "leg_id": leg.id if leg else None,
            "action": "Review",
            "action_url": reverse("task_detail", args=[task.id]),
        })

    unacked = (
        legs.filter(pickup_time_changed_at__isnull=False)
        .filter(
            Q(pickup_change_ack_at__isnull=True)
            | Q(pickup_change_ack_at__lt=F("pickup_time_changed_at"))
        )
        .select_related("reservation", "reservation__customer", "driver", "driver__profile")
        .order_by("pickup_time")
    )
    unacked_count = unacked.count()

    aside_items = []
    for leg in unacked[:ITEM_LIMIT * 2]:
        # A DAY move is the dangerous one — the trip has left the board it was
        # scheduled on — so it is called out rather than shown as a retime.
        day_moved = leg.pickup_date_was is not None
        was = _fmt(leg.pickup_time_was) if leg.pickup_time_was else ""
        if day_moved:
            was = f"{leg.pickup_date_was:%a %-d %b} {was}".strip()
        aside_items.append({
            "when": _fmt(leg.pickup_time),
            "was": was,
            "who": _guest(leg),
            "driver": driver_name(leg.driver) if leg.driver_id else "no chauffeur",
            "day_moved": day_moved,
            "leg_id": leg.id,
            "action_url": f"{reverse('schedule_board')}?date={target_date:%Y-%m-%d}"
                          f"&highlight={leg.id}",
        })

    return CheckResult(
        key=CHECK_FLIGHT, tone="amber", count=total, items=items, total_items=total,
        url=f"{reverse('task_queue')}?type={OperationalTask.TaskType.FLIGHT_VERIFICATION}",
        url_label="Tasks",
        aside=(
            f"{unacked_count} pickup time{'s' if unacked_count != 1 else ''} moved "
            f"and not yet acknowledged"
            if unacked_count else ""
        ),
        aside_action="acknowledge_pickup_changes" if unacked_count else "",
        aside_count=unacked_count,
        aside_items=aside_items,
    )


def unacked_pickup_change_ids(target_date):
    """Leg ids behind the pickup-change aside, for the Acknowledge-all action."""
    return list(
        board_legs(target_date)
        .filter(pickup_time_changed_at__isnull=False)
        .filter(
            Q(pickup_change_ack_at__isnull=True)
            | Q(pickup_change_ack_at__lt=F("pickup_time_changed_at"))
        )
        .values_list("id", flat=True)
    )


# ── 4. Conflicts and tight turns ──────────────────────────────────────

def open_conflict_tasks(target_date):
    """Open driver-conflict and tight-turn tasks for legs on ``target_date``.

    CLOSE ONLY by default. Measured at ~71 a day with roughly two thirds
    closing without anyone moving anything, the count is noise at a 7:15 gate
    (docs/scheduling-redesign/06_DAY_MANAGER.md §0.2).
    """
    qs = (
        OperationalTask.objects.filter(
            task_type__in=[
                OperationalTask.TaskType.DRIVER_CONFLICT,
                OperationalTask.TaskType.TIGHT_TURN,
            ],
            status__in=list(OperationalTask.OPEN_STATUSES),
            leg__in=board_legs(target_date),
        )
        .select_related("leg", "leg__driver", "leg__driver__profile")
        .order_by("priority", "due_at")
    )
    total = qs.count()

    items = []
    for task in qs[:ITEM_LIMIT]:
        meta = task.metadata or {}
        late = meta.get("conflict_minutes") or meta.get("late_minutes")
        items.append({
            "who": meta.get("driver_name") or driver_name(
                task.leg.driver if task.leg else None
            ),
            "when": _fmt(task.leg.pickup_time) if task.leg else "",
            "late": f"{late} min over" if late else task.get_task_type_display(),
            "tight": task.task_type == OperationalTask.TaskType.TIGHT_TURN,
            "task_id": task.id,
            "action": "Fix",
            "action_url": reverse("task_detail", args=[task.id]),
        })

    return CheckResult(
        key=CHECK_CONFLICTS, count=total, items=items, total_items=total,
        url=f"{reverse('task_queue')}?type={OperationalTask.TaskType.DRIVER_CONFLICT}",
        url_label="Tasks",
    )


def moves_broke_turns(target_date):
    """Turns that a just-moved pickup broke — the opener's line-by-line scan.

    Answers Opener SOP step 4 directly: *"any driver conflicts created by the
    new times?"* Not the day's standing conflicts, which the board already
    shows in red — only the ones this morning's flight matches created. See
    ``ops.move_impact`` for how the before/after is reconstructed.
    """
    from .move_impact import clashes_from_moves

    clashes = clashes_from_moves(target_date)

    items = []
    for clash in clashes[:ITEM_LIMIT]:
        items.append({
            "who": clash.driver_name,
            "when": _fmt(clash.curr_leg.pickup_time),
            "where": clash.why,
            "late": f"{clash.late_now} min short",
            "tight": clash.tier != "red",
            "action": "Board",
            "action_url": f"{reverse('schedule_board')}?date={target_date:%Y-%m-%d}"
                          f"&driver={clash.driver_id}",
        })

    return CheckResult(
        key=CHECK_MOVES, count=len(clashes), items=items, total_items=len(clashes),
        url=f"{reverse('schedule_board')}?date={target_date:%Y-%m-%d}",
    )


CHECKS = {
    CHECK_UNASSIGNED: unassigned_legs,
    CHECK_UNCONFIRMED: unconfirmed_chauffeurs,
    CHECK_FLIGHT: unreviewed_flight_alerts,
    CHECK_CONFLICTS: open_conflict_tasks,
    CHECK_MOVES: moves_broke_turns,
}


def run_checks(keys, target_date):
    """``{key: CheckResult}``. A broken check never takes the checklist down —
    it reports as uncountable and the row stays outstanding, which is safe."""
    import logging

    logger = logging.getLogger(__name__)
    out = {}
    for key in keys:
        fn = CHECKS.get(key)
        if fn is None:
            continue
        try:
            out[key] = fn(target_date)
        except Exception:
            logger.exception("Shift check %s failed for %s", key, target_date)
            out[key] = CheckResult(key=key, count=-1)
    return out
