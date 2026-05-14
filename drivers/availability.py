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
"""
from datetime import datetime, time, timedelta


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


def resolve_effective_availability(driver, target_date):
    """Combine weekly/default availability with any active exception for target_date.

    Returns a dict (see plan or callers for keys). Always returns a dict — never None."""
    base = _weekly_or_defaults(driver, target_date)
    exception = _pick_active_exception(list(driver.date_overrides.all()), target_date)

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
    return f"Driver works {fmt_hour_long(eff['start_hour'])} – {fmt_hour_long(eff['end_hour'])} today."


# ----- Window check (for warnings on assignment) -----

def is_pickup_within_window(eff, pickup_time, *, dropoff_dt=None):
    """Decide whether a leg pickup at `pickup_time` (datetime.time) falls inside the
    driver's effective availability for that date.

    Returns (ok: bool, reason: str). `reason` is empty when ok is True.
    Only returns ok=False when the system is *confident* there's a problem; an
    inconclusive case (driver is flexible / day not set) returns ok=True.

    `dropoff_dt` (optional) is the estimated end datetime; if provided, an
    `available_until` window also flags pickups that would finish past that time.
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
    elif eff.get("status") == "fixed_window":
        sh, eh = eff.get("start_hour"), eff.get("end_hour")
        if sh is not None and eh is not None and (pickup_time.hour < sh or pickup_time.hour >= eh):
            return (False, f"Pickup at {fmt_time_long(pickup_time)} is outside the driver's working hours ({fmt_hour_long(sh)}–{fmt_hour_long(eh)}).")

    return (True, "")
