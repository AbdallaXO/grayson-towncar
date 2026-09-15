"""
The weekly inspection round: a few cars a day, the whole fleet by Sunday.

The fleet manager walks a handful of units each day and runs a checklist on
each. The WEEK is the unit of accountability — every active car inspected once
between Monday and Sunday — so the round resets itself simply by asking about a
different ``week_start``. There is no reset job, nothing to clear down, and a
week that was missed stays on the record instead of being overwritten.

Which cars to walk today is the only clever part, and it composes with
``fleet_day``: a car can only be inspected if it is actually on the lot, so the
suggestion prefers the units with no chauffeur today, then those with the
longest hole in their day, then the ones nobody has looked at longest. It is a
SUGGESTION — the manager can open any car still due — and an already-inspected
car is never offered again that week.

Nothing here takes a car off the road. An inspection that finds something files
a ``VehicleIssue``, which carries the severity; only the downtime ledger ever
removes a unit from the pool.
"""
from __future__ import annotations

from datetime import date, timedelta

# ════════════════════════════════════════════════════════════════════════════
# THE CHECKLIST — edit this list, nothing else.
#
# Add, remove or reword items freely: the definitions live here, and an
# inspection stores what was ticked by KEY, so old records keep reading
# correctly even after an item is dropped. Only two rules:
#   • a `key` must be unique and must never be reused for a different question
#   • keep `key` short and lowercase; it is what lands in the database
# Every item can be ticked good, flagged, or marked not-applicable, and every
# item takes a note and a photo without anything being declared here.
# ════════════════════════════════════════════════════════════════════════════

CHECKLIST = [
    {
        "key": "exterior",
        "title": "Exterior",
        "items": [
            {"key": "body", "label": "Body and paint — no new damage"},
            {"key": "lights", "label": "Headlights, brake lights, indicators"},
            {"key": "tires", "label": "Tire tread and pressure, no damage"},
            {"key": "glass", "label": "Glass, mirrors and wipers"},
            {"key": "clean_out", "label": "Washed, presentable for a guest"},
        ],
    },
    {
        "key": "interior",
        "title": "Interior",
        "items": [
            {"key": "clean_in", "label": "Clean, no odor, no marks"},
            {"key": "seats", "label": "Seats and upholstery"},
            {"key": "climate", "label": "A/C and heat both working"},
            {"key": "belts", "label": "Every seatbelt works"},
        ],
    },
    {
        "key": "mechanical",
        "title": "Under the hood",
        "items": [
            {"key": "oil", "label": "Oil level"},
            {"key": "coolant", "label": "Coolant and washer fluid"},
            {"key": "brakes", "label": "Brakes feel right on the test drive"},
            {"key": "dash", "label": "No warning lights on the dash"},
        ],
    },
    {
        "key": "equipment",
        "title": "Equipment",
        "items": [
            {"key": "amenities", "label": "Water and amenities stocked"},
            {"key": "chargers", "label": "Phone chargers present"},
            {"key": "carseats", "label": "Car seats present and undamaged"},
            {"key": "spare", "label": "Spare, jack and warning triangle"},
        ],
    },
    {
        "key": "paperwork",
        "title": "Paperwork in the car",
        "items": [
            {"key": "registration", "label": "Registration"},
            {"key": "insurance", "label": "Insurance card"},
            {"key": "permit", "label": "MCO permit displayed"},
        ],
    },
]

# How many cars the manager aims to walk in a day. Nineteen active units across
# five working days is four a day; five leaves room to fall a day behind and
# still finish the week.
DAILY_TARGET = 5

STATE_OK = "ok"
STATE_FLAG = "flag"
STATE_NA = "na"
STATES = (STATE_OK, STATE_FLAG, STATE_NA)


def all_items():
    """Every item across every section, flattened, in checklist order."""
    return [item for section in CHECKLIST for item in section["items"]]


def item_labels():
    """{key: label} for rendering a stored result whose item may since have been
    reworded or removed."""
    return {item["key"]: item["label"] for item in all_items()}


def clean_results(raw):
    """Keep only the keys the checklist knows, and only the states it allows.

    The form is the only writer, but the payload arrives as JSON from a browser,
    so it is treated as untrusted: an unknown key or a made-up state is dropped
    rather than stored and rendered back later as though it meant something.
    """
    known = {item["key"] for item in all_items()}
    out = {}
    for key, value in (raw or {}).items():
        if key not in known or not isinstance(value, dict):
            continue
        state = value.get("state")
        if state not in STATES:
            continue
        entry = {"state": state}
        note = (value.get("note") or "").strip()
        if note:
            entry["note"] = note[:500]
        out[key] = entry
    return out


# ════════════════════════════════════════════════════════════════════════════
# The week
# ════════════════════════════════════════════════════════════════════════════

def week_start_for(day):
    """The Monday of ``day``'s ISO week. The whole reset mechanism."""
    return day - timedelta(days=day.weekday())


def week_end_for(day):
    return week_start_for(day) + timedelta(days=6)


def load_week(day):
    """Every active unit and this week's inspections, in two queries."""
    from dispatching import fleet_capacity
    from drivers.models import VehicleInspection

    start = week_start_for(day)
    units = fleet_capacity.fleet_units()
    done = list(
        VehicleInspection.objects
        .filter(week_start=start, vehicle_id__in=[u.id for u in units])
        .select_related("inspected_by", "issue")
        .prefetch_related("photos")
    )
    return {"day": day, "week_start": start, "units": units, "done": done}


def last_seen_map(units):
    """{vehicle_id: the most recent week_start it was inspected} — used only to
    break ties in the suggestion, so the car nobody has looked at for longest
    rises. One query."""
    from django.db.models import Max
    from drivers.models import VehicleInspection

    rows = (VehicleInspection.objects
            .filter(vehicle_id__in=[u.id for u in units])
            .values("vehicle_id")
            .annotate(last=Max("week_start")))
    return {row["vehicle_id"]: row["last"] for row in rows}


def build_week(loaded, day_rows=None, last_seen=None):
    """The inspection board for one week. Pure.

    ``day_rows`` is ``fleet_day.build_day(...)["rows"]`` for today, used only to
    order the suggestion by what is actually standing still. Without it the
    round still works — it simply cannot prefer the idle cars.
    """
    day, start = loaded["day"], loaded["week_start"]
    by_vehicle = {row.vehicle_id: row for row in loaded["done"]}
    last_seen = last_seen or {}
    day_by_number = {r["number"]: r for r in (day_rows or [])}

    tiles, due = [], []
    for unit in loaded["units"]:
        inspection = by_vehicle.get(unit.id)
        today_row = day_by_number.get(unit.vehicle_number)
        downtime = unit.downtime_on(day)

        if inspection is not None:
            state = "found" if inspection.found_something else "done"
        elif downtime is not None:
            # In the shop all week is not a miss; it is simply not walkable.
            state = "shop"
        else:
            state = "due"

        tile = {
            "unit": unit,
            "number": unit.vehicle_number,
            "vehicle_type": _type_label(unit),
            "state": state,
            "inspection": inspection,
            "downtime": downtime,
            "today": _today_note(today_row),
            "idle_today": bool(today_row and today_row["state"] in ("open", "down")),
            "window_minutes": _window_minutes(today_row),
            "last_seen": last_seen.get(unit.id),
            "href": f"/dispatching/fleet/inspections/{unit.id}/",
        }
        tiles.append(tile)
        if state == "due":
            due.append(tile)

    # The order the suggestion offers: standing still first, then the biggest
    # hole in the day, then longest since anyone looked, then unit number.
    due.sort(key=lambda t: (
        not t["idle_today"],
        -(t["window_minutes"] or 0),
        t["last_seen"] or _NEVER,
        _natural(t["number"]),
    ))

    counted = [t for t in tiles if t["state"] != "shop"]
    done_count = sum(1 for t in counted if t["state"] in ("done", "found"))
    found_count = sum(1 for t in counted if t["state"] == "found")
    remaining = len(counted) - done_count
    days_left = max(1, 7 - day.weekday()) if day.weekday() < 7 else 1

    tiles.sort(key=lambda t: ({"due": 0, "found": 1, "done": 2, "shop": 3}[t["state"]],
                              _natural(t["number"])))

    return {
        "day": day,
        "week_start": start,
        "week_end": start + timedelta(days=6),
        "tiles": tiles,
        "suggested": due[:DAILY_TARGET],
        "due_count": remaining,
        "done_count": done_count,
        "found_count": found_count,
        "total": len(counted),
        "in_shop": sum(1 for t in tiles if t["state"] == "shop"),
        "complete": remaining == 0,
        "pace": _pace(remaining, days_left, day),
        "headline": _headline(done_count, len(counted), found_count),
    }


# A car nobody has ever inspected sorts ahead of one inspected long ago.
_NEVER = date.min


def _pace(remaining, days_left, day):
    """What it takes from here to finish the week, said plainly."""
    if not remaining:
        return ""
    per_day = -(-remaining // days_left)          # ceil
    if day.weekday() >= 5 and remaining:
        return f"{remaining} still to do and the week is nearly out."
    if per_day <= DAILY_TARGET:
        return f"{remaining} to go — about {per_day} a day finishes the week."
    return (f"{remaining} to go with {days_left} day{'s' if days_left != 1 else ''} left — "
            f"that is {per_day} a day, above the usual {DAILY_TARGET}.")


def _headline(done, total, found):
    if not total:
        return "No active units to inspect."
    line = f"{done} of {total} inspected this week"
    if found:
        line += f" · {found} found something"
    return line


def _today_note(row):
    if row is None:
        return ""
    if row["state"] == "down":
        return row["note"]
    if row["state"] == "open":
        return "Sitting still today"
    if row["state"] == "unknown":
        return "Day not built yet"
    longest = row.get("longest_gap")
    if longest and longest.get("usable"):
        return f"Free {longest['from_label']} – {longest['to_label']}"
    return f"{row['trips']} trip{'s' if row['trips'] != 1 else ''} today"


def _window_minutes(row):
    """How much genuinely walkable time this car has today.

    Deliberately the longest USABLE window, not the longest hole: a three-hour
    gap that opens behind an airport arrival is not three hours you can hold a
    car for, and ranking by the raw number would push a car with no real window
    above one that has a real one — which is exactly what the tile then fails to
    show, because the label names usable windows only.
    """
    if row is None:
        return 0
    if row["state"] == "open":
        return 24 * 60
    usable = row.get("usable_windows") or []
    return max((w["minutes"] for w in usable), default=0)


def _type_label(unit):
    from dispatching import fleet_capacity
    return fleet_capacity.type_label(fleet_capacity.unit_type(unit))


def _natural(vehicle_number):
    number = (vehicle_number or "").strip()
    digits = "".join(ch for ch in number if ch.isdigit())
    return (0, int(digits), number) if digits else (1, 0, number)


def existing_for(unit, day):
    """This week's inspection for one unit, or None."""
    from drivers.models import VehicleInspection

    return (VehicleInspection.objects
            .filter(vehicle=unit, week_start=week_start_for(day))
            .select_related("issue", "inspected_by")
            .prefetch_related("photos")
            .first())


def form_sections(inspection=None):
    """The checklist ready to render, carrying any answers already saved."""
    saved = (inspection.results if inspection else {}) or {}
    out = []
    for section in CHECKLIST:
        items = []
        for item in section["items"]:
            entry = saved.get(item["key"]) or {}
            items.append({
                "key": item["key"],
                "label": item["label"],
                "state": entry.get("state", ""),
                "note": entry.get("note", ""),
            })
        out.append({"key": section["key"], "title": section["title"], "items": items})
    return out
