"""
What the fleet manager should look at, in the order it matters.

Pure functions over already-loaded rows — no queries, no clock reads beyond the
``today``/``now`` a caller passes in — so the desk view, the morning digest and
the tests all get the same list from the same inputs. Loading lives in
``fleet_desk.py``.

Three groups, because urgency is the only sort that survives a busy week:

  NOW    — something is wrong today: a car overdue back from the shop, a
           reported "do not drive", an open fault, an overdue service, expired
           paperwork, the Samsara feed dead.
  WEEK   — will be wrong within days: service or paperwork coming due, a car
           expected back soon, a planned downtime that now collides with demand.
  LATER  — worth knowing, no action this week: projected service dates,
           tight (not short) planned windows, a battery drifting low.
  SETUP  — the module isn't fully switched on: units with no service intervals
           or no compliance dates. Shown once, collapsed, not per unit.

Alert-fatigue rules, each of which exists because the alternative was tried
on the fleet list and read as a wall of chips:

  * Fuel is never here. A quarter tank is the dispatcher's evening problem and
    it is already on the vehicle table; it is not maintenance.
  * One service line per unit — the most urgent interval, not all four.
  * One paperwork line per unit, naming every document that is due.
  * "GPS quiet" on more than a handful of units collapses to one line; that
    many at once is the feed, not the cars.
  * Nothing here fires twice for the same fact through two different rules.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from business.datefmt import strf
from dispatching import fleet_health

CRITICAL, WARN, INFO = fleet_health.CRITICAL, fleet_health.WARN, fleet_health.INFO
NOW, WEEK, LATER, SETUP = "now", "week", "later", "setup"
LEVEL_RANK = {CRITICAL: 0, WARN: 1, INFO: 2}

# An open-ended downtime ("no return date") older than this needs a date on it.
NO_ETA_NAG_DAYS = 3
# An open issue nobody has touched for this long moves up a level.
ISSUE_STALE_DAYS = 7
# "Returning soon" window.
RETURNING_SOON_DAYS = 3
# Projected service dates further out than this are not worth a line.
PROJECTED_WINDOW_DAYS = 14
# More projected-service lines than this collapse into "N more" — a fleet
# whose intervals were all seeded on the same day projects them all at once.
PROJECTED_MAX_LINES = 4
# The same fault code opening this many episodes within the window is a
# recurring problem, not a glitch.
RECURRING_EPISODES = 3
RECURRING_WINDOW_DAYS = 30
# More quiet gateways than this at once is a feed story, not a per-car one.
STALE_GPS_COLLAPSE = 3


def _item(level, group, kind, title, detail="", *, vehicle=None, href="",
          sort_key=None, action=""):
    return {
        "level": level, "group": group, "kind": kind, "title": title,
        "detail": detail, "vehicle": vehicle, "href": href, "action": action,
        "sort_key": sort_key if sort_key is not None else "",
    }


def _unit(vehicle):
    return f"#{vehicle.vehicle_number}"


def _day(d):
    return strf(d, "%a %b %-d")


# ════════════════════════════════════════════════════════════════════════════
# The list
# ════════════════════════════════════════════════════════════════════════════

def collapse(items, *, covered=(), fold_at=3):
    """Fold a horizon group down to what a person can read in one glance.

    The groups are per-UNIT facts by design — that is what the morning digest
    needs. A page is different: seventeen rows of "MCO permit due" is seventeen
    copies of one fact, which is the exact wall the desk redesign removed and
    which ``paperwork_rows`` already collapses on this very page.

    So: drop kinds the page shows elsewhere (``covered``), and fold any kind
    with ``fold_at`` or more items into a single line naming the units. A kind
    with one or two items keeps its own rows, because "#005 battery drifting"
    is worth reading as itself.
    """
    by_kind = {}
    for it in items:
        if it["kind"] in covered:
            continue
        by_kind.setdefault(it["kind"], []).append(it)

    rank = {CRITICAL: 0, WARN: 1, INFO: 2}
    out = []
    for kind, group in by_kind.items():
        if len(group) < fold_at:
            out.extend(group)
            continue
        units = [_unit(i["vehicle"]) for i in group if i.get("vehicle") is not None]
        worst = min((g["level"] for g in group), key=lambda lv: rank.get(lv, 9))
        head = group[0]["title"]
        # "#001 paperwork: MCO permit due (in 15d)" -> "MCO permit due (in 15d)"
        if ":" in head:
            head = head.split(":", 1)[1].strip()
        out.append(_item(
            worst, group[0]["group"], kind,
            f"{head} — {len(group)} units",
            ", ".join(units[:6]) + (f" and {len(units) - 6} more" if len(units) > 6 else ""),
            action=group[0].get("action", ""),
        ))
    out.sort(key=lambda i: (rank.get(i["level"], 9), i["title"]))
    return out


def build_attention(*, today, now, vehicles, downtimes_open, issues_open,
                    faults_open, faults_recent, schedules, feed, assigned_today,
                    downtime_verdicts=None, projected_dates=None, href_for=None):
    """Everything worth the fleet manager's attention, grouped and sorted.

    Args:
        vehicles:          active FleetVehicle rows (with vehicle_type).
        downtimes_open:    open VehicleDowntime rows, ``.vehicle`` loaded.
        issues_open:       open VehicleIssue rows, ``.vehicle`` loaded.
        faults_open:       open VehicleFault rows, ``.vehicle`` loaded.
        faults_recent:     VehicleFault rows (open or resolved) first seen in
                           the recurrence window, for "keeps coming back".
        schedules:         active VehicleServiceSchedule rows, ``.vehicle`` loaded.
        feed:              ``fleet_health.feed_health(...)`` result.
        assigned_today:    {vehicle_id: driver display name} for today's plan.
        downtime_verdicts: {downtime_id: "clear"|"tight"|"conflict"} for
                           planned windows, from the capacity check.
        projected_dates:   {schedule_id: date|None} — "≈ due Sep 24 at this rate".
        href_for:          callable(vehicle) -> URL of its fleet page.

    Returns ``{"now": [...], "week": [...], "later": [...], "setup": [...],
    "counts": {"critical": n, "warn": n, "info": n, "total": n}}``.
    """
    downtime_verdicts = downtime_verdicts or {}
    projected_dates = projected_dates or {}
    href_for = href_for or (lambda v: "")
    items = []
    by_vehicle_id = {v.id: v for v in vehicles}

    # ── Feed ─────────────────────────────────────────────────────────────
    if feed and feed.get("level") in (CRITICAL, WARN):
        items.append(_item(
            feed["level"], NOW, "feed",
            f"Samsara: {feed['label'].lower()}", feed.get("detail", ""),
            sort_key="0", action="Every reading below is only as fresh as this.",
        ))

    # ── Downtimes ────────────────────────────────────────────────────────
    live_blocking = {}
    for d in downtimes_open:
        v = d.vehicle
        if d.is_overdue(today):
            days_over = (today - d.expected_back_on).days
            items.append(_item(
                CRITICAL, NOW, "downtime_overdue",
                f"{_unit(v)} was expected back {_day(d.expected_back_on)}"
                + (f" — {days_over} day{'s' if days_over != 1 else ''} ago" if days_over else " — today"),
                f"{d.reason}" + (f" · {d.vendor}" if d.vendor else ""),
                vehicle=v, href=href_for(v), sort_key=d.expected_back_on.isoformat(),
                action="Confirm it's back, or push the date out. Until then dispatch "
                       "sees it as usable with a 'not confirmed' note.",
            ))
            continue
        if d.is_live(today):
            live_blocking[v.id] = d
            if d.expected_back_on is None:
                age = (today - d.starts_on).days
                if age >= NO_ETA_NAG_DAYS:
                    items.append(_item(
                        WARN, NOW, "downtime_no_eta",
                        f"{_unit(v)} down {age} days with no return date",
                        f"{d.reason}" + (f" · {d.vendor}" if d.vendor else ""),
                        vehicle=v, href=href_for(v), sort_key=d.starts_on.isoformat(),
                        action="Get a date from the shop so dispatch can plan on it.",
                    ))
            else:
                until = (d.expected_back_on - today).days
                if 0 < until <= RETURNING_SOON_DAYS:
                    items.append(_item(
                        INFO, WEEK, "downtime_returning",
                        f"{_unit(v)} due back {_day(d.expected_back_on)}",
                        f"{d.reason}" + (f" · {d.vendor}" if d.vendor else ""),
                        vehicle=v, href=href_for(v), sort_key=d.expected_back_on.isoformat(),
                        action="Confirm when it lands so the record closes with the real date.",
                    ))
        elif d.is_planned(today):
            verdict = downtime_verdicts.get(d.id, "")
            days_out = (d.starts_on - today).days
            if verdict == "conflict":
                items.append(_item(
                    WARN, WEEK if days_out <= 7 else LATER, "downtime_conflict",
                    f"{_unit(v)} planned down {_day(d.starts_on)} now collides with demand",
                    f"{d.get_category_display()} · {d.reason}",
                    vehicle=v, href=href_for(v), sort_key=d.starts_on.isoformat(),
                    action="Bookings have grown since this was planned — move it or "
                           "warn dispatch.",
                ))
            elif verdict == "tight":
                items.append(_item(
                    INFO, LATER, "downtime_tight",
                    f"{_unit(v)} planned down {_day(d.starts_on)} leaves no spare car",
                    f"{d.get_category_display()} · {d.reason}",
                    vehicle=v, href=href_for(v), sort_key=d.starts_on.isoformat(),
                ))

    # Dispatch is using a car the ledger says is on a lift.
    for vehicle_id, downtime in live_blocking.items():
        driver = assigned_today.get(vehicle_id)
        if driver:
            v = downtime.vehicle
            items.append(_item(
                CRITICAL, NOW, "downtime_in_use",
                f"{_unit(v)} is marked down but {driver} has it today",
                f"{downtime.reason}",
                vehicle=v, href=href_for(v), sort_key="1",
                action="Either the car is back — close the downtime — or dispatch "
                       "needs to know it isn't.",
            ))

    # ── Reported issues ──────────────────────────────────────────────────
    for issue in issues_open:
        v = issue.vehicle
        age = (now - issue.reported_at).days if issue.reported_at else 0
        level = {"ground": CRITICAL, "soon": WARN, "watch": INFO}.get(issue.severity, WARN)
        group = {"ground": NOW, "soon": NOW, "watch": LATER}.get(issue.severity, NOW)
        stale = age >= ISSUE_STALE_DAYS
        if stale and level != CRITICAL:
            level = WARN if level == INFO else CRITICAL
            group = NOW
        who = issue.reported_by.get_full_name() or issue.reported_by.username if issue.reported_by else issue.get_source_display()
        items.append(_item(
            level, group, "issue",
            f"{_unit(v)}: {issue.title}",
            f"{issue.get_severity_display()} · reported by {who}"
            + (f" {age} day{'s' if age != 1 else ''} ago" if age else " today")
            + (" · still open" if stale else ""),
            vehicle=v, href=href_for(v), sort_key=issue.reported_at.isoformat() if issue.reported_at else "",
            action="Resolve it with what was done, or turn it into a downtime.",
        ))

    # ── Faults from the car ──────────────────────────────────────────────
    recurring = _recurring_codes(faults_recent, today)
    open_by_vehicle = {}
    for f in faults_open:
        open_by_vehicle.setdefault(f.vehicle_id, []).append(f)
    for vehicle_id, faults in open_by_vehicle.items():
        v = by_vehicle_id.get(vehicle_id) or faults[0].vehicle
        codes = [f.code or f.get_source_display() for f in faults]
        worst = CRITICAL if any(f.severity == "critical" for f in faults) or len(faults) > 1 else WARN
        first = min(f.first_seen_at for f in faults)
        again = [c for c in codes if (vehicle_id, c) in recurring]
        detail = ", ".join(
            f"{f.code or 'fault'}" + (f" — {f.description}" if f.description else "")
            for f in faults[:3]
        )
        if len(faults) > 3:
            detail += f" (+{len(faults) - 3} more)"
        items.append(_item(
            worst, NOW, "fault",
            f"{_unit(v)} reporting {len(faults)} fault code{'s' if len(faults) != 1 else ''}"
            + (" — recurring" if again else ""),
            detail + f" · since {strf(first, '%b %-d')}",
            vehicle=v, href=href_for(v), sort_key=first.isoformat(),
            action=("This code has come back " + ", ".join(sorted(set(again))) + " — worth a proper look, not a reset.")
            if again else "Read the code before the next assignment; a lit lamp on an airport run is a bad morning.",
        ))

    # ── Service intervals ────────────────────────────────────────────────
    per_vehicle_service = {}
    for s in schedules:
        v = s.vehicle
        odometer = v.odometer_miles
        findings = fleet_health.service_findings(s, odometer, today)
        if findings:
            f = findings[0]
            level = f["level"]
            candidate = (LEVEL_RANK[level], s, f, None)
        else:
            projected = projected_dates.get(s.id)
            if projected and 0 <= (projected - today).days <= PROJECTED_WINDOW_DAYS:
                candidate = (LEVEL_RANK[INFO], s, None, projected)
            else:
                continue
        current = per_vehicle_service.get(v.id)
        if current is None or candidate[0] < current[0]:
            per_vehicle_service[v.id] = candidate
    projected_items = []
    for vehicle_id, (rank, s, finding, projected) in per_vehicle_service.items():
        v = s.vehicle
        if finding:
            level = finding["level"]
            group = NOW if level == CRITICAL else WEEK
            items.append(_item(
                level, group, "service",
                f"{_unit(v)}: {finding['label'].lower()}",
                f"{finding['detail']}",
                vehicle=v, href=href_for(v), sort_key=str(rank),
                action="Book it on a clear day — the outlook shows which." if level != CRITICAL
                else "Overdue. Take it on the next quiet day, or accept the risk on record.",
            ))
        else:
            projected_items.append(_item(
                INFO, LATER, "service_projected",
                f"{_unit(v)}: {s.get_service_type_display().lower()} ≈ {_day(projected)} at this rate",
                "projected from the last 30 days of mileage — not a booking",
                vehicle=v, href=href_for(v), sort_key=projected.isoformat(),
            ))
    projected_items.sort(key=lambda it: it["sort_key"])
    if len(projected_items) > PROJECTED_MAX_LINES:
        rest = projected_items[PROJECTED_MAX_LINES:]
        projected_items = projected_items[:PROJECTED_MAX_LINES]
        projected_items.append(_item(
            INFO, LATER, "service_projected_more",
            f"{len(rest)} more units have service projected within {PROJECTED_WINDOW_DAYS} days",
            ", ".join(_unit(it["vehicle"]) for it in rest),
            sort_key=rest[-1]["sort_key"],
            action="Each car's page has the date. Space them out — the outlook shows the quiet days.",
        ))
    items.extend(projected_items)

    # ── Paperwork ────────────────────────────────────────────────────────
    for v in vehicles:
        findings = fleet_health.compliance_findings(v, today)
        for p in v.permits(day=today):
            if p["on_file"] and p["expires_on"]:
                days = (p["expires_on"] - today).days
                if days < 0:
                    findings.append(fleet_health._chip(CRITICAL, f"{p['label']} permit expired", f"{abs(days)}d ago"))
                elif days <= fleet_health.EXPIRY_WARN_DAYS:
                    findings.append(fleet_health._chip(WARN, f"{p['label']} permit due", f"in {days}d"))
        if not findings:
            continue
        worst = min(findings, key=lambda c: LEVEL_RANK[c["level"]])
        names = "; ".join(f"{c['label']} ({c['detail']})" for c in findings)
        items.append(_item(
            worst["level"], NOW if worst["level"] == CRITICAL else WEEK, "paperwork",
            f"{_unit(v)} paperwork: {names}",
            "",
            vehicle=v, href=href_for(v), sort_key="2",
            action="Renew and put the new date on the vehicle page.",
        ))

    # ── Telemetry that means a no-start or a blind spot ──────────────────
    stale_units = []
    for v in vehicles:
        if not v.samsara_vehicle_id:
            continue
        mv = v.samsara_battery_millivolts
        if mv is not None:
            volts = Decimal(mv) / 1000
            if mv <= fleet_health.BATTERY_CRITICAL_MV:
                items.append(_item(
                    WARN, NOW, "battery", f"{_unit(v)} battery {volts:.1f}V",
                    "below the no-start line — it may not turn over tomorrow morning",
                    vehicle=v, href=href_for(v), sort_key="3",
                    action="Have it started and checked before it's assigned an early run.",
                ))
            elif mv <= fleet_health.BATTERY_LOW_MV:
                items.append(_item(
                    INFO, LATER, "battery", f"{_unit(v)} battery {volts:.1f}V",
                    "running low — worth a load test at the next service",
                    vehicle=v, href=href_for(v), sort_key="9",
                ))
        age = fleet_health._telemetry_age_hours(v, now)
        if age is not None and age >= fleet_health.TELEMETRY_STALE_HOURS:
            stale_units.append((v, age))
    if stale_units and not (feed and feed.get("level") == CRITICAL):
        if len(stale_units) > STALE_GPS_COLLAPSE:
            items.append(_item(
                INFO, NOW, "gps_stale",
                f"{len(stale_units)} gateways quiet for 12h+",
                ", ".join(_unit(v) for v, _a in stale_units),
                sort_key="8",
                action="That many at once is the feed or the account, not the cars.",
            ))
        else:
            for v, age in stale_units:
                items.append(_item(
                    INFO, NOW, "gps_stale",
                    f"{_unit(v)} gateway quiet {fleet_health._compact_hours(age)}",
                    "no odometer, fault or battery reading until it reports again",
                    vehicle=v, href=href_for(v), sort_key="8",
                ))

    # ── Setup: the module is only as good as what's in it ────────────────
    scheduled_ids = {s.vehicle_id for s in schedules}
    no_intervals = [v for v in vehicles if v.id not in scheduled_ids]
    if no_intervals:
        items.append(_item(
            INFO, SETUP, "setup_intervals",
            f"{len(no_intervals)} unit{'s' if len(no_intervals) != 1 else ''} have no service intervals",
            ", ".join(_unit(v) for v in no_intervals),
            sort_key="0",
            action="Nothing can come due on a car with no interval. Use the standard "
                   "set to start, then correct any that differ.",
        ))
    # Seeding intervals without a baseline is honest, but it must not make the
    # desk go QUIET about maintenance — a car with an interval and no baseline
    # still cannot tell you anything is due. This is the second half of that
    # story, and it replaces the first as soon as the intervals exist.
    baseline_ids = {s.vehicle_id for s in schedules
                    if s.last_done_on is not None or s.last_done_odometer_miles is not None}
    no_baseline = [v for v in vehicles
                   if v.id in scheduled_ids and v.id not in baseline_ids]
    if no_baseline:
        items.append(_item(
            INFO, SETUP, "setup_baselines",
            f"{len(no_baseline)} unit{'s' if len(no_baseline) != 1 else ''} have intervals but no baseline",
            ", ".join(_unit(v) for v in no_baseline),
            sort_key="0b",
            action="Nothing can come due until one is set. Read the windshield sticker "
                   "into the next walk-around, or log the last service.",
        ))
    no_dates = [v for v in vehicles if not (v.registration_expires_on or v.insurance_expires_on
                                            or v.next_inspection_on)]
    if no_dates:
        items.append(_item(
            INFO, SETUP, "setup_paperwork",
            f"{len(no_dates)} unit{'s' if len(no_dates) != 1 else ''} have no registration, insurance or inspection dates",
            ", ".join(_unit(v) for v in no_dates),
            sort_key="1",
            action="Twenty minutes with the glovebox folders and expiries start warning themselves.",
        ))

    groups = {NOW: [], WEEK: [], LATER: [], SETUP: []}
    for it in items:
        groups[it["group"]].append(it)
    for group in groups.values():
        group.sort(key=lambda it: (LEVEL_RANK[it["level"]], it["sort_key"],
                                   it["vehicle"].vehicle_number if it["vehicle"] else ""))
    counts = {
        "critical": sum(1 for it in items if it["level"] == CRITICAL),
        "warn": sum(1 for it in items if it["level"] == WARN),
        "info": sum(1 for it in items if it["level"] == INFO),
        "total": len(items),
        "now": len(groups[NOW]),
    }
    groups["counts"] = counts
    return groups


def _recurring_codes(faults_recent, today):
    """{(vehicle_id, code)} for codes with >= RECURRING_EPISODES episodes
    first seen inside the recurrence window."""
    cutoff = today - timedelta(days=RECURRING_WINDOW_DAYS)
    seen = {}
    for f in faults_recent:
        if not f.code or f.first_seen_at is None or f.first_seen_at.date() < cutoff:
            continue
        seen[(f.vehicle_id, f.code)] = seen.get((f.vehicle_id, f.code), 0) + 1
    return {key for key, n in seen.items() if n >= RECURRING_EPISODES}


# ════════════════════════════════════════════════════════════════════════════
# The status board — every unit, one line
# ════════════════════════════════════════════════════════════════════════════

STATE_ORDER = {"down": 0, "unconfirmed": 1, "watch": 2, "planned": 3, "ready": 4}


def status_rows(*, today, now, vehicles, downtimes_open, issues_open, faults_open,
                schedules, assigned_today, href_for=None):
    """One row per active unit: state, why, who has it, where it is.

    State precedence: down (blocked today) > unconfirmed (expected back, not
    signed off) > watch (an open problem the car can still work with) >
    planned (a downtime booked within the week) > ready.
    """
    href_for = href_for or (lambda v: "")
    downtimes_by_vehicle, issues_by_vehicle, faults_by_vehicle, schedules_by_vehicle = {}, {}, {}, {}
    for d in downtimes_open:
        downtimes_by_vehicle.setdefault(d.vehicle_id, []).append(d)
    for i in issues_open:
        issues_by_vehicle.setdefault(i.vehicle_id, []).append(i)
    for f in faults_open:
        faults_by_vehicle.setdefault(f.vehicle_id, []).append(f)
    for s in schedules:
        schedules_by_vehicle.setdefault(s.vehicle_id, []).append(s)

    rows = []
    for v in vehicles:
        state, headline, sub, flags = "ready", "Ready", "", []
        blocking = v.downtime_on(today)
        planned = None
        unconfirmed = None
        for d in downtimes_by_vehicle.get(v.id, []):
            if d.is_unconfirmed_on(today):
                unconfirmed = d
            elif d.is_planned(today) and (d.starts_on - today).days <= 7:
                if planned is None or d.starts_on < planned.starts_on:
                    planned = d

        issues = issues_by_vehicle.get(v.id, [])
        faults = faults_by_vehicle.get(v.id, [])
        service_due = []
        odometer = v.odometer_miles
        for s in schedules_by_vehicle.get(v.id, []):
            service_due += fleet_health.service_findings(s, odometer, today)
        paperwork = fleet_health.compliance_findings(v, today)
        mv = v.samsara_battery_millivolts

        if blocking is not None:
            state = "down"
            headline = blocking.reason or blocking.get_category_display()
            if blocking.expected_back_on:
                sub = f"back {_day(blocking.expected_back_on)}"
                if blocking.vendor:
                    sub += f" · {blocking.vendor}"
            else:
                sub = "no return date" + (f" · {blocking.vendor}" if blocking.vendor else "")
        elif unconfirmed is not None:
            state = "unconfirmed"
            headline = f"Expected back {_day(unconfirmed.expected_back_on)} — not confirmed"
            sub = unconfirmed.reason
        else:
            problems = []
            for i in issues:
                if i.severity == "ground":
                    problems.append((0, f"Reported: {i.title}", i.get_severity_display()))
                elif i.severity == "soon":
                    problems.append((1, f"Reported: {i.title}", i.get_severity_display()))
            if faults:
                codes = ", ".join(f.code or "fault" for f in faults[:3])
                problems.append((1, f"{len(faults)} fault code{'s' if len(faults) != 1 else ''}: {codes}", ""))
            for f in service_due:
                problems.append((0 if f["level"] == CRITICAL else 2, f["label"], f["detail"]))
            for f in paperwork:
                problems.append((0 if f["level"] == CRITICAL else 2, f["label"], f["detail"]))
            if mv is not None and mv <= fleet_health.BATTERY_CRITICAL_MV:
                problems.append((1, f"Battery {Decimal(mv) / 1000:.1f}V", "no-start risk"))
            for i in issues:
                if i.severity == "watch":
                    problems.append((3, f"Watching: {i.title}", ""))
            if problems:
                problems.sort(key=lambda p: p[0])
                state = "watch"
                headline, sub = problems[0][1], problems[0][2]
                if len(problems) > 1:
                    sub = (sub + " · " if sub else "") + f"+{len(problems) - 1} more"
            elif planned is not None:
                state = "planned"
                headline = f"Down {_day(planned.starts_on)}"
                if planned.expected_back_on:
                    headline += f" – back {_day(planned.expected_back_on)}"
                sub = f"{planned.get_category_display()} · {planned.reason}"
            if planned is not None and state == "watch":
                flags.append(f"down {_day(planned.starts_on)}")

        fresh = v.samsara_is_fresh
        rows.append({
            "vehicle": v,
            "state": state,
            "headline": headline,
            "sub": sub,
            "flags": flags,
            "driver": assigned_today.get(v.id, ""),
            "idle_today": (state in ("ready", "watch", "planned")
                           and not assigned_today.get(v.id)),
            "position": v.samsara_last_location_label,
            "moving": v.samsara_movement_status == "driving" and fresh,
            "age": v.samsara_age_display(),
            "fresh": fresh,
            "engine": v.samsara_engine_state,
            "odometer": odometer,
            "href": href_for(v),
        })
    rows.sort(key=lambda r: (STATE_ORDER[r["state"]], _natural(r["vehicle"].vehicle_number)))
    return rows


def _natural(vehicle_number):
    number = (vehicle_number or "").strip()
    digits = "".join(ch for ch in number if ch.isdigit())
    return (0, int(digits), number) if digits else (1, 0, number)
