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

from django.db.models import Q
from django.utils import timezone

from drivers.availability import fmt_time_long
from . import scheduling
from .models import StaffOnCall, StaffScheduleOverride, STAFF_ROLE_LABELS


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


# ══════════════════════════════════════════════════════════════════════
# Weekly PATTERN — the recurring schedule by weekday (no dates, no DST).
#
# This is what the staffing board actually renders: the standard week
# straight from StaffWeeklySchedule (keyed by weekday), not a specific
# calendar week. Overnight 12–6 AM is the on-call window — shown quietly,
# never as a gap — since on-call is marked per night on the Time Clock page.
# Everything here is plain minutes-of-day (0–1440), so no timezone math.
# ══════════════════════════════════════════════════════════════════════

DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
DAY_NAMES_FULL = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
ONCALL_WINDOW = (0, 360)      # 12 AM–6 AM
OPERATING = (360, 1440)       # 6 AM–midnight (what we judge for gaps)
CORE = (540, 1200)            # 9 AM–8 PM
CORE_WEEKDAY_TARGET = 2       # Mon–Fri core wants 2 on
MIN_GAP_MIN = 60              # ignore shortfalls under an hour


# ── Per-person colour ─────────────────────────────────────────────────
# One stable colour per dispatcher so a shift can be traced across days and
# views at a glance. Ten hues spread around the wheel, muted enough to sit on
# the board's parchment ground and to keep white/ink text legible on the fill.
PALETTE = [
    ("teal",   "#0E7C86"),
    ("indigo", "#3A4BA0"),
    ("clay",   "#B4530A"),
    ("forest", "#2C7A4C"),
    ("plum",   "#7B3A86"),
    ("ocean",  "#1F6FB2"),
    ("rose",   "#A83458"),
    ("olive",  "#6B7A12"),
    ("bronze", "#8A6A12"),
    ("slate",  "#4F5A6B"),
]


def _rgba(hex_color, alpha):
    h = hex_color.lstrip("#")
    return f"rgba({int(h[0:2], 16)},{int(h[2:4], 16)},{int(h[4:6], 16)},{alpha})"


def _swatch(index):
    name, hexc = PALETTE[index % len(PALETTE)]
    return {
        "i": index % len(PALETTE),
        "name": name,
        "ink": hexc,
        # Tinted rather than solid so ink-dark hours stay readable on top, but
        # saturated enough that a bar reads as *that person's* colour at a glance.
        "fill": _rgba(hexc, 0.34),
        "fill2": _rgba(hexc, 0.48),
        "line": _rgba(hexc, 0.7),
        "glow": _rgba(hexc, 0.32),
    }


def assign_colors(roster):
    """``{user_id: swatch}`` — a stable colour per dispatcher.

    Seeded from the user id (so a colour doesn't shuffle when a teammate is
    added ahead of them alphabetically), then walked forward to the next free
    slot on collision, which keeps every visible dispatcher distinct as long as
    the roster fits the palette.
    """
    used, out = set(), {}
    for u in roster:
        base = u.id % len(PALETTE)
        idx = base
        for step in range(len(PALETTE)):
            cand = (base + step) % len(PALETTE)
            if cand not in used:
                idx = cand
                break
        used.add(idx)
        out[u.id] = _swatch(idx)
    return out


def _short_name(name):
    """'Joseph Adams' -> 'Joseph'; used where a bar is too narrow for the full name."""
    return name.split()[0] if name.split() else name


def _min_of(t):
    return t.hour * 60 + t.minute


def _fmt_min(m):
    m %= 1440
    h, mm = divmod(m, 60)
    ap = "a" if h < 12 else "p"
    h12 = h % 12 or 12
    return f"{h12}:{mm:02d}{ap}" if mm else f"{h12}{ap}"


def _fmt_min_long(m):
    """Minutes-of-day -> full readable clock: '9:00 AM', '7:30 AM', '12:00 AM'.

    Always shows minutes and a full AM/PM (the dispatcher view uses this; the
    admin board keeps the dense ``_fmt_min`` form)."""
    m %= 1440
    h, mm = divmod(m, 60)
    ap = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return f"{h12}:{mm:02d} {ap}"


def _pct_min(m):
    return round(m / 1440 * 100, 3)


def _runs_min(cover, lo, hi, predicate, min_len):
    """Maximal [a,b) runs in cover[lo:hi] where predicate(count) holds, length >= min_len."""
    out, i = [], lo
    while i < hi:
        if predicate(cover[i]):
            j = i
            while j < hi and predicate(cover[j]):
                j += 1
            if j - i >= min_len:
                out.append((i, j))
            i = j
        else:
            i += 1
    return out


def _lane_pack(shifts):
    """Greedy lane assignment so non-overlapping shifts share a row (by start)."""
    lanes = []
    for s in sorted(shifts, key=lambda x: x["sm"]):
        for ln in lanes:
            if ln[-1]["em"] <= s["sm"]:
                ln.append(s)
                break
        else:
            lanes.append([s])
    return lanes


def _pick_role(shifts, wanted):
    """The shift explicitly *assigned* ``wanted`` ('opener'/'closer'), or None.

    'both' counts for either. If two people are somehow assigned the same duty,
    the earliest in (opener) / latest out (closer) wins so the board still names
    exactly one — the conflict is reported separately by ``_role_notes``.
    """
    hits = [s for s in shifts if s.get("role") in (wanted, "both")]
    if not hits:
        return None
    return min(hits, key=lambda s: s["sm"]) if wanted == "opener" else max(hits, key=lambda s: s["em"])


def _role_notes(shifts, opener, closer):
    """Quiet, factual flags where the assigned duty doesn't match the hours.

    Assigning an opener who isn't first in is legitimate (someone may come in
    early to catch flights and still not own opening), so this never colours the
    day red — it just says so, once, in plain words.
    """
    notes = []
    if not shifts:
        return notes
    first_in = min(shifts, key=lambda s: s["sm"])
    last_out = max(shifts, key=lambda s: s["em"])
    if opener and opener["assigned"] and opener["uid"] != first_in["uid"]:
        notes.append(f'{_short_name(first_in["name"])} is in first ({_fmt_min(first_in["sm"])})')
    if closer and closer["assigned"] and closer["uid"] != last_out["uid"]:
        notes.append(f'{_short_name(last_out["name"])} is out last ({_fmt_min(last_out["em"])})')
    for wanted, label in (("opener", "Opener"), ("closer", "Closer")):
        if len([s for s in shifts if s.get("role") in (wanted, "both")]) > 1:
            notes.append(f"two people assigned {label}")
    return notes


def _resolve_duty(shifts, wanted):
    """The day's opener/closer as ``{uid, name, time, assigned}``, or None.

    An explicit assignment always wins; with none set the board falls back to
    deriving it from the hours (earliest in / latest out), which is how it read
    before roles existed — so an unconfigured roster looks exactly as it did.
    """
    if not shifts:
        return None
    s = _pick_role(shifts, wanted)
    assigned = s is not None
    if s is None:
        s = min(shifts, key=lambda x: x["sm"]) if wanted == "opener" else max(shifts, key=lambda x: x["em"])
    return {
        "uid": s["uid"], "name": s["name"], "short": _short_name(s["name"]),
        "time": _fmt_min(s["sm"]) if wanted == "opener" else _fmt_min(s["em"]),
        "assigned": assigned,
        "color": s.get("color"),
    }


def _build_day(shifts, *, key, name, full_name, sub, dow, is_today, is_past=False,
               oncall_windows=(), oncall=(), time_off=(), pending=()):
    """One rendered day, shared by the pattern view and every dated view.

    ``shifts`` are plain minute-of-day dicts (``uid/name/sm/em/role/color/...``)
    so this stays free of both timezone and ORM concerns — the callers resolve
    those. ``oncall_windows`` are (start_min, end_min) pairs that count as a
    covering body overnight; the pattern view passes the standing 12–6 AM window,
    a dated view passes whoever is actually marked on-call that night.

    ``oncall`` is who those windows belong to (name/short/window/color), which
    only a dated view can know — on-call is assigned per night, so the recurring
    pattern has nobody to name.
    """
    is_weekend = dow >= 5
    opener = _resolve_duty(shifts, "opener")
    closer = _resolve_duty(shifts, "closer")

    cover = [0] * 1440
    for lo, hi in oncall_windows:
        for a in range(max(0, lo), min(hi, 1440)):
            cover[a] += 1
    for s in shifts:
        for a in range(s["sm"], min(s["em"], 1440)):
            cover[a] += 1

    peak = max(cover[OPERATING[0]:], default=0) if shifts else 0
    gaps = _runs_min(cover, OPERATING[0], OPERATING[1], lambda c: c == 0, MIN_GAP_MIN)
    core_target = CORE_WEEKDAY_TARGET if dow < 5 else 1
    thin = _runs_min(cover, CORE[0], CORE[1], lambda c: 0 < c < core_target, MIN_GAP_MIN)

    if gaps:
        cue = {"level": "crit", "text": f"gap {_fmt_min(gaps[0][0])}–{_fmt_min(gaps[0][1])}"}
    elif thin:
        cue = {"level": "warn", "text": f"thin {_fmt_min(thin[0][0])}–{_fmt_min(thin[0][1])}"}
    elif not shifts:
        cue = {"level": "crit" if oncall_windows else "warn", "text": "no one on"}
    else:
        cue = {"level": "ok", "text": "covered"}

    lanes = []
    for ln in _lane_pack(shifts):
        bars = []
        for s in ln:
            width = _pct_min(min(s["em"], 1440) - s["sm"])
            role = s.get("role") or ""
            is_op = bool(opener and opener["uid"] == s["uid"])
            is_cl = bool(closer and closer["uid"] == s["uid"])
            bars.append({
                "uid": s["uid"],
                "name": s["name"],
                "short": _short_name(s["name"]),
                "left": _pct_min(s["sm"]),
                "width": width,
                "label": f'{_fmt_min(s["sm"])}–{_fmt_min(s["em"])}',
                "role": role,
                "role_label": STAFF_ROLE_LABELS.get(role, ""),
                "is_opener": is_op,
                "is_closer": is_cl,
                "assigned_role": bool(role),
                "color": s.get("color"),
                "overnight": s.get("overnight", False),
                "changed": s.get("changed", False),
                # Below ~13% of the day (≈3h) even a first name crowds the pill.
                "tight": width < 13,
            })
        lanes.append(bars)

    return {
        "key": key, "dow": dow, "name": name, "full_name": full_name, "sub": sub,
        "is_today": is_today, "is_past": is_past, "is_weekend": is_weekend,
        "on_count": len(shifts), "peak": peak, "cue": cue,
        "opener": opener, "closer": closer,
        "role_notes": _role_notes(shifts, opener, closer),
        "oncall": list(oncall),
        "oncall_names": [o["name"] for o in oncall],
        "oncall_band": {"left": _pct_min(ONCALL_WINDOW[0]), "width": _pct_min(ONCALL_WINDOW[1] - ONCALL_WINDOW[0])},
        "oncall_bands": [{"left": _pct_min(lo), "width": _pct_min(min(hi, 1440) - lo)} for lo, hi in oncall_windows],
        "lanes": lanes,
        "rail_gaps": [{"left": _pct_min(a), "width": _pct_min(b - a)} for a, b in gaps],
        "rail_thin": [{"left": _pct_min(a), "width": _pct_min(b - a)} for a, b in thin],
        "time_off": list(time_off),
        "pending": list(pending),
    }


def _row_cell(shift, *, day, time_off=None, pending=None):
    """One dispatcher × one day table cell, from an already-built shift dict."""
    if shift is None:
        base = {"is_working": False, "label": "—", "kind": "none"}
    else:
        base = {
            "is_working": True,
            "label": f'{_fmt_min(shift["sm"])}–{_fmt_min(shift["em"])}',
            "kind": "work",
            "overnight": shift.get("overnight", False),
            "role": shift.get("role") or "",
            "role_label": STAFF_ROLE_LABELS.get(shift.get("role") or "", ""),
            "changed": shift.get("changed", False),
            "is_opener": bool(day["opener"] and day["opener"]["uid"] == shift["uid"]),
            "is_closer": bool(day["closer"] and day["closer"]["uid"] == shift["uid"]),
        }
    if time_off:
        base.update(kind="timeoff", label=time_off.get("reason_label") or "Time off", time_off=time_off)
    elif pending:
        base["pending"] = pending
    base.update(is_today=day["is_today"], is_past=day["is_past"], is_weekend=day["is_weekend"], key=day["key"])
    return base


def weekly_pattern(roster, today_dow=None, colors=None):
    """The recurring weekly staffing pattern for the board.

    ``roster`` must be prefetched with ``weekly_schedule_rows``. Returns
    ``{weekdays: [...7], rows: [...per dispatcher]}`` — weekdays carries the
    coverage cue + timeline lanes; rows carries the table cells. Dateless by
    design: this is the standard week, not a specific one.
    """
    colors = colors or assign_colors(roster)
    by_dow = {d: [] for d in range(7)}          # weekday -> working shifts
    off_rows = defaultdict(set)                  # user.id -> weekdays explicitly off
    for u in roster:
        name = u.get_full_name() or u.username
        for r in u.weekly_schedule_rows.all():
            if r.is_working and r.start_time and r.end_time:
                sm, em = _min_of(r.start_time), _min_of(r.end_time)
                overnight = em <= sm
                if overnight:
                    em += 1440
                by_dow[r.day_of_week].append({
                    "uid": u.id, "name": name, "sm": sm, "em": em, "overnight": overnight,
                    "role": getattr(r, "role", "") or "", "color": colors.get(u.id),
                })
            else:
                off_rows[u.id].add(r.day_of_week)

    weekdays = [
        _build_day(
            by_dow[d], key=str(d), name=DAY_NAMES[d], full_name=DAY_NAMES_FULL[d], sub="",
            dow=d, is_today=(d == today_dow), oncall_windows=(ONCALL_WINDOW,),
        )
        for d in range(7)
    ]

    rows = []
    for u in roster:
        cells = []
        for d in range(7):
            mine = next((s for s in by_dow[d] if s["uid"] == u.id), None)
            cell = _row_cell(mine, day=weekdays[d])
            if mine is None and d in off_rows[u.id]:
                cell.update(label="Off", kind="off")
            cells.append(cell)
        worked = sum(1 for c in cells if c["is_working"])
        rows.append({
            "user": u, "name": u.get_full_name() or u.username, "color": colors.get(u.id),
            "cells": cells,
            "working_days": worked,
            "is_empty": worked == 0,
            "hours": _fmt_hours(sum(
                s["em"] - s["sm"] for d in range(7)
                for s in by_dow[d] if s["uid"] == u.id
            )),
        })

    return {"weekdays": weekdays, "rows": rows}


def _fmt_hours(minutes):
    """Total minutes -> '38h' / '37.5h' (one decimal only when it isn't whole)."""
    h = minutes / 60
    return f"{h:.0f}h" if abs(h - round(h)) < 0.05 else f"{h:.1f}h"


# ══════════════════════════════════════════════════════════════════════
# DATED views — the same board shape, but for real calendar dates.
#
# Identical output contract to ``weekly_pattern`` so the template renders one
# thing; the difference is that every day here is resolved through
# ``scheduling.resolve_staff_schedule``, so approved time off, custom hours and
# actual on-call assignments are all reflected. Any run of dates works: one day,
# a Mon–Sun week, or an arbitrary range.
# ══════════════════════════════════════════════════════════════════════

MAX_RANGE_DAYS = 31


def md(d):
    """date -> 'Aug 5'. Built without %-d, which Windows' strftime rejects."""
    return d.strftime("%b %d").replace(" 0", " ")


def _pending_requests(dates, roster):
    """``{(user_id, date): request}`` for time off awaiting a decision.

    Pending rows are invisible to the resolver on purpose — they must not move
    anyone's schedule — so the board reads them separately and draws them as a
    request laid over the day, not as an absence.
    """
    if not dates or not roster:
        return {}
    out = {}
    qs = (
        StaffScheduleOverride.objects
        .filter(status="pending", user__in=[u.id for u in roster], kind="off")
        .filter(Q(end_date__isnull=True, date__range=(dates[0], dates[-1]))
                | Q(end_date__gte=dates[0], date__lte=dates[-1]))
        .select_related("user")
    )
    for ov in qs:
        for d in dates:
            if ov.applies_on(d):
                out[(ov.user_id, d)] = {
                    "id": ov.id,
                    "name": ov.user.get_full_name() or ov.user.username,
                    "reason_label": ov.reason_label or "Time off",
                    "note": ov.note,
                    "range_display": ov.date_range_display,
                }
    return out


def dated_range(dates, roster, *, today=None, colors=None):
    """The board for an explicit list of dates (1–31), same shape as ``weekly_pattern``.

    ``roster`` must be prefetched with ``weekly_schedule_rows`` AND
    ``schedule_overrides``. One query each for on-call and pending requests.
    """
    if today is None:
        today = timezone.localdate()
    dates = list(dates)[:MAX_RANGE_DAYS]
    colors = colors or assign_colors(roster)

    oncall_by_date = defaultdict(list)
    oncall_by_user_date = {}
    if dates and roster:
        for oc in (StaffOnCall.objects
                   .filter(date__range=(dates[0], dates[-1]), user__in=[u.id for u in roster])
                   .select_related("user")):
            oncall_by_date[oc.date].append(oc)
            oncall_by_user_date[(oc.user_id, oc.date)] = oc
    pending_map = _pending_requests(dates, roster)

    day_shifts, day_off, day_offday, days = {}, {}, {}, []
    for d in dates:
        shifts, time_off, pending, plain_off = [], [], [], set()
        for u in roster:
            name = u.get_full_name() or u.username
            sched = scheduling.resolve_staff_schedule(u, d)
            if sched["is_working"] and sched["start_time"] and sched["end_time"]:
                sm, em = _min_of(sched["start_time"]), _min_of(sched["end_time"])
                overnight = em <= sm
                if overnight:
                    em += 1440
                shifts.append({
                    "uid": u.id, "name": name, "sm": sm, "em": em, "overnight": overnight,
                    "role": sched.get("role") or "", "color": colors.get(u.id),
                    "changed": sched.get("kind") == "custom_hours",
                })
            elif sched.get("time_off"):
                time_off.append({**sched["time_off"], "uid": u.id, "name": name,
                                 "short": _short_name(name), "color": colors.get(u.id)})
            elif sched["is_working"] is False:
                plain_off.add(u.id)          # a normal day off, not an absence
            req = pending_map.get((u.id, d))
            if req:
                pending.append({**req, "uid": u.id, "color": colors.get(u.id)})

        oncall_entries = oncall_by_date.get(d, [])
        windows, oncall = [], []
        for oc in oncall_entries:
            lo, hi = _min_of(oc.start_time), _min_of(oc.end_time)
            windows.append((lo, hi if hi > lo else hi + 1440))
            oc_name = oc.user.get_full_name() or oc.user.username
            oncall.append({
                "uid": oc.user_id,
                "name": oc_name,
                "short": _short_name(oc_name),
                "window": f"{_compact_time(oc.start_time)}–{_compact_time(oc.end_time)}",
                "color": colors.get(oc.user_id),
            })
        day = _build_day(
            shifts,
            key=d.strftime("%Y-%m-%d"),
            name=DAY_NAMES[d.weekday()],
            full_name=DAY_NAMES_FULL[d.weekday()],
            sub=md(d),
            dow=d.weekday(),
            is_today=(d == today),
            is_past=(d < today),
            oncall_windows=windows,
            oncall=oncall,
            time_off=time_off,
            pending=pending,
        )
        day["date"] = d
        day_shifts[d] = {s["uid"]: s for s in shifts}
        day_off[d] = {t["uid"]: t for t in time_off}
        day_offday[d] = plain_off
        days.append(day)

    rows = []
    for u in roster:
        cells, total = [], 0
        for day in days:
            d = day["date"]
            mine = day_shifts[d].get(u.id)
            cell = _row_cell(
                mine, day=day,
                time_off=day_off[d].get(u.id),
                pending=pending_map.get((u.id, d)),
            )
            if cell["kind"] == "none" and u.id in day_offday[d]:
                cell.update(label="Off", kind="off")
            if (u.id, d) in oncall_by_user_date:
                oc = oncall_by_user_date[(u.id, d)]
                cell["oncall_label"] = f"{_compact_time(oc.start_time)}–{_compact_time(oc.end_time)}"
            if mine:
                total += mine["em"] - mine["sm"]
            cells.append(cell)
        worked = sum(1 for c in cells if c["is_working"])
        rows.append({
            "user": u, "name": u.get_full_name() or u.username, "color": colors.get(u.id),
            "cells": cells,
            "working_days": worked,
            # Someone off sick all week still belongs on the board — they aren't
            # "unscheduled", they're absent — so time off keeps the row visible.
            "is_empty": worked == 0 and not any(c["kind"] == "timeoff" or c.get("pending") for c in cells),
            "hours": _fmt_hours(total),
        })

    return {"weekdays": days, "rows": rows}


# ══════════════════════════════════════════════════════════════════════
# Dispatcher-facing view — "my week & who I'm on with" (Phase 2).
#
# The calm flip side of weekly_pattern, scoped to ONE viewer. It answers
# "which days do I work, who am I on with, and where are the handoffs?" and
# deliberately carries NO coverage-risk fields — no gaps, no "thin", no
# targets, no headcount-vs-target, no red. Reassuring, never alarming.
# Same recurring StaffWeeklySchedule data (weekday pattern, Mon–Sun) as
# weekly_pattern; a dispatcher never sees the admin board's risk language.
# ══════════════════════════════════════════════════════════════════════


def _pattern_by_dow(roster):
    """weekday -> list of working shift dicts across ``roster`` (recurring pattern).

    Kept separate from ``weekly_pattern``'s own build so the dispatcher view
    can't destabilise the admin board. ``roster`` must be prefetched with
    ``weekly_schedule_rows``. Overnight shifts roll ``em`` past 1440.
    """
    by_dow = {d: [] for d in range(7)}
    for u in roster:
        name = u.get_full_name() or u.username
        for r in u.weekly_schedule_rows.all():
            if r.is_working and r.start_time and r.end_time:
                sm, em = _min_of(r.start_time), _min_of(r.end_time)
                overnight = em <= sm
                if overnight:
                    em += 1440
                by_dow[r.day_of_week].append({
                    "uid": u.id, "name": name, "sm": sm, "em": em, "overnight": overnight,
                    "start_label": _fmt_min_long(sm), "end_label": _fmt_min_long(em),
                })
    return by_dow


def _my_shifts(me):
    """My weekday -> shift dict (or None for an explicit off), plus has_schedule."""
    my_shifts, has_schedule = {}, False
    if me is not None:
        for r in me.weekly_schedule_rows.all():
            has_schedule = True
            if r.is_working and r.start_time and r.end_time:
                sm, em = _min_of(r.start_time), _min_of(r.end_time)
                overnight = em <= sm
                if overnight:
                    em += 1440
                my_shifts[r.day_of_week] = {
                    "sm": sm, "em": em, "overnight": overnight,
                    "start_label": _fmt_min_long(sm), "end_label": _fmt_min_long(em),
                    "label": f"{_fmt_min_long(sm)} – {_fmt_min_long(em)}",
                }
            else:
                my_shifts[r.day_of_week] = None
    return my_shifts, has_schedule


def _me_label(user):
    """The viewer's own display name — their real name, or "You" if none is set."""
    return user.get_full_name() or "You"


def _roster_on(shifts, user):
    """Everyone working a day, as display rows (viewer flagged ``is_me`` and shown
    by their own name), sorted by start with the day's opener/closer marked."""
    shifts = sorted(shifts, key=lambda s: (s["sm"], s["em"]))
    opener = min(shifts, key=lambda s: s["sm"]) if shifts else None
    closer = max(shifts, key=lambda s: s["em"]) if shifts else None
    me_label = _me_label(user)
    return [{
        "name": me_label if s["uid"] == user.id else s["name"],
        "window": f'{s["start_label"]} – {s["end_label"]}',
        "is_me": s["uid"] == user.id,
        "is_opener": opener is not None and s is opener,
        "is_closer": closer is not None and s is closer,
        "overnight": s["overnight"],
    } for s in shifts], opener, closer


def my_week(user, roster, today_dow=None):
    """One dispatcher's whole week (Mon–Sun, recurring pattern).

    Shows *every* day — including days the viewer is off — with who's working and
    their hours, so a dispatcher can review coverage across the week. The viewer's
    own row is flagged ``is_me``. This is the standard-pattern reference; the
    *actual* day, with one-off sick/off overrides applied, is ``day_view_actual``.
    ``roster`` must be prefetched with ``weekly_schedule_rows``. No risk fields.
    """
    me = next((u for u in roster if u.id == user.id), None)
    by_dow = _pattern_by_dow(roster)
    my_shifts, has_schedule = _my_shifts(me)

    days, working_days = [], 0
    for d in range(7):
        roster_on, opener, closer = _roster_on(by_dow[d], user)
        mine = my_shifts.get(d)
        is_working = mine is not None
        if is_working:
            working_days += 1
        days.append({
            "dow": d, "name": DAY_NAMES[d], "is_today": d == today_dow, "is_weekend": d >= 5,
            "is_working": is_working,
            "label": mine["label"] if is_working else ("Off" if d in my_shifts else "—"),
            "is_opener": bool(is_working and opener and opener["uid"] == user.id),
            "is_closer": bool(is_working and closer and closer["uid"] == user.id),
            "roster_on": roster_on,
            "on_count": len(by_dow[d]),
        })

    return {
        "days": days,
        "working_days": working_days,
        "has_schedule": has_schedule,
        "on_roster": me is not None,
        "me_name": (me.get_full_name() or me.username) if me else "",
    }


# ── The day's story (shared by pattern + actual-date views) ───────────

def _overlaps(mine_sm, mine_em, shifts, exclude_uid):
    """Coworkers in ``shifts`` whose shift overlaps [mine_sm, mine_em), sorted by
    start. Each carries sm/em so the story builder can thread the hand-offs."""
    withs = []
    for s in shifts:
        if s["uid"] == exclude_uid:
            continue
        if s["sm"] < mine_em and mine_sm < s["em"]:
            withs.append({"name": s["name"], "sm": s["sm"], "em": s["em"]})
    withs.sort(key=lambda w: (w["sm"], w["em"]))
    return withs


def _story_beats(mine, withs):
    """Chronological day-story beats from the viewer's shift + overlapping coworkers.

    ``mine`` = {sm, em, start_label, end_label}; ``withs`` from ``_overlaps``. Beats:
    who opens, each hand-off while I'm on (a coworker leaves → who's left carries
    on), and who I hand to when I leave (or I'm the closer). Calm, factual.
    """
    a_sm, a_em = mine["sm"], mine["em"]
    if not withs:
        return []
    parts = [{"name": "You", "you": True, "sm": a_sm, "em": a_em}]
    parts += [{"name": w["name"], "you": False, "sm": w["sm"], "em": w["em"]} for w in withs]

    beats = []
    first_in = min(parts, key=lambda p: (p["sm"], p["name"]))
    if first_in["you"]:
        beats.append({"kind": "open_me", "time": mine["start_label"]})
    else:
        beats.append({"kind": "open", "who": first_in["name"],
                      "time": _fmt_min_long(first_in["sm"]), "until": _fmt_min_long(first_in["em"])})
    for lv in sorted((p for p in parts if not p["you"] and p["em"] < a_em), key=lambda p: p["em"]):
        remaining = [("You" if p["you"] else p["name"]) for p in parts
                     if p is not lv and p["sm"] <= lv["em"] < p["em"]]
        beats.append({"kind": "leave", "who": lv["name"], "time": _fmt_min_long(lv["em"]),
                      "remaining": remaining})
    staying = sorted((p for p in parts if not p["you"] and p["sm"] < a_em < p["em"]), key=lambda p: -p["em"])
    if staying:
        beats.append({"kind": "handoff", "time": mine["end_label"],
                      "to": [{"name": p["name"], "until": _fmt_min_long(p["em"])} for p in staying]})
    else:
        beats.append({"kind": "close_me", "time": mine["end_label"]})
    return beats


# ── The *actual* day — recurring pattern resolved against one-off overrides ──

def _resolved_shift(uid, name, sched):
    """A resolved-schedule dict → a shift dict (or None if off/none), long labels."""
    if not sched.get("is_working") or not sched.get("start_time") or not sched.get("end_time"):
        return None
    sm, em = _min_of(sched["start_time"]), _min_of(sched["end_time"])
    overnight = em <= sm
    if overnight:
        em += 1440
    return {"uid": uid, "name": name, "sm": sm, "em": em, "overnight": overnight,
            "start_label": _fmt_min_long(sm), "end_label": _fmt_min_long(em)}


def _pattern_working(user, dow):
    """True if the user's *recurring* pattern has them working ``dow`` (0–6)."""
    for r in user.weekly_schedule_rows.all():
        if r.day_of_week == dow:
            return bool(r.is_working and r.start_time and r.end_time)
    return False


def day_view_actual(user, roster, target_date, today):
    """The *actual* day for ``target_date``: each schedule resolved against one-off
    overrides (sick/off, custom hours), so the timeline, story, and 'off today'
    notes reflect what's really happening — unlike the recurring pattern.

    ``roster`` must be prefetched with ``weekly_schedule_rows`` AND
    ``schedule_overrides``. Returns the dict the day-view panel renders.
    """
    dow = target_date.weekday()
    shifts, exceptions = [], []
    for u in roster:
        name = u.get_full_name() or u.username
        sched = scheduling.resolve_staff_schedule(u, target_date)
        sh = _resolved_shift(u.id, name, sched)
        if sh:
            shifts.append(sh)
        # Surface anyone whose day differs from their usual pattern (calm, factual).
        if sched.get("has_exception") and u.id != user.id:
            if not sched.get("is_working") and _pattern_working(u, dow):
                exceptions.append({"name": name, "kind": "off", "label": ""})
            elif sched.get("is_working") and sched.get("kind") == "custom_hours" and sh:
                exceptions.append({"name": name, "kind": "custom",
                                   "label": f'{sh["start_label"]} – {sh["end_label"]}'})

    roster_on, opener, closer = _roster_on(shifts, user)
    timeline = _day_timeline(user, shifts)

    mine = next((s for s in shifts if s["uid"] == user.id), None)
    beats, label = [], None
    if mine:
        label = f'{mine["start_label"]} – {mine["end_label"]}'
        beats = _story_beats(mine, _overlaps(mine["sm"], mine["em"], shifts, user.id))

    return {
        "dow": dow, "name": DAY_NAMES[dow], "is_today": target_date == today,
        "is_working": mine is not None, "label": label,
        "is_opener": bool(mine and opener and opener["uid"] == user.id),
        "is_closer": bool(mine and closer and closer["uid"] == user.id),
        "beats": beats, "timeline": timeline, "roster_on": roster_on,
        "on_count": len(shifts), "exceptions": exceptions,
    }


def my_week_actual(user, roster, monday, today):
    """One ``day_view_actual`` for each date of the week starting ``monday`` (Mon–Sun)."""
    return [day_view_actual(user, roster, monday + timedelta(days=i), today) for i in range(7)]


def _day_timeline(user, shifts):
    """Single-day timeline: one row per person (Gantt style, no lane-packing),
    sorted by start. Each bar carries geometry plus where to put its hours label
    so it never clips — inside a wide bar, otherwise just outside the near end."""
    shifts = sorted(shifts, key=lambda s: (s["sm"], s["em"]))
    opener = min(shifts, key=lambda s: s["sm"]) if shifts else None
    closer = max(shifts, key=lambda s: s["em"]) if shifts else None
    me_label = _me_label(user)

    bars = []
    for s in shifts:
        left = _pct_min(s["sm"])
        width = _pct_min(min(s["em"], 1440) - s["sm"])
        end = round(left + width, 3)
        # Name always sits inside the pill; the time joins it inside when the pill
        # is wide enough, otherwise it floats just outside the near end (no clip).
        if width >= 22:
            time_side = "inside"
        elif end <= 72:
            time_side = "right"
        else:
            time_side = "left"
        bars.append({
            "name": me_label if s["uid"] == user.id else s["name"],
            "is_me": s["uid"] == user.id,
            "left": left, "width": width, "end": end,
            "hours": f'{s["start_label"]} – {s["end_label"]}',
            "is_opener": bool(opener and s is opener),
            "is_closer": bool(closer and s is closer),
            "overnight": s["overnight"],
            "time_side": time_side,
        })

    return {
        "bars": bars,
        "on_count": len(shifts),
        "i_am_working": any(s["uid"] == user.id for s in shifts),
        "oncall_band": {"left": _pct_min(ONCALL_WINDOW[0]),
                        "width": _pct_min(ONCALL_WINDOW[1] - ONCALL_WINDOW[0])},
    }


def my_today_timeline(user, roster, today_dow):
    """The single-day pattern timeline for ``today_dow`` (thin wrapper on
    ``_day_timeline``). The live view uses ``day_view_actual`` instead, which also
    applies one-off overrides; this remains for the pattern-only case."""
    return _day_timeline(user, _pattern_by_dow(roster)[today_dow])
