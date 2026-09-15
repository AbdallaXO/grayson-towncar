"""
The Fleet desk's "Do now" queue: one row per unit that needs a decision, each
carrying its own action — and the three small pictures beside it (the state
bar, the shop panel, the paperwork rows).

Pure functions over already-loaded rows, like ``fleet_attention``. The
difference is the unit of account: ``fleet_attention`` lists FACTS (a fault, an
overdue service, an expired permit) for the morning digest; this lists UNITS,
because the fleet manager acts on a car, not on a fact. Three rules from the
redesign, each of which fixed something the fact list got wrong on a real
morning:

  * One row per unit. Four codes on #004 are one problem with four chips, not
    four lines.
  * Fleet-wide paperwork is grouped, with one action. Seventeen "MCO permit
    due in 16 days" lines were seventeen copies of one fact.
  * Handled feels handled. A unit that is booked in or already off the road
    drops below the open rows and takes the settled colour; acting on a row
    must visibly shrink the queue.

Every tag is one of five, and nothing else is ever used as a status:

  Needs the shop       an open problem the car should not keep working with
  Watch                an open problem it can work with, for now
  Not confirmed back   expected back from the shop, nobody has said so
  Booked in            a shop slot is held (handled)
  Off the road         down today, on the ledger (handled)
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from business.datefmt import strf
from dispatching import fleet_health
from dispatching.fleet_attention import RECURRING_EPISODES, RECURRING_WINDOW_DAYS, _natural

NEEDS_SHOP, WATCH, UNCONFIRMED, BOOKED, DOWN = (
    "Needs the shop", "Watch", "Not confirmed back", "Booked in", "Off the road")
TONE = {NEEDS_SHOP: "critical", WATCH: "caution", UNCONFIRMED: "caution",
        BOOKED: "settled", DOWN: "settled"}
ORDER = {NEEDS_SHOP: 0, UNCONFIRMED: 1, WATCH: 2, BOOKED: 3, DOWN: 4}

# A window length worth proposing for each kind of problem.
HOURS_FOR = {NEEDS_SHOP: 4, WATCH: 2}
# An open-ended downtime older than this needs a date on it.
NO_ETA_NAG_DAYS = 3
# An open issue nobody has touched for this long is called out as such.
ISSUE_STALE_DAYS = 7

STATE_COLOURS = {
    "ready": "#2C6A4A", "watch": "#C9A227", "planned": "#2B4A6F",
    "unconfirmed": "#7A5E12", "down": "#A32A1F",
}
STATE_LABELS = {
    "ready": "ready", "watch": "watch", "planned": "booked in",
    "unconfirmed": "back? unconfirmed", "down": "off the road",
}
STATE_BAR_ORDER = ("ready", "watch", "planned", "unconfirmed", "down")


def _unit(vehicle):
    return f"#{vehicle.vehicle_number}"


def _day(d):
    return strf(d, "%a %b %-d")


def _list(names):
    """'#005, #11 and #12'."""
    names = list(names)
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


def _age(moment, now, today):
    """'since this morning' / 'since yesterday' / 'open since Aug 31'."""
    if moment is None:
        return ""
    local = timezone.localtime(moment)
    days = (today - local.date()).days
    if days <= 0:
        if local.hour < 12:
            return "since this morning"
        if local.hour < 17:
            return "since this afternoon"
        return "since this evening"
    if days == 1:
        return "since yesterday"
    return f"open since {strf(local, '%b %-d')}"


def _who(name):
    """A chauffeur's name the way it is said: the assignment stores the login
    form ('roberto'), the sentence wants 'Roberto'."""
    name = (name or "").strip()
    return name.title() if name and name == name.lower() else name


# ════════════════════════════════════════════════════════════════════════════
# The queue
# ════════════════════════════════════════════════════════════════════════════

def build_queue(*, today, now, vehicles, downtimes_open, issues_open, faults_open,
                faults_recent, schedules, assigned_today, downtime_verdicts=None,
                href_for=None):
    """One row per unit with something to decide, open rows first.

    Returns ``{"rows": [...], "summary": {"open", "urgent", "handled", "text"}}``.
    Each row carries ``tag``, ``tone``, ``handled``, ``age``, ``title``,
    ``meaning``, ``codes`` (chips), ``primary``/``secondary`` actions, and the
    ``category``/``reason`` a downtime opened from the row would carry.
    """
    downtime_verdicts = downtime_verdicts or {}
    href_for = href_for or (lambda v: "")

    downtimes_by, issues_by, faults_by, schedules_by = {}, {}, {}, {}
    for d in downtimes_open:
        downtimes_by.setdefault(d.vehicle_id, []).append(d)
    for i in issues_open:
        issues_by.setdefault(i.vehicle_id, []).append(i)
    for f in faults_open:
        faults_by.setdefault(f.vehicle_id, []).append(f)
    for s in schedules:
        schedules_by.setdefault(s.vehicle_id, []).append(s)
    recurring = _recurring_counts(faults_recent, today)

    rows = []
    for v in vehicles:
        row = _row_for(
            v, today=today, now=now,
            downtimes=downtimes_by.get(v.id, []),
            issues=issues_by.get(v.id, []),
            faults=faults_by.get(v.id, []),
            schedules=schedules_by.get(v.id, []),
            driver=_who(assigned_today.get(v.id, "")),
            verdicts=downtime_verdicts, recurring=recurring, href=href_for(v),
        )
        if row is not None:
            rows.append(row)

    rows.sort(key=lambda r: (ORDER[r["tag"]], _natural(r["number"])))
    open_rows = [r for r in rows if not r["handled"]]
    urgent = sum(1 for r in open_rows if r["tag"] == NEEDS_SHOP)
    handled = len(rows) - len(open_rows)
    if open_rows:
        text = f"{len(open_rows)} open · {urgent} can't wait"
    else:
        text = "nothing open"
    if handled:
        text += f" · {handled} handled"
    return {
        "rows": rows,
        "summary": {"open": len(open_rows), "urgent": urgent, "handled": handled, "text": text},
    }


def _row_for(v, *, today, now, downtimes, issues, faults, schedules, driver,
             verdicts, recurring, href):
    number = v.vehicle_number
    base = {
        "unit": v, "number": number, "unit_id": v.id, "href": href,
        "codes": [], "downtime": None, "downtime_id": None,
        "category": "repair", "reason": "",
        "hours": HOURS_FOR[WATCH],
    }

    live = next((d for d in downtimes if d.is_live(today)), None)
    overdue = next((d for d in downtimes if d.is_overdue(today)), None)
    planned = min((d for d in downtimes if d.is_planned(today)),
                  key=lambda d: d.starts_on, default=None)
    problems = _problems(v, today=today, now=now, issues=issues, faults=faults,
                         schedules=schedules, recurring=recurring)

    # ── Off the road (handled) ───────────────────────────────────────────
    if live is not None:
        meaning = [f"Out of the planner's pool since {_day(live.starts_on)}"]
        if live.expected_back_on:
            meaning[0] += f", back {_day(live.expected_back_on)}"
        elif (today - live.starts_on).days >= NO_ETA_NAG_DAYS:
            meaning[0] += " with no return date"
        if live.vendor:
            meaning[0] += f" · {live.vendor}"
        meaning[0] += "."
        if live.expected_back_on is None and (today - live.starts_on).days >= NO_ETA_NAG_DAYS:
            meaning.append("Get a date from the shop so dispatch can plan on it.")
        if driver:
            meaning.append(f"But {driver} has it today — either it's back, or dispatch "
                           f"needs to know it isn't.")
        else:
            meaning.append("Dispatch has been told. Log the service when the shop hands it back.")
        return {
            **base, "tag": DOWN, "tone": TONE[DOWN], "handled": True,
            "age": f"since {_day(live.starts_on)}" if live.starts_on < today else "from today",
            "title": f"{_unit(v)} — {live.reason or live.get_category_display()}",
            "meaning": " ".join(meaning),
            "codes": _chips(faults, today, now, recurring),
            "downtime": live, "downtime_id": live.id,
            "primary": {"label": "Mark back on the road", "kind": "close",
                        "default_date": today.isoformat()},
            "secondary": {"label": "Log the service", "kind": "link", "href": href},
        }

    # ── Expected back, nobody has said so (open) ─────────────────────────
    if overdue is not None:
        over = (today - overdue.expected_back_on).days
        return {
            **base, "tag": UNCONFIRMED, "tone": TONE[UNCONFIRMED], "handled": False,
            "age": ("expected back today" if over == 0
                    else f"expected back {over} day{'s' if over != 1 else ''} ago"),
            "title": f"{_unit(v)} — expected back {_day(overdue.expected_back_on)}, not confirmed",
            "meaning": (f"{overdue.reason}" + (f" · {overdue.vendor}" if overdue.vendor else "")
                        + ". Dispatch sees it as usable with a “not confirmed” note. "
                        + "Confirm it's back, or push the date out."),
            "codes": _chips(faults, today, now, recurring),
            "downtime": overdue, "downtime_id": overdue.id,
            "primary": {"label": "It's back", "kind": "close",
                        "default_date": overdue.expected_back_on.isoformat()},
            "secondary": {"label": "Push the date", "kind": "link", "href": href},
        }

    # ── Booked in (handled) ──────────────────────────────────────────────
    if planned is not None:
        verdict = verdicts.get(planned.id, "")
        when = _day(planned.starts_on)
        if planned.expected_back_on and (planned.expected_back_on - planned.starts_on).days > 1:
            when += f" – {_day(planned.expected_back_on - timedelta(days=1))}"
        meaning = [f"Shop slot held for {when} · {planned.reason}."]
        if verdict == "conflict":
            meaning.append("Bookings have grown since — dispatch would now be short that day. "
                           "Move it or warn them.")
        elif verdict == "tight":
            meaning.append("Nothing to spare that day if a booking lands.")
        else:
            meaning.append("Dispatch plans around it.")
        if problems:
            meaning.append(problems[0]["note"])
        return {
            **base, "tag": BOOKED, "tone": TONE[BOOKED], "handled": True,
            "age": when.split(" – ")[0],
            "title": f"{_unit(v)} — {problems[0]['part'] if problems else planned.reason}",
            "meaning": " ".join(meaning),
            "codes": _chips(faults, today, now, recurring),
            "downtime": planned, "downtime_id": planned.id,
            "primary": {"label": "Change the window", "kind": "link", "href": href},
            "secondary": {"label": "Cancel the booking", "kind": "cancel"},
        }

    if not problems:
        return None

    # ── Something open on a car that is still working ────────────────────
    urgent = any(p["critical"] for p in problems)
    tag = NEEDS_SHOP if urgent else WATCH
    first = problems[0]
    parts = [p["part"] for p in problems[:2]]
    title = f"{_unit(v)} — " + (", and ".join(parts) if len(parts) == 2 else parts[0])
    meaning = [first["note"]]
    if len(problems) > 2:
        meaning.append("Also: " + "; ".join(p["part"] for p in problems[2:]) + ".")
    if first["kind"] != "gps":
        if driver:
            meaning.append(f"{driver} has it today.")
        else:
            meaning.append("No chauffeur on it today — nothing is stopping this one.")
    facts = _facts(v)
    if facts:
        meaning.append(facts)

    if first["kind"] == "gps":
        primary = {"label": "Check the tracker", "kind": "link", "href": href}
        secondary = {"label": f"Open {_unit(v)}", "kind": "link", "href": href}
    else:
        primary = {"label": "Find a window", "kind": "finder", "hours": HOURS_FOR[tag]}
        secondary = {"label": "Take off road", "kind": "takeoff"}
    return {
        **base, "tag": tag, "tone": TONE[tag], "handled": False,
        "age": first["age"],
        "title": title,
        "meaning": " ".join(meaning),
        "codes": _chips(faults, today, now, recurring),
        "category": first["category"],
        "reason": first["reason"][:200],
        "hours": HOURS_FOR[tag],
        "primary": primary,
        "secondary": secondary,
    }


def _problems(v, *, today, now, issues, faults, schedules, recurring):
    """What is open on a working car, most urgent first. Each: ``part`` (for
    the title), ``note`` (one sentence of consequence), ``age``, ``critical``,
    ``kind``, and the ``category``/``reason`` a downtime for it would carry."""
    out = []
    for i in issues:
        who = ""
        if i.reported_by is not None:
            who = i.reported_by.get_full_name() or i.reported_by.username
        else:
            who = i.get_source_display()
        age_days = (now - i.reported_at).days if i.reported_at else 0
        stale = age_days >= ISSUE_STALE_DAYS
        note = f"{i.get_severity_display()} · reported by {who}"
        note += (f" {age_days} day{'s' if age_days != 1 else ''} ago." if age_days else " today.")
        if stale:
            note += " Still open — resolve it with what was done, or turn it into a downtime."
        out.append({
            "rank": {"ground": 0, "soon": 1, "watch": 3}.get(i.severity, 1),
            "kind": "issue", "part": i.title, "note": note,
            "age": _age(i.reported_at, now, today),
            "critical": i.severity in ("ground", "soon") or (stale and i.severity == "watch"),
            "category": "repair", "reason": i.title,
        })

    if faults:
        n = len(faults)
        codes = [f.code or "fault" for f in faults]
        again = sorted({c for c in codes if recurring.get((v.id, c), 0) >= RECURRING_EPISODES})
        critical = any(f.severity == "critical" for f in faults) or n > 1
        if again:
            note = (f"{_list(again)} {'has' if len(again) == 1 else 'have'} come back "
                    f"{recurring[(v.id, again[0])]} times in {RECURRING_WINDOW_DAYS} days — "
                    f"worth a proper look, not a reset.")
        elif n > 1:
            note = f"{n} codes lit at once — read them before the next assignment."
        elif faults[0].severity == "critical":
            note = "The car flags this one as serious — read it before the next assignment."
        else:
            note = "Logged as a warning by the car. Driveable, but keep it off the long runs until someone looks at it."
        first_seen = min(f.first_seen_at for f in faults)
        out.append({
            "rank": 1, "kind": "fault",
            "part": f"{n} fault code{'s' if n != 1 else ''} lit" + (" — recurring" if again else ""),
            "note": note, "age": _age(first_seen, now, today), "critical": critical,
            "category": "repair", "reason": f"{n} fault code{'s' if n != 1 else ''}: {', '.join(codes)}",
        })

    odometer = v.odometer_miles
    service = []
    for s in schedules:
        for f in fleet_health.service_findings(s, odometer, today):
            service.append((s, f))
    if service:
        service.sort(key=lambda sf: 0 if sf[1]["level"] == fleet_health.CRITICAL else 1)
        s, f = service[0]
        overdue = f["level"] == fleet_health.CRITICAL
        out.append({
            "rank": 0 if overdue else 2, "kind": "service",
            "part": f"{f['label'].lower()} · {f['detail']}",
            "note": ("Overdue — take it on the next quiet day, or accept the risk on record."
                     if overdue else "Book it on a clear day — the finder below shows which."),
            "age": "", "critical": overdue,
            "category": "repair" if s.service_type == "other" else "maintenance",
            "reason": f"{s.get_service_type_display()} ({f['detail']})",
        })

    if v.samsara_vehicle_id:
        mv = v.samsara_battery_millivolts
        if mv is not None and mv <= fleet_health.BATTERY_CRITICAL_MV:
            volts = Decimal(mv) / 1000
            out.append({
                "rank": 2, "kind": "battery", "part": f"battery at {volts:.1f}V",
                "note": "Below the no-start line — have it started and checked before an early run.",
                "age": "", "critical": False, "category": "repair",
                "reason": f"Battery {volts:.1f}V",
            })
        age = fleet_health._telemetry_age_hours(v, now)
        if age is not None and age >= fleet_health.TELEMETRY_STALE_HOURS:
            stamp = v.samsara_last_seen_at or v.samsara_odometer_at
            days = int(age // 24)
            out.append({
                "rank": 3, "kind": "gps", "part": "tracker has stopped reporting",
                "note": (f"No odometer, fault or battery readings since "
                         f"{strf(timezone.localtime(stamp), '%b %-d')}. Everything shown for it is "
                         + (f"{days} days stale." if days >= 2 else "stale.")),
                "age": f"{days} days quiet" if days >= 2 else f"{int(age)}h quiet",
                "critical": False, "category": "repair", "reason": "Tracker not reporting",
            })

    out.sort(key=lambda p: p["rank"])
    return out


def _facts(v):
    """'Battery at 11.8V, tank at 22%.' — the readings worth a glance."""
    bits = []
    mv = v.samsara_battery_millivolts
    if mv is not None and mv <= fleet_health.BATTERY_LOW_MV:
        bits.append(f"Battery at {Decimal(mv) / 1000:.1f}V")
    fuel = v.samsara_fuel_percent
    if fuel is not None and fuel <= fleet_health.FUEL_LOW_PCT:
        bits.append(("tank" if bits else "Tank") + f" at {fuel}%")
    return ", ".join(bits) + "." if bits else ""


def _chips(faults, today, now, recurring):
    chips = []
    for f in sorted(faults, key=lambda f: f.first_seen_at):
        code = f.code or f.get_source_display()
        chips.append({
            "code": code,
            "description": f.description or "No description from the car.",
            "since": _age(f.first_seen_at, now, today),
            "seen": f.occurrence_count,
            "severity": f.get_severity_display(),
            "recurring": recurring.get((f.vehicle_id, f.code), 0) >= RECURRING_EPISODES,
        })
    return chips


def _recurring_counts(faults_recent, today):
    cutoff = today - timedelta(days=RECURRING_WINDOW_DAYS)
    seen = {}
    for f in faults_recent:
        if not f.code or f.first_seen_at is None or f.first_seen_at.date() < cutoff:
            continue
        seen[(f.vehicle_id, f.code)] = seen.get((f.vehicle_id, f.code), 0) + 1
    return seen


# ════════════════════════════════════════════════════════════════════════════
# The state bar, the shop panel, the paperwork rows
# ════════════════════════════════════════════════════════════════════════════

def state_bar(board):
    """Segments for the fleet-state bar from the status board. Every unit is
    in exactly one state; segments with nothing in them are omitted so a
    zero never draws a sliver."""
    counts = {}
    for r in board:
        counts[r["state"]] = counts.get(r["state"], 0) + 1
    return [
        {"key": s, "label": STATE_LABELS[s], "n": counts[s], "color": STATE_COLOURS[s]}
        for s in STATE_BAR_ORDER if counts.get(s)
    ]


def shop_panel(*, today, in_shop, planned, idle_today, downtime_verdicts=None):
    """What the shop panel says: bookings held, units off the road now, and
    which units have no chauffeur today."""
    downtime_verdicts = downtime_verdicts or {}
    booked = []
    for d in planned:
        when = _day(d.starts_on)
        if d.expected_back_on and (d.expected_back_on - d.starts_on).days > 1:
            when += f" – {_day(d.expected_back_on - timedelta(days=1))}"
        booked.append({
            "downtime": d, "unit": _unit(d.vehicle), "when": when, "reason": d.reason,
            "verdict": downtime_verdicts.get(d.id, ""),
        })
    down = []
    for d in in_shop:
        down.append({
            "downtime": d, "unit": _unit(d.vehicle), "reason": d.reason, "vendor": d.vendor,
            "since": _day(d.starts_on),
            "back": _day(d.expected_back_on) if d.expected_back_on else "",
            "overdue": d.is_overdue(today),
        })
    idle = [_unit(r["vehicle"]) for r in idle_today]
    if idle:
        idle_text = (f"{_list(idle)} {'has' if len(idle) == 1 else 'have'} no chauffeur today — "
                     f"a service fits without touching Dispatch.")
    else:
        idle_text = "Every unit has a chauffeur today."
    return {"booked": booked, "down": down, "idle": idle, "idle_text": idle_text}


PAPERWORK = (
    ("registration_expires_on", "Registration"),
    ("insurance_expires_on", "Insurance"),
    ("next_inspection_on", "Inspection"),
)
PERMIT_LABELS = {"mco": "MCO airport permits", "sanford": "Sanford airport permits",
                 "port_canaveral": "Port Canaveral permits"}


def paperwork_rows(vehicles, today, *, list_href="", href_for=None):
    """Fleet-wide paperwork, grouped by document and state. One row says
    "MCO airport permits — all 19 units" instead of nineteen lines."""
    href_for = href_for or (lambda v: "")
    total = len(vehicles)
    groups = {}   # (label, state) -> {"units": [...], "days": min days}

    def add(label, state, v, days=None):
        g = groups.setdefault((label, state), {"units": [], "days": None})
        g["units"].append(v)
        if days is not None and (g["days"] is None or days < g["days"]):
            g["days"] = days

    for v in vehicles:
        for field, label in PAPERWORK:
            due = getattr(v, field, None)
            if due is None:
                continue
            days = (due - today).days
            if days < 0:
                add(label, "expired", v, days)
            elif days <= fleet_health.EXPIRY_WARN_DAYS:
                add(label, "due", v, days)
        for p in v.permits(day=today):
            if not p["on_file"] or not p["expires_on"]:
                continue
            days = (p["expires_on"] - today).days
            label = PERMIT_LABELS[p["key"]]
            if days < 0:
                add(label, "expired", v, days)
            elif days <= fleet_health.EXPIRY_WARN_DAYS:
                add(label, "due", v, days)

    rows = []
    for (label, state), g in groups.items():
        units = sorted(g["units"], key=lambda v: _natural(v.vehicle_number))
        if state == "expired":
            badge, tone, action = "Expired", "critical", "Renew"
        else:
            n = g["days"]
            badge = "today" if n == 0 else f"{n} day{'s' if n != 1 else ''}"
            tone, action = "caution", "Renew"
        rows.append({
            "badge": badge, "tone": tone, "label": label, "sort": (0 if state == "expired" else 1, g["days"] or 0),
            "units_text": _units_text(units, total),
            "href": list_href if len(units) > 1 else href_for(units[0]),
            "action": action + (" all" if len(units) > 1 else ""),
        })
    rows.sort(key=lambda r: r["sort"])

    missing = [v for v in vehicles if not (v.registration_expires_on or v.insurance_expires_on
                                          or v.next_inspection_on)]
    if missing:
        missing.sort(key=lambda v: _natural(v.vehicle_number))
        rows.append({
            "badge": "Missing", "tone": "muted", "label": "Registration, insurance and inspection dates",
            "sort": (2, 0), "units_text": _units_text(missing, total),
            "href": list_href if len(missing) > 1 else href_for(missing[0]),
            "action": "Add them" if len(missing) > 1 else "Add them",
        })
    return rows


def _units_text(units, total):
    if total and len(units) == total and total > 1:
        return f"all {total} units"
    names = [_unit(v) for v in units]
    if len(names) > 6:
        return f"{len(names)} of {total} units"
    return _list(names)
