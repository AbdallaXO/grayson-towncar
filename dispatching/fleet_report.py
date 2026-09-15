"""
Fleet performance over a window, for management.

Everything is derived from rows the fleet job already produces by doing the
job — downtimes, service records, issues, fault episodes, daily mileage — so
the report is only as complete as the ledger, and it says so wherever a
figure rests on partial data. No number here is stored; re-run it and it
reflects whatever was corrected since.

The questions it answers, and where each answer comes from:

  How much of the fleet is normally operational?   downtime ledger vs unit-days
  Which vehicles are down most, and why?           downtime ledger, by category
  Which vehicles need the most maintenance?        service records + cost
  Are we ahead of preventative maintenance?        each PM record vs the one before
  Are we scheduling downtime around demand?        the verdict saved when planned,
                                                   plus the day's bookings vs the median
  Which vehicles are becoming unreliable?          issues + fault episodes + repairs
  Are some units racking up far more miles?        daily mileage, within a type
  What keeps repeating?                            issue titles and fault codes seen twice+
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import timedelta
from decimal import Decimal
from statistics import median

from django.db.models import Count, Q, Sum

from dispatching.fleet_capacity import type_label, unit_type

# Service types that are preventative (scheduled), as opposed to a repair.
PREVENTATIVE_TYPES = {"oil", "tires", "brakes", "transmission", "inspection"}
# Downtime categories that were CHOSEN, and so could have been placed on a
# quiet day. A breakdown lands where it lands.
PLANNABLE_CATEGORIES = {"maintenance", "tires", "inspection", "recall"}
# Repeat threshold for "what keeps repeating".
REPEAT_MIN = 2
# Mileage spread within a type worth calling out.
IMBALANCE_RATIO = Decimal("1.5")


def build_report(start, end, today):
    """All the numbers for ``[start, end]`` inclusive. See the module docstring."""
    from drivers.models import (
        FleetVehicle, VehicleDayReading, VehicleDowntime, VehicleFault, VehicleIssue,
        VehicleServiceRecord, VehicleServiceSchedule,
    )
    from reservations.models import Leg

    window_days = (end - start).days + 1
    end_exclusive = end + timedelta(days=1)
    units = list(FleetVehicle.objects.filter(is_active=True).select_related("vehicle_type")
                 .order_by("vehicle_number"))
    unit_ids = [u.id for u in units]
    per = {u.id: _blank_row(u, window_days) for u in units}

    # ── Downtime ─────────────────────────────────────────────────────────
    downtimes = list(
        VehicleDowntime.objects.filter(vehicle_id__in=unit_ids, starts_on__lt=end_exclusive)
        .filter(Q(ended_on__isnull=True) | Q(ended_on__gt=start))
        .select_related("vehicle")
        .order_by("starts_on")
    )
    by_category = Counter()
    placement = {"clear": 0, "tight": 0, "conflict": 0, "unchecked": 0,
                 "on_quiet_day": 0, "on_busy_day": 0, "total": 0}
    daily_legs = _daily_leg_counts(Leg, start, end)
    legs_median = median(daily_legs.values()) if daily_legs else None

    for d in downtimes:
        if d.ended_on is not None:
            effective_end = d.ended_on
        elif d.starts_on <= today:
            effective_end = min(today, end) + timedelta(days=1)
        else:
            continue  # planned for later — it hasn't cost a day yet
        lo, hi = max(d.starts_on, start), min(effective_end, end_exclusive)
        days = (hi - lo).days
        if days <= 0 and d.starts_on >= start and d.starts_on <= end:
            days = 1
        if days <= 0:
            continue
        row = per[d.vehicle_id]
        row["down_days"] += days
        row["down_episodes"] += 1
        row["down_by_category"][d.category] += days
        by_category[d.category] += days
        if d.category in PLANNABLE_CATEGORIES and start <= d.starts_on <= end:
            placement["total"] += 1
            placement[d.demand_verdict or "unchecked"] += 1
            if legs_median is not None:
                legs_that_day = daily_legs.get(d.starts_on, 0)
                if legs_that_day <= legs_median:
                    placement["on_quiet_day"] += 1
                else:
                    placement["on_busy_day"] += 1

    # ── Mileage ──────────────────────────────────────────────────────────
    for r in (VehicleDayReading.objects
              .filter(vehicle_id__in=unit_ids, date__gte=start, date__lte=end)
              .values("vehicle_id")
              .annotate(miles=Sum("miles_driven"),
                        known=Count("id", filter=Q(miles_driven__isnull=False)),
                        total=Count("id"))):
        row = per[r["vehicle_id"]]
        row["miles"] = r["miles"]
        row["known_days"] = r["known"]
        row["reading_days"] = r["total"]
        if r["miles"] is not None and r["known"]:
            row["miles_per_day"] = (Decimal(r["miles"]) / r["known"]).quantize(Decimal("0.1"))

    # ── Service ──────────────────────────────────────────────────────────
    records = list(
        VehicleServiceRecord.objects.filter(vehicle_id__in=unit_ids,
                                            performed_on__gte=start, performed_on__lte=end)
        .order_by("performed_on", "id")
    )
    schedules = {(s.vehicle_id, s.service_type): s
                 for s in VehicleServiceSchedule.objects.filter(vehicle_id__in=unit_ids)}
    pm = {"on_time": 0, "late": 0, "unknown": 0, "late_by_miles": []}
    prior_cache = {}
    for rec in records:
        row = per[rec.vehicle_id]
        row["service_events"] += 1
        if rec.cost:
            row["service_cost"] += rec.cost
        if rec.service_type == "repair":
            row["repairs"] += 1
        if rec.service_type in PREVENTATIVE_TYPES:
            row["pm_events"] += 1
            verdict, late_by = _pm_verdict(rec, schedules.get((rec.vehicle_id, rec.service_type)),
                                           VehicleServiceRecord, prior_cache)
            pm[verdict] += 1
            if verdict == "late":
                row["pm_late"] += 1
                if late_by is not None:
                    pm["late_by_miles"].append(late_by)
            elif verdict == "on_time":
                row["pm_on_time"] += 1

    # ── Issues and faults ────────────────────────────────────────────────
    issue_titles = Counter()
    for i in (VehicleIssue.objects.filter(vehicle_id__in=unit_ids,
                                          reported_at__date__gte=start, reported_at__date__lte=end)
              .select_related("vehicle")):
        row = per[i.vehicle_id]
        row["issues"] += 1
        if i.resolved_at is None:
            row["issues_open"] += 1
        issue_titles[(i.vehicle_id, _normalise_title(i.title))] += 1

    fault_codes = Counter()
    for f in (VehicleFault.objects.filter(vehicle_id__in=unit_ids,
                                          first_seen_at__date__gte=start, first_seen_at__date__lte=end)):
        row = per[f.vehicle_id]
        row["fault_episodes"] += 1
        if f.code:
            fault_codes[(f.vehicle_id, f.code)] += 1

    # ── Roll-ups ─────────────────────────────────────────────────────────
    rows = list(per.values())
    for row in rows:
        row["availability_pct"] = _pct(row["unit_days"] - row["down_days"], row["unit_days"])
        row["trouble_score"] = (row["down_episodes"] * 3 + row["repairs"] * 2
                                + row["issues"] + row["fault_episodes"])
    total_unit_days = sum(r["unit_days"] for r in rows)
    total_down = sum(r["down_days"] for r in rows)

    imbalance = _mileage_imbalance(rows)
    repeats = _repeats(rows, issue_titles, fault_codes, per)

    # ── Has the ledger ever been used at all? ────────────────────────────
    # 100% available / $0 spend / no downtime is what a PERFECT month looks
    # like and also what an UNTOUCHED LEDGER looks like, and they are wildly
    # different things to tell a manager. The distinguishing question is not
    # "was anything recorded in this window" but "has anything ever been
    # recorded" — so this looks at all time, not the window.
    ledger = {
        "downtimes": VehicleDowntime.objects.filter(vehicle_id__in=unit_ids).count(),
        "services": VehicleServiceRecord.objects.filter(vehicle_id__in=unit_ids).count(),
        "issues": VehicleIssue.objects.filter(vehicle_id__in=unit_ids).count(),
    }
    # Per-dependency, not one flag: a single reported issue does not make
    # AVAILABILITY meaningful, and logging one oil change does not make DOWNTIME
    # meaningful. Each half of the report is only as true as the records it is
    # actually built from.
    #
    #   availability, downtime days, what-causes-downtime, placement  <- downtimes
    #   spend, PM adherence, most-maintenance                         <- service records
    #
    # Becoming-unreliable and mileage balance are measured by the telemetry
    # poller rather than by anyone remembering to fill a form in, so they stay
    # trustworthy either way — which is exactly why they lead when the rest
    # cannot.
    ledger["has_downtime"] = ledger["downtimes"] > 0
    ledger["has_service"] = ledger["services"] > 0
    ledger["started"] = ledger["has_downtime"] or ledger["has_service"]
    ledger["measured_only"] = not ledger["started"]

    return {
        "start": start, "end": end, "today": today, "window_days": window_days,
        "units": len(units),
        "ledger": ledger,
        "availability_pct": _pct(total_unit_days - total_down, total_unit_days),
        "total_down_days": total_down,
        "total_unit_days": total_unit_days,
        "down_by_category": sorted(
            ({"category": c, "label": _category_label(c), "days": n} for c, n in by_category.items()),
            key=lambda x: -x["days"]),
        "most_down": sorted([r for r in rows if r["down_days"]], key=lambda r: -r["down_days"])[:5],
        "most_maintenance": sorted([r for r in rows if r["service_events"]],
                                   key=lambda r: (-r["service_cost"], -r["service_events"]))[:5],
        "least_reliable": sorted([r for r in rows if r["trouble_score"]],
                                 key=lambda r: -r["trouble_score"])[:5],
        "pm": {**pm, "total": pm["on_time"] + pm["late"] + pm["unknown"],
               "median_late_by_miles": int(median(pm["late_by_miles"])) if pm["late_by_miles"] else None},
        "placement": placement,
        "legs_median": legs_median,
        "imbalance": imbalance,
        "repeats": repeats,
        "rows": rows,
        "service_cost_total": sum((r["service_cost"] for r in rows), Decimal("0")),
    }


# ════════════════════════════════════════════════════════════════════════════
# helpers
# ════════════════════════════════════════════════════════════════════════════

def _blank_row(unit, window_days):
    return {
        "vehicle": unit, "type": unit_type(unit), "type_label": type_label(unit_type(unit)),
        "unit_days": window_days, "down_days": 0, "down_episodes": 0,
        "down_by_category": Counter(),
        "miles": None, "known_days": 0, "reading_days": 0, "miles_per_day": None,
        "service_events": 0, "service_cost": Decimal("0"), "repairs": 0,
        "pm_events": 0, "pm_late": 0, "pm_on_time": 0,
        "issues": 0, "issues_open": 0, "fault_episodes": 0,
    }


def _pct(part, whole):
    if not whole:
        return None
    return round(100.0 * part / whole, 1)


def _category_label(category):
    from drivers.models import VehicleDowntime
    return dict(VehicleDowntime.CATEGORY_CHOICES).get(category, category)


def _daily_leg_counts(Leg, start, end):
    rows = (Leg.objects.filter(pickup_date__gte=start, pickup_date__lte=end)
            .exclude(reservation__status__in=("cancelled", "canceled"))
            .exclude(status="cancelled")
            .values("pickup_date").annotate(n=Count("id")))
    return {r["pickup_date"]: r["n"] for r in rows}


def _pm_verdict(record, schedule, VehicleServiceRecord, cache):
    """Was this preventative service done before it fell due?

    Compares the record to the PREVIOUS record of the same type on the same
    car, using the interval on the car's schedule. Miles first, then days.
    'unknown' when there is no prior record or no interval — the first oil
    change logged can't be judged, and saying so beats guessing.
    """
    key = (record.vehicle_id, record.service_type)
    prior = cache.get(key)
    if prior is None:
        prior = (VehicleServiceRecord.objects
                 .filter(vehicle_id=record.vehicle_id, service_type=record.service_type,
                         performed_on__lt=record.performed_on)
                 .order_by("-performed_on", "-id").first())
    cache[key] = record  # the next record of this type compares against this one
    if prior is None or schedule is None:
        return "unknown", None
    if (schedule.interval_miles and prior.odometer_miles is not None
            and record.odometer_miles is not None):
        due_at = prior.odometer_miles + Decimal(schedule.interval_miles)
        late_by = record.odometer_miles - due_at
        return ("late" if late_by > 0 else "on_time"), (int(late_by) if late_by > 0 else None)
    if schedule.interval_days:
        due_on = prior.performed_on + timedelta(days=schedule.interval_days)
        return ("late" if record.performed_on > due_on else "on_time"), None
    return "unknown", None


def _mileage_imbalance(rows):
    """Within each type, the hardest- and lightest-worked unit by miles/day."""
    by_type = defaultdict(list)
    for r in rows:
        if r["miles_per_day"] is not None and r["type"]:
            by_type[r["type"]].append(r)
    out = []
    for vtype, group in by_type.items():
        if len(group) < 2:
            continue
        group.sort(key=lambda r: r["miles_per_day"])
        lo, hi = group[0], group[-1]
        ratio = (hi["miles_per_day"] / lo["miles_per_day"]) if lo["miles_per_day"] else None
        out.append({
            "type": vtype, "label": type_label(vtype), "units": len(group),
            "hardest": hi, "lightest": lo,
            "ratio": ratio.quantize(Decimal("0.1")) if ratio is not None else None,
            "flag": bool(ratio is not None and ratio >= IMBALANCE_RATIO),
        })
    out.sort(key=lambda x: (x["ratio"] is None, -(x["ratio"] or 0)))
    return out


def _normalise_title(title):
    return " ".join((title or "").lower().split())[:60]


def _repeats(rows, issue_titles, fault_codes, per):
    out = []
    for (vehicle_id, title), n in issue_titles.items():
        if n >= REPEAT_MIN:
            out.append({"vehicle": per[vehicle_id]["vehicle"], "what": title, "times": n,
                        "kind": "reported"})
    for (vehicle_id, code), n in fault_codes.items():
        if n >= REPEAT_MIN:
            out.append({"vehicle": per[vehicle_id]["vehicle"], "what": f"fault {code}", "times": n,
                        "kind": "fault"})
    out.sort(key=lambda x: (-x["times"], x["vehicle"].vehicle_number))
    return out
