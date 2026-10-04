"""Regular shifts (structured shifts, Stage 1) — the logic behind each driver's
confirmed weekly shift and the switch that hands it to auto-assign.

A regular shift is, per weekday, a shape (Morning / Midday / Evening —
drivers.models.ShiftTemplate) plus a start and an end, stored in the new
shift_template / shift_start / shift_end fields of DriverWeeklySchedule. An end
at or before the start is the next day. Times are base -> base (07 §6.1): the
start is when the driver leaves base, the end when he is back.

What lives here:
  * the switch (SchedulerSettings.regular_shift_windows): read through
    regular_windows_on(), written only by set_regular_windows();
  * the shapes, cached for 60s (templates_by_id);
  * the roster that must have a confirmed regular shift before the switch can
    go on;
  * suggestions pre-filled from the last 8 weeks of real work (S6, C8);
  * validation (hard limits, the 12-hour ceiling, rest between days) and the
    softer band warnings (U11: the bands are targets, not limits);
  * save_regular_shift, which writes ONLY the new fields on an existing row so
    the legacy hour reading cannot move while the switch is off (S1);
  * the labels dispatcher pages show.

Plan: docs/scheduling-redesign/08_STAGE1_FOUNDATION_PLAN.md (Task 3).
Design: docs/scheduling-redesign/07_STRUCTURED_SHIFTS_DESIGN.md.
"""
from __future__ import annotations

import logging
import math
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Optional

from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from dispatching import handoff_chain as hc
from dispatching.models import REGULAR_WINDOWS_CACHE_KEY, SchedulerSettings
from drivers.availability import fmt_time_long
from drivers.models import (
    SHIFT_TEMPLATES_CACHE_KEY,
    Driver,
    DriverWeeklySchedule,
    ShiftTemplate,
    _time_minutes,
)

logger = logging.getLogger(__name__)

LOOKBACK_DAYS = 56              # pre-fill reads the 8 weeks before today (C8)
REGULAR_DAY_MIN_WEEKS = 4       # a weekday worked in >= 4 of those 8 weeks is regular
ROUND_MIN = 5                   # suggested start rounds down, end rounds up, to 5 min
NIGHT_TAIL_END = time(2, 0)     # a pickup before 02:00 belongs to the previous day's shift
DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

_CACHE_SECONDS = 60


@dataclass(frozen=True)
class DayShift:
    """One weekday of a regular shift. template_id None = Off."""
    day: int
    template_id: Optional[int]
    start: Optional[time]
    end: Optional[time]

    def minutes(self) -> Optional[tuple[int, int]]:
        """(start, end) in minutes after midnight, the end +1440 when it is at or
        before the start. None when the day is Off or a time is missing."""
        return _shift_minutes(self)


@dataclass(frozen=True)
class DaySuggestion:
    """What the last 8 weeks say about one weekday. template_id None = not a
    regular day (worked in fewer than REGULAR_DAY_MIN_WEEKS weeks)."""
    day: int
    weeks_worked: int
    template_id: Optional[int]
    start: Optional[time]
    end: Optional[time]


def _shift_minutes(day) -> Optional[tuple[int, int]]:
    """DayShift / DaySuggestion -> (start, end) minutes, the end +1440 when it
    is at or before the start; None when Off or a time is missing."""
    if day.template_id is None or day.start is None or day.end is None:
        return None
    start, end = _time_minutes(day.start), _time_minutes(day.end)
    if end <= start:
        end += 1440
    return start, end


def _to_time(minutes) -> time:
    """Minutes after midnight (any day) -> time of day. 1575 -> 02:15."""
    m = int(minutes) % 1440
    return time(m // 60, m % 60)


def _name(driver) -> str:
    return str(driver).strip()


# ════════════════════════════════════════════════════════════════════════════
# The switch (S3)
# ════════════════════════════════════════════════════════════════════════════

def regular_windows_on() -> bool:
    """Whether auto-assign reads confirmed regular shifts. Cached in Django's
    cache for 60s under REGULAR_WINDOWS_CACHE_KEY; every writer deletes it. A
    missing settings row reads as off."""
    on = cache.get(REGULAR_WINDOWS_CACHE_KEY)
    if on is None:
        on = bool(SchedulerSettings.objects.filter(pk=1)
                  .values_list("regular_shift_windows", flat=True).first())
        cache.set(REGULAR_WINDOWS_CACHE_KEY, on, _CACHE_SECONDS)
    return on


def set_regular_windows(on: bool, user) -> tuple[bool, str]:
    """Turn the switch on or off. Returns (done, message).

    Turning it on is refused while any roster driver still lacks a confirmed
    regular shift; turning it off always works. Writes with filter().update(),
    never by saving a process-cached settings row (production runs several
    workers, each holding its own copy), then clears every cached copy."""
    if on:
        missing = drivers_without_regular_shift()
        if missing:
            n = len(missing)
            names = ", ".join(_name(d) for d in missing[:3])
            if n == 1:
                return False, f"1 driver still needs a regular shift: {names}"
            return False, (f"{n} drivers still need a regular shift: {names}"
                           + ("…" if n > 3 else ""))
    row, _ = SchedulerSettings.objects.get_or_create(pk=1)
    SchedulerSettings.objects.filter(pk=row.pk).update(regular_shift_windows=bool(on))
    SchedulerSettings.clear_cache()
    logger.info("Regular-shift switch turned %s by %s", "on" if on else "off",
                getattr(user, "username", user))
    return True, ""


# ════════════════════════════════════════════════════════════════════════════
# Shapes, roster, current days
# ════════════════════════════════════════════════════════════════════════════

def templates_by_id() -> dict[int, ShiftTemplate]:
    """{id: ShiftTemplate}, cached for 60s under SHIFT_TEMPLATES_CACHE_KEY."""
    templates = cache.get(SHIFT_TEMPLATES_CACHE_KEY)
    if templates is None:
        templates = {t.id: t for t in ShiftTemplate.objects.all()}
        cache.set(SHIFT_TEMPLATES_CACHE_KEY, templates, _CACHE_SECONDS)
    return templates


def clear_template_cache() -> None:
    """Call after saving any ShiftTemplate."""
    cache.delete(SHIFT_TEMPLATES_CACHE_KEY)


def roster_drivers() -> list[Driver]:
    """Everyone who needs a regular shift before the switch can go on: active
    in-house chauffeurs, minus the placeholder and demo records Day Setup also
    leaves out. Sorted by name; weekly rows prefetched for current_days()."""
    from dispatching.day_setup import _is_excluded
    drivers = (Driver.objects
               .filter(is_active=True, driver_type="inhouse", portal_role="driver")
               .select_related("profile")
               .prefetch_related("weekly_schedule"))
    return sorted((d for d in drivers if not _is_excluded(d)),
                  key=lambda d: _name(d).casefold())


def drivers_without_regular_shift() -> list[Driver]:
    return [d for d in roster_drivers() if d.regular_shift_confirmed_at is None]


def current_days(driver) -> list[DayShift]:
    """The driver's stored regular shift, Monday..Sunday. A weekday with no row,
    or with no shape, is Off. Reads weekly_schedule.all(), so a prefetch holds."""
    rows = {r.day_of_week: r for r in driver.weekly_schedule.all()}
    days = []
    for i in range(7):
        row = rows.get(i)
        if row is None or row.shift_template_id is None:
            days.append(DayShift(i, None, None, None))
        else:
            days.append(DayShift(i, row.shift_template_id, row.shift_start, row.shift_end))
    return days


def _by_day(days) -> list:
    """Seven entries Monday..Sunday; a weekday missing from ``days`` is Off."""
    given = {d.day: d for d in days}
    return [given.get(i) or DayShift(i, None, None, None) for i in range(7)]


# ════════════════════════════════════════════════════════════════════════════
# Suggestions from the last 8 weeks (S6)
# ════════════════════════════════════════════════════════════════════════════

def _band_distance(minutes, band) -> float:
    lo, hi = band
    if minutes < lo:
        return lo - minutes
    if minutes > hi:
        return minutes - hi
    return 0


def _nearest_template(raw_start, templates):
    """The shape whose start band is nearest raw_start; ties go to the earlier
    shape (sort_order)."""
    return min(templates, key=lambda t: _band_distance(raw_start, t.start_band_minutes()))


def suggest_regular_shifts(drivers, today: date) -> dict[int, list[DaySuggestion]]:
    """{driver_id: [DaySuggestion Monday..Sunday]} from the 56 days before today.

    ONE Leg query for every driver. A pickup before 02:00 finishes the day
    before when that day was worked (it had a pickup at 02:00 or later);
    otherwise it is early work on its own date, so a night-only day is never
    moved onto the wrong weekday. Today's pickups before 02:00 count for
    yesterday. Per worked day, every leg is held to the same base -> base rule
    the engine checks: the raw start is the earliest of each pickup minus its
    own drive from base and pickup buffer, and picks the shape whose start band
    is nearest; an Evening day starts a further 25 min earlier (report before
    car-ready); the end is the latest of each leg's P50 occupancy end plus its
    own return to base. A start before midnight is held at 00:00 (the weekday
    can't begin the day before).
    A weekday worked in at least 4 of the 8 weeks is regular: its shape is the
    one most of those days had (a tie goes to the shape nearest the median raw
    start), and its start and end are medians over the days of that shape —
    start rounded down and end rounded up to 5 min, the end capped at the
    shape's longest shift."""
    from dispatching.analytics import categorize_location
    from reservations.models import Leg

    templates = sorted(templates_by_id().values(), key=lambda t: (t.sort_order, t.id))
    ids = [d.id for d in drivers]
    window_start = today - timedelta(days=LOOKBACK_DAYS)
    night_end = _time_minutes(NIGHT_TAIL_END)
    legs = (Leg.objects
            # One day either side of [window_start, today): the day before tells
            # whether window_start's night pickups finish it, and today's night
            # pickups finish yesterday. Neither outside day is suggested from.
            .filter(driver_id__in=ids, pickup_date__gte=window_start - timedelta(days=1),
                    pickup_date__lte=today)
            # Both Reservation cancellation spellings; Leg.status only uses two-L.
            .exclude(status="cancelled")
            .exclude(reservation__status__in=("cancelled", "canceled"))
            .values_list("driver_id", "pickup_date", "pickup_time",
                         "pickup_location", "dropoff_location"))

    zones = {}

    def zone(text):
        if text not in zones:
            zones[text] = categorize_location(text)
        return zones[text]

    # {(driver_id, board date): [(pickup minutes, pickup zone, drop zone)]}
    on_board = defaultdict(list)
    for driver_id, pickup_date, pickup_time, pickup_loc, drop_loc in legs:
        on_board[(driver_id, pickup_date)].append(
            (_time_minutes(pickup_time), zone(pickup_loc), zone(drop_loc)))
    day_work = {key for key, stops in on_board.items()
                if any(minutes >= night_end for minutes, _, _ in stops)}

    # {(driver_id, worked date): [(pickup minutes on that date, pickup zone, drop zone)]}
    worked = defaultdict(list)
    for (driver_id, board_day), stops in on_board.items():
        prev = board_day - timedelta(days=1)
        for minutes, pickup_zone, drop_zone in stops:
            if minutes < night_end and (driver_id, prev) in day_work:
                worked[(driver_id, prev)].append((minutes + 1440, pickup_zone, drop_zone))
            else:
                worked[(driver_id, board_day)].append((minutes, pickup_zone, drop_zone))

    # {(driver_id, weekday): [(raw start, template, start, end) per worked day]}
    by_weekday = defaultdict(list)
    for (driver_id, day), stops in worked.items():
        if not window_start <= day < today:
            continue                    # the day before the lookback, or today
        if not templates:               # no shapes to suggest; still count the weeks
            by_weekday[(driver_id, day.weekday())].append(None)
            continue
        raw_start = min(minutes - hc.shift_lead_min("morning", pickup_zone)
                        for minutes, pickup_zone, _ in stops)
        tpl = _nearest_template(raw_start, templates)
        start = raw_start - (hc.EVENING_REPORT_LEAD_MIN if tpl.kind == "evening" else 0)
        start = max(0, start)
        base = datetime.combine(day, time(0))
        end = max(
            (hc.occupancy_interval(base + timedelta(minutes=minutes),
                                   hc.occupancy_kind(pickup_zone, drop_zone), "p50")[1]
             - base).total_seconds() / 60 + hc.shift_tail_min(tpl.kind, drop_zone)
            for minutes, pickup_zone, drop_zone in stops)
        by_weekday[(driver_id, day.weekday())].append((raw_start, tpl, start, end))

    result = {}
    for driver_id in ids:
        days = []
        for wd in range(7):
            entries = by_weekday.get((driver_id, wd), [])
            if len(entries) < REGULAR_DAY_MIN_WEEKS or not templates:
                days.append(DaySuggestion(wd, len(entries), None, None, None))
                continue
            counts = Counter(t.id for _, t, _, _ in entries)
            top = max(counts.values())
            tied = [t for t in templates if counts.get(t.id) == top]
            tpl = (tied[0] if len(tied) == 1 else
                   _nearest_template(statistics.median(r for r, _, _, _ in entries), tied))
            mine = [(s, e) for _, t, s, e in entries if t.id == tpl.id]
            start = math.floor(statistics.median(s for s, _ in mine) / ROUND_MIN) * ROUND_MIN
            end = math.ceil(statistics.median(e for _, e in mine) / ROUND_MIN) * ROUND_MIN
            end = min(end, start + tpl.max_span_minutes)
            days.append(DaySuggestion(wd, len(entries), tpl.id, _to_time(start), _to_time(end)))
        result[driver_id] = days
    return result


# ════════════════════════════════════════════════════════════════════════════
# Validation (S8, S11) and band warnings (U11)
# ════════════════════════════════════════════════════════════════════════════

def _hard_minutes(earliest, latest, latest_next_day):
    """Driver.hard_window_minutes() for values not yet saved (the profile form)."""
    lo = _time_minutes(earliest) if earliest is not None else None
    hi = None
    if latest is not None:
        hi = _time_minutes(latest) + (1440 if latest_next_day else 0)
    return lo, hi


def _day_limit_messages(day, mins, earliest, latest, hard_lo, hard_hi) -> list[str]:
    name = DAY_NAMES[day.day]
    out = []
    if hard_lo is not None and mins[0] < hard_lo:
        out.append(f"{name}: starts at {fmt_time_long(day.start)} — before this driver's "
                   f"earliest start ({fmt_time_long(earliest)}).")
    if hard_hi is not None and mins[1] > hard_hi:
        out.append(f"{name}: ends at {fmt_time_long(day.end)} — after this driver's "
                   f"latest finish ({fmt_time_long(latest)}).")
    return out


def _days_a_week_message(days, max_days_per_week) -> list[str]:
    working = sum(1 for d in days if d.template_id is not None)
    if max_days_per_week is not None and working > max_days_per_week:
        return [f"{working} working days — more than this driver's limit of "
                f"{max_days_per_week} a week."]
    return []


def _hm(minutes) -> str:
    h, m = divmod(int(minutes), 60)
    return f"{h}h {m}m"


def _shape_choice(templates) -> str:
    """'Morning, Midday or Evening' — the shapes there are, in order."""
    names = [t.name for t in sorted(templates.values(), key=lambda t: (t.sort_order, t.id))]
    if not names:
        return "a shift"
    if len(names) == 1:
        return names[0]
    return f"{', '.join(names[:-1])} or {names[-1]}"


def validate_regular_shift(days, *, templates, hard_earliest_start, hard_latest_finish,
                           hard_latest_finish_next_day, max_days_per_week,
                           rest_min) -> list[str]:
    """Every reason this week can't be saved, in day order; [] when it can.

    Per working day: a shape that exists in ``templates``, both times given,
    no longer than the shape's longest shift,
    inside the driver's earliest start / latest finish, and at least ``rest_min``
    off before the next day's shift (Sunday -> Monday included; 0 = no rest
    check). Then the days-a-week limit."""
    week = _by_day(days)
    hard_lo, hard_hi = _hard_minutes(hard_earliest_start, hard_latest_finish,
                                     hard_latest_finish_next_day)
    out = []
    for i, day in enumerate(week):
        if day.template_id is None:
            continue
        name = DAY_NAMES[i]
        tpl = templates.get(day.template_id)
        if tpl is None:                 # a shape that doesn't exist (any more)
            out.append(f"{name}: pick {_shape_choice(templates)}, or set the day to Off.")
            continue
        mins = _shift_minutes(day)
        if mins is None:
            out.append(f"{name}: pick a start and an end time, or set the day to Off.")
            continue
        max_span = tpl.max_span_minutes
        if mins[1] - mins[0] > max_span:
            out.append(f"{name}: a shift longer than {max_span / 60:g} hours isn't allowed.")
        out += _day_limit_messages(day, mins, hard_earliest_start, hard_latest_finish,
                                   hard_lo, hard_hi)
        nxt = _shift_minutes(week[(i + 1) % 7])
        if rest_min and rest_min > 0 and nxt is not None:
            gap = max(0, nxt[0] + 1440 - mins[1])
            if gap < rest_min:
                out.append(f"{name} to {DAY_NAMES[(i + 1) % 7]}: only {_hm(gap)} off between "
                           f"shifts; the minimum is {_hm(rest_min)}.")
    out += _days_a_week_message(week, max_days_per_week)
    return out


def limit_messages(days, *, hard_earliest_start, hard_latest_finish,
                   hard_latest_finish_next_day, max_days_per_week) -> list[str]:
    """Only the earliest-start, latest-finish and days-a-week checks — what the
    profile form re-runs when a manager edits the hard limits."""
    week = _by_day(days)
    hard_lo, hard_hi = _hard_minutes(hard_earliest_start, hard_latest_finish,
                                     hard_latest_finish_next_day)
    out = []
    for day in week:
        mins = _shift_minutes(day)
        if mins is not None:
            out += _day_limit_messages(day, mins, hard_earliest_start, hard_latest_finish,
                                       hard_lo, hard_hi)
    out += _days_a_week_message(week, max_days_per_week)
    return out


def _end_in_band(end, band) -> bool:
    """An end (maybe +1440) inside an end band (maybe crossing midnight), on
    whichever day lines them up."""
    lo, hi = band
    return any(lo <= end + shift <= hi for shift in (-1440, 0, 1440))


def band_warnings(days, templates) -> list[str]:
    """A warning, not an error, for each day outside its shape's usual bands."""
    out = []
    for day in _by_day(days):
        mins = _shift_minutes(day)
        tpl = templates.get(day.template_id) if mins is not None else None
        if tpl is None:
            continue
        s_lo, s_hi = tpl.start_band_minutes()
        if not (s_lo <= mins[0] <= s_hi) or not _end_in_band(mins[1], tpl.end_band_minutes()):
            times = f"{fmt_time_long(day.start)}–{fmt_time_long(day.end)}"
            out.append(f"{DAY_NAMES[day.day]}: {times} is outside the usual {tpl.name} "
                       f"shape ({tpl.band_label()}).")
    return out


def band_fill(template) -> tuple[time, time]:
    """Default times when a shape is picked with blank times: leave at the
    latest usual start, back at the latest usual end, or sooner if that would
    run past the shape's longest shift. Seed: Morning 06:00–16:00, Midday
    09:00–21:00, Evening 16:00–02:15."""
    start = _time_minutes(template.start_latest)
    end = template.end_band_minutes()[1]
    if end <= start:
        end += 1440                     # the end band sits after midnight
    return _to_time(start), _to_time(min(end, start + template.max_span_minutes))


# ════════════════════════════════════════════════════════════════════════════
# Saving
# ════════════════════════════════════════════════════════════════════════════

def save_regular_shift(driver, days, user) -> None:
    """Store and confirm a driver's regular shift. Raises ValueError (with every
    message) and writes nothing when validate_regular_shift objects.

    Writes ONLY shift_template / shift_start / shift_end on an existing row, so
    the legacy fields the engine reads with the switch off stay exactly as they
    were (S1). A working day with no row gets one copied from the driver's
    default_* values — the same reading the resolver already gave that day. An
    Off day with no row gets none: for a confirmed driver, a weekday with no
    row (or no shape) IS Off — current_days() reads it that way, and so must
    anything that applies the regular shift (Task 4's switch-on resolver)."""
    week = _by_day(days)
    errors = validate_regular_shift(
        week, templates=templates_by_id(),
        hard_earliest_start=driver.hard_earliest_start,
        hard_latest_finish=driver.hard_latest_finish,
        hard_latest_finish_next_day=driver.hard_latest_finish_next_day,
        max_days_per_week=driver.max_days_per_week,
        rest_min=SchedulerSettings.get_settings().rest_min_gap_minutes)
    if errors:
        raise ValueError("\n".join(errors))

    legacy_defaults = {
        "is_available": True,
        "shift_type": driver.default_shift_type,
        "start_hour": driver.default_start_hour,
        "end_hour": driver.default_end_hour,
        "flexible": driver.default_flexible,
        "max_hours": driver.default_max_hours,
        "preferred_shift": driver.default_preferred_shift,
        "preference": driver.default_preference,
    }
    with transaction.atomic():
        for day in week:
            if day.template_id is None:
                DriverWeeklySchedule.objects.filter(driver=driver, day_of_week=day.day).update(
                    shift_template=None, shift_start=None, shift_end=None)
                continue
            shift = {"shift_template_id": day.template_id,
                     "shift_start": day.start, "shift_end": day.end}
            DriverWeeklySchedule.objects.update_or_create(
                driver=driver, day_of_week=day.day,
                defaults=shift, create_defaults={**legacy_defaults, **shift})
        driver.regular_shift_confirmed_at = timezone.now()
        driver.regular_shift_confirmed_by = user
        driver.save(update_fields=["regular_shift_confirmed_at", "regular_shift_confirmed_by"])
    # A driver passed in with weekly_schedule prefetched would still show the
    # rows from before the save to current_days().
    getattr(driver, "_prefetched_objects_cache", {}).pop("weekly_schedule", None)


# ════════════════════════════════════════════════════════════════════════════
# Labels
# ════════════════════════════════════════════════════════════════════════════

_SHORT_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _shift_text(day, templates, sep) -> str:
    tpl = templates.get(day.template_id)
    name = tpl.name if tpl is not None else "Regular"
    if day.start is None or day.end is None:
        return name
    return f"{name} {fmt_time_long(day.start)}{sep}{fmt_time_long(day.end)}"


def day_label(day: DayShift, templates) -> str:
    """'Morning 4:10 AM – 3:30 PM', or 'Off'."""
    if day.template_id is None:
        return "Off"
    return _shift_text(day, templates, " – ")


def summary_label(days, templates) -> str:
    """The week on one line, consecutive days with the same shift grouped:
    'Mon–Fri Morning 4:10 AM–3:30 PM · Sat Evening 2:15 PM–2:15 AM'. Off days
    are left out; 'Off every day' when nothing is worked. Takes DayShifts or
    DaySuggestions."""
    groups = []                         # [[first day, last day, key, day]]
    for day in sorted(days, key=lambda d: d.day):
        if day.template_id is None:
            continue
        key = (day.template_id, day.start, day.end)
        if groups and groups[-1][2] == key and groups[-1][1] == day.day - 1:
            groups[-1][1] = day.day
        else:
            groups.append([day.day, day.day, key, day])
    if not groups:
        return "Off every day"
    parts = []
    for first, last, _, day in groups:
        span = _SHORT_DAYS[first]
        if last != first:
            span += f"–{_SHORT_DAYS[last]}"
        parts.append(f"{span} {_shift_text(day, templates, '–')}")
    return " · ".join(parts)
