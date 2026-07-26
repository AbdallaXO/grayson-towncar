"""
Cross-dispatcher coverage aggregation for the staffing board.

The staffing board answers a question a per-cell grid physically cannot:
"is each *day* adequately covered?" Coverage is a property of a column (a day),
so for each day we build a **concurrency timeline** from every dispatcher's
resolved *planned* schedule and read off the metrics that matter:

    peak / minimum concurrent headcount, opener, closer, overnight workers,
    "alone" spans (headcount == 1), and interior gaps (headcount == 0).

Design notes
------------
* Planned data only. This module reads NO ``TimeClockShift`` — "scheduled" and
  "actual" stay separate concepts (the bridge is ``ops.scheduling.schedule_vs_actual``,
  used elsewhere). The board overlays live/clocked state in a later phase.
* Reuses ``ops.scheduling.resolve_staff_schedule`` for every cell (override →
  weekly → off priority) — the resolver is the single source of truth; we never
  re-derive precedence or read override rows directly.
* Reuses ``dispatching.schedule_risk`` for the risk vocabulary so the staff and
  driver boards speak the same language (covered / tight / understaffed / critical).
* Timezone: every interval is an aware Eastern datetime built exactly like
  ``schedule_vs_actual`` (make_aware(combine(date, time))), so DST-day windows
  are 23h/25h correctly and never compared as raw minutes-of-day.
* Overnight windows (``end <= start``) roll their end to the next day and are
  split across the two calendar days they touch: a Monday 8pm–2am shift
  contributes 8pm–midnight to Monday's timeline and midnight–2am to Tuesday's.
* No DB access of its own beyond what the resolver reads from prefetched
  managers — callers MUST hand in a roster prefetched with
  ``weekly_schedule_rows`` + ``schedule_overrides`` to stay O(roster) in queries.
"""

from datetime import datetime, timedelta

from django.utils import timezone

from drivers.availability import fmt_time_long
from dispatching.schedule_risk import classify_risk, survivability_ok
from . import scheduling


# Minimum concurrent dispatchers wanted per weekday (0=Mon … 6=Sun). A plain
# constant on purpose: a table is scope creep until the founder needs no-deploy
# edits. Weekdays want two on at the thinnest moment; weekends want at least one.
COVERAGE_TARGET = {0: 2, 1: 2, 2: 2, 3: 2, 4: 2, 5: 1, 6: 1}
DEFAULT_TARGET = 1

RISK_LABELS = {
    "covered": "Covered",
    "tight": "Tight",
    "understaffed": "Understaffed",
    "critical": "Critical",
}


def target_for(date):
    return COVERAGE_TARGET.get(date.weekday(), DEFAULT_TARGET)


# ── time helpers ──────────────────────────────────────────────────────

def _et_day_bounds(date):
    """[00:00, next 00:00) as aware ET datetimes. Mirrors ops.scheduling."""
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.combine(date, datetime.min.time()), tz)
    return start, start + timedelta(days=1)


def _dt_label(dt):
    """Aware datetime → '4:30 PM' in local (Eastern) wall-clock."""
    return fmt_time_long(timezone.localtime(dt).time())


def _compact_time(t):
    """time(9,0) -> '9a'; time(17,30) -> '5:30p'; time(0,0) -> '12a'. Grid-dense."""
    if t is None:
        return ""
    h = t.hour % 12 or 12
    mer = "a" if t.hour < 12 else "p"
    return f"{h}:{t.minute:02d}{mer}" if t.minute else f"{h}{mer}"


def _resolved_window(sched, date):
    """A resolved schedule dict → (start_dt, end_dt) aware, or None if off/none.

    Overnight (``end <= start``) rolls the end to the next calendar day, so the
    returned interval is always positive-length and may cross midnight.
    """
    if not sched.get("is_working") or not sched.get("start_time") or not sched.get("end_time"):
        return None
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.combine(date, sched["start_time"]), tz)
    end = timezone.make_aware(datetime.combine(date, sched["end_time"]), tz)
    if end <= start:
        end += timedelta(days=1)
    return start, end


# ── interval sweep ────────────────────────────────────────────────────

def _segments(intervals):
    """Step function of concurrent count over the union span of ``intervals``.

    ``intervals`` = list of (start_dt, end_dt). Returns [(seg_start, seg_end,
    count)] over consecutive boundary points. Because boundary points are exactly
    the interval endpoints, the first and last segments always have count >= 1,
    so every count==0 segment is necessarily an *interior* gap. A shift ending
    exactly when another starts reads as continuous (no false gap).
    """
    if not intervals:
        return []
    points = sorted({i[0] for i in intervals} | {i[1] for i in intervals})
    segs = []
    for a, b in zip(points, points[1:]):
        count = sum(1 for s, e in intervals if s <= a and e >= b)
        segs.append((a, b, count))
    return segs


def _runs(segs, predicate):
    """Merge consecutive segments whose count matches ``predicate`` into
    [(start_dt, end_dt)] runs."""
    runs, cur = [], None
    for a, b, c in segs:
        if predicate(c):
            cur = (cur[0], b) if (cur and cur[1] == a) else (a, b)
        elif cur:
            runs.append(cur)
            cur = None
    if cur:
        runs.append(cur)
    return runs


def _who_at(intervals_with_user, a, b):
    """The single user covering segment [a, b), for 'alone' labelling."""
    for s, e, u in intervals_with_user:
        if s <= a and e >= b:
            return u
    return None


# ── public API ────────────────────────────────────────────────────────

def day_coverage(target_date, roster, *, today=None, target=None):
    """Aggregate one calendar day's planned coverage across ``roster``.

    ``roster`` should be prefetched (weekly_schedule_rows, schedule_overrides).
    Includes the tail of the *previous* day's overnight shifts in the concurrency
    math (someone who came on at 8pm yesterday still counts at 1am today).
    """
    if today is None:
        today = timezone.localdate()
    if target is None:
        target = target_for(target_date)

    day_start, day_end = _et_day_bounds(target_date)
    prev = target_date - timedelta(days=1)

    workers = []          # shifts that START today — drives opener/closer/list
    clipped = []          # (start, end, user) intervals inside [day_start, day_end)

    for u in roster:
        # This day's window.
        sched = scheduling.resolve_staff_schedule(u, target_date)
        win = _resolved_window(sched, target_date)
        if win:
            start, end = win
            workers.append({
                "user": u,
                "name": u.get_full_name() or u.username,
                "start_dt": start,
                "end_dt": end,
                "start_label": fmt_time_long(sched["start_time"]),
                "end_label": fmt_time_long(sched["end_time"]),
                "start_compact": _compact_time(sched["start_time"]),
                "end_compact": _compact_time(sched["end_time"]),
                "is_overnight": end > day_end,
                "has_exception": sched.get("has_exception", False),
            })
            cs, ce = max(start, day_start), min(end, day_end)
            if ce > cs:
                clipped.append((cs, ce, u))
        # Previous day's overnight tail spilling into this day.
        pwin = _resolved_window(scheduling.resolve_staff_schedule(u, prev), prev)
        if pwin:
            ps, pe = pwin
            cs, ce = max(ps, day_start), min(pe, day_end)
            if ce > cs:
                clipped.append((cs, ce, u))

    workers.sort(key=lambda w: (w["start_dt"], w["name"]))

    intervals = [(s, e) for s, e, _ in clipped]
    segs = _segments(intervals)
    counts = [c for _, _, c in segs]
    peak = max(counts) if counts else 0
    min_concurrent = min(counts) if counts else 0

    gap_runs = _runs(segs, lambda c: c == 0)
    alone_runs = _runs(segs, lambda c: c == 1)
    gaps = [{"start_label": _dt_label(a), "end_label": _dt_label(b)} for a, b in gap_runs]
    alone = []
    for a, b in alone_runs:
        u = _who_at(clipped, a, b)
        alone.append({
            "start_label": _dt_label(a),
            "end_label": _dt_label(b),
            "name": (u.get_full_name() or u.username) if u else "",
        })

    on_count = len(workers)
    delta = min_concurrent - target
    if peak == 0 or gap_runs:
        # Nobody on, or a hole inside the covered day — always the worst state.
        risk = "critical"
    else:
        risk = classify_risk(delta, [])
    survives = survivability_ok(min_concurrent, target)

    opener = None
    closer = None
    if workers:
        first = min(workers, key=lambda w: w["start_dt"])
        last = max(workers, key=lambda w: w["end_dt"])
        opener = {"name": first["name"], "time_label": first["start_label"]}
        closer = {"name": last["name"], "time_label": _dt_label(last["end_dt"]),
                  "is_overnight": last["is_overnight"]}
    overnight = [{"name": w["name"], "end_label": _dt_label(w["end_dt"])}
                 for w in workers if w["is_overnight"]]

    return {
        "date": target_date,
        "weekday": target_date.weekday(),
        "day_name": target_date.strftime("%a"),
        "day_name_full": target_date.strftime("%A"),
        "is_today": target_date == today,
        "is_past": target_date < today,
        "is_weekend": target_date.weekday() >= 5,
        "target": target,
        "on_count": on_count,
        "peak": peak,
        "min_concurrent": min_concurrent,
        "delta": delta,
        "risk": risk,
        "risk_label": RISK_LABELS.get(risk, risk.title()),
        "survives_callout": survives,
        "opener": opener,
        "closer": closer,
        "overnight": overnight,
        "gaps": gaps,
        "alone": alone,
        "workers": workers,
    }


def _cell(sched, date, today):
    """One dispatcher × one day cell for the grid, from a resolved schedule dict."""
    is_working = bool(sched.get("is_working"))
    st, et = sched.get("start_time"), sched.get("end_time")
    overnight = bool(is_working and st and et and et <= st)
    if is_working:
        label = f"{_compact_time(st)}–{_compact_time(et)}"  # en dash
    elif sched.get("kind") in ("off", "custom_hours"):
        label = "Off"
    else:
        label = "—"  # em dash — no schedule set
    return {
        "date": date,
        "weekday": date.weekday(),
        "is_today": date == today,
        "is_weekend": date.weekday() >= 5,
        "is_working": is_working,
        "kind": sched.get("kind"),
        "has_exception": sched.get("has_exception", False),
        "is_overnight": overnight,
        "label": label,
        "note": sched.get("note", ""),
        "tooltip": sched.get("tooltip", ""),
    }


def week_coverage(monday, roster, *, today=None):
    """Everything the staffing board renders for the Mon-anchored week.

    Returns ``{dates, days, rows}`` where ``days`` is 7 ``day_coverage`` dicts
    (the top summary strip) and ``rows`` is one per dispatcher with 7 grid cells.
    ``roster`` must be prefetched with weekly_schedule_rows + schedule_overrides.
    """
    if today is None:
        today = timezone.localdate()
    dates = [monday + timedelta(days=i) for i in range(7)]

    days = [day_coverage(d, roster, today=today) for d in dates]

    rows = []
    for u in roster:
        cells = [_cell(scheduling.resolve_staff_schedule(u, d), d, today) for d in dates]
        rows.append({
            "user": u,
            "name": u.get_full_name() or u.username,
            "cells": cells,
            "working_days": sum(1 for c in cells if c["is_working"]),
        })

    return {"dates": dates, "days": days, "rows": rows}
