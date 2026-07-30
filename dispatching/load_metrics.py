"""Chauffeur load metrics — the single source of these numbers.

Both the dispatcher view (`/dispatching/chauffeur-load/`) and the admin KPI view
(`/dispatching/chauffeur-kpis/`) read from here. If the two pages ever disagree it is a
bug, so no view is allowed to compute any of this itself.

Design notes that are easy to get wrong (see docs/chauffeur-load-views.md and the
methodology appendix of SOPS/chauffeur-load-metrics.md, SOP-003):

* Availability comes ONLY from ``drivers.availability.resolve_effective_availability``.
  Never re-derive it — same rule as turnaround labels reusing the feasibility engine.

* ``employment_type`` decides what an available day MEANS. For a full-timer it is a
  commitment, so an available day with no trips (an "idle day") is a real finding. For a
  part-timer it is an offer, so an idle day is normal. Mixing the two cohorts in one
  ranking makes part-timers look starved, so comparisons happen WITHIN cohort — that rule
  lives in dispatching/load_insights.py, which consumes these rows.

* Everything a row reports is COUNTED from records — days, trips, dates. The estimated
  and money figures this module used to carry (utilisation %, share of work, revenue,
  pay, margin) were removed 2026-07-29: money belongs to the future Driver economics
  page, and a metric that needs a footnote doesn't ship. Rationale: SOP-003.
"""

from __future__ import annotations

import statistics
from datetime import timedelta

from django.db.models import Count
from django.db.models.functions import Coalesce

# A flexible/open day has no window to measure, so it is worth an assumed number of
# hours. Founder decision 2026-07-29: 12. Used only when the driver has no explicit
# max_hours. Nothing rendered on the load pages depends on this any more — it is kept
# (with available_hours_for) for the future Driver economics page, which will need an
# hours denominator again.
FLEX_DAY_HOURS = 12.0

#: Leg statuses that count as work actually performed.
WORKED_LEG_STATUSES = ("completed",)

EMPLOYMENT_LABELS = {
    "full_time": "Full time",
    "part_time": "Part time",
    "": "Unlabelled",
}

#: Avatar palette, keyed by driver id modulo length. Lives here (not in page JS) so the
#: server-rendered exceptions list and the client-rendered roster use the same colour.
AVATAR_COLORS = (
    "#C9A227", "#9B7BC4", "#7BAEC4", "#C47B95", "#E89B5C", "#5CB8E8", "#7BC49B",
    "#E8C95C", "#B85CE8", "#5CE89B", "#E85C95", "#5CE8E0", "#E8855C", "#9BE85C",
    "#5C95E8", "#E8A85C",
)


def avatar_color(driver_id) -> str:
    return AVATAR_COLORS[driver_id % len(AVATAR_COLORS)]


# ──────────────────────────────────────────────────────────────────────────────
# availability
# ──────────────────────────────────────────────────────────────────────────────

def available_hours_for(eff) -> float:
    """Hours a driver was available on one day, from a resolved availability dict.

    Currently unused by the load pages (they report counted days only) but kept, tested,
    for the Driver economics page: this is the one honest way to turn availability into
    an hours denominator, and the partial-day narrowing below is easy to get wrong.

    A fixed window is measured. An open/flex day is an estimate (max_hours, else
    FLEX_DAY_HOURS). A partial-day exception with explicit times narrows the window —
    the resolver leaves base start/end alone when the day was already available, so
    without this the denominator would ignore "available until 2pm".
    """
    if not eff.get("is_available"):
        return 0.0

    start, end = eff.get("start_hour") or 0, eff.get("end_hour") or 0

    ex_type = eff.get("exception_type")
    ex_start, ex_end = eff.get("exception_start_time"), eff.get("exception_end_time")
    if ex_type == "available_until" and ex_end:
        end = min(end, ex_end.hour) if end > start else ex_end.hour
    elif ex_type == "available_after" and ex_start:
        start = max(start, ex_start.hour) if end > start else ex_start.hour
    elif ex_type == "available_window" and ex_start and ex_end:
        start, end = ex_start.hour, ex_end.hour
    elif ex_type == "unavailable_window" and ex_start and ex_end:
        # Blocked slice out of the middle of the day.
        span = (end - start) if end > start else (24 - start) + end
        blocked = max(0, min(end, ex_end.hour) - max(start, ex_start.hour))
        return float(max(0, span - blocked))

    is_open_flex = (eff.get("shift_type") == "full_day" and eff.get("flexible"))
    if is_open_flex and ex_type not in ("available_until", "available_after", "available_window"):
        max_hours = eff.get("max_hours")
        return float(max_hours) if max_hours else FLEX_DAY_HOURS

    if end > start:
        return float(end - start)
    if end < start:
        return float((24 - start) + end)      # wraps midnight
    return 0.0


# ──────────────────────────────────────────────────────────────────────────────
# day strip
# ──────────────────────────────────────────────────────────────────────────────

def build_day_cells(driver, today, worked_by_date, *, back=21, forward=7):
    """Per-day cells for the strip: `back` days of history, then `forward` upcoming.

    ``worked_by_date`` maps date -> trip count for THIS driver.
    Cell types: worked / spare / offday / timeoff. A future day can only be spare,
    offday or timeoff — nothing has happened yet, and "spare" reads correctly there as
    capacity not yet used.
    """
    from drivers.availability import resolve_effective_availability

    cells = []
    for offset in range(-back, forward):
        d = today + timedelta(days=offset)
        eff = resolve_effective_availability(driver, d)
        legs = worked_by_date.get(d, 0)

        if eff.get("is_available") and legs:
            kind = "worked"
        elif not eff.get("is_available"):
            kind = "timeoff" if eff.get("exception_reason") in ("vacation", "sick") else "offday"
        else:
            kind = "spare"

        cells.append({
            "date": d,
            "kind": kind,
            "legs": legs,
            "is_future": offset >= 0,
            "is_today": offset == 0,
        })
    return cells


# ──────────────────────────────────────────────────────────────────────────────
# rows
# ──────────────────────────────────────────────────────────────────────────────

def build_load_rows(start, end, *, today=None, drivers=None, lite=False):
    """One row per active in-house driver for the window [start, end] inclusive.

    ``lite=True`` skips the presentation extras — day cells, vehicle mix, next time
    off — and is used for the prior comparison window, where only the counted numbers
    matter. Lite rows must not be passed to serialize_rows.

    Affiliates are excluded by design — we don't own their hours, so idle days have no
    honest meaning for them. Affiliate reporting is separate work.
    """
    from django.utils import timezone
    from drivers.availability import resolve_effective_availability
    from drivers.models import Driver
    from reservations.models import Leg

    today = today or timezone.localdate()
    window_days = (end - start).days + 1

    if drivers is None:
        drivers = Driver.objects.filter(driver_type="inhouse", is_active=True)
    drivers = (drivers.select_related("profile")
               .prefetch_related("weekly_schedule", "date_overrides", "preferred_vehicles")
               .order_by("profile__first_name", "profile__last_name", "profile__username"))
    drivers = list(drivers)
    if not drivers:
        return []

    # ── one grouped aggregate for all worked legs, not a query per driver ──
    per_driver_day = {}
    agg = (
        Leg.objects
        .filter(driver__in=drivers, pickup_date__gte=start, pickup_date__lte=end,
                status__in=WORKED_LEG_STATUSES)
        .exclude(reservation__status="cancelled")
        .values("driver_id", "pickup_date")
        .annotate(legs=Count("id"))
    )
    for r in agg:
        per_driver_day.setdefault(r["driver_id"], {})[r["pickup_date"]] = r["legs"]

    strip_by_driver = {}
    class_mix = {}
    car_counts = {}
    if not lite:
        # Strip needs recent days that may sit outside the metric window.
        strip_start = today - timedelta(days=21)
        strip_agg = (
            Leg.objects
            .filter(driver__in=drivers, pickup_date__gte=min(strip_start, start),
                    pickup_date__lte=max(today, end), status__in=WORKED_LEG_STATUSES)
            .exclude(reservation__status="cancelled")
            .values("driver_id", "pickup_date").annotate(legs=Count("id"))
        )
        for r in strip_agg:
            strip_by_driver.setdefault(r["driver_id"], {})[r["pickup_date"]] = r["legs"]

        # ── vehicle: class mix from legs + the physical car ──
        # Leg.vehicle is only an OVERRIDE and is set on well under 1% of legs; the
        # reservation-level vehicle is the real source. Reading Leg.vehicle alone renders
        # "—" for almost every driver, so coalesce leg -> reservation.
        for r in (Leg.objects
                  .filter(driver__in=drivers, pickup_date__gte=start, pickup_date__lte=end,
                          status__in=WORKED_LEG_STATUSES)
                  .annotate(vclass=Coalesce("vehicle__vehicle_type",
                                            "reservation__vehicle__vehicle_type"))
                  .values("driver_id", "vclass")
                  .annotate(n=Count("id")).order_by("-n")):
            class_mix.setdefault(r["driver_id"], []).append((r["vclass"], r["n"]))

        from drivers.models import DriverVehicleAssignment
        for a in (DriverVehicleAssignment.objects
                  .filter(driver__in=drivers, date__gte=start, date__lte=end)
                  .values("driver_id", "vehicle__vehicle_number").annotate(n=Count("id"))):
            car_counts.setdefault(a["driver_id"], []).append((a["vehicle__vehicle_number"], a["n"]))

    dates = [start + timedelta(days=i) for i in range(window_days)]
    rows = []

    for d in drivers:
        days = per_driver_day.get(d.id, {})
        legs = sum(days.values())
        worked_days = len(days)

        avail_days = 0
        for dt in dates:
            eff = resolve_effective_availability(d, dt)
            if eff.get("is_available"):
                avail_days += 1

        emp = d.employment_type or ""
        row = {
            "driver": d,
            "id": d.id,
            "name": _display_name(d),
            "initials": _initials(d),
            "color": avatar_color(d.id),
            "employment_type": emp,
            "employment_label": EMPLOYMENT_LABELS.get(emp, "Unlabelled"),
            "is_full_time": emp == "full_time",
            "legs": legs,
            "worked_days": worked_days,
            "avail_days": avail_days,
            # Absolute, counted, threshold-free: days marked available with no trips.
            "idle_days": max(0, avail_days - worked_days),
            "per_worked_day": (legs / worked_days) if worked_days else 0.0,
            "per_available_day": (legs / avail_days) if avail_days else 0.0,
            "per_week": legs / (window_days / 7) if window_days else 0.0,
            "per_month": legs / (window_days / 30) if window_days else 0.0,
            # For the consecutive-days rule in load_insights. Not serialized.
            "worked_dates": sorted(days),
        }

        if not lite:
            row["vehicle_classes"] = _fmt_class_mix(class_mix.get(d.id, []))
            row["vehicle_car"] = _fmt_car(d, car_counts.get(d.id, []))
            row["next_time_off"] = _next_time_off(d, today)
            row["cells"] = build_day_cells(d, today, strip_by_driver.get(d.id, {}))

        rows.append(row)

    return rows


def build_fleet_summary(rows, *, window_days=None):
    """Fleet totals plus the distribution stats the page needs.

    Idle totals are reported PER COHORT, never as one fleet number. A combined
    "all idle days" figure adds full-time idle days (a real finding) to part-time idle
    days (normal by definition) and the sum means nothing.
    """
    if not rows:
        return {"drivers": 0, "cohorts": {}}

    def _med(vals):
        vals = [v for v in vals if v is not None]
        return statistics.median(vals) if vals else None

    legs = sum(r["legs"] for r in rows)
    summary = {
        "drivers": len(rows),
        "legs": legs,
        "worked_days": sum(r["worked_days"] for r in rows),
        "avail_days": sum(r["avail_days"] for r in rows),
        "median_per_worked_day": _med([r["per_worked_day"] for r in rows if r["worked_days"]]),
        "unlabelled": sum(1 for r in rows if not r["employment_type"]),
        "full_time": sum(1 for r in rows if r["employment_type"] == "full_time"),
        "part_time": sum(1 for r in rows if r["employment_type"] == "part_time"),
        # Concrete and actionable: available in the window, yet drove nothing at all.
        "zero_work_drivers": sorted(
            r["name"] for r in rows if r["avail_days"] and not r["legs"]
        ),
    }

    cohorts = {}
    for key in ("full_time", "part_time", ""):
        group = [r for r in rows if r["employment_type"] == key]
        if not group:
            continue
        idle = sum(r["idle_days"] for r in group)
        avail = sum(r["avail_days"] for r in group)
        cohorts[key] = {
            "n": len(group),
            "idle_days": idle,
            "avail_days": avail,
            "worked_days": sum(r["worked_days"] for r in group),
            "legs": sum(r["legs"] for r in group),
            # Share of this cohort's available days that ended with no trips.
            "idle_share": (idle / avail) if avail else None,
        }
    summary["cohorts"] = cohorts

    ft = cohorts.get("full_time")
    summary["ft_idle_days"] = ft["idle_days"] if ft else 0
    summary["ft_avail_days"] = ft["avail_days"] if ft else 0
    summary["ft_idle_share"] = ft["idle_share"] if ft else None
    return summary


def serialize_rows(rows, *, flagged_ids=None):
    """JSON-safe rows for the page's client-side table.

    Drops the Driver instance and converts dates. ``flagged_ids`` marks drivers on the
    exceptions list so the roster can emphasise their idle number — the flag is computed
    server-side and only ever passed on the KPI page, so the dispatcher payload carries
    no judgement, only counts.
    """
    flagged_ids = flagged_ids or set()
    out = []
    for r in rows:
        out.append({
            "id": r["id"],
            "name": r["name"],
            "initials": r["initials"],
            "color": r["color"],
            "employment_type": r["employment_type"],
            "employment_label": r["employment_label"],
            "is_full_time": r["is_full_time"],
            "legs": r["legs"],
            "worked_days": r["worked_days"],
            "avail_days": r["avail_days"],
            "idle_days": r["idle_days"],
            "per_worked_day": round(r["per_worked_day"], 3),
            "per_available_day": round(r["per_available_day"], 3),
            "per_week": round(r["per_week"], 3),
            "per_month": round(r["per_month"], 3),
            "flagged": r["id"] in flagged_ids,
            "vehicle_classes": r["vehicle_classes"],
            "vehicle_car": r["vehicle_car"],
            "next_time_off": r["next_time_off"],
            "cells": [
                {"kind": c["kind"], "legs": c["legs"],
                 "is_future": c["is_future"], "is_today": c["is_today"],
                 "label": c["date"].strftime("%a %b ") + str(c["date"].day)}
                for c in r["cells"]
            ],
        })
    return out


# ──────────────────────────────────────────────────────────────────────────────
# helpers
# ──────────────────────────────────────────────────────────────────────────────

def _display_name(d):
    first = (d.profile.first_name or "").strip()
    last = (d.profile.last_name or "").strip()
    return f"{first} {last}".strip() or d.profile.username


def _initials(d):
    first = (d.profile.first_name or "").strip()
    last = (d.profile.last_name or "").strip()
    return ((first[:1] + last[:1]) or _display_name(d)[:2]).upper()


def _fmt_class_mix(pairs):
    """'Towncar' or 'Towncar · SUV' — only classes that are a real share of the work."""
    if not pairs:
        return "—"
    total = sum(n for _, n in pairs)
    kept = [c for c, n in pairs if c and n / total >= 0.15][:3]
    return " · ".join(kept) if kept else (pairs[0][0] or "—")


def _fmt_car(driver, counts):
    """Their regular car if recorded, else the most-assigned car, else 'rotates'."""
    preferred = list(driver.preferred_vehicles.all())
    if preferred:
        label = preferred[0].vehicle_number
        return f"{label} (+{len(preferred) - 1} more)" if len(preferred) > 1 else label

    named = [(c, n) for c, n in counts if c]
    if not named:
        return "—"
    named.sort(key=lambda x: -x[1])
    top, top_n = named[0]
    total = sum(n for _, n in named)
    if total and top_n / total < 0.5:
        return "rotates"
    return f"{top} (+{len(named) - 1} more)" if len(named) > 1 else top


def _next_time_off(driver, today):
    """Soonest approved upcoming time off, as a short label."""
    upcoming = [
        o for o in driver.date_overrides.all()
        if o.status == "approved" and o.exception_type == "off"
        and (o.end_date or o.date) >= today
    ]
    if not upcoming:
        return None
    o = min(upcoming, key=lambda x: x.date)
    # No %-d / %-I on Windows — build the day number explicitly.
    start_label = f"{o.date:%b} {o.date.day}"
    if o.end_date and o.end_date != o.date:
        if o.end_date.month == o.date.month:
            return f"{start_label}–{o.end_date.day}"
        return f"{start_label}–{o.end_date:%b} {o.end_date.day}"
    return start_label
