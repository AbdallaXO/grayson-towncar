"""
Shared write path for flight-driven pickup moves.

Both the dispatcher-facing match endpoints (dispatching.views) and the
guest-facing flight-verification auto-adjust (dispatching.flight_verify_views)
funnel through apply_pickup_time_move() so every flight-driven move gets the
same treatment: the board's "time changed" stamping, a durable AuditLog row,
and a real simple_history row attributed to whoever actually caused it. Lives
in its own small module so flight_verify_views never imports dispatching.views
(circular-import risk).

WHY THIS USES save() AND NOT queryset.update()
----------------------------------------------
It used to write with ``Leg.objects.filter(id=...).update(...)`` to skip
simple_history and the expensive save() work. That had a nasty consequence:
the move left NO history row at all, so it stayed invisible until the next
ordinary save() on that leg — and then django-simple-history's consecutive-
snapshot diff folded it into THAT row, under THAT person's name. A driver
tapping "Accept" in the portal could appear to have retimed their own trip
hours after the fact.

save(update_fields=[...]) is the fix and costs nothing here: Leg.save() has a
fast path that skips the expensive recalculation whenever update_fields misses
_EXPENSIVE_FIELDS, and pickup_time/pickup_date are not in that set. Going
through save() also lets the model do its own move stamping (one copy of that
logic, not two) and lets the driver-push/NTFY signals see a retime, which the
queryset update silently bypassed.
"""

import logging

from django.utils import timezone

from reservations.models import AuditLog, Leg
from business.datefmt import strf

logger = logging.getLogger(__name__)


def _fmt_time(t):
    return t.strftime("%I:%M %p").lstrip("0") if t else ""


def _fmt_date(d):
    return strf(d, "%b %-d, %Y") if d else ""


def apply_pickup_time_move(leg, new_time, user=None, note="Flight match", new_date=None):
    """
    Single write path for a flight-driven pickup move.

    Moves pickup_time, and pickup_date too when ``new_date`` is passed and
    differs. Callers must pass ``new_date`` deliberately — a match that only
    ever writes the time is what puts an 11:25 PM arrival onto the wrong
    calendar day, so crossing a day is always an explicit decision made
    upstream, never a side effect here.

    The model's save() hook owns the "time changed" badge stamping (preserving
    the earliest pre-change values across successive moves, and clearing the
    badge on a net-zero A→B→A revert). This function owns the durable AuditLog
    rows and the history attribution.

    ``user`` may be None for guest-triggered moves; the history row then reads
    as System rather than borrowing whoever happens to be in the thread-local
    request. Returns True if the pickup actually moved.
    """
    old_time = leg.pickup_time
    old_date = leg.pickup_date

    # Drop seconds. Flight-derived times carry them, and the driver night-bonus
    # window opens at 22:01:00 while the customer after-hours window opens at
    # 22:00:00 — so a 22:00:30 pickup falls in the gap and bills the guest while
    # paying the driver nothing. Those two windows are deliberately different
    # (a 22:00 pickup earning no bonus is the rule, not a bug); the seconds are
    # just noise, and they also make an unchanged time look changed.
    if new_time is not None and new_time.second:
        new_time = new_time.replace(second=0, microsecond=0)

    date_moves = new_date is not None and new_date != old_date
    time_moves = new_time is not None and new_time != old_time
    if not date_moves and not time_moves:
        return False

    # Re-anchor the model's change detection to the values we just read, so the
    # stamping is driven by this move rather than by whatever state the caller's
    # in-memory instance happened to be carrying.
    leg._original_pickup_time = old_time
    leg._original_pickup_date = old_date

    update_fields = []
    if time_moves:
        leg.pickup_time = new_time
        update_fields.append("pickup_time")
    if date_moves:
        leg.pickup_date = new_date
        update_fields.append("pickup_date")

    # Attribution for the history row. simple_history reads _history_user
    # first, falling back to the request thread-local — being explicit keeps a
    # guest-triggered move from being stamped with an unrelated signed-in staff
    # member who merely had a tab open.
    leg._history_user = user
    leg._change_reason = note[:100] if note else None

    leg.save(update_fields=update_fields)

    # Durable audit rows: one per field, so "what moved this leg's date?" is a
    # direct query and not a scan through snapshot diffs.
    rows = []
    if time_moves:
        rows.append(("pickup_time", _fmt_time(old_time), _fmt_time(new_time)))
    if date_moves:
        rows.append(("pickup_date", _fmt_date(old_date), _fmt_date(new_date)))

    for field_name, old_value, new_value in rows:
        try:
            AuditLog.objects.create(
                model_name="Leg",
                object_id=leg.id,
                action="updated",
                field_name=field_name,
                old_value=old_value,
                new_value=new_value,
                user=user,
                username=user.username if user else "guest",
                notes=note,
            )
        except Exception as e:
            logger.warning(
                f"Pickup-move audit log failed for leg {leg.id} ({field_name}): {e}"
            )

    return True


def describe_pickup_move(old_date, old_time, new_date, new_time):
    """
    Plain-language summary of a pickup move, for dispatcher-facing confirms and
    timeline rows. Leads with the day when the day changes, because that is the
    part people miss.
    """
    if new_date is not None and old_date is not None and new_date != old_date:
        delta_days = (new_date - old_date).days
        direction = "later" if delta_days > 0 else "earlier"
        day_word = "day" if abs(delta_days) == 1 else "days"
        return (
            f"{_fmt_date(old_date)} {_fmt_time(old_time)} → "
            f"{_fmt_date(new_date)} {_fmt_time(new_time)} "
            f"({abs(delta_days)} {day_word} {direction})"
        )
    return f"{_fmt_time(old_time)} → {_fmt_time(new_time)}"


def humanize_shift_minutes(minutes):
    """'23h 10m later' / '50 min earlier' / 'no change'."""
    if minutes is None:
        return ""
    if minutes == 0:
        return "no change"
    direction = "later" if minutes > 0 else "earlier"
    m = abs(minutes)
    if m < 60:
        return f"{m} min {direction}"
    hours, rem = divmod(m, 60)
    if rem:
        return f"{hours}h {rem}m {direction}"
    return f"{hours}h {direction}"


def pickup_shift_minutes(old_date, old_time, new_date, new_time):
    """
    Signed minutes between the old and new pickup moments, counting the date.
    A time-only match that leaves the date behind reads as +1390 here, which is
    the number that should have stopped it.
    """
    from datetime import datetime

    if not (old_time and new_time):
        return None
    base_date = old_date or timezone.localdate()
    old_dt = datetime.combine(base_date, old_time)
    new_dt = datetime.combine(new_date or base_date, new_time)
    return int((new_dt - old_dt).total_seconds() // 60)
