"""Regular shifts (structured shifts, Stage 1) — the logic behind each driver's
confirmed weekly shift and the switch that hands it to auto-assign.

A regular shift is, per weekday, a shape (Morning / Midday / Evening / Float —
drivers.models.ShiftTemplate) plus a start and an end, stored in the new
shift_template / shift_start / shift_end fields of DriverWeeklySchedule. An end
at or before the start is the next day; both times blank means the shape's
usual times (band_fill). Times are base -> base (07 §6.1): the start is when the
driver leaves base, the end when he is back.

Each driver also has a usual shift (Driver.shift_role, S15): the default every
working day starts from and the "Morning driver" label. A single day can still
differ (S17): another shape, a second shape it may be instead ("Morning or
Evening", alt_template), or its own limits ("Thursday: done by 3 PM"). Float
(S16) means any shape, wherever the day needs him. A Float or two-shape day is
an "open" day: its typed times are labels only, and it is held to 12 hours base
to base by the rules door (Task 3c) rather than by its times.

What lives here:
  * the switch (SchedulerSettings.regular_shift_windows): read through
    regular_windows_on(), written only by set_regular_windows();
  * the shapes, cached for 60s (templates_by_id);
  * the roster that must have a confirmed regular shift before the switch can
    go on;
  * suggestions pre-filled from the last 8 weeks of real work (S6, C8), and
    the usual shift they point to (suggest_role);
  * regular_window — the ONE place a day's switch-on window is built (S4, S18);
  * validation (hard and per-day limits, the 12-hour ceiling, rest between
    days) and the softer band warnings (U11: the bands are targets, not limits);
  * save_regular_shift, which writes ONLY the new fields on an existing row so
    the legacy hour reading cannot move while the switch is off (S1);
  * the labels dispatcher pages show.

Plan: docs/scheduling-redesign/08_STAGE1_FOUNDATION_PLAN.md (Tasks 3 and 3b).
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
FLOAT_KIND = "float"            # ShiftTemplate.kind of "any shape" (S16)

_CACHE_SECONDS = 60


@dataclass(frozen=True)
class DayShift:
    """One weekday of a regular shift. template_id None = Off.

    Both times blank = the shape's usual times. alt_template_id is a second
    shape the day may be instead ("Morning or Evening"). day_earliest /
    day_latest are this day's own limits on top of the driver's hard limits;
    day_latest_next_day puts the finish-by after midnight."""
    day: int
    template_id: Optional[int]
    start: Optional[time]
    end: Optional[time]
    alt_template_id: Optional[int] = None
    day_earliest: Optional[time] = None
    day_latest: Optional[time] = None
    day_latest_next_day: bool = False

    def minutes(self) -> Optional[tuple[int, int]]:
        """(start, end) of the TYPED times in minutes after midnight, the end
        +1440 when it is at or before the start. None when the day is Off or a
        time is missing (effective_minutes fills blank times in)."""
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


@dataclass(frozen=True)
class RegularWindow:
    """A working day's window with the switch on, in minutes after 00:00 of
    the day (end may pass 1440). kind is the shape's kind, or "float" for a
    Float or two-shape day; max_span_min is the base-to-base ceiling the rules
    door holds the day to (Task 3c)."""
    start_min: int
    end_min: int
    kind: str
    max_span_min: int


def _span(start: time, end: time) -> tuple[int, int]:
    """(start, end) minutes, the end +1440 when it is at or before the start."""
    s, e = _time_minutes(start), _time_minutes(end)
    if e <= s:
        e += 1440
    return s, e


def _shift_minutes(day) -> Optional[tuple[int, int]]:
    """DayShift / DaySuggestion -> (start, end) minutes of the typed times, the
    end +1440 when it is at or before the start; None when Off or a time is
    missing."""
    if day.template_id is None or day.start is None or day.end is None:
        return None
    return _span(day.start, day.end)


def _shapes(day, templates) -> list:
    """The shapes a working day may take: its own, then its second one. A
    second shape that doesn't exist or repeats the first is left out here
    (validation refuses it). [] when Off or the shape doesn't exist. Reads a
    DaySuggestion too (no second shape)."""
    tpl = templates.get(day.template_id)
    if tpl is None:
        return []
    alt_id = getattr(day, "alt_template_id", None)
    alt = templates.get(alt_id) if alt_id not in (None, day.template_id) else None
    return [tpl] if alt is None else [tpl, alt]


def _open_day(day, templates) -> bool:
    """A Float day, or one with a second shape: it can be any of its shapes,
    so its typed times are labels only."""
    shapes = _shapes(day, templates)
    return len(shapes) > 1 or any(t.kind == FLOAT_KIND for t in shapes)


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
    """The driver's stored regular shift, Monday..Sunday, with each day's
    options. A weekday with no row, or with no shape, is Off. Reads
    weekly_schedule.all(), so a prefetch holds."""
    rows = {r.day_of_week: r for r in driver.weekly_schedule.all()}
    days = []
    for i in range(7):
        row = rows.get(i)
        if row is None or row.shift_template_id is None:
            days.append(DayShift(i, None, None, None))
        else:
            days.append(DayShift(i, row.shift_template_id, row.shift_start, row.shift_end,
                                 alt_template_id=row.alt_template_id,
                                 day_earliest=row.day_earliest_start,
                                 day_latest=row.day_latest_finish,
                                 day_latest_next_day=row.day_latest_finish_next_day))
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
    shape's longest shift. A suggestion is always a fixed shape: Float's start
    band covers the whole day, so it would win every start between two shapes,
    and picking Float is the manager's call."""
    from dispatching.analytics import categorize_location
    from reservations.models import Leg

    templates = sorted((t for t in templates_by_id().values() if t.kind != FLOAT_KIND),
                       key=lambda t: (t.sort_order, t.id))
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


def suggest_role(suggestions, templates) -> Optional[int]:
    """The usual shift a week of suggestions points to: the shape most regular
    days have, a tie going to the earlier shape (sort_order). None when no day
    is regular."""
    counts = Counter(s.template_id for s in suggestions if s.template_id in templates)
    if not counts:
        return None
    top = max(counts.values())
    return min((tid for tid, n in counts.items() if n == top),
               key=lambda tid: (templates[tid].sort_order, tid))


# ════════════════════════════════════════════════════════════════════════════
# A day's times and its window with the switch on (S4, S17, S18)
# ════════════════════════════════════════════════════════════════════════════

def effective_minutes(day, templates) -> Optional[tuple[int, int]]:
    """(start, end) minutes of a working day: its typed times or, when both are
    blank, its shape's usual times (band_fill). None when the day is Off, the
    shape doesn't exist, or only one time is given."""
    if day.template_id is None:
        return None
    if day.start is not None and day.end is not None:
        return _span(day.start, day.end)
    tpl = templates.get(day.template_id)
    if tpl is None or day.start is not None or day.end is not None:
        return None
    return _span(*band_fill(tpl))


def regular_window(day, templates, *, hard_lo: Optional[int],
                   hard_hi: Optional[int]) -> Optional[RegularWindow]:
    """The window a working day gives auto-assign with the switch on — the only
    place it is built (Task 4 calls this).

    A single fixed shape is as wide as its longest shift (M) on the edge that
    does not move (U11, C10: the handover edge floats): Morning is fixed at its
    start (s, s + M), Evening at its end (e − M, e) and never before 00:00,
    Midday keeps both times. Times are the typed ones, or the shape's usual
    times when blank. A Float or two-shape day runs from the earliest usual
    start of its shapes to the latest usual end, kind "float", with M the
    smallest longest-shift among them; its typed times are labels only. The
    rules door uses kind for the drive from and back to base: "float" has no
    evening report time and a full night return, the cautious choice.

    Then the window is clipped to the driver's hard limits (hard_lo / hard_hi,
    from Driver.hard_window_minutes()) and the day's own. None for an Off day,
    a shape that doesn't exist, only one typed time — or when the limits leave
    no time at all (validation refuses saving such a day)."""
    shapes = _shapes(day, templates)
    if not shapes:
        return None
    max_span = min(t.max_span_minutes for t in shapes)
    if _open_day(day, templates):
        kind = FLOAT_KIND
        start = min(_time_minutes(t.start_earliest) for t in shapes)
        end = max(t.end_band_minutes()[1] for t in shapes)
    else:
        mins = effective_minutes(day, templates)
        if mins is None:
            return None
        kind = shapes[0].kind
        start, end = mins
        if kind == "morning":
            end = start + max_span
        elif kind == "evening":
            start = max(0, end - max_span)
    day_lo, day_hi = _hard_minutes(day.day_earliest, day.day_latest, day.day_latest_next_day)
    start = max(v for v in (start, hard_lo, day_lo) if v is not None)
    end = min(v for v in (end, hard_hi, day_hi) if v is not None)
    if start >= end:
        return None
    return RegularWindow(start, end, kind, max_span)


# ════════════════════════════════════════════════════════════════════════════
# Validation (S8, S11, S17) and band warnings (U11)
# ════════════════════════════════════════════════════════════════════════════

def _hard_minutes(earliest, latest, latest_next_day):
    """Driver.hard_window_minutes() for values not yet saved (the profile form)."""
    lo = _time_minutes(earliest) if earliest is not None else None
    hi = None
    if latest is not None:
        hi = _time_minutes(latest) + (1440 if latest_next_day else 0)
    return lo, hi


def _day_limit_messages(day, templates, earliest, latest, hard_lo, hard_hi) -> list[str]:
    """The limit checks for one working day with a known shape and both or
    neither time: typed times against the driver's hard limits, then against
    the day's own; then, when no typed time was refused, that the limits still
    leave time for a shift at all. A day with blank times, or an open day
    (typed times are labels), is clipped by the limits, not refused by them."""
    name = DAY_NAMES[day.day]
    day_lo, day_hi = _hard_minutes(day.day_earliest, day.day_latest, day.day_latest_next_day)
    mins = None if _open_day(day, templates) else _shift_minutes(day)
    out = []
    if mins is not None:
        if hard_lo is not None and mins[0] < hard_lo:
            out.append(f"{name}: starts at {fmt_time_long(day.start)} — before this driver's "
                       f"earliest start ({fmt_time_long(earliest)}).")
        if hard_hi is not None and mins[1] > hard_hi:
            out.append(f"{name}: ends at {fmt_time_long(day.end)} — after this driver's "
                       f"latest finish ({fmt_time_long(latest)}).")
        if day_lo is not None and mins[0] < day_lo:
            out.append(f"{name}: starts at {fmt_time_long(day.start)} — before that day's "
                       f"earliest start ({fmt_time_long(day.day_earliest)}).")
        if day_hi is not None and mins[1] > day_hi:
            out.append(f"{name}: ends at {fmt_time_long(day.end)} — after that day's "
                       f"finish-by ({fmt_time_long(day.day_latest)}).")
    if not out and regular_window(day, templates, hard_lo=hard_lo, hard_hi=hard_hi) is None:
        out.append(f"{name}: the start and finish limits leave no time for a shift.")
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
    """'Morning, Midday, Evening or Float' — the shapes there are, in order."""
    names = [t.name for t in sorted(templates.values(), key=lambda t: (t.sort_order, t.id))]
    if not names:
        return "a shift"
    if len(names) == 1:
        return names[0]
    return f"{', '.join(names[:-1])} or {names[-1]}"


def _shape_problem(day, templates) -> Optional[str]:
    """Why a working day's shape, or its second shape, can't be saved."""
    name = DAY_NAMES[day.day]
    unknown = f"{name}: pick {_shape_choice(templates)}, or set the day to Off."
    tpl = templates.get(day.template_id)
    if tpl is None:                     # a shape that doesn't exist (any more)
        return unknown
    if day.alt_template_id is None:
        return None
    if day.alt_template_id == day.template_id:
        return f"{name}: the second shift must be different from the first."
    alt = templates.get(day.alt_template_id)
    if alt is None:
        return unknown
    if FLOAT_KIND in (tpl.kind, alt.kind):
        return f"{name}: Float already covers every shift — no second shift needed."
    return None


def _one_time_blank(day) -> bool:
    return (day.start is None) != (day.end is None)


def validate_regular_shift(days, *, templates, hard_earliest_start, hard_latest_finish,
                           hard_latest_finish_next_day, max_days_per_week,
                           rest_min) -> list[str]:
    """Every reason this week can't be saved, in day order; [] when it can.

    Per working day: a shape that exists in ``templates`` (and a second shape,
    if any, that exists, differs, and isn't Float on either side); both times
    or neither (neither = the shape's usual times); no longer than the shape's
    longest shift; inside the driver's limits and the day's own (limit_messages
    has the details); and at least ``rest_min`` off between the end of one
    day's window and the start of the next's (Sunday -> Monday included; 0 = no
    rest check). A Float or two-shape day skips the span and rest checks: its
    typed times are labels, and the base-to-base 12h check in the rules door
    (Task 3c) holds it. Then the days-a-week limit."""
    week = _by_day(days)
    hard_lo, hard_hi = _hard_minutes(hard_earliest_start, hard_latest_finish,
                                     hard_latest_finish_next_day)

    def fixed_window(day):
        """The day's switch-on window when it is a single fixed shape, else None."""
        if day.template_id is None or _open_day(day, templates):
            return None
        return regular_window(day, templates, hard_lo=hard_lo, hard_hi=hard_hi)

    out = []
    for i, day in enumerate(week):
        if day.template_id is None:
            continue
        name = DAY_NAMES[i]
        problem = _shape_problem(day, templates)
        if problem:
            out.append(problem)
            continue
        if _one_time_blank(day):
            out.append(f"{name}: pick a start and an end time, or set the day to Off.")
            continue
        if not _open_day(day, templates):
            mins = effective_minutes(day, templates)
            max_span = templates[day.template_id].max_span_minutes
            if mins[1] - mins[0] > max_span:
                out.append(f"{name}: a shift longer than {max_span / 60:g} hours isn't allowed.")
        out += _day_limit_messages(day, templates, hard_earliest_start, hard_latest_finish,
                                   hard_lo, hard_hi)
        here, there = fixed_window(day), fixed_window(week[(i + 1) % 7])
        if rest_min and rest_min > 0 and here is not None and there is not None:
            gap = max(0, there.start_min + 1440 - here.end_min)
            if gap < rest_min:
                out.append(f"{name} to {DAY_NAMES[(i + 1) % 7]}: only {_hm(gap)} off between "
                           f"shifts; the minimum is {_hm(rest_min)}.")
    out += _days_a_week_message(week, max_days_per_week)
    return out


def limit_messages(days, *, hard_earliest_start, hard_latest_finish,
                   hard_latest_finish_next_day, max_days_per_week,
                   templates=None) -> list[str]:
    """Only the limit checks — what the profile form re-runs when a manager
    edits the hard limits. Per working day: a typed start before the driver's
    earliest start or the day's own, a typed end after his latest finish or
    the day's finish-by, or limits that leave no time for a shift at all. Then
    the days-a-week limit. ``templates`` defaults to templates_by_id()."""
    templates = templates_by_id() if templates is None else templates
    week = _by_day(days)
    hard_lo, hard_hi = _hard_minutes(hard_earliest_start, hard_latest_finish,
                                     hard_latest_finish_next_day)
    out = []
    for day in week:
        if day.template_id in templates and not _one_time_blank(day):
            out += _day_limit_messages(day, templates, hard_earliest_start, hard_latest_finish,
                                       hard_lo, hard_hi)
    out += _days_a_week_message(week, max_days_per_week)
    return out


def _end_in_band(end, band) -> bool:
    """An end (maybe +1440) inside an end band (maybe crossing midnight), on
    whichever day lines them up."""
    lo, hi = band
    return any(lo <= end + shift <= hi for shift in (-1440, 0, 1440))


def band_warnings(days, templates) -> list[str]:
    """A warning, not an error, for each day whose typed times sit outside its
    shape's usual bands. Blank times are the usual times; a Float or two-shape
    day's times are labels, so neither is warned about."""
    out = []
    for day in _by_day(days):
        mins = _shift_minutes(day)
        tpl = templates.get(day.template_id) if mins is not None else None
        if tpl is None or _open_day(day, templates):
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
    09:00–21:00, Evening 16:00–02:15 (Float too, though a Float day's times
    are labels only — regular_window doesn't read them)."""
    start = _time_minutes(template.start_latest)
    end = template.end_band_minutes()[1]
    if end <= start:
        end += 1440                     # the end band sits after midnight
    return _to_time(start), _to_time(min(end, start + template.max_span_minutes))


# ════════════════════════════════════════════════════════════════════════════
# Saving
# ════════════════════════════════════════════════════════════════════════════

_OFF_DAY = {"shift_template": None, "shift_start": None, "shift_end": None,
            "alt_template": None, "day_earliest_start": None, "day_latest_finish": None,
            "day_latest_finish_next_day": False}


def save_regular_shift(driver, days, user, *, role_template_id: Optional[int] = None) -> None:
    """Store and confirm a driver's regular shift and usual shift
    (role_template_id; None = not set). Raises ValueError (with every message)
    and writes nothing when the usual shift doesn't exist or
    validate_regular_shift objects.

    Writes ONLY the regular-shift fields (the shape, times, second shape and
    the day's own limits) on an existing row, so the legacy fields the engine
    reads with the switch off stay exactly as they were (S1). An Off day has
    all of them cleared. A working day with no row gets one copied from the
    driver's default_* values — the same reading the resolver already gave
    that day. An Off day with no row gets none: for a confirmed driver, a
    weekday with no row (or no shape) IS Off — current_days() reads it that
    way, and so must anything that applies the regular shift (Task 4's
    switch-on resolver)."""
    week = _by_day(days)
    templates = templates_by_id()
    errors = []
    if role_template_id is not None and role_template_id not in templates:
        errors.append(f"Pick {_shape_choice(templates)} as the usual shift.")
    errors += validate_regular_shift(
        week, templates=templates,
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
                    **_OFF_DAY)
                continue
            shift = {"shift_template_id": day.template_id,
                     "shift_start": day.start, "shift_end": day.end,
                     "alt_template_id": day.alt_template_id,
                     "day_earliest_start": day.day_earliest,
                     "day_latest_finish": day.day_latest,
                     "day_latest_finish_next_day": bool(day.day_latest_next_day)}
            DriverWeeklySchedule.objects.update_or_create(
                driver=driver, day_of_week=day.day,
                defaults=shift, create_defaults={**legacy_defaults, **shift})
        driver.regular_shift_confirmed_at = timezone.now()
        driver.regular_shift_confirmed_by = user
        driver.shift_role_id = role_template_id
        driver.save(update_fields=["regular_shift_confirmed_at", "regular_shift_confirmed_by",
                                   "shift_role"])
    # A driver passed in with weekly_schedule prefetched would still show the
    # rows from before the save to current_days().
    getattr(driver, "_prefetched_objects_cache", {}).pop("weekly_schedule", None)


# ════════════════════════════════════════════════════════════════════════════
# Labels
# ════════════════════════════════════════════════════════════════════════════

_SHORT_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _shift_text(day, templates, sep, option_sep) -> str:
    """A working day in words: 'Morning 4:10 AM – 3:30 PM', 'Morning (usual
    times)', 'Morning or Evening', 'Float', then the day's own limits
    ('· not before 6 AM', '· done by 3 PM'). A Float or two-shape day shows its
    shapes only: its typed times are labels the engine doesn't use. Reads a
    DaySuggestion too (no options)."""
    shapes = _shapes(day, templates)
    name = shapes[0].name if shapes else "Regular"
    if len(shapes) > 1:
        text = " or ".join(t.name for t in shapes)
    elif shapes and shapes[0].kind == FLOAT_KIND:
        text = name
    elif day.start is not None and day.end is not None:
        text = f"{name} {fmt_time_long(day.start)}{sep}{fmt_time_long(day.end)}"
    elif shapes and day.start is None and day.end is None:
        text = f"{name} (usual times)"
    else:
        text = name
    earliest = getattr(day, "day_earliest", None)
    latest = getattr(day, "day_latest", None)
    if earliest is not None:
        text += f"{option_sep}not before {fmt_time_long(earliest)}"
    if latest is not None:
        text += f"{option_sep}done by {fmt_time_long(latest)}"
        if getattr(day, "day_latest_next_day", False):
            text += " (next day)"
    return text


def day_label(day: DayShift, templates) -> str:
    """'Morning 4:10 AM – 3:30 PM', 'Morning (usual times)', 'Morning or
    Evening', 'Float', with ' · not before 6 AM' / ' · done by 3 PM' when the
    day has its own limits — or 'Off'."""
    if day.template_id is None:
        return "Off"
    return _shift_text(day, templates, " – ", " · ")


def role_label(driver, templates) -> str:
    """The usual shift in words: 'Morning driver', 'Midday driver', 'Evening
    driver', 'Float — any shift', or '' when none is set. Reads shift_role_id
    only, so it never costs a query."""
    tpl = templates.get(driver.shift_role_id)
    if tpl is None:
        return ""
    if tpl.kind == FLOAT_KIND:
        return f"{tpl.name} — any shift"
    return f"{tpl.name} driver"


def summary_label(days, templates) -> str:
    """The week on one line, consecutive days that read the same grouped:
    'Mon–Fri Morning 4:10 AM–3:30 PM · Sat Evening 2:15 PM–2:15 AM'. A day's
    own limits follow a comma ('Thu Morning 4:10 AM–3:30 PM, done by 3 PM').
    Off days are left out; 'Off every day' when nothing is worked. Takes
    DayShifts or DaySuggestions."""
    groups = []                         # [[first day, last day, text]]
    for day in sorted(days, key=lambda d: d.day):
        if day.template_id is None:
            continue
        text = _shift_text(day, templates, "–", ", ")
        if groups and groups[-1][2] == text and groups[-1][1] == day.day - 1:
            groups[-1][1] = day.day
        else:
            groups.append([day.day, day.day, text])
    if not groups:
        return "Off every day"
    parts = []
    for first, last, text in groups:
        span = _SHORT_DAYS[first]
        if last != first:
            span += f"–{_SHORT_DAYS[last]}"
        parts.append(f"{span} {text}")
    return " · ".join(parts)
