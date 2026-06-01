"""
Service helpers for the ops task system.
Provides create_task() and log_communication() used by signals, views, and scheduler.
"""

import logging
from datetime import timedelta
from django.db import IntegrityError
from django.utils import timezone
from .models import (
    OperationalTask,
    CommunicationAttempt,
    StaffActivity,
    TimeClockShift,
    TimeClockBreak,
)

logger = logging.getLogger(__name__)


def create_task(
    task_type,
    title,
    due_at=None,
    priority=OperationalTask.Priority.MEDIUM,
    description="",
    reservation=None,
    leg=None,
    lead=None,
    contact_form=None,
    created_by=None,
    assigned_to=None,
    escalate_at=None,
    blocked_by=None,
    metadata=None,
    max_attempts=5,
):
    """
    Create an OperationalTask with dedup check.
    Returns the task if created, or None if a duplicate open task already exists.
    """
    if due_at is None:
        due_at = timezone.now()

    # Dedup: check for existing open task of same type on same related object
    dedup_filter = {
        "task_type": task_type,
        "status__in": list(OperationalTask.OPEN_STATUSES),
    }
    if leg:
        dedup_filter["leg"] = leg
    elif reservation:
        dedup_filter["reservation"] = reservation
    elif lead:
        dedup_filter["lead"] = lead
    elif contact_form:
        dedup_filter["contact_form"] = contact_form
    else:
        # Manual tasks or tasks without related objects don't dedup
        dedup_filter = None

    if dedup_filter and OperationalTask.objects.filter(**dedup_filter).exists():
        return None

    # Cooldown: don't recreate a task that was recently closed/cancelled for the
    # same object. Prevents the 30-min scanner from resurrecting tasks that staff
    # already resolved or that were auto-closed as stale.
    if dedup_filter:
        cooldown_filter = {
            k: v for k, v in dedup_filter.items() if k != "status__in"
        }
        cooldown_filter["status__in"] = [
            OperationalTask.Status.COMPLETED,
            OperationalTask.Status.CANCELLED,
        ]
        cooldown_filter["resolved_at__gte"] = timezone.now() - timedelta(hours=2)
        if OperationalTask.objects.filter(**cooldown_filter).exists():
            return None

    task = OperationalTask.objects.create(
        task_type=task_type,
        title=title,
        due_at=due_at,
        priority=priority,
        description=description,
        reservation=reservation,
        leg=leg,
        lead=lead,
        contact_form=contact_form,
        created_by=created_by,
        assigned_to=assigned_to,
        escalate_at=escalate_at,
        blocked_by=blocked_by,
        metadata=metadata or {},
        max_attempts=max_attempts,
    )
    logger.info(f"Ops task created: [{task.get_priority_display()}] {title} (#{task.id})")
    return task


def close_task(task, resolved_by=None, resolution_notes="", auto=False):
    """
    Mark an OperationalTask as completed.
    """
    if not task.is_open:
        return

    task.status = OperationalTask.Status.COMPLETED
    task.resolved_at = timezone.now()
    task.resolved_by = resolved_by
    task.resolution_notes = resolution_notes or ("Auto-closed" if auto else "")
    task.save(update_fields=["status", "resolved_at", "resolved_by", "resolution_notes", "updated_at"])
    logger.info(f"Ops task closed: {task.title} (#{task.id}) — {task.resolution_notes}")


def cancel_task(task, reason=""):
    """
    Cancel an OperationalTask (e.g. reservation was cancelled).
    """
    if not task.is_open:
        return

    task.status = OperationalTask.Status.CANCELLED
    task.resolved_at = timezone.now()
    task.resolution_notes = reason or "Cancelled"
    task.save(update_fields=["status", "resolved_at", "resolution_notes", "updated_at"])
    logger.info(f"Ops task cancelled: {task.title} (#{task.id}) — {reason}")


def close_tasks_for_reservation(reservation, resolved_by=None, task_types=None, reason=""):
    """
    Close all open tasks linked to a reservation (and its legs).
    Optionally filter by task_types list.
    """
    qs = OperationalTask.objects.filter(
        status__in=list(OperationalTask.OPEN_STATUSES),
    ).filter(
        # Tasks linked to this reservation directly or via its legs
        models_Q_reservation_or_legs(reservation)
    )
    if task_types:
        qs = qs.filter(task_type__in=task_types)

    for task in qs:
        close_task(task, resolved_by=resolved_by, resolution_notes=reason, auto=True)


def models_Q_reservation_or_legs(reservation):
    """Build a Q filter for tasks linked to a reservation or any of its legs."""
    from django.db.models import Q

    leg_ids = list(reservation.legs.values_list("id", flat=True))
    q = Q(reservation=reservation)
    if leg_ids:
        q |= Q(leg_id__in=leg_ids)
    return q


def log_communication(task, channel, outcome, user, notes="", contact_value="", duration=None, metadata=None):
    """
    Log a communication attempt on a task and update the task's attempt counters.
    Returns the CommunicationAttempt instance.
    """
    attempt = CommunicationAttempt.objects.create(
        task=task,
        channel=channel,
        outcome=outcome,
        staff_user=user,
        notes=notes,
        contact_value=contact_value,
        duration_seconds=duration,
        metadata=metadata or {},
    )

    task.attempts += 1
    task.last_attempt_at = timezone.now()
    task.save(update_fields=["attempts", "last_attempt_at", "updated_at"])

    # Log staff activity
    StaffActivity.objects.create(
        user=user,
        action_type=StaffActivity.ActionType.COMM_LOGGED,
        task=task,
        metadata={"channel": channel, "outcome": outcome},
    )

    logger.info(
        f"Comm logged on task #{task.id}: {channel} → {outcome} by {user}"
    )
    return attempt


# ─────────────────────────────────────────────────────────────────────────────
# Staff time clock — clock in/out + unpaid breaks for office dispatchers.
# These are the ONLY places that mutate TimeClockShift / TimeClockBreak state,
# so the state machine (CLOCKED_OUT / CLOCKED_IN / ON_BREAK) stays consistent.
# Every function takes an explicit `now` so tests can pin the clock.
# ─────────────────────────────────────────────────────────────────────────────


class TimeClockError(Exception):
    """Raised when a time-clock transition is not allowed (e.g. double clock-in)."""


def get_open_shift(user):
    """Return the user's currently-open shift (breaks prefetched), or None."""
    return (
        TimeClockShift.objects.filter(user=user, clock_out_at__isnull=True)
        .prefetch_related("breaks")
        .first()
    )


def clock_in(user, now=None):
    """Start a new shift. Raises TimeClockError if already clocked in."""
    now = now or timezone.now()
    if get_open_shift(user):
        raise TimeClockError("You're already clocked in.")
    try:
        shift = TimeClockShift.objects.create(user=user, clock_in_at=now)
    except IntegrityError:
        # Lost a race against the partial unique constraint.
        raise TimeClockError("You're already clocked in.")
    logger.info(f"Time clock: {user} clocked IN (shift #{shift.id})")
    return shift


def clock_out(user, now=None):
    """End the open shift. Auto-closes an in-progress break. Raises if not clocked in."""
    now = now or timezone.now()
    shift = get_open_shift(user)
    if not shift:
        raise TimeClockError("You're not clocked in.")
    open_break = shift.open_break
    if open_break:
        open_break.break_end_at = now
        open_break.auto_closed = True
        open_break.save(update_fields=["break_end_at", "auto_closed"])
    shift.clock_out_at = now
    shift.save(update_fields=["clock_out_at", "updated_at"])
    logger.info(f"Time clock: {user} clocked OUT (shift #{shift.id})")
    return shift


def start_break(user, now=None):
    """Begin an unpaid break. Raises if not clocked in or already on break."""
    now = now or timezone.now()
    shift = get_open_shift(user)
    if not shift:
        raise TimeClockError("You're not clocked in.")
    if shift.open_break:
        raise TimeClockError("You're already on a break.")
    try:
        brk = TimeClockBreak.objects.create(shift=shift, break_start_at=now)
    except IntegrityError:
        raise TimeClockError("You're already on a break.")
    logger.info(f"Time clock: {user} started BREAK (shift #{shift.id})")
    return brk


def end_break(user, now=None):
    """End the in-progress break. Raises if not on a break."""
    now = now or timezone.now()
    shift = get_open_shift(user)
    open_break = shift.open_break if shift else None
    if not open_break:
        raise TimeClockError("You're not on a break.")
    open_break.break_end_at = now
    open_break.save(update_fields=["break_end_at"])
    logger.info(f"Time clock: {user} ended BREAK (shift #{open_break.shift_id})")
    return open_break


def auto_close_stale_shifts(max_hours=16, now=None):
    """
    Close shifts left open longer than ``max_hours`` (someone forgot to clock
    out). The close time is CAPPED at ``clock_in + max_hours`` so a forgotten
    clock-out never credits days of work; the shift is flagged ``auto_closed``
    so the founder can spot and correct it.

    Called lazily from the overview view — this app has no scheduler.
    Returns the number of shifts closed.
    """
    now = now or timezone.now()
    cutoff = now - timedelta(hours=max_hours)
    stale = TimeClockShift.objects.filter(
        clock_out_at__isnull=True, clock_in_at__lt=cutoff
    ).prefetch_related("breaks")

    closed = 0
    for shift in stale:
        cap = shift.clock_in_at + timedelta(hours=max_hours)
        open_break = shift.open_break
        if open_break:
            # Clamp the break inside [start, cap]; a break that started after
            # the cap collapses to zero length rather than going negative.
            open_break.break_end_at = max(cap, open_break.break_start_at)
            open_break.auto_closed = True
            open_break.save(update_fields=["break_end_at", "auto_closed"])
        note = f"Auto-closed (left open > {max_hours}h)."
        shift.note = f"{shift.note}\n{note}".strip() if shift.note else note
        shift.clock_out_at = cap
        shift.auto_closed = True
        shift.save(update_fields=["clock_out_at", "auto_closed", "note", "updated_at"])
        closed += 1
        logger.info(f"Time clock: auto-closed stale shift #{shift.id} for {shift.user}")
    return closed


# ─────────────────────────────────────────────────────────────────────────────
# Admin (superuser) corrections — punch / add / edit / delete on behalf of staff.
# Every mutating function stamps edited_by/edited_at (so manual edits are
# distinguishable from auto_closed), validates, and raises TimeClockError on bad
# input. These are the ONLY admin write paths for time-clock rows.
# ─────────────────────────────────────────────────────────────────────────────


def _fmt_local(dt):
    return timezone.localtime(dt).strftime("%m/%d %I:%M %p")


def _stamp(shift, by, now=None):
    shift.edited_by = by
    shift.edited_at = now or timezone.now()


def _overlaps(user, start, end, exclude_id=None):
    """
    Return the first of ``user``'s shifts overlapping the half-open interval
    [start, end), or None. A shift with NULL clock_out_at extends to +infinity;
    pass end=None for an open-ended new interval.
    """
    qs = TimeClockShift.objects.filter(user=user)
    if exclude_id:
        qs = qs.exclude(pk=exclude_id)
    if end is not None:
        qs = qs.filter(clock_in_at__lt=end)
    for s in qs:
        s_end = s.clock_out_at  # None = open = +inf
        if (s_end is None or start < s_end) and (end is None or s.clock_in_at < end):
            return s
    return None


def admin_punch_in(user, by, now=None, at=None):
    """
    Open a shift for ``user`` as an admin action. Defaults to now; pass ``at``
    to backdate the start (e.g. they forgot to clock in earlier). Raises if
    already clocked in, the time is in the future, or it overlaps a shift.
    """
    now = now or timezone.now()
    start = at or now
    if start > now:
        raise TimeClockError("Punch-in time can't be in the future.")
    if get_open_shift(user):
        raise TimeClockError("They're already clocked in.")
    conflict = _overlaps(user, start, None)
    if conflict:
        raise TimeClockError(f"Overlaps an existing shift starting {_fmt_local(conflict.clock_in_at)}.")
    shift = TimeClockShift.objects.create(user=user, clock_in_at=start, edited_by=by, edited_at=now)
    logger.info(f"Time clock: {by} punched IN {user} at {_fmt_local(start)} (shift #{shift.id})")
    return shift


def admin_punch_out(user, by, now=None, at=None):
    """
    Close ``user``'s open shift as an admin action. Defaults to now; pass ``at``
    to set the clock-out time (e.g. they forgot to clock out earlier). Auto-closes
    an open break at the same time. Raises if not clocked in, before clock-in, or
    in the future.
    """
    now = now or timezone.now()
    end = at or now
    if end > now:
        raise TimeClockError("Punch-out time can't be in the future.")
    shift = get_open_shift(user)
    if not shift:
        raise TimeClockError("They're not clocked in.")
    if end <= shift.clock_in_at:
        raise TimeClockError("Punch-out time must be after the clock-in time.")
    open_break = shift.open_break
    if open_break:
        if open_break.break_start_at > end:
            raise TimeClockError("A break starts after that punch-out time — fix the break first.")
        open_break.break_end_at = end
        open_break.auto_closed = True
        open_break.save(update_fields=["break_end_at", "auto_closed"])
    shift.clock_out_at = end
    _stamp(shift, by, now)
    shift.save(update_fields=["clock_out_at", "edited_by", "edited_at", "updated_at"])
    logger.info(f"Time clock: {by} punched OUT {user} at {_fmt_local(end)} (shift #{shift.id})")
    return shift


def admin_create_shift(user, clock_in_at, clock_out_at, by, note="", now=None):
    """Create a completed historical shift for ``user`` (end>start, no overlap)."""
    now = now or timezone.now()
    if clock_in_at is None or clock_out_at is None:
        raise TimeClockError("Both clock-in and clock-out times are required.")
    if clock_out_at <= clock_in_at:
        raise TimeClockError("Clock-out must be after clock-in.")
    conflict = _overlaps(user, clock_in_at, clock_out_at)
    if conflict:
        raise TimeClockError(f"Overlaps an existing shift starting {_fmt_local(conflict.clock_in_at)}.")
    shift = TimeClockShift.objects.create(
        user=user, clock_in_at=clock_in_at, clock_out_at=clock_out_at,
        note=note or "", edited_by=by, edited_at=now,
    )
    logger.info(f"Time clock: {by} added shift for {user} (#{shift.id})")
    return shift


def admin_update_shift(shift, clock_in_at, clock_out_at, by, note=None, now=None):
    """Edit a shift's in/out times. Validates overlap (excluding self) + breaks-in-bounds."""
    now = now or timezone.now()
    if clock_in_at is None:
        raise TimeClockError("Clock-in time is required.")
    if clock_out_at is not None and clock_out_at <= clock_in_at:
        raise TimeClockError("Clock-out must be after clock-in.")
    conflict = _overlaps(shift.user, clock_in_at, clock_out_at, exclude_id=shift.pk)
    if conflict:
        raise TimeClockError(f"Overlaps an existing shift starting {_fmt_local(conflict.clock_in_at)}.")
    upper = clock_out_at or now
    for b in shift.breaks.all():
        b_end = b.break_end_at or now
        if b.break_start_at < clock_in_at or b_end > upper:
            raise TimeClockError("A break falls outside the new shift times — fix the breaks first.")
    shift.clock_in_at = clock_in_at
    shift.clock_out_at = clock_out_at
    if note is not None:
        shift.note = note
    _stamp(shift, by, now)
    shift.save(update_fields=["clock_in_at", "clock_out_at", "note", "edited_by", "edited_at", "updated_at"])
    logger.info(f"Time clock: {by} edited shift #{shift.id} for {shift.user}")
    return shift


def admin_delete_shift(shift):
    """Delete a shift (cascades its breaks)."""
    sid, user = shift.id, shift.user
    shift.delete()
    logger.info(f"Time clock: deleted shift #{sid} for {user}")


def _validate_break(shift, start, end, now):
    if start is None:
        raise TimeClockError("Break start time is required.")
    if end is not None and end < start:
        raise TimeClockError("Break end must be at or after break start.")
    lower = shift.clock_in_at
    upper = shift.clock_out_at or now
    if start < lower or (end or start) > upper:
        raise TimeClockError("Break must fall within the shift.")


def _break_overlaps(shift, start, end, exclude_id=None):
    for b in shift.breaks.all():
        if exclude_id and b.id == exclude_id:
            continue
        b_end = b.break_end_at  # None = open
        if (b_end is None or start < b_end) and (end is None or b.break_start_at < end):
            return b
    return None


def admin_add_break(shift, break_start_at, break_end_at, by, now=None):
    """Add a break to a shift (within bounds, non-overlapping)."""
    now = now or timezone.now()
    _validate_break(shift, break_start_at, break_end_at, now)
    if _break_overlaps(shift, break_start_at, break_end_at):
        raise TimeClockError("Break overlaps another break on this shift.")
    brk = TimeClockBreak.objects.create(
        shift=shift, break_start_at=break_start_at, break_end_at=break_end_at,
    )
    _stamp(shift, by, now)
    shift.save(update_fields=["edited_by", "edited_at", "updated_at"])
    logger.info(f"Time clock: {by} added break to shift #{shift.id}")
    return brk


def admin_update_break(brk, break_start_at, break_end_at, by, now=None):
    """Edit a break's times (within bounds, non-overlapping)."""
    now = now or timezone.now()
    shift = brk.shift
    _validate_break(shift, break_start_at, break_end_at, now)
    if _break_overlaps(shift, break_start_at, break_end_at, exclude_id=brk.id):
        raise TimeClockError("Break overlaps another break on this shift.")
    brk.break_start_at = break_start_at
    brk.break_end_at = break_end_at
    brk.save(update_fields=["break_start_at", "break_end_at"])
    _stamp(shift, by, now)
    shift.save(update_fields=["edited_by", "edited_at", "updated_at"])
    logger.info(f"Time clock: {by} edited break #{brk.id} on shift #{shift.id}")
    return brk


def admin_delete_break(brk, by=None, now=None):
    """Delete a break; stamps the parent shift's editor when ``by`` is given."""
    shift = brk.shift
    bid = brk.id
    brk.delete()
    if by is not None:
        _stamp(shift, by, now)
        shift.save(update_fields=["edited_by", "edited_at", "updated_at"])
    logger.info(f"Time clock: deleted break #{bid} on shift #{shift.id}")
