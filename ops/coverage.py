"""
Cross-dispatcher coverage aggregation for the staffing board.

Answers a question a per-cell grid can't: "is each *day* adequately covered?"
For each day we build a concurrency timeline from every dispatcher's resolved
*planned* schedule (plus anyone marked on-call), compare it against a
time-of-day **target**, and surface the single worst coverage problem.

Why tiered targets
------------------
A flat "2 people, all day" target lights the whole week red, because nobody is
scheduled 2–6 AM (that's the on-call window) — so the thinnest moment is always
0. Real staffing is tiered: ~2 during the busy core, 1 at the edges and
overnight (covered by an on-call person). We encode that in ``target_at`` and
judge each part of the day against the target for *that* time. Edit the few
constants below to match how you actually staff — they drive every risk colour.

On-call
-------
Someone marked on-call (StaffOnCall, default 12 AM–6 AM) counts as a real body
for coverage during that window — it's additive to any regular shift. So a night
*with* an on-call person reads covered; a night with nobody reads under-target.
On-call is planned coverage only; whether they actually *logged* the on-call is
a separate concept (kept apart, like the time clock).

Everything is planned-schedule only — this module reads no TimeClockShift.
Timezone: intervals are aware Eastern datetimes (make_aware(combine(date,time)))
so DST days are 23h/25h correctly; overnight shifts (end<=start) split across
the two calendar days they touch. Callers must hand in a roster prefetched with
weekly_schedule_rows + schedule_overrides to stay O(roster) in queries.
"""

from collections import defaultdict
from datetime import datetime, time, timedelta

from django.utils import timezone

from drivers.availability import fmt_time_long
from . import scheduling
from .models import StaffOnCall


# ── Coverage targets (EDIT THESE to match how you staff) ──────────────
# Minimum dispatchers wanted, by time of day. "Core" = the busy midday block;
# everything outside it (early open, evening, and the 12–6 AM on-call window)
# wants just 1. On-call fills that overnight 1.
CORE_START = time(9, 0)    # 9 AM
CORE_END = time(20, 0)     # 8 PM
CORE_TARGET = {0: 2, 1: 2, 2: 2, 3: 2, 4: 2, 5: 1, 6: 1}  # Mon–Fri want 2, weekends 1
EDGE_TARGET = 1            # opening / evening / overnight (on-call)

# A shortfall shorter than this isn't a real coverage problem — it's a handoff
# sliver (e.g. on-call ends 6:00, the opener arrives 6:30). Keeps the board calm.
MIN_ISSUE = timedelta(minutes=30)

RISK_LABELS = {
    "covered": "Covered",
    "tight": "Tight",
    "understaffed": "Understaffed",
    "critical": "Critical",
}


def target_at(t, weekday):
    """Minimum bodies wanted at local time ``t`` on ``weekday``."""
    if CORE_START <= t < CORE_END:
        return CORE_TARGET.get(weekday, 1)
    return EDGE_TARGET


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
    """Resolved schedule dict → (start_dt, end_dt) aware, or None if off/none.
    Overnight (end<=start) rolls the end to the next day."""
    if not sched.get("is_working") or not sched.get("start_time") or not sched.get("end_time"):
        return None
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.combine(date, sched["start_time"]), tz)
    end = timezone.make_aware(datetime.combine(date, sched["end_time"]), tz)
    if end <= start:
        end += timedelta(days=1)
    return start, end


# ── interval sweep ────────────────────────────────────────────────────

def _segments(intervals, boundary_points):
    """Step function of concurrent count across the whole day.

    ``boundary_points`` (the day bounds + core-hours boundaries) are always
    included, so the entire day is evaluated — an uncovered stretch with nobody
    scheduled still shows as count 0 — and no segment straddles a target change.
    Returns [(seg_start, seg_end, count)].
    """
    pts = sorted(set(boundary_points) | {i[0] for i in intervals} | {i[1] for i in intervals})
    out = []
    for a, b in zip(pts, pts[1:]):
        count = sum(1 for s, e in intervals if s <= a and e >= b)
        out.append((a, b, count))
    return out


def _merge_ct(enriched):
    """Merge consecutive (a,b,count,target) segments that share count+target."""
    merged = []
    for a, b, c, t in enriched:
        if merged and merged[-1][2] == c and merged[-1][3] == t:
            merged[-1] = (merged[-1][0], b, c, t)
        else:
            merged.append((a, b, c, t))
    return merged


# ── public API ────────────────────────────────────────────────────────

def day_coverage(target_date, roster, *, today=None, oncall_map=None):
    """Aggregate one calendar day's planned coverage across ``roster``.

    ``oncall_map`` (optional) maps date -> list[StaffOnCall]; pass it to avoid a
    per-day query. Includes the previous day's overnight tail and same-day
    on-call bodies in the concurrency math.
    """
    if today is None:
        today = timezone.localdate()
    tz = timezone.get_current_timezone()
    day_start, day_end = _et_day_bounds(target_date)
    day_secs = (day_end - day_start).total_seconds()
    prev = target_date - timedelta(days=1)
    weekday = target_date.weekday()

    workers = []
    intervals = []   # (start, end) covering bodies inside the day — shifts + on-call

    for u in roster:
        sched = scheduling.resolve_staff_schedule(u, target_date)
        win = _resolved_window(sched, target_date)
        if win:
            start, end = win
            workers.append({
                "name": u.get_full_name() or u.username,
                "start_dt": start,
                "end_dt": end,
                "start_label": fmt_time_long(sched["start_time"]),
                "is_overnight": end > day_end,
            })
            cs, ce = max(start, day_start), min(end, day_end)
            if ce > cs:
                intervals.append((cs, ce))
        pwin = _resolved_window(scheduling.resolve_staff_schedule(u, prev), prev)
        if pwin:
            ps, pe = pwin
            cs, ce = max(ps, day_start), min(pe, day_end)
            if ce > cs:
                intervals.append((cs, ce))

    # On-call bodies (default 12–6 AM), additive to any shift.
    if oncall_map is None:
        entries = list(
            StaffOnCall.objects.filter(date=target_date, user__in=[u.id for u in roster])
            .select_related("user")
        )
    else:
        entries = oncall_map.get(target_date, [])
    oncall = []
    oncall_bands = []   # {left,width} for the timeline on-call overlay
    for oc in entries:
        s = timezone.make_aware(datetime.combine(target_date, oc.start_time), tz)
        e = timezone.make_aware(datetime.combine(target_date, oc.end_time), tz)
        if e <= s:
            e += timedelta(days=1)
        oncall.append({
            "name": oc.user.get_full_name() or oc.user.username,
            "window": f"{_compact_time(oc.start_time)}–{_compact_time(oc.end_time)}",
        })
        cs, ce = max(s, day_start), min(e, day_end)
        if ce > cs:
            intervals.append((cs, ce))
            oncall_bands.append({
                "left": round((cs - day_start).total_seconds() / day_secs * 100, 3),
                "width": round((ce - cs).total_seconds() / day_secs * 100, 3),
            })

    workers.sort(key=lambda w: (w["start_dt"], w["name"]))

    # Sweep, split at the core-hours boundaries so target is constant per segment.
    core_lo = timezone.make_aware(datetime.combine(target_date, CORE_START), tz)
    core_hi = timezone.make_aware(datetime.combine(target_date, CORE_END), tz)
    segs = _segments(intervals, (day_start, day_end, core_lo, core_hi))
    enriched = [(a, b, c, target_at(timezone.localtime(a).time(), weekday)) for a, b, c in segs]
    peak = max((c for _, _, c, _ in enriched), default=0)

    # Timeline: each segment as a % band across the day, coloured by coverage
    # level, for the visual (Timeline) view. Adjacent equal segments merged.
    timeline = []
    for a, b, c, t in enriched:
        level = "gap" if c == 0 else ("thin" if c < t else "ok")
        seg = {
            "left": round((a - day_start).total_seconds() / day_secs * 100, 3),
            "width": round((b - a).total_seconds() / day_secs * 100, 3),
            "level": level, "count": c, "target": t,
            "from": _dt_label(a), "to": _dt_label(b),
        }
        if timeline and timeline[-1]["level"] == level and timeline[-1]["count"] == c:
            timeline[-1]["width"] = round(timeline[-1]["width"] + seg["width"], 3)
            timeline[-1]["to"] = seg["to"]
        else:
            timeline.append(seg)

    # Merge into runs, then only stretches longer than MIN_ISSUE count as a
    # problem — handoff slivers are ignored so the board stays calm.
    sig = [(a, b, c, t) for a, b, c, t in _merge_ct(enriched) if (b - a) > MIN_ISSUE]
    worst_deficit = max([t - c for _, _, c, t in sig], default=0)
    worst_deficit = max(0, worst_deficit)
    crit = any(c == 0 and t >= 2 for _, _, c, t in sig) or worst_deficit >= 2
    soft = worst_deficit >= 1

    if crit:
        risk = "critical"
    elif soft:
        risk = "understaffed"
    elif any(c == t and t >= 2 for _, _, c, t in enriched):
        risk = "tight"      # met core target exactly — no buffer
    else:
        risk = "covered"

    # Single worst issue for the strip. Priority: daytime hole > big shortfall >
    # overnight/edge uncovered > small shortfall.
    issues = [(a, b, c, t) for a, b, c, t in sig if c < t]

    def _sev(seg):
        a, b, c, t = seg
        if c == 0 and t >= 2:
            return 0
        if t - c >= 2:
            return 1
        if c == 0:
            return 2
        return 3

    worst_issue = {"level": "ok", "text": "Fully covered"}
    if issues:
        a, b, c, t = min(issues, key=lambda s: (_sev(s), -(s[3] - s[2]), s[0]))
        span = f"{_dt_label(a)} – {_dt_label(b)}"
        if c == 0 and t >= 2:
            worst_issue = {"level": "crit", "text": f"No coverage {span}"}
        elif t - c >= 2:
            worst_issue = {"level": "crit", "text": f"{c} of {t} · {span}"}
        elif c == 0:
            # Only the actual overnight window reads as an on-call gap; a morning
            # or evening hole is just "uncovered".
            overnight = timezone.localtime(a).time() < time(6)
            worst_issue = {"level": "soft", "text": (f"No on-call {span}" if overnight else f"Uncovered {span}")}
        else:
            worst_issue = {"level": "soft", "text": f"{c} of {t} · {span}"}

    coverage_span = None
    if intervals:
        coverage_span = f"{_dt_label(min(s for s, _ in intervals))} – {_dt_label(max(e for _, e in intervals))}"

    return {
        "date": target_date,
        "weekday": weekday,
        "day_name": target_date.strftime("%a"),
        "day_name_full": target_date.strftime("%A"),
        "is_today": target_date == today,
        "is_past": target_date < today,
        "is_weekend": weekday >= 5,
        "risk": risk,
        "risk_label": RISK_LABELS.get(risk, risk.title()),
        "on_count": len(workers),
        "oncall": oncall,
        "peak": peak,
        "worst_issue": worst_issue,
        "coverage_span": coverage_span,
        "opener": ({"name": workers[0]["name"], "time_label": workers[0]["start_label"]} if workers else None),
        "overnight": [w["name"] for w in workers if w["is_overnight"]],
        "timeline": timeline,
        "oncall_bands": oncall_bands,
    }


def _cell(sched, date, today, oncall=None):
    """One dispatcher × one day cell for the grid, from a resolved schedule dict."""
    is_working = bool(sched.get("is_working"))
    st, et = sched.get("start_time"), sched.get("end_time")
    overnight = bool(is_working and st and et and et <= st)
    if is_working:
        label = f"{_compact_time(st)}–{_compact_time(et)}"
    elif sched.get("kind") in ("off", "custom_hours"):
        label = "Off"
    else:
        label = "—"
    return {
        "date": date,
        "weekday": date.weekday(),
        "is_today": date == today,
        "is_weekend": date.weekday() >= 5,
        "is_working": is_working,
        "kind": sched.get("kind"),
        "has_exception": sched.get("has_exception", False),
        "is_overnight": overnight,
        "is_oncall": oncall is not None,
        "oncall_label": (f"{_compact_time(oncall.start_time)}–{_compact_time(oncall.end_time)}" if oncall else ""),
        "label": label,
        "note": sched.get("note", ""),
        "tooltip": sched.get("tooltip", ""),
    }


def week_coverage(monday, roster, *, today=None):
    """Everything the staffing board renders for the Mon-anchored week:
    ``{dates, days, rows}``. One query for the week's on-call entries."""
    if today is None:
        today = timezone.localdate()
    dates = [monday + timedelta(days=i) for i in range(7)]

    oncall_by_date = defaultdict(list)
    oncall_by_user_date = {}
    for oc in (
        StaffOnCall.objects.filter(date__range=(dates[0], dates[-1]), user__in=[u.id for u in roster])
        .select_related("user")
    ):
        oncall_by_date[oc.date].append(oc)
        oncall_by_user_date[(oc.user_id, oc.date)] = oc

    days = [day_coverage(d, roster, today=today, oncall_map=oncall_by_date) for d in dates]

    rows = []
    for u in roster:
        cells = [
            _cell(scheduling.resolve_staff_schedule(u, d), d, today, oncall=oncall_by_user_date.get((u.id, d)))
            for d in dates
        ]
        rows.append({
            "user": u,
            "name": u.get_full_name() or u.username,
            "cells": cells,
            "working_days": sum(1 for c in cells if c["is_working"]),
            "oncall_days": sum(1 for c in cells if c["is_oncall"]),
        })

    return {"dates": dates, "days": days, "rows": rows}
