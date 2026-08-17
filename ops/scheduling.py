"""
Staff scheduling resolver + scheduled-vs-actual comparison for office dispatchers.

Single source of truth for "what is this staffer's planned schedule on date X?"
and "how did their actual clocked time compare?". Schedules are admin-set only
(no approval workflow). Schedule TimeFields are Eastern wall-clock and are always
combined with a date via timezone.make_aware(..., get_current_timezone()); shifts
are stored as aware datetimes.

Resolution priority for a date (mirrors drivers/availability.py):
    1. Single-date StaffScheduleOverride (end_date is None) on that date.
    2. Range override (date <= d <= end_date); most recently updated wins.
    3. StaffWeeklySchedule row for that weekday.
    4. Nothing set -> kind="none" ("No schedule").

Only ``status="approved"`` overrides are considered — a pending time-off request
changes nothing about the schedule until a manager approves it. Pending rows are
surfaced by the staffing board directly, never through this resolver.
"""

from datetime import datetime, timedelta

from django.utils import timezone

from drivers.availability import fmt_time_long
from .models import StaffScheduleOverride, TimeClockShift  # noqa: F401 (StaffScheduleOverride for type clarity)


# Comparison thresholds (minutes)
LATE_GRACE_MIN = 5
EARLY_GRACE_MIN = 5
SHORT_THRESHOLD_MIN = 15
NO_SHOW_AFTER_MIN = 30  # working + nothing clocked this long past start -> no-show flag

STATUS_LABELS = {
    "on_time": "On time",
    "late_start": "Late start",
    "left_early": "Left early",
    "short": "Short",
    "over": "Over",
    "absent": "Absent",
    "no_schedule": "—",
    "extra": "Unscheduled",
    "untracked": "—",     # scheduled, but before this person started using the clock
    "upcoming": "Upcoming",  # scheduled today, shift hasn't started yet
}

# Statuses that aren't worth surfacing as a flag (no data / nothing to act on).
QUIET_STATUSES = {"no_schedule", "untracked", "upcoming"}


def _fmt_hm(total_minutes):
    h, m = divmod(int(total_minutes), 60)
    return f"{h}h {m}m"


def _window_label(start_time, end_time):
    if start_time is None or end_time is None:
        return "—"
    return f"{fmt_time_long(start_time)} – {fmt_time_long(end_time)}"


# ── Resolver ──

def _pick_active_override(overrides, target_date, statuses=("approved",)):
    """Pick the override applying to target_date. Single-date beats range; tie by updated_at desc.

    ``statuses`` gates which approval states count — the resolver only ever sees
    approved rows, so a pending request is inert until decided.
    """
    single, ranges = [], []
    for ov in overrides:
        if statuses and getattr(ov, "status", "approved") not in statuses:
            continue
        if ov.end_date is None:
            if ov.date == target_date:
                single.append(ov)
        elif ov.date <= target_date <= ov.end_date:
            ranges.append(ov)
    pool = single or ranges
    if not pool:
        return None

    def _key(o):
        return (getattr(o, "updated_at", None) or getattr(o, "created_at", None), o.id or 0)

    return max(pool, key=_key)


def _weekly_row(user, target_date):
    """Weekly row for target_date's weekday, read from the (often prefetched) manager."""
    dow = target_date.weekday()
    for row in user.weekly_schedule_rows.all():
        if row.day_of_week == dow:
            return row
    return None


def resolve_staff_schedule(user, date):
    """
    Resolve a staffer's planned schedule for `date`. Always returns a dict:
    {is_working, start_time, end_time, kind, has_exception, note, display_label, tooltip}.
    ``is_working`` is None when no schedule exists at all (kind="none").
    """
    result = {
        "is_working": None,
        "start_time": None,
        "end_time": None,
        "kind": "none",
        "has_exception": False,
        "note": "",
        "display_label": "No schedule",
        "tooltip": "No schedule set for this day.",
        "role": "",
        "time_off": None,
    }

    weekly = _weekly_row(user, date)
    if weekly is not None:
        result["role"] = getattr(weekly, "role", "") or ""
        if weekly.is_working:
            label = _window_label(weekly.start_time, weekly.end_time)
            result.update(
                is_working=True,
                start_time=weekly.start_time,
                end_time=weekly.end_time,
                kind="weekly",
                note=weekly.note or "",
                display_label=label,
                tooltip=f"Scheduled {label}.",
            )
        else:
            result.update(
                is_working=False,
                kind="off",
                note=weekly.note or "",
                display_label="Off",
                tooltip="Not scheduled to work.",
            )

    override = _pick_active_override(list(user.schedule_overrides.all()), date)
    if override is not None:
        result["has_exception"] = True
        if override.note:
            result["note"] = override.note
        # A one-off role assignment wins over the recurring one; blank keeps it.
        if getattr(override, "role", ""):
            result["role"] = override.role
        if override.kind == "off":
            reason = getattr(override, "reason_label", "") or ""
            result.update(
                is_working=False, start_time=None, end_time=None, kind="off",
                display_label="Off", tooltip=f"Off — {reason}." if reason else "Off (one-time exception).",
                time_off={
                    "id": override.id,
                    "reason": getattr(override, "reason", "") or "",
                    "reason_label": reason,
                    "note": override.note or "",
                    "range_display": override.date_range_display,
                    "requested": bool(getattr(override, "requested_by_staff", False)),
                },
            )
        elif override.kind == "custom_hours":
            label = _window_label(override.start_time, override.end_time)
            result.update(
                is_working=True,
                start_time=override.start_time,
                end_time=override.end_time,
                kind="custom_hours",
                display_label=f"Custom {label}",
                tooltip=f"Custom hours {label} (one-time).",
            )
        elif override.kind == "note" and override.note:
            result["tooltip"] = override.note

    return result


def week_schedule(user, monday_date):
    """Return 7 day-dicts (Mon..Sun): {date, weekday, day_name, ...resolve fields}."""
    out = []
    for i in range(7):
        d = monday_date + timedelta(days=i)
        sched = dict(resolve_staff_schedule(user, d))
        sched["date"] = d
        sched["weekday"] = d.weekday()
        sched["day_name"] = d.strftime("%a")
        out.append(sched)
    return out


# ── Scheduled vs actual ──

def _et_day_bounds(date):
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.combine(date, datetime.min.time()), tz)
    return start, start + timedelta(days=1)


def schedule_vs_actual(user, date, shifts=None, now=None, tracking_since=None):
    """
    Compare the resolved schedule for `date` against the staffer's actual shifts
    that ET day. Returns {status, label, scheduled_label, actual_label,
    scheduled_minutes, actual_minutes, detail}.

    `shifts` may be a pre-fetched list of the user's shifts (the management hub
    batches one query for all staff); when None it is queried.

    `tracking_since` (a date) is when this staffer first started clocking in.
    Scheduled days before it have no clock data — they're reported as "untracked"
    rather than "absent", so the system doesn't flag no-shows for dates that
    predate the time clock itself.
    """
    now = now or timezone.now()
    tz = timezone.get_current_timezone()
    sched = resolve_staff_schedule(user, date)
    day_start, day_end = _et_day_bounds(date)

    if shifts is None:
        shifts = list(
            TimeClockShift.objects.filter(
                user=user, clock_in_at__gte=day_start, clock_in_at__lt=day_end
            ).prefetch_related("breaks")
        )
    else:
        shifts = [s for s in shifts if day_start <= s.clock_in_at < day_end]

    actual_net_seconds = sum(s.worked_seconds(now) for s in shifts)
    actual_minutes = int(actual_net_seconds // 60)
    first_in = min((s.clock_in_at for s in shifts), default=None)
    has_open = any(s.is_open for s in shifts)
    last_out = None
    if shifts:
        closed_outs = [s.clock_out_at for s in shifts if s.clock_out_at]
        last_out = now if has_open else (max(closed_outs) if closed_outs else None)

    # Scheduled window as aware ET datetimes (handle crossing midnight).
    sched_start_dt = sched_end_dt = None
    scheduled_seconds = 0.0
    if sched["is_working"] and sched["start_time"] and sched["end_time"]:
        sched_start_dt = timezone.make_aware(datetime.combine(date, sched["start_time"]), tz)
        sched_end_dt = timezone.make_aware(datetime.combine(date, sched["end_time"]), tz)
        if sched_end_dt <= sched_start_dt:
            sched_end_dt += timedelta(days=1)
        scheduled_seconds = (sched_end_dt - sched_start_dt).total_seconds()
    scheduled_minutes = int(scheduled_seconds // 60)

    detail = {}
    if sched["kind"] == "none":
        status = "no_schedule"
    elif sched["is_working"] is False:
        status = "extra" if shifts else "no_schedule"
    elif not shifts:
        if tracking_since is None or date < tracking_since:
            status = "untracked"  # no clock data existed for this person yet
        elif sched_start_dt and now < sched_start_dt:
            status = "upcoming"   # scheduled later today; shift hasn't started
        else:
            status = "absent"
            if sched_start_dt and now > sched_start_dt + timedelta(minutes=NO_SHOW_AFTER_MIN):
                detail["no_show"] = True
    else:
        late = bool(sched_start_dt and first_in and first_in > sched_start_dt + timedelta(minutes=LATE_GRACE_MIN))
        early = bool(
            sched_end_dt and last_out and not has_open
            and last_out < sched_end_dt - timedelta(minutes=EARLY_GRACE_MIN)
        )
        # Don't flag short/over while the shift is still OPEN — worked time only
        # counts up to `now`, so an on-time, currently-working staffer would
        # falsely read "Short" for most of the day. Evaluate once clocked out.
        short = not has_open and actual_net_seconds < scheduled_seconds - SHORT_THRESHOLD_MIN * 60
        over = not has_open and actual_net_seconds > scheduled_seconds + SHORT_THRESHOLD_MIN * 60
        detail.update(late_start=late, left_early=early, short=short, over=over)
        if late:
            status = "late_start"
        elif early:
            status = "left_early"
        elif short:
            status = "short"
        elif over:
            status = "over"
        else:
            status = "on_time"

    return {
        "status": status,
        "label": STATUS_LABELS.get(status, status),
        "scheduled_label": sched["display_label"],
        "actual_label": _fmt_hm(actual_minutes),
        "scheduled_minutes": scheduled_minutes,
        "actual_minutes": actual_minutes,
        "detail": detail,
    }
