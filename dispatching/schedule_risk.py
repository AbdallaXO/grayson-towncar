"""
Schedule risk engine.

Pure functions that compute per-day coverage risk, exception impact, and
action items for the in-house schedule page. No DB access — caller hands
in already-resolved data structures, engine returns plain dicts that the
template can render directly.

Concepts
--------
Each day is evaluated against a flat ``target`` head-count. From there:

    scheduled_actual         = drivers working that day after applying
                               approved overrides (PTO/leave hides them,
                               flexible overrides bring an off-day driver
                               back, partial-day overrides keep them in)
    scheduled_after_pending  = same, but assuming every pending request
                               were approved — used to surface "if you
                               approve this you'll be understaffed"
    flexible_count           = scheduled drivers whose shift is flex/full_day

The five risk states:

    covered      delta >= +2
    tight        delta in (0, +1)
    understaffed delta == -1
    critical     delta <= -2  or  an essential shift has 0 coverage
    no_data      day has no driver data yet (placeholder, not used today)

Survivability: ``scheduled_actual - 1 >= target``. A day can meet target
yet still be one call-out away from going under.

The action-item generator surfaces, in priority order: critical days,
pending requests that would drop a day below target, understaffed days,
days with no flexible backup, days at-target with no survivability.

Anything in this module is safe to import from views and templates without
side effects.
"""
from __future__ import annotations

from datetime import date as _date_type, timedelta
from typing import Any

# Risk thresholds. See module docstring.
COVERAGE_TARGET_DEFAULT = 14
COVERED_DELTA = 2     # +2 or better
TIGHT_MAX = 1         # delta in 0..1 = tight
UNDERSTAFFED_MIN = -1 # delta == -1 = understaffed
# Below -1 = critical.

# Shifts that should not be empty on a normal day. If any of these are
# zero on a working day we escalate to "critical" even if total count
# meets target.
ESSENTIAL_SHIFTS = ("morning", "evening")

# Buckets whose drivers are available across the whole day and so cover
# every essential shift even though they aren't tagged as "morning" or
# "evening". A flex/full-day driver answers any call; a split driver
# works both ends of the day. Without this, a roster of mostly-flex
# drivers reads as "no morning coverage" every day, which is wrong.
SHIFTS_THAT_COVER_ESSENTIALS = ("flex", "split", "set")

# Display order for shift coverage rollups.
SHIFT_BUCKETS = ("morning", "midday", "evening", "night", "split", "flex", "set")

DAY_NAMES_SHORT = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
DAY_NAMES_FULL = (
    "Monday", "Tuesday", "Wednesday", "Thursday",
    "Friday", "Saturday", "Sunday",
)


# ── classification helpers ────────────────────────────────────────────

def classify_risk(delta: int, shift_gaps: list[str]) -> str:
    """Map a coverage delta + essential-shift gaps to a risk label."""
    if any(g in ESSENTIAL_SHIFTS for g in shift_gaps) or delta <= -2:
        return "critical"
    if delta == -1:
        return "understaffed"
    if delta <= TIGHT_MAX:
        return "tight"
    return "covered"


def survivability_ok(scheduled: int, target: int) -> bool:
    """Can the day survive one driver calling out?"""
    return (scheduled - 1) >= target


# ── per-driver effective-day resolution ───────────────────────────────

# Override statuses the engine treats as effective. Anything else (denied,
# cancelled) is ignored. "pending" rows are tracked separately for the
# what-if numbers but do NOT shift the "actual" coverage.
APPROVED_STATUSES = ("approved",)
PENDING_STATUSES = ("pending",)


def _override_takes_driver_off(override: dict) -> bool:
    """True if the override fully removes the driver from that day's pool.

    "off" = day_off / vacation / sick — full day out.
    "available_until" / "available_after" / "available_window" /
    "unavailable_window" all keep the driver partially in (we count them
    as scheduled for the head-count, the shift_gap pass surfaces the
    partial window).
    "flexible" / "note_only" never remove the driver.
    """
    return override.get("exception_type") == "off"


def _override_brings_driver_in(override: dict) -> bool:
    """True if the override forces an otherwise-off driver to work."""
    return override.get("exception_type") == "flexible"


def resolve_day_state(
    driver_day: dict,
    overrides_on_date: list[dict],
) -> dict:
    """Combine a driver's weekly default for a day_idx with any overrides
    matching that calendar date. Returns a dict the engine can roll up.

    driver_day comes from the existing view loop and already includes
    ``is_off``, ``design_bucket``, ``flexible``, ``shift_type``.
    overrides_on_date is the subset of that driver's overrides that
    cover the date in question.
    """
    base_off = driver_day["is_off"]
    bucket = driver_day["design_bucket"]
    flexible = bool(driver_day.get("flexible"))

    approved = [o for o in overrides_on_date if o.get("status") in APPROVED_STATUSES]
    pending = [o for o in overrides_on_date if o.get("status") in PENDING_STATUSES]

    # Apply approved overrides on top of the weekly default.
    effective_off = base_off
    effective_pending_off = base_off
    for o in approved:
        if _override_takes_driver_off(o):
            effective_off = True
            effective_pending_off = True
        elif _override_brings_driver_in(o):
            effective_off = False
            effective_pending_off = False
    for o in pending:
        # Pending requests don't shift "actual" but do shift the what-if.
        if _override_takes_driver_off(o):
            effective_pending_off = True

    return {
        "off": effective_off,
        "off_after_pending": effective_pending_off,
        "bucket": "off" if effective_off else bucket,
        "flexible": flexible and not effective_off,
        "has_partial_window": any(
            o.get("exception_type") in (
                "available_until", "available_after",
                "available_window", "unavailable_window",
            )
            for o in approved
        ),
        "approved_overrides": approved,
        "pending_overrides": pending,
    }


# ── day-level rollup ──────────────────────────────────────────────────

def compute_day_risk(
    day_idx: int,
    date_obj: _date_type,
    driver_states: list[dict],
    target: int,
) -> dict:
    """Roll up one day across all drivers into a DayRisk dict."""
    scheduled = 0
    scheduled_after_pending = 0
    off = 0
    pending_off = 0
    flexible = 0
    shift_counts = {k: 0 for k in SHIFT_BUCKETS}

    for st in driver_states:
        if not st["off"]:
            scheduled += 1
            shift_counts[st["bucket"]] = shift_counts.get(st["bucket"], 0) + 1
            if st["flexible"]:
                flexible += 1
        else:
            off += 1
        if not st["off_after_pending"]:
            scheduled_after_pending += 1
        else:
            pending_off += 1

    delta = scheduled - target
    delta_after_pending = scheduled_after_pending - target

    # Shift gaps: essential shifts with 0 coverage, where "coverage"
    # includes flex/split/set drivers (they're available across the day).
    flex_covering = sum(
        shift_counts.get(b, 0) for b in SHIFTS_THAT_COVER_ESSENTIALS
    )
    shift_gaps = [
        s for s in ESSENTIAL_SHIFTS
        if shift_counts.get(s, 0) + flex_covering == 0
    ]

    risk_level = classify_risk(delta, shift_gaps)
    risk_after_pending = classify_risk(delta_after_pending, shift_gaps)
    survives = survivability_ok(scheduled, target)

    gaps: list[str] = []
    recs: list[str] = []
    if risk_level == "critical":
        if delta <= -2:
            gaps.append(f"{abs(delta)} drivers below target")
            recs.append("Critical: pull a flexible backup or call a contractor.")
        for s in shift_gaps:
            gaps.append(f"No {s} coverage")
            recs.append(f"Move one flexible driver into {s} coverage.")
    elif risk_level == "understaffed":
        gaps.append("1 driver below target")
        recs.append("Add one driver before approving any time-off for this day.")
    elif risk_level == "tight":
        if not survives:
            gaps.append("No buffer — one call-out drops below target")
            recs.append("Identify a backup driver in case of a call-out.")
        if flexible == 0:
            gaps.append("No flexible backups available")
            recs.append("Confirm one flexible driver as on-call.")
        if not gaps:
            recs.append("Coverage holds but margin is thin.")
    elif risk_level == "covered":
        if pending_off and risk_after_pending in ("tight", "understaffed", "critical"):
            recs.append(
                f"Pending request(s) would drop this day to {risk_after_pending}."
            )
        else:
            recs.append("Coverage is fine — no action needed.")

    return {
        "day_idx": day_idx,
        "day_name": DAY_NAMES_SHORT[day_idx],
        "day_name_full": DAY_NAMES_FULL[day_idx],
        "date": date_obj,
        "date_short": f"{date_obj.strftime('%b')} {date_obj.day}",
        "target": target,
        "scheduled_count": scheduled,
        "scheduled_after_pending": scheduled_after_pending,
        "off_count": off,
        "pending_off_count": pending_off,
        "flexible_count": flexible,
        "shift_coverage": dict(shift_counts),
        "delta": delta,
        "delta_after_pending": delta_after_pending,
        "delta_label": f"+{delta}" if delta > 0 else str(delta),
        "risk_level": risk_level,
        "risk_after_pending": risk_after_pending,
        "survives_one_callout": survives,
        "survivability_label": (
            "Can survive 1 call-out" if survives
            else "Cannot survive 1 call-out"
        ),
        "shift_gaps": shift_gaps,
        "gaps": gaps,
        "recommended_actions": recs,
    }


# ── per-exception what-if ────────────────────────────────────────────

def compute_exception_impact(
    exception: dict,
    week_day_risks: list[dict],
    week_dates: list[_date_type],
    driver_states_by_date: dict,
    target: int,
) -> dict:
    """For one override, compute coverage impact across the days it touches.

    Returns dict with worst-day delta_before / delta_after and a short
    note like "Makes Thursday tight". Days outside the visible week are
    not analyzed (the page only renders the current week).
    """
    start = exception.get("_start") or exception.get("date")
    end = exception.get("_end") or exception.get("end_date") or start
    if isinstance(start, str):
        start = _date_type.fromisoformat(start)
    if isinstance(end, str):
        end = _date_type.fromisoformat(end)

    affected_days: list[dict] = []
    for i, d in enumerate(week_dates):
        if start <= d <= end:
            affected_days.append(week_day_risks[i])

    # The exception's own scheduled-after-pending number already reflects
    # its hypothetical approval, so we just compare current vs after-pending.
    is_pending = exception.get("status") in PENDING_STATUSES

    if not affected_days:
        # Keep the same key set as the normal return below so downstream
        # consumers (build_action_items, templates) never KeyError on an
        # out-of-week exception.
        return {
            "is_pending": is_pending,
            "impact_level": "no_issue",
            "impact_label": "Outside this week",
            "affected_day_idxs": [],
            "affected_day_names": [],
            "delta_before": None,
            "delta_after": None,
            "delta_before_label": None,
            "delta_after_label": None,
            "risk_before": None,
            "risk_after": None,
            "recommended_action": "",
            "note": "",
        }
    is_off_request = exception.get("exception_type") == "off"

    # Worst affected day = lowest delta_after_pending among affected.
    worst = min(affected_days, key=lambda d: d["delta_after_pending"])

    delta_before = worst["delta"]
    delta_after = worst["delta_after_pending"]
    risk_before = worst["risk_level"]
    risk_after = worst["risk_after_pending"]

    if not is_off_request:
        # Partial-day / flexible / note don't change head-count; report no
        # head-count impact but still surface the day(s) affected.
        impact_level = "no_issue"
        note = f"Affects {worst['day_name']}; no head-count impact."
        rec = "Review the time window with dispatch."
    elif risk_after == "critical":
        impact_level = "critical"
        note = (
            f"Would make {worst['day_name']} critical "
            f"(coverage {worst['scheduled_after_pending']}/{target})."
        )
        rec = "Do not approve without adding a backup driver."
    elif risk_after == "understaffed":
        impact_level = "understaffed"
        note = (
            f"Creates understaffing on {worst['day_name']} "
            f"({worst['scheduled_after_pending']}/{target})."
        )
        rec = "Add a backup driver before approving."
    elif risk_after == "tight" and risk_before == "covered":
        impact_level = "tight"
        note = (
            f"Drops {worst['day_name']} from covered to tight "
            f"(+{delta_before} → {worst['delta_after_pending']:+d})."
        )
        rec = "Approvable, but leaves no buffer for a call-out."
    else:
        impact_level = "no_issue"
        note = f"No coverage issue on {worst['day_name']}."
        rec = "Safe to approve."

    return {
        "is_pending": is_pending,
        "impact_level": impact_level,
        "impact_label": {
            "no_issue":     "No issue",
            "tight":        "Makes day tight",
            "understaffed": "Creates understaffing",
            "critical":     "Critical",
        }.get(impact_level, impact_level.title()),
        "affected_day_idxs": [d["day_idx"] for d in affected_days],
        "affected_day_names": [d["day_name"] for d in affected_days],
        "delta_before": delta_before,
        "delta_after": delta_after,
        "delta_before_label": f"+{delta_before}" if delta_before > 0 else str(delta_before),
        "delta_after_label": f"+{delta_after}" if delta_after > 0 else str(delta_after),
        "risk_before": risk_before,
        "risk_after": risk_after,
        "recommended_action": rec,
        "note": note,
    }


# ── action item + gap surfacing ───────────────────────────────────────

# Severity order so we can sort: crit first, then warn, then info.
_SEVERITY_ORDER = {"crit": 0, "warn": 1, "info": 2}


def build_action_items(
    day_risks: list[dict],
    exception_impacts: list[dict],
) -> list[dict]:
    """Generate the top "Action Needed" list. Critical days first, then
    pending requests that worsen a day, then understaffed days, then
    tight-with-no-buffer.
    """
    items: list[dict] = []

    # 1. Critical days.
    for d in day_risks:
        if d["risk_level"] == "critical":
            detail_bits = []
            if d["delta"] <= -2:
                detail_bits.append(
                    f"{d['scheduled_count']}/{d['target']} scheduled"
                )
            if d["shift_gaps"]:
                detail_bits.append("no " + "/".join(d["shift_gaps"]) + " coverage")
            items.append({
                "severity": "crit",
                "title": f"Critical: {d['day_name_full']} {d['date_short']}",
                "detail": (
                    "; ".join(detail_bits)
                    if detail_bits else f"{d['scheduled_count']}/{d['target']} scheduled"
                ),
                "day_idx": d["day_idx"],
            })

    # 2. Pending exceptions that would drop the worst day below target.
    for imp in exception_impacts:
        if not imp["is_pending"]:
            continue
        if imp["impact_level"] in ("understaffed", "critical"):
            items.append({
                "severity": "crit",
                "title": (
                    f"Pending: {imp['driver_name']} — "
                    f"{imp['exception_date_display']}"
                ),
                "detail": imp["note"],
                "day_idx": imp["affected_day_idxs"][0] if imp["affected_day_idxs"] else None,
                "exception_id": imp["exception_id"],
            })
        elif imp["impact_level"] == "tight":
            items.append({
                "severity": "warn",
                "title": (
                    f"Pending: {imp['driver_name']} — "
                    f"{imp['exception_date_display']}"
                ),
                "detail": imp["note"],
                "day_idx": imp["affected_day_idxs"][0] if imp["affected_day_idxs"] else None,
                "exception_id": imp["exception_id"],
            })

    # 3. Understaffed days (already-approved coverage gaps).
    for d in day_risks:
        if d["risk_level"] == "understaffed":
            items.append({
                "severity": "warn",
                "title": f"Understaffed: {d['day_name_full']} {d['date_short']}",
                "detail": f"{d['scheduled_count']}/{d['target']} scheduled",
                "day_idx": d["day_idx"],
            })

    # 4. Tight days that can't survive one call-out.
    for d in day_risks:
        if d["risk_level"] == "tight" and not d["survives_one_callout"]:
            items.append({
                "severity": "warn",
                "title": f"Tight: {d['day_name_full']} {d['date_short']}",
                "detail": (
                    f"{d['scheduled_count']}/{d['target']} scheduled — "
                    "one call-out drops below target"
                ),
                "day_idx": d["day_idx"],
            })

    # 5. Tight days with no flexible backup at all.
    for d in day_risks:
        if d["risk_level"] == "tight" and d["flexible_count"] == 0 and d["survives_one_callout"]:
            items.append({
                "severity": "info",
                "title": f"Watch: {d['day_name_full']} {d['date_short']}",
                "detail": "No flexible/backup drivers — confirm an on-call.",
                "day_idx": d["day_idx"],
            })

    # Stable sort by severity then day_idx so the list reads top-down.
    items.sort(key=lambda it: (_SEVERITY_ORDER.get(it["severity"], 99),
                                it.get("day_idx") if it.get("day_idx") is not None else 99))
    return items


def build_coverage_gaps(day_risks: list[dict]) -> list[dict]:
    """Compact list of every day that has any kind of gap — for the
    "Coverage Gaps" panel. Days with no gaps are excluded.
    """
    out: list[dict] = []
    for d in day_risks:
        bullets: list[str] = []
        if d["delta"] < 0:
            bullets.append(f"{abs(d['delta'])} below target ({d['scheduled_count']}/{d['target']})")
        if d["pending_off_count"] and d["delta_after_pending"] < 0:
            bullets.append(
                f"Pending requests would drop to {d['scheduled_after_pending']}/{d['target']}"
            )
        if d["flexible_count"] == 0 and d["risk_level"] != "covered":
            bullets.append("No flexible/backup drivers")
        for s in d["shift_gaps"]:
            bullets.append(f"No {s} coverage")
        if d["risk_level"] == "tight" and d["survives_one_callout"] is False:
            bullets.append("One call-out away from understaffed")

        if bullets:
            out.append({
                "day_idx": d["day_idx"],
                "day_name": d["day_name"],
                "day_name_full": d["day_name_full"],
                "date_short": d["date_short"],
                "risk_level": d["risk_level"],
                "bullets": bullets,
            })
    return out


# ── top-level entrypoint ──────────────────────────────────────────────

def compute_week_risk(
    driver_rows: list[dict],
    overrides_by_driver_date: dict[tuple[int, _date_type], list[dict]],
    week_dates: list[_date_type],
    target: int = COVERAGE_TARGET_DEFAULT,
) -> dict:
    """Top-level entry point.

    driver_rows           — the existing per-driver row dicts (need ``id`` and
                            ``days[0..6]`` with is_off/design_bucket/flexible).
    overrides_by_driver_date — (driver_id, date) → list of override dicts.
                            Each override dict must have status,
                            exception_type, and date/end_date or _start/_end.
    week_dates            — the seven dates in display order.
    target                — flat per-day target. Default 14.

    Returns ``{"days": [...], "action_items": [...], "coverage_gaps": [...],
              "exception_impacts_by_id": {...}}``.
    """
    # Per-day rollup.
    day_risks: list[dict] = []
    driver_states_by_date: dict[_date_type, list[dict]] = {}
    for i, d in enumerate(week_dates):
        states: list[dict] = []
        for row in driver_rows:
            driver_id = row["id"]
            driver_day = row["days"][i]
            overrides = overrides_by_driver_date.get((driver_id, d), [])
            states.append(resolve_day_state(driver_day, overrides))
        driver_states_by_date[d] = states
        day_risks.append(compute_day_risk(i, d, states, target))

    return {
        "days": day_risks,
        "driver_states_by_date": driver_states_by_date,
    }


def attach_exception_impacts(
    week_risk: dict,
    exceptions: list[dict],
    week_dates: list[_date_type],
    target: int = COVERAGE_TARGET_DEFAULT,
) -> list[dict]:
    """For each exception dict, compute and attach an ``impact`` key. Returns
    a flat list of ``{exception_id, driver_name, ..., **impact}`` records
    suitable for the action-item generator.
    """
    flat: list[dict] = []
    for ex in exceptions:
        impact = compute_exception_impact(
            ex,
            week_risk["days"],
            week_dates,
            week_risk["driver_states_by_date"],
            target,
        )
        ex["impact"] = impact
        flat.append({
            "exception_id": ex.get("id"),
            "driver_name": ex.get("driver_name", ""),
            "driver_id": ex.get("driver_id"),
            "exception_date_display": ex.get("date_display") or ex.get("date_short", ""),
            **impact,
        })
    return flat
