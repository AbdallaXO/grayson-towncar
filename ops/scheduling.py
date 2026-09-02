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
from .models import (  # noqa: F401 (StaffScheduleOverride for type clarity)
    StaffScheduleOverride,
    StaffExtraShift,
    TimeClockShift,
    WORK_LOCATION_LABELS,
    WORK_LOCATION_SHORT,
)


# Comparison thresholds (minutes)
LATE_GRACE_MIN = 5
EARLY_GRACE_MIN = 5
SHORT_THRESHOLD_MIN = 15
NO_SHOW_AFTER_MIN = 30  # working + nothing clocked this long past start -> no-show flag
# How early someone may clock in ahead of their window without it counting
# as an unscheduled punch that needs approval.
EARLY_CLOCKIN_GRACE_MIN = 30

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
        "location": "",
        "location_label": "",
        "location_flipped": False,
        "time_off": None,
        "override_id": None,
    }

    weekly = _weekly_row(user, date)
    weekly_location = ""
    if weekly is not None:
        result["role"] = getattr(weekly, "role", "") or ""
        weekly_location = getattr(weekly, "location", "") or ""
        result["location"] = weekly_location
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
        result["override_id"] = override.id
        result["override_range"] = override.date_range_display
        if override.note:
            result["note"] = override.note
        # A one-off role assignment wins over the recurring one; blank keeps it.
        if getattr(override, "role", ""):
            result["role"] = override.role
        # Same for the work location — this is the "usually WFH, in office
        # this Tuesday" flip. Flag it so the boards can call it out.
        if getattr(override, "location", ""):
            result["location"] = override.location
            result["location_flipped"] = override.location != weekly_location
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

    # Location only means something on a working day.
    if result["is_working"] is not True:
        result["location"] = ""
        result["location_flipped"] = False
    result["location_label"] = WORK_LOCATION_SHORT.get(result["location"], "")

    return result


def extra_shifts_on(user, date, *, only_if_working=True, primary=None):
    """The additional shifts (split-shift halves) this staffer has on ``date``.

    Read from the (often prefetched) ``extra_shifts`` manager, so a caller that
    prefetched it stays at O(roster) queries. Returns them sorted by start.

    ``only_if_working`` drops the extras when the primary schedule says they're
    off that day — an approved day off has to clear the *whole* day, otherwise an
    old recurring split-shift row would quietly put somebody back on while they
    are on vacation.
    """
    rows = [e for e in user.extra_shifts.all() if e.applies_on(date)]
    if only_if_working and rows:
        # Callers that already resolved the day pass it in — this is pure Python
        # over prefetched rows either way, but re-resolving is wasted work.
        primary = primary if primary is not None else resolve_staff_schedule(user, date)
        # kind == "off" with an exception is time off / a one-off day off.
        if primary["is_working"] is False and primary["has_exception"]:
            return []
    return sorted(rows, key=lambda e: (e.start_time, e.end_time))


def clock_in_schedule_check(user, at=None):
    """
    Is a clock-in at ``at`` inside this staffer's planned schedule?
    Returns ``(in_schedule, reason)`` — reason is a short human sentence
    explaining the flag when ``in_schedule`` is False, else "".

    Rules:
    * Inside any planned window that day (primary or split-shift half),
      including up to ``EARLY_CLOCKIN_GRACE_MIN`` before it starts -> fine.
    * A window from *yesterday* that crosses midnight still counts (the
      overnight closer clocking in at 12:30 AM is on schedule).
    * No schedule configured at all (kind "none") -> fine. An unconfigured
      roster must never start demanding approvals.
    * A day off, or a punch outside every window -> unscheduled.
    """
    at = at or timezone.now()
    tz = timezone.get_current_timezone()
    local = timezone.localtime(at)
    d = local.date()

    def _windows(date_):
        sched = resolve_staff_schedule(user, date_)
        wins = []
        if sched["is_working"] and sched["start_time"] and sched["end_time"]:
            wins.append((sched["start_time"], sched["end_time"]))
        for e in extra_shifts_on(user, date_, primary=sched):
            wins.append((e.start_time, e.end_time))
        return sched, wins

    def _span(date_, start_t, end_t):
        a = timezone.make_aware(datetime.combine(date_, start_t), tz)
        b = timezone.make_aware(datetime.combine(date_, end_t), tz)
        crosses = b <= a
        if crosses:
            b += timedelta(days=1)
        return a, b, crosses

    sched_today, wins_today = _windows(d)
    _, wins_prev = _windows(d - timedelta(days=1))

    grace = timedelta(minutes=EARLY_CLOCKIN_GRACE_MIN)
    spans = [_span(d, s, e) for s, e in wins_today]
    # Yesterday's windows only matter if they spill past midnight into today.
    spans += [sp for sp in (_span(d - timedelta(days=1), s, e) for s, e in wins_prev) if sp[2]]
    for a, b, _crosses in spans:
        if a - grace <= at <= b:
            return True, ""

    if sched_today["kind"] == "none":
        return True, ""  # today is unconfigured — nothing to be outside of

    when = fmt_time_long(local.time())
    if sched_today["is_working"] is False:
        off = sched_today.get("time_off")
        if off and off.get("reason_label"):
            return False, f"Clocked in at {when} while booked off ({off['reason_label']})."
        return False, f"Clocked in at {when} — not scheduled to work {local.strftime('%A')}."
    if wins_today:
        planned = " + ".join(_window_label(s, e) for s, e in wins_today)
        return False, f"Clocked in at {when}, outside the scheduled {planned}."
    return False, f"Clocked in at {when}, outside the scheduled hours."


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

    # Scheduled windows as aware ET datetimes (handle crossing midnight). A split
    # day has more than one, so scheduled time is the SUM of the halves — take
    # only the primary and an 8-hour split day reads a phantom "Short" every time.
    def _window(start_t, end_t):
        a = timezone.make_aware(datetime.combine(date, start_t), tz)
        b = timezone.make_aware(datetime.combine(date, end_t), tz)
        if b <= a:
            b += timedelta(days=1)
        return a, b

    windows = []
    if sched["is_working"] and sched["start_time"] and sched["end_time"]:
        windows.append(_window(sched["start_time"], sched["end_time"]))
    for extra in extra_shifts_on(user, date):
        windows.append(_window(extra.start_time, extra.end_time))

    # Lateness is judged against the first window, leaving early against the last.
    sched_start_dt = min((a for a, _ in windows), default=None)
    sched_end_dt = max((b for _, b in windows), default=None)
    scheduled_seconds = sum((b - a).total_seconds() for a, b in windows)
    scheduled_minutes = int(scheduled_seconds // 60)
    split_windows = len(windows) > 1

    detail = {}
    if not windows and sched["kind"] == "none":
        status = "no_schedule"
    elif not windows and sched["is_working"] is False:
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

    # A split day has to *say* it's a split day, or the numbers look wrong: the
    # label names both halves so "9:00 AM – 1:00 PM + 5:00 PM – 9:00 PM" explains
    # an 8h scheduled total that no single window accounts for.
    if split_windows:
        scheduled_label = " + ".join(
            f"{fmt_time_long(timezone.localtime(a).time())} – {fmt_time_long(timezone.localtime(b).time())}"
            for a, b in sorted(windows)
        )
    else:
        scheduled_label = sched["display_label"]

    return {
        "status": status,
        "label": STATUS_LABELS.get(status, status),
        "scheduled_label": scheduled_label,
        "actual_label": _fmt_hm(actual_minutes),
        "scheduled_minutes": scheduled_minutes,
        "actual_minutes": actual_minutes,
        "is_split": split_windows,
        "detail": detail,
    }
