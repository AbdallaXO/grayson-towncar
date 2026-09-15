"""
Loads everything the Fleet desk shows — the one place that queries, so the
desk view, the morning digest and the tests all see the same picture.

The arithmetic lives elsewhere and is pure:
  * ``fleet_attention`` — the prioritised list and the per-unit status board
  * ``fleet_capacity``  — busy days, spare cars, downtime verdicts
  * ``fleet_health``    — readiness chips, service-due, feed health

DB-only, like every fleet page: nothing here calls Samsara.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.urls import reverse
from django.utils import timezone

from dispatching import fleet_attention, fleet_capacity, fleet_health, fleet_queue
from dispatching.fleet_sync import FEED_VEHICLE_STATS
from dispatching.mileage import days_to_cover, usage_rate

DESK_OUTLOOK_DAYS = 14
RATE_WINDOW_DAYS = 30
# How many quiet, clear days to name as opportunities.
OPPORTUNITY_COUNT = 3


def _href(vehicle):
    return reverse("fleet_detail", args=[vehicle.pk])


def load_desk(today=None, now=None, *, outlook_days=DESK_OUTLOOK_DAYS, use_cache=True):
    """Everything the desk renders, as one dict. See the keys at the bottom."""
    from django.db.models import Count, Q, Sum
    from drivers.models import (
        DriverVehicleAssignment, FleetSyncState, VehicleDayReading, VehicleDowntime,
        VehicleFault, VehicleIssue, VehicleServiceSchedule,
    )

    now = now or timezone.now()
    today = today or timezone.localdate(now)

    units = fleet_capacity.fleet_units()
    unit_ids = [u.id for u in units]
    by_id = {u.id: u for u in units}

    downtimes_open = []
    for u in units:
        for d in u.open_downtimes():
            d.vehicle = u
            downtimes_open.append(d)

    issues_open = list(
        VehicleIssue.objects.filter(vehicle_id__in=unit_ids, resolved_at__isnull=True)
        .select_related("reported_by")
    )
    for i in issues_open:
        i.vehicle = by_id[i.vehicle_id]

    faults_open = list(VehicleFault.objects.filter(vehicle_id__in=unit_ids, resolved_at__isnull=True))
    for f in faults_open:
        f.vehicle = by_id[f.vehicle_id]
    recurrence_cutoff = now - timedelta(days=fleet_attention.RECURRING_WINDOW_DAYS)
    faults_recent = list(VehicleFault.objects.filter(
        vehicle_id__in=unit_ids, first_seen_at__gte=recurrence_cutoff))

    schedules = list(VehicleServiceSchedule.objects.filter(vehicle_id__in=unit_ids, is_active=True))
    for s in schedules:
        s.vehicle = by_id[s.vehicle_id]

    assigned_today = {}
    for a in (DriverVehicleAssignment.objects.filter(date=today, vehicle_id__in=unit_ids)
              .select_related("driver__profile")):
        assigned_today[a.vehicle_id] = str(a.driver)

    feed = fleet_health.feed_health(
        FleetSyncState.objects.filter(feed=FEED_VEHICLE_STATS).first(), now)

    # Mileage rate per unit, for projected service dates — same aggregate the
    # fleet list uses, so the two never disagree.
    window_start = today - timedelta(days=RATE_WINDOW_DAYS)
    rates = {}
    for row in (VehicleDayReading.objects
                .filter(vehicle_id__in=unit_ids, date__gte=window_start, date__lte=today)
                .values("vehicle_id")
                .annotate(miles=Sum("miles_driven"),
                          known=Count("id", filter=Q(miles_driven__isnull=False)))):
        if row["miles"] is not None and row["known"]:
            rates[row["vehicle_id"]] = (Decimal(row["miles"]) / row["known"]).quantize(Decimal("0.1"))
    projected_dates = {}
    for s in schedules:
        odometer = s.vehicle.odometer_miles
        due = s.due_at_odometer_miles
        per_day = rates.get(s.vehicle_id)
        if due is None or odometer is None or per_day is None:
            continue
        days_out = days_to_cover(due - Decimal(odometer), per_day)
        if days_out:
            projected_dates[s.id] = today + timedelta(days=days_out)

    # Outlook, and from it the verdict on every planned window inside it.
    outlook_rows = fleet_capacity.outlook(today, outlook_days, units, today=today, use_cache=use_cache)
    by_day = {r["date"]: r for r in outlook_rows}
    downtime_verdicts = {}
    for d in downtimes_open:
        if not d.is_planned(today):
            continue
        end = d.expected_back_on or (d.starts_on + timedelta(days=1))
        worst = "clear"
        day = d.starts_on
        while day < end:
            r = by_day.get(day)
            if r and fleet_capacity.LEVEL_RANK[r["level"]] > fleet_capacity.LEVEL_RANK[worst]:
                worst = r["level"]
            day += timedelta(days=1)
        downtime_verdicts[d.id] = worst

    attention = fleet_attention.build_attention(
        today=today, now=now, vehicles=units, downtimes_open=downtimes_open,
        issues_open=issues_open, faults_open=faults_open, faults_recent=faults_recent,
        schedules=schedules, feed=feed, assigned_today=assigned_today,
        downtime_verdicts=downtime_verdicts, projected_dates=projected_dates,
        href_for=_href,
    )
    board = fleet_attention.status_rows(
        today=today, now=now, vehicles=units, downtimes_open=downtimes_open,
        issues_open=issues_open, faults_open=faults_open, schedules=schedules,
        assigned_today=assigned_today, href_for=_href,
    )

    # Opportunities: the quietest clear days ahead, and cars with no driver
    # today (a quick job can happen without touching the board).
    quiet_days = sorted(
        [r for r in outlook_rows[1:] if r["level"] == "clear"],
        key=lambda r: (r["utilisation"], r["date"]),
    )[:OPPORTUNITY_COUNT]
    # Has Dispatch built today yet? No assignment rows at all means every unit
    # reads as having no chauffeur, which is not the same as being free — see
    # fleet_day for the same rule and the measured gradient behind it.
    built = bool(assigned_today)
    idle_today = ([r for r in board if r["idle_today"] and r["state"] != "down"]
                  if built else [])

    in_shop = [d for d in downtimes_open if d.is_live(today) or d.is_overdue(today)]
    in_shop.sort(key=lambda d: (d.expected_back_on or today + timedelta(days=3650), d.starts_on))
    planned = [d for d in downtimes_open if d.is_planned(today)]
    planned.sort(key=lambda d: d.starts_on)

    # The redesigned desk: one row per unit with a decision on it, and the
    # three small pictures beside it. Same loaded rows, so the queue and the
    # digest can never disagree about what is open.
    queue = fleet_queue.build_queue(
        today=today, now=now, vehicles=units, downtimes_open=downtimes_open,
        issues_open=issues_open, faults_open=faults_open, faults_recent=faults_recent,
        schedules=schedules, assigned_today=assigned_today,
        downtime_verdicts=downtime_verdicts, href_for=_href, built=built,
    )
    state_bar = fleet_queue.state_bar(board)
    shop = fleet_queue.shop_panel(today=today, in_shop=in_shop, planned=planned,
                                  idle_today=idle_today, downtime_verdicts=downtime_verdicts,
                                  built=built)
    paperwork = fleet_queue.paperwork_rows(
        units, today, list_href=reverse("fleet_list"), href_for=_href)
    setup_intervals = next(
        (it for it in attention["setup"] if it["kind"] == "setup_intervals"), None)
    setup_baselines = next(
        (it for it in attention["setup"] if it["kind"] == "setup_baselines"), None)
    # The week's round, as one sentence. The Desk is the page he lives on, and
    # until this landed the fleet's largest recurring obligation was reachable
    # only by remembering to open a tab.
    from dispatching import fleet_inspection
    inspection = fleet_inspection.week_summary(today, units)

    # The forward view. Folded, and with paperwork dropped because the Paperwork
    # block on the same page already groups it fleet-wide — rendering both is
    # how the first version of this desk ended up with 17 copies of one fact.
    horizon = {
        "week": fleet_attention.collapse(attention["week"], covered=("paperwork",)),
        "later": fleet_attention.collapse(attention["later"], covered=("paperwork",)),
    }

    summary = {
        "units": len(units),
        "ready": sum(1 for r in board if r["state"] == "ready"),
        "watch": sum(1 for r in board if r["state"] == "watch"),
        "down": sum(1 for r in board if r["state"] in ("down",)),
        "unconfirmed": sum(1 for r in board if r["state"] == "unconfirmed"),
        "planned": len(planned),
        "idle_today": len(idle_today),
        "built": built,
    }

    return {
        "today": today,
        "now": now,
        "built": built,
        "units": units,
        "feed": feed,
        "attention": attention,
        "board": board,
        "summary": summary,
        "outlook": outlook_rows,
        "outlook_days": outlook_days,
        "quiet_days": quiet_days,
        "idle_today": idle_today,
        "in_shop": in_shop,
        "planned": planned,
        "downtime_verdicts": downtime_verdicts,
        "issues_open": issues_open,
        "queue": queue,
        "state_bar": state_bar,
        "shop": shop,
        "paperwork": paperwork,
        "setup_intervals": setup_intervals,
        "setup_baselines": setup_baselines,
        "inspection": inspection,
        "horizon": horizon,
    }


def load_attention(today=None, now=None):
    """Just the prioritised list — what the morning digest reads."""
    return load_desk(today=today, now=now)["attention"]


def quick_now_count(today=None):
    """The navbar pill: how many things are wrong RIGHT NOW, from the cheap
    facts alone (no outlook, no mileage). Four indexed queries."""
    from django.db.models import Q
    from drivers.models import FleetVehicle, VehicleDowntime, VehicleFault, VehicleIssue

    today = today or timezone.localdate()
    active = FleetVehicle.objects.filter(is_active=True)
    overdue = VehicleDowntime.objects.filter(
        vehicle__in=active, ended_on__isnull=True, expected_back_on__lte=today).count()
    issues = VehicleIssue.objects.filter(
        vehicle__in=active, resolved_at__isnull=True, severity__in=("ground", "soon")).count()
    faults = (VehicleFault.objects.filter(vehicle__in=active, resolved_at__isnull=True)
              .values("vehicle_id").distinct().count())
    expired = active.filter(
        Q(registration_expires_on__lt=today) | Q(insurance_expires_on__lt=today)
        | Q(next_inspection_on__lt=today)).count()
    return overdue + issues + faults + expired
