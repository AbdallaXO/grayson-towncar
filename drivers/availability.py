"""
Driver availability resolver and label helpers.

Single source of truth for "what is this driver's effective availability on date X?"
Used by the legs dashboard, schedule board, in-house schedule editor, and the
drag/drop feasibility check, so dispatchers see identical wording everywhere.

Resolution priority for a given date:
    1. Single-date exception (DriverDateOverride with end_date is None) on that date.
    2. Range exception (start_date <= date <= end_date); most recently updated wins.
    3. Recurring DriverWeeklySchedule for that day_of_week.
    4. Driver.default_* fields.

A confirmed regular shift (structured shifts, Stage 1 — drivers/regular_shifts.py)
always adds keys that describe it. Only with SchedulerSettings.regular_shift_windows
on does it replace 3 and 4: a working day becomes a fixed window to the minute, an
Off day is unavailable. A driver without a confirmed regular shift reads as before.
"""
import logging
from dataclasses import replace as _dc_replace
from datetime import datetime, time, timedelta

from django.db.models import prefetch_related_objects

logger = logging.getLogger(__name__)


# ----- Hour formatting -----

def fmt_hour_short(h):
    """4 -> '4a', 12 -> '12p', 23 -> '11p'."""
    h = int(h)
    if h == 0:  return "12a"
    if h < 12:  return f"{h}a"
    if h == 12: return "12p"
    return f"{h - 12}p"


def fmt_hour_long(h):
    """4 -> '4 AM', 12 -> '12 PM', 23 -> '11 PM'."""
    h = int(h)
    if h == 0:  return "12 AM"
    if h < 12:  return f"{h} AM"
    if h == 12: return "12 PM"
    return f"{h - 12} PM"


def fmt_time_long(t):
    """time(16, 30) -> '4:30 PM'.  time(16, 0) -> '4 PM'."""
    if t is None:
        return ""
    h, m = t.hour, t.minute
    if m == 0:
        return fmt_hour_long(h)
    if h == 0:
        return f"12:{m:02d} AM"
    if h < 12:
        return f"{h}:{m:02d} AM"
    if h == 12:
        return f"12:{m:02d} PM"
    return f"{h - 12}:{m:02d} PM"


# ----- Resolver -----

def _pick_active_exception(overrides, target_date):
    """From a driver's overrides, pick the one that applies to target_date.
    Single-date exceptions win over ranges; ties are broken by updated_at desc."""
    single = []
    ranges = []
    for ov in overrides:
        if ov.end_date is None:
            if ov.date == target_date:
                single.append(ov)
        else:
            if ov.date <= target_date <= ov.end_date:
                ranges.append(ov)

    pool = single or ranges
    if not pool:
        return None

    def _key(o):
        # updated_at may be None on freshly built objects; fall back to created_at then id
        return (
            getattr(o, "updated_at", None) or getattr(o, "created_at", None),
            o.id or 0,
        )

    return max(pool, key=_key)


def _weekly_or_defaults(driver, target_date):
    """Return a dict of underlying weekly/defaults (no exception applied yet)."""
    day_of_week = target_date.weekday()
    for entry in driver.weekly_schedule.all():
        if entry.day_of_week == day_of_week:
            return {
                "is_available":    entry.is_available,
                "shift_type":      entry.shift_type,
                "start_hour":      entry.start_hour,
                "end_hour":        entry.end_hour,
                "flexible":        entry.flexible,
                "max_hours":       entry.max_hours,
                "preferred_shift": entry.preferred_shift,
                "preference":      entry.preference,
                "scheduling_notes": entry.scheduling_notes,
                "source":          "weekly",
            }
    return {
        "is_available":    True,
        "shift_type":      driver.default_shift_type,
        "start_hour":      driver.default_start_hour,
        "end_hour":        driver.default_end_hour,
        "flexible":        driver.default_flexible,
        "max_hours":       driver.default_max_hours,
        "preferred_shift": driver.default_preferred_shift,
        "preference":      driver.default_preference,
        "scheduling_notes": "",
        "source":          "default",
    }


def _fmt_minutes(minutes):
    """Minutes after midnight (maybe past 1440) -> '2:15 AM'."""
    from drivers.regular_shifts import minutes_to_time
    return fmt_time_long(minutes_to_time(minutes))


def _fmt_span(minutes):
    """720 -> '12 hours', 690 -> '11 hours 30 min'."""
    h, m = divmod(int(minutes), 60)
    hours = f"{h} hour" + ("" if h == 1 else "s")
    return f"{hours} {m} min" if m else hours


def _regular_day(driver, target_date):
    """(DayShift, templates) of a confirmed regular shift for target_date's
    weekday, or None when the driver has no confirmed regular shift — checked
    first, so an unconfirmed driver costs no query. A weekday with no row, or
    a row with no shape, is Off. Reads weekly_schedule.all() (a prefetch
    holds) and the shapes cached by regular_shifts.templates_by_id()."""
    if driver.regular_shift_confirmed_at is None:
        return None
    from drivers import regular_shifts as rs
    return rs.current_days(driver)[target_date.weekday()], rs.templates_by_id()


def _without_day_limits(day):
    """The day with its own limits ('Thursday: done by 3 PM') taken off."""
    return _dc_replace(day, day_earliest=None, day_latest=None, day_latest_next_day=False)


def _regular_shift_info(day, templates):
    """A confirmed working day in words and minutes, for the result's
    regular_shift key: {'kind', 'name', 'start_min', 'end_min', 'label'}.
    start_min / end_min are the shift's own hours: its typed times, or the
    shape's usual times when blank; a Float or two-shape day (kind 'float')
    covers what its shapes cover, its typed times being labels only. The
    hard and day limits are not applied here (the window applies them); the
    label names the day's own. None for an Off day or a shape that doesn't
    exist."""
    from drivers import regular_shifts as rs
    shapes = rs.day_shapes(day, templates)
    if not shapes:
        return None
    if rs.is_open_day(day, templates):
        kind = rs.FLOAT_KIND
        span = rs.regular_window(_without_day_limits(day), templates,
                                 hard_lo=None, hard_hi=None)
        start, end = (span.start_min, span.end_min) if span else (None, None)
    else:
        kind = shapes[0].kind
        start, end = rs.effective_minutes(day, templates) or (None, None)
    return {"kind": kind, "name": " or ".join(t.name for t in shapes),
            "start_min": start, "end_min": end, "label": rs.day_label(day, templates)}


def _apply_regular(base, driver, day, templates):
    """Switch on: the confirmed regular shift replaces the weekly/default layer,
    before any exception is applied. Returns the day's RegularWindow, or None.

    A working day becomes available, non-flexible, its shift_type the shape's
    kind ('full_day' for a Float or two-shape day — an existing shift type, so
    shift-type readers keep working), its hours the window's in whole hours
    for code that still reads hours. The window itself is built in one place,
    regular_shifts.regular_window, clipped to the driver's hard limits and the
    day's own. An Off day is unavailable: save_regular_shift makes no row for
    one, so a missing row never falls back to the default_* hours.

    The editors refuse the two days that give no window, but admin can save
    them; both are logged. When the limits — the driver's or the day's,
    edited later — leave no time, the day is unavailable: they are hard, so
    no work is planned outside them. A shape that can't be read (one typed
    time, or a shape newer than the 60s shape cache) keeps today's reading
    rather than guess."""
    from dispatching.feasibility_guards import legacy_hours
    from drivers import regular_shifts as rs
    if day.template_id is None:
        base["is_available"] = False
        return None
    hard_lo, hard_hi = driver.hard_window_minutes()
    window = rs.regular_window(day, templates, hard_lo=hard_lo, hard_hi=hard_hi)
    if window is None:
        weekday = rs.DAY_NAMES[day.day]
        if rs.regular_window(_without_day_limits(day), templates,
                             hard_lo=None, hard_hi=None) is None:
            logger.warning("Regular shift of driver %s (%s) on %s can't be read; "
                           "using the weekly hours instead.", driver.pk, driver, weekday)
            return None
        logger.warning("Limits of driver %s (%s) leave no time for the regular shift "
                       "on %s; the day reads as unavailable.", driver.pk, driver, weekday)
        base["is_available"] = False
        return None
    base["is_available"] = True
    base["flexible"] = False
    base["shift_type"] = "full_day" if window.kind == rs.FLOAT_KIND else window.kind
    base["start_hour"], base["end_hour"] = legacy_hours(window.start_min, window.end_min)
    return window


def _day_before_window(driver, target_date):
    """The RegularWindow of the regular day on the date before target_date,
    clipped to the driver's hard limits and that day's own, as _apply_regular
    clips it; None when that day is Off, can't be read or its limits leave no
    time. Only the regular shift is read, not that date's exceptions: it tells
    the pickup warning whether an early pickup falls in a shift that started
    the day before. No query (as _regular_day), and nothing logged — resolving
    that date logs its own."""
    from drivers import regular_shifts as rs
    regular = _regular_day(driver, target_date - timedelta(days=1))
    if regular is None:
        return None
    hard_lo, hard_hi = driver.hard_window_minutes()
    return rs.regular_window(*regular, hard_lo=hard_lo, hard_hi=hard_hi)


def resolve_effective_availability(driver, target_date, *, regular_windows=None):
    """Combine weekly/default availability with any active exception for target_date.

    Returns a dict (see plan or callers for keys). Always returns a dict — never None.

    Every result also carries the driver's hard limits, and, for a confirmed
    regular shift, regular_shift (that weekday's working day, or None),
    regular_day_off and shift_role_label ('Morning driver'; '' when not
    confirmed). With the switch off nothing else moves. ``regular_windows`` is
    the switch (SchedulerSettings.regular_shift_windows); None reads it, and
    only for a confirmed driver. With it on, a confirmed working day is a
    fixed window to the minute — window_start_min / window_end_min /
    window_kind / window_max_span_min, None otherwise — and an Off day is
    unavailable. Approved time off still wins; a flexible exception falls back
    to today's reading; a partial-day exception sits on top of the window.
    Alongside a window, day_before_window_start_min / _end_min are the date
    before's regular window (minutes after 00:00 of that date), None when that
    day gives none; is_pickup_within_window reads them."""
    if (driver.regular_shift_confirmed_at is not None
            and "weekly_schedule" not in getattr(driver, "_prefetched_objects_cache", {})):
        # A confirmed driver's weekly rows are read twice (the weekly layer and
        # the regular shift): fetch them once, then hand the driver back as it
        # came, so an edit made after this call is never read stale.
        prefetch_related_objects([driver], "weekly_schedule")
        try:
            return _resolve(driver, target_date, regular_windows)
        finally:
            driver._prefetched_objects_cache.pop("weekly_schedule", None)
    return _resolve(driver, target_date, regular_windows)


def _resolve(driver, target_date, regular_windows):
    """resolve_effective_availability, once the weekly rows are in hand."""
    base = _weekly_or_defaults(driver, target_date)
    # Only approved overrides affect the schedule. Pending driver-submitted
    # requests must be explicitly approved (or auto-approved by dispatcher
    # creating them) before they take effect.
    exception = _pick_active_exception(
        # Filter the (typically prefetched) date_overrides in Python so the
        # daily planner doesn't fire one query per driver — .filter() would
        # bypass the prefetch cache.
        [o for o in driver.date_overrides.all() if o.status == "approved"],
        target_date,
    )

    regular = _regular_day(driver, target_date)
    window = None
    # Time off needs no window, and a flexible exception falls back to today's
    # reading (S4): neither reads the switch nor the regular shift's hours.
    if regular is not None and (exception is None
                                or exception.exception_type not in ("off", "flexible")):
        if regular_windows is None:
            from drivers.regular_shifts import regular_windows_on
            regular_windows = regular_windows_on()
        if regular_windows:
            window = _apply_regular(base, driver, *regular)
    before = _day_before_window(driver, target_date) if window is not None else None

    if regular is None:
        regular_shift, regular_day_off, role = None, False, ""
    else:
        from drivers.regular_shifts import role_label
        day, templates = regular
        regular_shift = _regular_shift_info(day, templates)
        regular_day_off = day.template_id is None
        role = role_label(driver, templates)

    eff = {
        "is_available":    base["is_available"],
        "shift_type":      base["shift_type"],
        "start_hour":      base["start_hour"],
        "end_hour":        base["end_hour"],
        "flexible":        base["flexible"],
        "max_hours":       base["max_hours"],
        "preferred_shift": base["preferred_shift"],
        "preference":      base["preference"],
        "scheduling_notes": base["scheduling_notes"],
        "exception":       exception,
        "has_exception":   exception is not None,
        "exception_type":  None,
        "exception_start_time": None,
        "exception_end_time":   None,
        "exception_notes":      "",
        "exception_reason":     "",
        # Regular shift (structured shifts, Stage 1). The window keys are set
        # only with the switch on; regular_window_keys() reads them.
        "regular_shift":        regular_shift,
        "regular_day_off":      regular_day_off,
        "shift_role_label":     role,
        "hard_earliest_start":  driver.hard_earliest_start,
        "hard_latest_finish":   driver.hard_latest_finish,
        "hard_latest_finish_next_day": driver.hard_latest_finish_next_day,
        "window_start_min":     window.start_min if window else None,
        "window_end_min":       window.end_min if window else None,
        "window_kind":          window.kind if window else None,
        "window_max_span_min":  window.max_span_min if window else None,
        "day_before_window_start_min": before.start_min if before else None,
        "day_before_window_end_min":   before.end_min if before else None,
    }

    if exception is not None:
        eff["exception_type"]       = exception.exception_type
        eff["exception_start_time"] = exception.start_time
        eff["exception_end_time"]   = exception.end_time
        eff["exception_notes"]      = exception.notes or ""
        eff["exception_reason"]     = exception.reason or ""

        et = exception.exception_type
        if et == "off":
            eff["is_available"] = False
            eff["shift_type"]   = "off"
            eff["start_hour"]   = 0
            eff["end_hour"]     = 0
            eff["flexible"]     = False
        elif et == "flexible":
            # Driver chose to work even though normally off (or override window)
            eff["is_available"] = True
            eff["shift_type"]   = "full_day"
            eff["flexible"]     = True
            if not base["is_available"]:
                # Wasn't scheduled to work; give a reasonable default window
                eff["start_hour"] = 4
                eff["end_hour"]   = 23
        elif et in ("available_until", "available_after", "available_window", "unavailable_window"):
            # Driver IS working today, but with a partial-day limitation
            if not base["is_available"]:
                # Day was off; treat the exception window as the working window
                eff["is_available"] = True
                eff["shift_type"]   = "full_day"
                eff["flexible"]     = True
                eff["start_hour"]   = 4
                eff["end_hour"]     = 23
        # note_only → leave base alone, just attach the note

    eff["status"] = _classify_status(eff)
    eff["display_label"] = format_availability_label(eff)
    eff["tooltip"] = format_availability_tooltip(eff)
    eff["notes"] = _combine_notes(eff)
    return eff


def _classify_status(eff):
    """Return one of: 'off', 'limited', 'flexible', 'fixed_window'."""
    if not eff["is_available"]:
        return "off"
    et = eff.get("exception_type")
    if et in ("available_until", "available_after", "available_window", "unavailable_window"):
        return "limited"
    # A Float or two-shape regular day is full_day but never flexible: fixed_window.
    if eff.get("shift_type") == "full_day" and eff.get("flexible"):
        return "flexible"
    return "fixed_window"


def _combine_notes(eff):
    parts = []
    if eff.get("exception_notes"):
        parts.append(eff["exception_notes"])
    if eff.get("scheduling_notes"):
        parts.append(eff["scheduling_notes"])
    return " · ".join(parts)


# ----- Label / tooltip formatting -----

EXCEPTION_LABELS = {
    "off":                "Off",
    "available_until":    "Until",
    "available_after":    "After",
    "available_window":   "Window",
    "unavailable_window": "Unavailable",
    "flexible":           "Flexible",
    "note_only":          "Note",
}


def format_availability_label(eff):
    """Short label for driver cards. Examples:
        'Flexible'
        'Off'
        'Available 4 AM – 5 PM'
        'Until 4 PM'
        'After 12 PM'
        'Window 8 AM – 2 PM'
        'Unavailable 10 AM – 1 PM'
    """
    if not eff["is_available"]:
        return "Off"

    et = eff.get("exception_type")
    st = eff.get("exception_start_time")
    en = eff.get("exception_end_time")

    if et == "available_until" and en is not None:
        return f"Until {fmt_time_long(en)}"
    if et == "available_after" and st is not None:
        return f"After {fmt_time_long(st)}"
    if et == "available_window" and st is not None and en is not None:
        return f"Window {fmt_time_long(st)} – {fmt_time_long(en)}"
    if et == "unavailable_window" and st is not None and en is not None:
        base = _underlying_label(eff)
        return f"{base} · Unavailable {fmt_time_long(st)} – {fmt_time_long(en)}"

    return _underlying_label(eff)


def _underlying_label(eff):
    """Label ignoring partial-day exception (used when overlaying a window)."""
    if eff.get("window_start_min") is not None and eff.get("regular_shift"):
        return eff["regular_shift"]["label"]        # 'Morning 4:10 AM – 3:30 PM'
    if eff.get("shift_type") == "full_day" and eff.get("flexible"):
        return "Flexible"
    sh = eff.get("start_hour", 0)
    eh = eff.get("end_hour", 0)
    return f"Available {fmt_hour_long(sh)} – {fmt_hour_long(eh)}"


def format_availability_tooltip(eff):
    """Hover text explaining the label."""
    status = eff["status"]
    if status == "flexible":
        return "Flexible — no fixed start/end. Schedule any physically possible time today."
    if status == "off":
        if eff.get("exception_reason"):
            reason_pretty = eff["exception_reason"].replace("_", " ").title()
            return f"Driver is off ({reason_pretty})."
        return "Driver is not scheduled to work today."
    if status == "limited":
        et = eff.get("exception_type")
        notes = eff.get("exception_notes")
        base_msg = ""
        if et == "available_until":
            base_msg = "Driver requested to finish by this time (one-time exception)."
        elif et == "available_after":
            base_msg = "Driver is unavailable until this time (one-time exception)."
        elif et == "available_window":
            base_msg = "Driver is only available within this window today (one-time exception)."
        elif et == "unavailable_window":
            base_msg = "Driver is unavailable inside this window today (one-time exception)."
        if notes:
            return f"{base_msg} Note: {notes}"
        return base_msg
    # fixed_window
    regular = eff.get("regular_shift")
    start_min, end_min = eff.get("window_start_min"), eff.get("window_end_min")
    if start_min is not None and regular:
        from drivers.regular_shifts import FLOAT_KIND
        if eff.get("window_kind") == FLOAT_KIND:
            # A Float or two-shape day can sit anywhere in what its limits
            # leave, held to its longest shift — not its shapes' full spread.
            next_day = " the next day" if end_min >= 1440 else ""
            return (f"Regular {regular['name']} shift, any time between "
                    f"{_fmt_minutes(start_min)} and {_fmt_minutes(end_min)}{next_day}, "
                    f"up to {_fmt_span(eff['window_max_span_min'])}.")
        if regular["start_min"] is not None and regular["end_min"] is not None:
            next_day = " (ends next day)" if regular["end_min"] >= 1440 else ""
            return (f"Regular {regular['name']} shift, "
                    f"{_fmt_minutes(regular['start_min'])} – "
                    f"{_fmt_minutes(regular['end_min'])}{next_day}.")
    return f"Driver works {fmt_hour_long(eff['start_hour'])} – {fmt_hour_long(eff['end_hour'])} today."


def typed_hours_skip_stub_when_off(driver, eff, target_date):
    """True when hours typed in the Auto-Assign modal for a confirmed driver
    whose day reads as off skip the stub, as typed hours do on a working day
    (S4, S9): his regular Off day, or approved time off on a working day.

    Not when his limits leave that working day no time, or its shift can't be
    read: the limits are hard, and typed hours would plan work outside them.
    A flexible exception keeps today's reading (S4). `eff` is
    resolve_effective_availability(driver, target_date). The caller reads
    the switch; for a driver with no regular shift this costs no query."""
    et = eff.get("exception_type")
    if et == "flexible":
        return False
    if eff.get("regular_day_off"):
        return True
    if et != "off":
        return False
    regular = _regular_day(driver, target_date)
    if regular is None:
        return False
    from drivers import regular_shifts as rs
    hard_lo, hard_hi = driver.hard_window_minutes()
    return rs.regular_window(*regular, hard_lo=hard_lo, hard_hi=hard_hi) is not None


# ----- Window check (for warnings on assignment) -----

def is_pickup_within_window(eff, pickup_time, *, dropoff_dt=None):
    """Decide whether a leg pickup at `pickup_time` (datetime.time) falls inside the
    driver's effective availability for that date.

    Returns (ok: bool, reason: str). `reason` is empty when ok is True.
    Only returns ok=False when the system is *confident* there's a problem; an
    inconclusive case (driver is flexible / day not set) returns ok=True.

    `dropoff_dt` (optional) is the estimated end datetime; if provided, an
    `available_until` window also flags pickups that would finish past that time.

    A regular shift with the switch on (window_start_min set) is checked to the
    minute instead of the whole-hour working hours, and on top of any partial-day
    exception: a pickup is outside when it is before the window's start or at or
    after its end. The window counts minutes from 00:00 of the date, so an early
    pickup (01:00) is outside an evening shift that starts that afternoon. The
    warning says it falls in the shift that starts the day before only when the
    date before's regular window (day_before_window_*) runs past it.
    """
    if not eff.get("is_available"):
        return (False, "Driver is off this date.")

    et = eff.get("exception_type")
    st = eff.get("exception_start_time")
    en = eff.get("exception_end_time")

    if et == "available_until" and en is not None:
        if pickup_time >= en:
            return (False, f"Pickup at {fmt_time_long(pickup_time)} is after the driver's cutoff ({fmt_time_long(en)}).")
        if dropoff_dt is not None:
            end_dt = datetime.combine(dropoff_dt.date(), en)
            if dropoff_dt > end_dt + timedelta(minutes=15):
                return (False, f"Trip likely finishes past {fmt_time_long(en)} (driver requested cutoff).")
    elif et == "available_after" and st is not None:
        if pickup_time < st:
            return (False, f"Pickup at {fmt_time_long(pickup_time)} is before the driver is available ({fmt_time_long(st)}).")
    elif et == "available_window" and st is not None and en is not None:
        if pickup_time < st or pickup_time >= en:
            return (False, f"Pickup at {fmt_time_long(pickup_time)} is outside the driver's window ({fmt_time_long(st)}–{fmt_time_long(en)}).")
    elif et == "unavailable_window" and st is not None and en is not None:
        if st <= pickup_time < en:
            return (False, f"Pickup at {fmt_time_long(pickup_time)} is inside the driver's blocked window ({fmt_time_long(st)}–{fmt_time_long(en)}).")
    elif eff.get("status") == "fixed_window" and eff.get("window_start_min") is None:
        sh, eh = eff.get("start_hour"), eff.get("end_hour")
        if sh is not None and eh is not None and (pickup_time.hour < sh or pickup_time.hour >= eh):
            return (False, f"Pickup at {fmt_time_long(pickup_time)} is outside the driver's working hours ({fmt_hour_long(sh)}–{fmt_hour_long(eh)}).")

    start_min, end_min = eff.get("window_start_min"), eff.get("window_end_min")
    if start_min is not None and end_min is not None:
        p = pickup_time.hour * 60 + pickup_time.minute
        if p < start_min or p >= end_min:
            # A shift past midnight says so ("2:15 PM–2:15 AM next day"); an early
            # pickup inside its after-midnight hours is before this date's shift
            # starts — said plainly, since 1 AM reads as inside "2:15 PM–2:15 AM".
            # It belongs to the day before's shift only when he works one that
            # runs past it, so only then does the warning say so.
            shift = (f"{_fmt_minutes(start_min)}–{_fmt_minutes(end_min)}"
                     + (" next day" if end_min >= 1440 else ""))
            pickup = fmt_time_long(pickup_time)
            if p < start_min and p + 1440 < end_min:
                reason = f"Pickup at {pickup} is before the driver's regular shift starts ({shift})."
                b_start = eff.get("day_before_window_start_min")
                b_end = eff.get("day_before_window_end_min")
                if b_start is not None and b_end is not None and b_start <= p + 1440 < b_end:
                    reason += " It falls in the shift that starts the day before."
                return (False, reason)
            return (False, f"Pickup at {pickup} is outside the driver's regular shift ({shift}).")

    return (True, "")


# ----- Prominent visual helpers (red label badge + on-grid timeline band) -----

def format_exception_badge(eff):
    """Concise text for a prominent red "limited availability" pill, or "" if none.

    Examples: 'Unavailable 10:30 AM – 12:30 PM', 'Until 4 PM', 'After 12 PM',
    'Window 8 AM – 2 PM'. Returns "" for a full day off (shown as 'Off' elsewhere) or
    when there is no limiting exception — so callers can render the pill only when truthy.
    """
    if not eff.get("is_available"):
        return ""
    et = eff.get("exception_type")
    st = eff.get("exception_start_time")
    en = eff.get("exception_end_time")
    if et == "unavailable_window" and st is not None and en is not None:
        return f"Unavailable {fmt_time_long(st)} – {fmt_time_long(en)}"
    if et == "available_until" and en is not None:
        return f"Until {fmt_time_long(en)}"
    if et == "available_after" and st is not None:
        return f"After {fmt_time_long(st)}"
    if et == "available_window" and st is not None and en is not None:
        return f"Window {fmt_time_long(st)} – {fmt_time_long(en)}"
    return ""


def availability_block_bands(eff, display_start, total_display_minutes):
    """UNAVAILABLE regions to shade on a driver timeline for a partial-day exception.

    Each item is {'left_pct', 'width_pct', 'label'} positioned in the SAME coordinate
    system the dispatch views use for job slots: a time ``t`` maps to
        ((t.hour - display_start) * 60 + t.minute) / total_display_minutes * 100
    (see dispatching/views.py slot positioning), so a band lines up exactly with the
    slots and the hour grid. The timeline's right edge (100%) corresponds to
    ``total_display_minutes`` minutes after ``display_start``.

    Handles the partial-day exception types:
      unavailable_window [s,e] -> one band [s, e]
      available_after  s       -> one band [day-start, s]
      available_until  e       -> one band [e, day-end]
      available_window [s,e]   -> two bands [day-start, s] and [e, day-end]
    Returns [] when the driver is off (rendered as an 'Off' row, not a band) or there
    is no limiting exception. Spans clipped outside the visible timeline are dropped.
    """
    if not eff.get("is_available") or not total_display_minutes:
        return []
    et = eff.get("exception_type")
    st = eff.get("exception_start_time")
    en = eff.get("exception_end_time")

    def _mins(t):
        return (t.hour - display_start) * 60 + t.minute

    def _band(start_min, end_min):
        left = max(0.0, min(100.0, start_min / total_display_minutes * 100))
        right = max(0.0, min(100.0, end_min / total_display_minutes * 100))
        width = round(right - left, 1)
        if width <= 0:
            return None
        return {"left_pct": round(left, 1), "width_pct": width, "label": "Unavailable"}

    spans = []
    if et == "unavailable_window" and st is not None and en is not None:
        spans.append((_mins(st), _mins(en)))
    elif et == "available_after" and st is not None:
        spans.append((0, _mins(st)))
    elif et == "available_until" and en is not None:
        spans.append((_mins(en), total_display_minutes))
    elif et == "available_window" and st is not None and en is not None:
        spans.append((0, _mins(st)))
        spans.append((_mins(en), total_display_minutes))

    return [b for b in (_band(a, z) for a, z in spans) if b]


_SHIFT_PREF_WORDS = {
    "morning": "mornings",
    "midday":  "middays",
    "evening": "evenings",
    "night":   "nights",
}


def format_shift_preference(eff):
    """Plain-language time-of-day preference for a driver, or "" if none.

    Only the *preference* nuance — the flexible/fixed state is already shown
    separately everywhere this label appears (the unlock/lock icon + the shift
    badge), so we don't repeat "Flexible" here (it read as "Flexible" twice):
        preferred_shift  -> 'Prefers mornings'
        no preferred_shift -> '' (the flexible/fixed badge alone is enough)
    """
    if not eff.get("is_available"):
        return ""
    word = _SHIFT_PREF_WORDS.get(eff.get("preferred_shift") or "")
    if not word:
        return ""
    return f"Prefers {word}"
