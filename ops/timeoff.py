"""
Office-staff time off: requesting it, deciding on it, and listing it.

One model backs both halves — ``StaffScheduleOverride`` with ``kind="off"``.
A manager adding time off writes an ``approved`` row (it takes effect at once);
a dispatcher requesting it writes a ``pending`` row, which the schedule resolver
ignores entirely until someone approves it. That split is the whole point: a
request must never quietly move coverage before a human agrees to it.

Nothing here notifies anyone. Approving a request changes the board and the
requester's own schedule page, and that is all — no SMS, no email, no escalation.
The manager who decides is expected to be the one talking to the person.
"""

from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from .models import StaffScheduleOverride


MAX_REQUEST_DAYS = 60       # a single request can't span more than ~2 months
MAX_LEAD_DAYS = 550         # ...and can't be booked more than ~18 months out

ACTIVE_STATUSES = ("pending", "approved")


class TimeOffError(ValueError):
    """A request the caller should show back to the user, not a 500."""


# ── serialisation ─────────────────────────────────────────────────────

def serialize(ov, *, viewer=None):
    """One request as a plain dict for a template or JSON response."""
    return {
        "id": ov.id,
        "user_id": ov.user_id,
        "name": ov.user.get_full_name() or ov.user.username,
        "date": ov.date,
        "end_date": ov.end_date,
        "range_display": ov.date_range_display,
        "days": ov.day_count,
        "reason": ov.reason,
        "reason_label": ov.reason_label or "Time off",
        "note": ov.note,
        "status": ov.status,
        "status_label": ov.status_label,
        "requested": ov.requested_by_staff,
        "denial_reason": ov.denial_reason,
        "created_at": ov.created_at,
        "decided_at": ov.decided_at,
        "decided_by": (ov.decided_by.get_full_name() or ov.decided_by.username) if ov.decided_by_id else "",
        "is_mine": bool(viewer and ov.user_id == viewer.id),
        "starts_in": (ov.date - timezone.localdate()).days,
    }


def _base_qs(roster=None):
    qs = StaffScheduleOverride.objects.filter(kind="off").select_related("user", "decided_by")
    if roster is not None:
        qs = qs.filter(user__in=[u.id for u in roster])
    return qs


def _current_or_future(qs, today):
    """Rows that still matter: single days from today on, ranges not yet ended."""
    return qs.filter(Q(end_date__isnull=True, date__gte=today) | Q(end_date__gte=today))


def pending_requests(roster=None, *, today=None, limit=50):
    """Time off awaiting a decision, soonest first — the manager's queue."""
    today = today or timezone.localdate()
    qs = _current_or_future(_base_qs(roster).filter(status="pending"), today)
    return [serialize(o) for o in qs.order_by("date", "user__first_name")[:limit]]


def upcoming_approved(roster=None, *, today=None, limit=50, horizon_days=60):
    """Approved time off from today through ``horizon_days`` out."""
    today = today or timezone.localdate()
    qs = _current_or_future(_base_qs(roster).filter(status="approved"), today)
    qs = qs.filter(date__lte=today + timedelta(days=horizon_days))
    return [serialize(o) for o in qs.order_by("date", "user__first_name")[:limit]]


def my_requests(user, *, today=None, limit=25):
    """One dispatcher's own time off — pending, approved and recently declined.

    Denied and cancelled rows are kept in the list (for dates that haven't passed)
    so a dispatcher can see the answer instead of watching their request vanish.
    """
    today = today or timezone.localdate()
    qs = _current_or_future(
        StaffScheduleOverride.objects.filter(kind="off", user=user).select_related("user", "decided_by"),
        today,
    )
    return [serialize(o, viewer=user) for o in qs.order_by("date")[:limit]]


# ── mutations ─────────────────────────────────────────────────────────

def _validate_window(start, end, today):
    if not start:
        raise TimeOffError("Pick a start date.")
    end = end or start
    if end < start:
        raise TimeOffError("The end date has to be on or after the start date.")
    if (end - start).days + 1 > MAX_REQUEST_DAYS:
        raise TimeOffError(f"Keep a single request to {MAX_REQUEST_DAYS} days or fewer.")
    if start > today + timedelta(days=MAX_LEAD_DAYS):
        raise TimeOffError("That start date is too far out.")
    return start, end


def _overlapping(user, start, end, exclude_id=None):
    qs = StaffScheduleOverride.objects.filter(
        user=user, kind="off", status__in=ACTIVE_STATUSES,
    ).filter(
        Q(end_date__isnull=True, date__range=(start, end))
        | Q(end_date__gte=start, date__lte=end)
    )
    if exclude_id:
        qs = qs.exclude(id=exclude_id)
    return qs.first()


def submit_request(user, start, end=None, *, reason="", note="", by=None, approved=False, today=None):
    """Create a time-off entry for ``user``.

    ``approved=True`` is the manager path (takes effect immediately); the default
    is a staff request that sits pending. Overlapping an existing pending or
    approved block is rejected rather than silently stacked — two rows covering
    the same day would make "who is off" ambiguous.
    """
    today = today or timezone.localdate()
    start, end = _validate_window(start, end, today)

    clash = _overlapping(user, start, end)
    if clash:
        state = "already booked off" if clash.status == "approved" else "already requested off"
        raise TimeOffError(f"{clash.date_range_display} is {state}.")

    # Unknown reasons (e.g. a stale tab still offering a retired one) are
    # stored blank rather than as a value nothing can label.
    if reason not in dict(StaffScheduleOverride.REASON_CHOICES):
        reason = ""

    return StaffScheduleOverride.objects.create(
        user=user,
        date=start,
        end_date=None if end == start else end,
        kind="off",
        reason=reason,
        note=(note or "")[:200],
        created_by=by or user,
        requested_by_staff=not approved,
        status="approved" if approved else "pending",
        decided_by=by if approved else None,
        decided_at=timezone.now() if approved else None,
    )


def decide(request_obj, by, *, approve, denial_reason=""):
    """Approve or decline a pending request. Approving is what makes it real."""
    if request_obj.kind != "off":
        raise TimeOffError("That entry isn't a time-off request.")
    if approve:
        clash = _overlapping(request_obj.user, request_obj.date,
                             request_obj.end_date or request_obj.date,
                             exclude_id=request_obj.id)
        if clash and clash.status == "approved":
            raise TimeOffError(f"{clash.date_range_display} is already approved off.")
        request_obj.status = "approved"
        request_obj.denial_reason = ""
    else:
        request_obj.status = "denied"
        request_obj.denial_reason = (denial_reason or "")[:200]
    request_obj.decided_by = by
    request_obj.decided_at = timezone.now()
    request_obj.save(update_fields=["status", "denial_reason", "decided_by", "decided_at", "updated_at"])
    return request_obj


def cancel(request_obj):
    """Withdraw a request. A dispatcher may cancel their own; a manager, anyone's.

    Deleted outright rather than left as a "cancelled" row, so an approved block
    puts the schedule straight back to normal and a withdrawn request stops
    occupying the queue.
    """
    request_obj.delete()
    return True
